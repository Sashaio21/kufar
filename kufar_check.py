"""
Консольная проверка: получится ли забрать объявления с re.kufar.by
через curl_cffi (requests-подобный API, но с имитацией TLS/HTTP2
отпечатка настоящего браузера — часто помогает против блокировок,
которые режут обычный requests/urllib).

Установка:
    pip install curl_cffi==0.16.3 beautifulsoup4

Это только проверочный скрипт: один раз запрашивает страницу и
печатает в консоль, что нашлось. Никакого Telegram и цикла опроса
пока нет — сначала убеждаемся, что сайт вообще отдаёт данные.
"""

import json
import sys

from curl_cffi import requests as cffi_requests
from bs4 import BeautifulSoup

TARGET_URL = "https://re.kufar.by/l/grodno/snyat/kvartiru/1k?cur=USD&prc=r%3A0%2C200"

# Профиль браузера, под который curl_cffi имитирует TLS/HTTP2 отпечаток.
# Есть смысл попробовать разные варианты, если один не сработает:
# "chrome124", "chrome120", "chrome110", "safari17_0", "edge101" и т.д.
IMPERSONATE = "chrome124"

HEADERS = {
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def strategy_next_data(soup: BeautifulSoup):
    """Ищем объявления в <script id="__NEXT_DATA__">, если сайт на Next.js
    и данные приезжают в исходном HTML (SSR)."""
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


def strategy_raw_links(soup: BeautifulSoup):
    """Запасной вариант: прямые ссылки на объявления в HTML."""
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


def main():
    print(f"Запрашиваю: {TARGET_URL}")
    print(f"impersonate = {IMPERSONATE}")

    try:
        resp = cffi_requests.get(
            TARGET_URL,
            headers=HEADERS,
            impersonate=IMPERSONATE,
            timeout=20,
        )
    except Exception as e:
        print(f"[ОШИБКА] Не удалось выполнить запрос: {e}")
        sys.exit(1)

    print(f"HTTP статус: {resp.status_code}")
    print(f"Размер ответа: {len(resp.text)} байт")

    if resp.status_code != 200:
        print("[ВНИМАНИЕ] Статус не 200 — возможно, всё ещё блокировка "
              "(403/429/капча) либо редирект. Ниже первые 500 символов ответа:")
        print(resp.text[:500])
        sys.exit(1)

    soup = BeautifulSoup(resp.text, "html.parser")

    ads = strategy_next_data(soup)
    if ads:
        print(f"\n[OK] Найдено через __NEXT_DATA__: {len(ads)} объявлений")
        for url in ads[:10]:
            print("  -", url)
        return

    ads = strategy_raw_links(soup)
    if ads:
        print(f"\n[OK] Найдено через прямые ссылки в HTML: {len(ads)} объявлений")
        for url in ads[:10]:
            print("  -", url)
        return

    print(
        "\n[НЕ НАЙДЕНО] Ни один способ не сработал. Возможные причины:\n"
        "  1) Сайт всё же рендерит список через JS в браузере (SSR не отдаёт "
        "данные) — тогда нужен Playwright/Selenium, curl_cffi тут не поможет, "
        "он не выполняет JS.\n"
        "  2) Антибот-защита распознала запрос, несмотря на imitацию браузера "
        "(например, TLS ok, но не хватает cookie/JS-челленджа, который выдаёт "
        "сайт при первом заходе).\n"
        "  3) Структура страницы изменилась и её нужно посмотреть вручную.\n"
        "Сохраняю HTML в response_debug.html, чтобы можно было посмотреть глазами."
    )
    with open("response_debug.html", "w", encoding="utf-8") as f:
        f.write(resp.text)


if __name__ == "__main__":
    main()