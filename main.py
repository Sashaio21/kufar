"""
Мониторинг новых объявлений на re.kufar.by через curl_cffi (имитация
TLS/HTTP2-отпечатка настоящего браузера, без запуска самого браузера)
и уведомления в Telegram.

Требуемые библиотеки:
    pip install curl_cffi==0.16.3 beautifulsoup4 pyTelegramBotAPI

Отличие от версии на requests:
    requests имеет собственный, легко узнаваемый TLS/HTTP2-отпечаток
    (JA3/JA4), по которому антибот-защиты режут запрос ещё до того,
    как посмотрят на заголовки. curl_cffi использует патченный libcurl,
    который умеет выдавать себя за конкретный браузер (Chrome/Safari/Edge)
    на уровне TLS-рукопожатия — это самая частая причина, почему "requests
    видит пустую/заблокированную страницу, а браузер - нормальную".

Это НЕ решает проблему, если сайт рендерит список объявлений через JS
уже в браузере (тогда curl_cffi, как и requests, увидит только каркас
страницы без данных - JS он не выполняет). В таком случае поможет
только Selenium/Playwright.

Скрипт пробует несколько профилей imitации браузера и несколько
стратегий парсинга по очереди, явно пишет в лог, что сработало.
Если НИ ОДНА комбинация не сработала - он это не скрывает.
"""

import json
import os
import time
import logging

from curl_cffi import requests as cffi_requests
from curl_cffi.requests.exceptions import RequestException
from bs4 import BeautifulSoup
import telebot

# ---------------------- НАСТРОЙКИ ----------------------

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")          # получить у @BotFather
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")      # свой chat_id (узнать у @userinfobot)

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    raise RuntimeError(
        "Не заданы переменные окружения TELEGRAM_TOKEN и/или TELEGRAM_CHAT_ID. "
        "Передай их при запуске контейнера, например через docker run -e "
        "или через .env файл с docker-compose."
    )

TARGET_URL = "https://re.kufar.by/l/grodno/snyat/kvartiru/1k?cur=USD&prc=r%3A0%2C200"

CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", 5 * 60))
SEEN_IDS_FILE = os.environ.get("SEEN_IDS_FILE", "seen_ads.json")

# Профили браузера для имитации TLS/HTTP2-отпечатка curl_cffi.
# Пробуются по очереди, пока один не сработает (см. fetch_current_ads).
# Полный список поддерживаемых значений смотри в README curl_cffi -
# он меняется от версии к версии.
IMPERSONATE_PROFILES = os.environ.get(
    "IMPERSONATE_PROFILES",
    "chrome124,chrome120,chrome110,safari17_0,edge101",
).split(",")

