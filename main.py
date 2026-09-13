"""
Мониторинг новых объявлений на re.kufar.by через curl_cffi (имитация
TLS/HTTP2-отпечатка настоящего браузера) и уведомления в Telegram.

Проверка запускается автоматически, по внутреннему расписанию (фоновый
поток). Интервал хранится в файле и переживает перезапуск контейнера.
Всё управление - командами Telegram-бота:
    /status    - текущий интервал и результат последней проверки
    /interval  - показать/сменить периодичность (в секундах)
    /check     - запустить проверку немедленно
    /help      - список команд

Внешнего HTTP-эндпоинта для cron больше нет - расписание полностью
внутри контейнера. Остался только GET /health для docker/мониторинга,
чтобы можно было проверить, что процесс жив.

Требуемые библиотеки:
    pip install curl_cffi==0.16.3 beautifulsoup4 pyTelegramBotAPI flask waitress
"""

import json
import os
import time
import logging
import threading
from datetime import datetime

from flask import Flask, jsonify
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

# Только этот chat_id может управлять ботом командами. Кто угодно другой,
# кто напишет боту, получит отказ - иначе посторонний человек смог бы
# менять интервал или дёргать проверки.
ALLOWED_CHAT_ID = str(TELEGRAM_CHAT_ID)

TARGET_URL = "https://re.kufar.by/l/grodno/snyat/kvartiru/1k?cur=USD&prc=r%3A0%2C200"

SEEN_IDS_FILE = os.environ.get("SEEN_IDS_FILE", "seen_ads.json")
STATE_FILE = os.environ.get("STATE_FILE", "monitor_state.json")
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", 8080))

DEFAULT_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", 5 * 60))
# Защита от того, чтобы через /interval случайно/специально не поставили
# слишком частый опрос и не словили бан на сайте.
MIN_INTERVAL_SECONDS = int(os.environ.get("MIN_INTERVAL_SECONDS", 60))

IMPERSONATE_PROFILES = os.environ.get(
    "IMPERSONATE_PROFILES",
    "chrome124,chrome120,chrome110,safari17_0,edge101",
).split(",")

HEADERS = {
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

DISABLE_SSL_VERIFY = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("kufar_monitor")

bot = telebot.TeleBot(TELEGRAM_TOKEN)
app = Flask(__name__)

# ---------------------- ОБЩЕЕ СОСТОЯНИЕ ----------------------

state_lock = threading.Lock()
check_lock = threading.Lock()          # чтобы две проверки не бежали параллельно
interval_changed_event = threading.Event()

current_interval_seconds = DEFAULT_INTERVAL_SECONDS
last_check_info = {"time": None, "status": "никогда не запускалась", "message": "", "new_ads": 0, "total_ads": 0}


def load_state():
    global current_interval_seconds
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            current_interval_seconds = int(data.get("interval_seconds", DEFAULT_INTERVAL_SECONDS))
        except (json.JSONDecodeError, ValueError, TypeError):
            log.warning("Не удалось прочитать %s, использую интервал по умолчанию.", STATE_FILE)


def save_state():
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"interval_seconds": current_interval_seconds}, f, ensure_ascii=False, indent=2)


def format_interval(seconds: int) -> str:
    if seconds % 3600 == 0:
        return f"{seconds // 3600} ч ({seconds} сек)"
    if seconds % 60 == 0:
        return f"{seconds // 60} мин ({seconds} сек)"
    return f"{seconds} сек"


def set_interval(seconds: int):
    global current_interval_seconds
    with state_lock:
        current_interval_seconds = seconds
        save_state()
    interval_changed_event.set()  # прерываем текущее ожидание в scheduler_loop


def get_interval() -> int:
    with state_lock:
        return current_interval_seconds


