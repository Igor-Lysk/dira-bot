"""Komo.co.il — вторая доска объявлений с серверным рендерингом.

Найдена при разведке 2 сентября. Как и Homeless, отдаёт готовый HTML без
антибота и без браузера, и так же структурирована: заголовок содержит город,
район и улицу, отдельными блоками идут цена и описание с числом комнат,
метражом и этажом.

Две ловушки, обе стоили времени при разведке:

* Шекель записан HTML-сущностью `&#8362;`, а не символом. Проверка «есть ли ₪
  в тексте» дала ноль, и страница выглядела пустой, хотя объявления там были.
* Название города должно совпадать буквально: «תל אביב» возвращает пустую
  выдачу, работает только «תל אביב יפו».

Пагинация здесь через AJAX: `page`, `p`, `pageNum` в адресе ничего не меняют,
всегда приходят те же 20 карточек. Для почасового опроса этого достаточно —
новые объявления появляются сверху.
"""

import html as html_lib
import logging
import re
import urllib.parse
from typing import Optional

import httpx

from core.sources import source_cities

log = logging.getLogger(__name__)

BASE = "https://www.komo.co.il"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/123.0 Safari/537.36")

_AD_RE = re.compile(
    r'<div id="modaaRowDv(\d+)".*?</div>\s*</div>\s*</div>', re.S)
_TITLE_RE = re.compile(r'<h2 class="title">(.*?)</h2>', re.S)
_PRICE_RE = re.compile(r'<div class="price">(.*?)</div>', re.S)
_DESC_RE = re.compile(r'<div class="description">(.*?)</div>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")

_ROOMS_RE = re.compile(r"(\d+(?:\.\d)?)\s*חדרים")
_SQM_RE = re.compile(r"\((\d+)\s*מ")
_FLOOR_RE = re.compile(r"קומה:\s*(\d+)(?:\s*מתוך\s*(\d+))?")


def _clean(fragment: str) -> str:
    return re.sub(r"\s+", " ", html_lib.unescape(_TAG_RE.sub(" ", fragment or ""))).strip()


def _price(fragment: str) -> Optional[int]:
    text = _clean(fragment).replace(",", "")
    m = re.search(r"\d{3,6}", text)
    if not m:
        return None
    value = int(m.group())
    return value if 500 <= value <= 200000 else None


def _to_raw(ad_id: str, block: str, city_name: str) -> Optional[dict]:
    title = _clean((_TITLE_RE.search(block) or [None, ""])[1] if _TITLE_RE.search(block) else "")
    if not title:
        return None
    desc = _clean((_DESC_RE.search(block).group(1)) if _DESC_RE.search(block) else "")
    price_block = _PRICE_RE.search(block)
    price = _price(price_block.group(1)) if price_block else None

    # «רמת גן, יד לבנים, צל הגבעה 16» — город, район, улица с номером
    parts = [p.strip() for p in title.split(",") if p.strip()]
    district = parts[1] if len(parts) > 2 else None
    street = parts[-1] if len(parts) > 1 else None

    rooms = _ROOMS_RE.search(desc)
    sqm = _SQM_RE.search(desc)
    floor = _FLOOR_RE.search(desc)

    return {
        "source": "komo",
        "source_id": f"komo_{ad_id}",
        "channel": "komo",
        "url": f"{BASE}/code/nadlan/details/?modaaNum={ad_id}",
        "raw_text": f"{title}. {desc}" + (f" {price} ₪" if price else ""),
        "facts": {
            "city": city_name,
            "district": district,
            "street": street,
            "rooms": float(rooms.group(1)) if rooms else None,
            "area_sqm": int(sqm.group(1)) if sqm else None,
            "floor": int(floor.group(1)) if floor else None,
            "total_floors": int(floor.group(2)) if floor and floor.group(2) else None,
            "price": price,
            "deal_type": "rent",
        },
    }


async def collect(cities: list) -> list:
    targets = source_cities(cities, "komo")
    if not targets:
        return []
    results = []
    async with httpx.AsyncClient(timeout=45, follow_redirects=True,
                                 headers={"User-Agent": UA,
                                          "Accept-Language": "he-IL,he;q=0.9"}) as client:
        for name, hebrew in targets:
            url = (f"{BASE}/code/nadlan/apartments-for-rent.asp?nehes=1&cityName="
                   + urllib.parse.quote_plus(hebrew))
            try:
                response = await client.get(url)
                response.raise_for_status()
            except Exception as e:                    # noqa: BLE001
                log.warning("komo %s: %s", name, e)
                continue
            found = 0
            for ad_id, block in [(m.group(1), m.group(0)) for m in _AD_RE.finditer(response.text)]:
                raw = _to_raw(ad_id, block, name)
                if raw:
                    results.append(raw)
                    found += 1
            log.info("komo %s: %d объявлений", name, found)
    return results