HEADERS = {
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("kufar_monitor")

bot = telebot.TeleBot(TELEGRAM_TOKEN)


def load_seen_ids():
    if os.path.exists(SEEN_IDS_FILE):
        with open(SEEN_IDS_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen_ids(ids):
    with open(SEEN_IDS_FILE, "w", encoding="utf-8") as f:
        json.dump(list(ids), f, ensure_ascii=False, indent=2)


def strategy_next_data(soup):
    """Пытаемся вытащить объявления из <script id="__NEXT_DATA__">,
    если сайт сделан на Next.js и рендерит данные на сервере."""
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag or not tag.string:
        return None

    try:
        data = json.loads(tag.string)
    except json.JSONDecodeError:
        return None

    found = []

    def walk(node):
        if isinstance(node, dict):
            for key in ("ads", "items", "listings", "adverts"):
                if key in node and isinstance(node[key], list):
                    found.append(node[key])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    if not found:
        return None

    best = max(found, key=len)
    ad_urls = []
    for item in best:
        if isinstance(item, dict):
            ad_id = item.get("ad_id") or item.get("id")
            link = item.get("ad_link") or item.get("url")
            if link:
                ad_urls.append(link)
            elif ad_id:
                ad_urls.append(f"https://www.kufar.by/item/{ad_id}")
    return ad_urls or None


def strategy_raw_links(soup):
    """Запасной вариант: ищем ссылки на объявления прямо в HTML."""
    links = soup.select("a[href*='/vi/']")
    ad_urls = []
    for link in links:
        href = link.get("href")
        if not href:
            continue
        if href.startswith("/"):
            href = "https://re.kufar.by" + href
        if href not in ad_urls:
            ad_urls.append(href)
    return ad_urls or None


# Поставь True, только если знаешь, что проблема в антивирусе/прокси с
# SSL-инспекцией на твоей машине, и не можешь быстро это починить.
# Это отключает проверку подлинности сертификата сайта - небезопасно
# для постоянного использования, годится как временный обход.
DISABLE_SSL_VERIFY = False

if DISABLE_SSL_VERIFY:
    # заставляем pyTelegramBotAPI тоже не проверять сертификат
    telebot.apihelper.CUSTOM_REQUEST_KWARGS = {"verify": False}


def fetch_page():
    """Пробует запросить страницу под разными профилями браузера,
    пока не получит осмысленный ответ (статус 200). Возвращает
    (html_text, profile_used) либо кидает исключение, если все
    профили провалились."""
    last_error = None

    for profile in IMPERSONATE_PROFILES:
        profile = profile.strip()
        try:
            resp = cffi_requests.get(
                TARGET_URL,
                headers=HEADERS,
                impersonate=profile,
                timeout=20,
                verify=not DISABLE_SSL_VERIFY,
            )
        except RequestException as e:
            log.warning("Профиль %s: ошибка запроса (%s), пробую следующий.", profile, e)
            last_error = e
            continue

        if resp.status_code == 200:
            log.info("Профиль %s сработал (HTTP 200).", profile)
            return resp.text, profile

        log.warning(
            "Профиль %s: сайт ответил HTTP %d, пробую следующий профиль.",
            profile, resp.status_code,
        )
        last_error = RuntimeError(f"HTTP {resp.status_code} с профилем {profile}")

    raise last_error or RuntimeError("Не удалось получить страницу ни одним из профилей")


def fetch_current_ads():
    html_text, profile = fetch_page()
    soup = BeautifulSoup(html_text, "html.parser")

    ads = strategy_next_data(soup)
    if ads:
        log.info(
            "Данные получены через __NEXT_DATA__ (%d объявлений, профиль %s).",
            len(ads), profile,
        )
        return ads

    ads = strategy_raw_links(soup)
    if ads:
        log.info(
            "Данные получены через прямые ссылки в HTML (%d объявлений, профиль %s).",
            len(ads), profile,
        )
        return ads

    return None


def check_once():
    seen = load_seen_ids()
    try:
        current_ads = fetch_current_ads()
    except Exception as e:
        log.error("Ошибка при запросе страницы: %s", e)
        return

    if current_ads is None:
        log.warning(
            "Не удалось найти объявления ни одним из способов. "
            "Похоже, сайт рендерит список объявлений через JS уже в браузере "
            "(curl_cffi, как и requests, такое не видит - он не выполняет JS). "
            "В этом случае нужен вариант со Selenium/Playwright - скажи, и я "
            "пришлю такую версию."
        )
        return

    new_ads = [url for url in current_ads if url not in seen]

    if new_ads:
        log.info("Найдено новых объявлений: %d", len(new_ads))
        for url in new_ads:
            try:
                bot.send_message(TELEGRAM_CHAT_ID, f"Новое объявление:\n{url}")
            except Exception as e:
                log.error("Не удалось отправить сообщение в Telegram: %s", e)

    save_seen_ids(seen.union(current_ads))


def main():
    log.info(
        "Запуск мониторинга (curl_cffi). Проверка каждые %d секунд. Профили: %s",
        CHECK_INTERVAL_SECONDS, ", ".join(p.strip() for p in IMPERSONATE_PROFILES),
    )
    while True:
        check_once()
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