def record_check_result(result: dict):
    with state_lock:
        last_check_info["time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        last_check_info["status"] = result.get("status")
        last_check_info["message"] = result.get("message", "")
        last_check_info["new_ads"] = result.get("new_ads", 0)
        last_check_info["total_ads"] = result.get("total_ads_on_page", 0)


def get_last_check_info() -> dict:
    with state_lock:
        return dict(last_check_info)


# ---------------------- ПОИСК ОБЪЯВЛЕНИЙ ----------------------

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


def fetch_page():
    """Пробует запросить страницу под разными профилями браузера,
    пока не получит HTTP 200. Возвращает (html_text, profile_used)
    либо кидает исключение, если все профили провалились."""
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


def check_once() -> dict:
    """Одна проверка. Возвращает dict с результатом - используется и
    HTTP-эндпоинтом, и командой /check, и фоновым расписанием."""
    if not check_lock.acquire(blocking=False):
        result = {"status": "skipped", "message": "Проверка уже выполняется, пропускаю"}
        return result

    try:
        seen = load_seen_ids()

        try:
            current_ads = fetch_current_ads()
        except Exception as e:
            log.error("Ошибка при запросе страницы: %s", e)
            result = {"status": "error", "message": str(e)}
            record_check_result(result)
            return result

        if current_ads is None:
            msg = (
                "Не удалось найти объявления ни одним из способов. Похоже, сайт "
                "рендерит список через JS уже в браузере, либо антибот распознал "
                "запрос по другим признакам, чем TLS-отпечаток."
            )
            log.warning(msg)
            result = {"status": "no_data", "message": msg}
            record_check_result(result)
            return result

        new_ads = [url for url in current_ads if url not in seen]

        if new_ads:
            log.info("Найдено новых объявлений: %d", len(new_ads))
            for url in new_ads:
                try:
                    bot.send_message(TELEGRAM_CHAT_ID, f"Новое объявление:\n{url}")
                except Exception as e:
                    log.error("Не удалось отправить сообщение в Telegram: %s", e)

        save_seen_ids(seen.union(current_ads))

        result = {
            "status": "ok",
            "total_ads_on_page": len(current_ads),
            "new_ads": len(new_ads),
        }
        record_check_result(result)
        return result
    finally:
        check_lock.release()


# ---------------------- ФОНОВОЕ РАСПИСАНИЕ ----------------------

def scheduler_loop():
    log.info("Фоновое расписание запущено, интервал: %s", format_interval(get_interval()))
    while True:
        check_once()
        wait_seconds = get_interval()
        interrupted = interval_changed_event.wait(timeout=wait_seconds)
        interval_changed_event.clear()
        if interrupted:
            log.info("Интервал изменён, применяю новое значение: %s", format_interval(get_interval()))


# ---------------------- TELEGRAM-КОМАНДЫ ----------------------

def _authorized(message) -> bool:
    return str(message.chat.id) == ALLOWED_CHAT_ID


HELP_TEXT = (
    "/status - текущий интервал и результат последней проверки\n"
    "/interval - показать периодичность\n"
    "/interval <секунды> - сменить периодичность (минимум "
    f"{MIN_INTERVAL_SECONDS} сек)\n"
    "/check - запустить проверку немедленно\n"
    "/help - это сообщение"
)


@bot.message_handler(commands=["help"])
def cmd_help(message):
    if not _authorized(message):
        return
    bot.reply_to(message, HELP_TEXT)


@bot.message_handler(commands=["status"])
def cmd_status(message):
    if not _authorized(message):
        return
    info = get_last_check_info()
    text = (
        f"Интервал проверки: {format_interval(get_interval())}\n"
        f"Последняя проверка: {info['time'] or 'ещё не запускалась'}\n"
        f"Статус: {info['status']}\n"
    )
    if info["status"] == "ok":
        text += f"Объявлений на странице: {info['total_ads']}, новых: {info['new_ads']}"
    elif info["message"]:
        text += f"Подробности: {info['message']}"
    bot.reply_to(message, text)


@bot.message_handler(commands=["interval"])
def cmd_interval(message):
    if not _authorized(message):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        bot.reply_to(
            message,
            f"Текущий интервал: {format_interval(get_interval())}\n"
            f"Чтобы изменить: /interval <секунды>, минимум {MIN_INTERVAL_SECONDS}.",
        )
        return

    raw = parts[1].strip()
    try:
        seconds = int(raw)
    except ValueError:
        bot.reply_to(message, "Нужно целое число секунд, например: /interval 300")
        return

    if seconds < MIN_INTERVAL_SECONDS:
        bot.reply_to(
            message,
            f"Слишком часто - минимум {MIN_INTERVAL_SECONDS} сек, чтобы не словить "
            f"бан от сайта. Указано: {seconds}.",
        )
        return

    set_interval(seconds)
    bot.reply_to(message, f"Готово. Новый интервал: {format_interval(seconds)}.")


@bot.message_handler(commands=["check"])
def cmd_check(message):
    if not _authorized(message):
        return
    bot.reply_to(message, "Запускаю проверку...")
    result = check_once()
    if result["status"] == "ok":
        text = f"Готово. Объявлений на странице: {result['total_ads_on_page']}, новых: {result['new_ads']}."
    elif result["status"] == "skipped":
        text = "Проверка уже выполняется прямо сейчас, подожди её результата."
    else:
        text = f"Проверка завершилась с проблемой ({result['status']}): {result.get('message', '')}"
    bot.reply_to(message, text)


def bot_polling_loop():
    log.info("Telegram-бот запущен, слушаю команды (long polling).")
    while True:
        try:
            bot.infinity_polling(skip_pending=True, timeout=30)
        except Exception as e:
            log.error("Ошибка long polling, перезапускаю через 5 секунд: %s", e)
            time.sleep(5)


# ---------------------- HTTP /health (для докера/мониторинга) ----------------------

@app.route("/health", methods=["GET"])
def health_endpoint():
    return jsonify({"status": "alive"}), 200


if __name__ == "__main__":
    load_state()

    threading.Thread(target=scheduler_loop, daemon=True).start()
    threading.Thread(target=bot_polling_loop, daemon=True).start()

    log.info(
        "HTTP-сервер запущен на %s:%d (только /health для мониторинга).",
        HOST, PORT,
    )
    try:
        from waitress import serve
        serve(app, host=HOST, port=PORT)
    except ImportError:
        log.warning("waitress не установлен - использую встроенный dev-сервер Flask.")
        app.run(host=HOST, port=PORT)
