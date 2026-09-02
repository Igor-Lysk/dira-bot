"""Yad2 — крупнейшая доска объявлений Израиля.

Отдаёт структурированный JSON внутри страницы (`__NEXT_DATA__`), но закрыта
антиботом: прямой запрос возвращает 200 и страницу без данных. Работает через
прокси-провайдеров — проверено с сервера: ScraperAPI отдаёт полную страницу.

Два отличия от v1:

1. **Города берутся из профилей.** В v1 в конфиге лежали шесть городов, а в URL
   было захардкожено `city=5000` — Тель-Авив. Остальные пять не запрашивались
   ни разу, отсюда 66 объявлений за 33 дня (F-10).
2. **Факты приходят из источника.** Цена, комнаты, метраж, этаж, улица и тег
   мамада есть в JSON — извлекать их из текста не нужно, и модель для таких
   объявлений не вызывается вовсе.
"""

import json
import logging
import random
import urllib.parse
from typing import Optional

import httpx

from core import settings
from core.sources import source_cities

log = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
]

MAMAD_TAG_IDS = {1009}
MAMAD_TAG_NAMES = {'ממ"ד', "ממ״ד", "ממד"}


def _feed_url(city_code: str, max_price: Optional[int], min_rooms: Optional[float]) -> str:
    params = {"city": city_code}
    if max_price:
        params["maxPrice"] = str(int(max_price))
    if min_rooms:
        params["minRooms"] = str(min_rooms).rstrip("0").rstrip(".")
    return "https://www.yad2.co.il/realestate/rent?" + urllib.parse.urlencode(params)


async def _fetch(client: httpx.AsyncClient, url: str) -> Optional[str]:
    """Провайдеры по очереди: первый, кто вернул страницу с данными, побеждает.

    Никакого учёта квот — просто пробуем по порядку. Когда месячные лимиты
    обновляются, верхний провайдер сам начинает работать снова.
    """
    headers = {"User-Agent": random.choice(USER_AGENTS)}
    target = urllib.parse.quote(url, safe="")
    providers = []
    if settings.SCRAPERAPI_KEY:
        providers.append(("scraperapi",
                          f"https://api.scraperapi.com/?api_key={settings.SCRAPERAPI_KEY}&url={target}"))
    if settings.SCRAPEDO_KEY:
        providers.append(("scrape.do",
                          f"https://api.scrape.do/?token={settings.SCRAPEDO_KEY}&url={target}&render=true&geoCode=il"))
    if settings.SCRAPINGBEE_KEY:
        providers.append(("scrapingbee",
                          f"https://app.scrapingbee.com/api/v1/?api_key={settings.SCRAPINGBEE_KEY}&url={target}&render_js=true"))
    providers.append(("прямой", url))

    for name, request_url in providers:
        try:
            response = await client.get(request_url, headers=headers, timeout=90)
        except Exception as e:                        # noqa: BLE001
            log.warning("yad2 через %s: %s", name, type(e).__name__)
            continue
        if response.status_code == 200 and "__NEXT_DATA__" in response.text:
            return response.text
        log.info("yad2 через %s: %s, данных нет", name, response.status_code)
    return None


def _parse(html: str) -> list:
    start = html.find("__NEXT_DATA__")
    if start < 0:
        return []
    brace = html.index("{", start)
    depth, end = 0, brace
    for i in range(brace, len(html)):
        if html[i] == "{":
            depth += 1
        elif html[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    data = json.loads(html[brace:end])
    queries = data["props"]["pageProps"]["dehydratedState"].get("queries", [])
    feed = next((q for q in queries if "rent-feed" in str(q.get("queryKey", ""))), None)
    if not feed:
        return []
    payload = feed["state"]["data"]
    items = []
    for section in ("private", "agency", "platinum"):
        chunk = payload.get(section)
        if isinstance(chunk, list):
            items.extend((item, section) for item in chunk)
    return items


def _has_mamad(tags: list) -> Optional[str]:
    for tag in tags or []:
        if tag.get("id") in MAMAD_TAG_IDS or tag.get("name", "") in MAMAD_TAG_NAMES:
            return "yes"
    return None        # тега нет — это «нет данных», а не «мамада нет»


def _to_raw(item: dict, section: str, city_name: str) -> Optional[dict]:
    token = item.get("token")
    if not token:
        return None
    address = item.get("address", {})
    house = address.get("house", {})
    details = item.get("additionalDetails", {})
    tags = item.get("tags", [])

    street = address.get("street", {}).get("text", "")
    number = house.get("number")
    full_street = " ".join(str(p) for p in (street, number) if p) or None
    price = item.get("price")
    rooms = details.get("roomsCount")
    sqm = details.get("squareMeter")
    floor = house.get("floor")

    text_parts = [details.get("property", {}).get("text", ""), city_name, full_street or ""]
    if rooms:
        text_parts.append(f"{rooms} חדרים")
    if sqm:
        text_parts.append(f"{sqm} מ״ר")
    if floor is not None:
        text_parts.append(f"קומה {floor}")
    if price:
        text_parts.append(f"{price} ₪")
    text_parts += [t.get("name", "") for t in tags if t.get("name")]

    return {
        "source": "yad2",
        "source_id": f"yad2_{token}",
        "channel": f"yad2/{city_name}",
        "url": f"https://www.yad2.co.il/item/{token}",
        "raw_text": ", ".join(p for p in text_parts if p),
        "facts": {
            "city": city_name,
            "district": address.get("neighborhood", {}).get("text") or None,
            "street": full_street,
            "rooms": float(rooms) if rooms else None,
            "area_sqm": int(sqm) if sqm else None,
            "floor": int(floor) if isinstance(floor, (int, float)) else None,
            "price": int(price) if price else None,
            "mamad": _has_mamad(tags),
            "deal_type": "rent",
            "contact_type": "private" if section == "private" else "agent",
        },
    }


async def collect(cities: list, max_price: int = None, min_rooms: float = None) -> list:
    targets = source_cities(cities, "yad2")
    if not targets:
        return []
    results = []
    async with httpx.AsyncClient(follow_redirects=True) as client:
        for name, code in targets:
            html = await _fetch(client, _feed_url(code, max_price, min_rooms))
            if not html:
                log.warning("yad2 %s: страницу получить не удалось", name)
                continue
            try:
                items = _parse(html)
            except Exception as e:                    # noqa: BLE001
                log.warning("yad2 %s: разбор не удался: %s", name, e)
                continue
            found = 0
            for item, section in items:
                raw = _to_raw(item, section, name)
                if raw:
                    results.append(raw)
                    found += 1
            log.info("yad2 %s: %d объявлений", name, found)
    return results
