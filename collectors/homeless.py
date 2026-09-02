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

from core.sources import source_cities

log = logging.getLogger(__name__)

BASE = "https://www.homeless.co.il"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/123.0 Safari/537.36")

_ROW_RE = re.compile(r'<tr[^>]*id="ad_(\d+)"[^>]*>(.*?)</tr>', re.S)
_CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")

# Порядок колонок в таблице (проверен на живой странице):
# 0 чекбокс · 1 фото · 2 тип · 3 город · 4 район · 5 улица · 6 комнаты
# 7 этаж · 8 цена · 9 дата въезда · 10 дата публикации
COL = {"type": 2, "city": 3, "district": 4, "street": 5, "rooms": 6,
       "floor": 7, "price": 8, "entry": 9, "published": 10}


def _text(cell: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", cell)).strip()


def _number(value: str) -> Optional[float]:
    m = re.search(r"\d+(?:[.,]\d+)?", (value or "").replace(",", ""))
    return float(m.group()) if m else None


def _entry_date(value: str) -> Optional[str]:
    """«01/01/2026» → ISO; «מיידי» → now."""
    value = (value or "").strip()
    if not value:
        return None
    if "מיידי" in value:
        return "now"
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", value)
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None


def _to_raw(ad_id: str, cells: list, city_name: str) -> Optional[dict]:
    if len(cells) <= COL["published"]:
        return None
    get = lambda key: cells[COL[key]] if COL[key] < len(cells) else ""   # noqa: E731

    price = _number(get("price"))
    rooms = _number(get("rooms"))
    floor = _number(get("floor"))
    street, district = get("street"), get("district")

    # Текст собираем сами — он нужен и для карточки, и чтобы отработали общие
    # фильтры (саблет, комната с соседями), и как исходник, если позже
    # понадобится переизвлечение.
    parts = [get("type"), get("city"), district, street]
    if rooms:
        parts.append(f"{rooms:g} חדרים")
    if floor is not None:
        parts.append(f"קומה {floor:g}")
    if price:
        parts.append(f"{price:g} ₪")
    if get("entry"):
        parts.append(f"כניסה {get('entry')}")
    raw_text = ", ".join(p for p in parts if p)

    return {
        "source": "homeless",
        "source_id": f"hl_{ad_id}",
        "channel": "homeless",
        "url": f"{BASE}/rent/viewad,{ad_id}.aspx",
        "raw_text": raw_text,
        # Факты из источника: их не надо извлекать и незачем перепроверять моделью.
        "facts": {
            "city": city_name,
            "district": district or None,
            "street": street or None,
            "rooms": rooms,
            "floor": int(floor) if floor is not None else None,
            "price": int(price) if price else None,
            "entry_date": _entry_date(get("entry")),
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
