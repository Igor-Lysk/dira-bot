"""Homeless.co.il — доска объявлений с серверным рендерингом.

Самый дешёвый источник из всех: обычный HTTP-запрос, никакого прокси и никакого
браузера. Проверено с сервера — страница отдаёт готовый HTML и не блокируется.

И, что важнее, таблица уже структурирована: тип жилья, город, район, улица,
комнаты, этаж, цена, дата въезда. То есть модель для этих объявлений не нужна
вовсе — факты приходят из источника, а не извлекаются из текста. Каждое такое
объявление экономит вызов модели.
"""

import logging
import re
import urllib.parse
from typing import Optional

import httpx

from core.sources import city_from_hebrew, source_cities

log = logging.getLogger(__name__)

BASE = "https://www.homeless.co.il"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/123.0 Safari/537.36")

_ROW_RE = re.compile(r'<tr[^>]*id="ad_(\d+)"[^>]*>(.*?)</tr>', re.S)
_CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")

# Разбор идёт по содержимому ячеек, а не по их номерам.
#
# Причина конкретная: в таблице встречаются строки и с 12 ячейками, и с 11 —
# когда этаж не указан, он не пустеет, а исчезает, и всё правое съезжает влево.
# Жёсткие индексы давали «9 ₪ в месяц» и «этаж 15500»: цена попадала в этаж, а
# дата — в цену. Вылезло сразу же, как только карточки попали в ленту бота.
#
# Стабильна только левая часть: тип, город, район, улица. Дальше опираемся на
# то, что видно в самой ячейке — шекель, формат даты, маленькое число.

COL_TYPE, COL_CITY, COL_DISTRICT, COL_STREET = 2, 3, 4, 5

_PRICE_CELL_RE = re.compile(r"[\d,]{3,}\s*(?:₪|&#8362;)")
_DATE_CELL_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
_NUM_CELL_RE = re.compile(r"^\d+(?:\.\d)?$")


def _text(cell: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", cell)).strip()


def _number(value: str) -> Optional[float]:
    m = re.search(r"\d+(?:[.,]\d+)?", (value or "").replace(",", ""))
    return float(m.group()) if m else None


def _entry_date(value: str) -> Optional[str]:
    """«01/01/2026» → ISO, «מיידי» → now, «גמיש» (по договорённости) → нет данных."""
    value = (value or "").strip()
    if not value or "גמיש" in value:
        return None
    if "מיידי" in value:
        return "now"
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", value)
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None


def _to_raw(ad_id: str, cells: list, city_name: str) -> Optional[dict]:
    if len(cells) <= COL_STREET:
        return None

    price_at = next((i for i, c in enumerate(cells) if "₪" in c), None)
    if price_at is None:
        return None
    price = _number(cells[price_at])

    # между улицей и ценой стоят комнаты и, если он указан, этаж
    middle = [c for c in cells[COL_STREET + 1:price_at] if _NUM_CELL_RE.match(c)]
    rooms = float(middle[0]) if middle else None
    floor = float(middle[1]) if len(middle) > 1 else None

    # сразу за ценой — дата въезда, последняя дата в строке — дата публикации.
    # Приводим её к ISO: в таблице она в виде 02/09/2026, а в таком виде даты
    # не сравниваются ни в SQL, ни между источниками.
    entry = cells[price_at + 1] if price_at + 1 < len(cells) else ""
    posted_raw = next((c for c in reversed(cells) if _DATE_CELL_RE.match(c)), None)
    posted = None
    if posted_raw:
        d, m, y = posted_raw.split("/")
        posted = f"{y}-{m}-{d}"

    district, street = cells[COL_DISTRICT], cells[COL_STREET]

    parts = [cells[COL_TYPE], cells[COL_CITY], district, street]
    if rooms:
        parts.append(f"{rooms:g} חדרים")
    if floor is not None:
        parts.append(f"קומה {floor:g}")
    if price:
        parts.append(f"{price:g} ₪")
    if entry:
        parts.append(f"כניסה {entry}")
    raw_text = ", ".join(p for p in parts if p)

    return {
        "source": "homeless",
        "source_id": f"hl_{ad_id}",
        "channel": "homeless",
        "url": f"{BASE}/rent/viewad,{ad_id}.aspx",
        "raw_text": raw_text,
        "posted_at": posted,
        # Факты из источника: их не надо извлекать и незачем перепроверять моделью.
        "facts": {
            # город берём из самого объявления: в выдаче по городу попадаются
            # соседние населённые пункты
            "city": city_from_hebrew(cells[COL_CITY], city_name),
            "district": district or None,
            "street": street or None,
            "rooms": rooms,
            "floor": int(floor) if floor is not None else None,
            "price": int(price) if price else None,
            "entry_date": _entry_date(entry),
            "deal_type": "rent",
        },
    }


async def collect(cities: list) -> list:
    """Собрать объявления по нужным городам. Пустой список городов — ничего не делаем."""
    targets = source_cities(cities, "homeless")
    if not targets:
        return []

    results = []
    async with httpx.AsyncClient(timeout=45, follow_redirects=True,
                                 headers={"User-Agent": UA}) as client:
        for name, hebrew in targets:
            url = f"{BASE}/rent/city=" + urllib.parse.quote(hebrew)
            try:
                response = await client.get(url)
                response.raise_for_status()
            except Exception as e:                    # noqa: BLE001
                log.warning("homeless %s: %s", name, e)
                continue
            found = 0
            for ad_id, body in _ROW_RE.findall(response.text):
                cells = [_text(c) for c in _CELL_RE.findall(body)]
                raw = _to_raw(ad_id, cells, name)
                if raw:
                    results.append(raw)
                    found += 1
            log.info("homeless %s: %d объявлений", name, found)
    return results
