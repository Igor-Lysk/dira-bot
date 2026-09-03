"""Цена объявления относительно рынка.

Зачем это понадобилось раньше, чем планировалось. Ранжирование награждало
дешевизну, и наверх дайджеста выходили четырёхкомнатные квартиры в Рамат-Гане
за 2 716 ₪ при медиане 6 500. Проверка исходных строк показала, что парсер
разобрал их верно — это то, что публикует доска. То есть дёшево здесь не
признак находки, а признак мусорного объявления: устаревшего, приманки или
на самом деле комнаты в квартире.

Значит, цену надо сравнивать не с бюджетом пользователя, а с рынком.

Медианы считаем по своей же базе — она растёт и отражает ровно те источники,
которые мы читаем. Пока данных мало, опираемся на внешний ориентир.

Внешние ориентиры (проверены 3 сентября 2026):
* ЦСБ Израиля, I квартал 2026, средняя аренда по городу, все размеры квартир:
  Тель-Авив 7 351, Рамат-Ган 5 826, Холон 4 885, Бней-Брак 4 397, Бат-Ям 4 348,
  в среднем по стране 5 027 ₪.
* Август 2026, медианы: по стране около 5 000, Тель-Авив около 9 000, а по
  трёхкомнатным в Тель-Авиве около 10 300 ₪.

Наши собственные медианы по 3–4.5 комнатам на тот же момент: Тель-Авив 8 800,
Рамат-Ган 6 500, Гиватаим 6 500, Бней-Брак 4 850. Выше средних ЦСБ на 10–20%,
что ожидаемо: ЦСБ считает по всем размерам и по действующим договорам, а мы —
по запрашиваемым ценам на квартиры от 3 комнат. Порядок величин сходится, то
есть база пригодна для сравнения.
"""

import logging
import statistics
import time
from typing import Optional

log = logging.getLogger(__name__)

# Ориентиры на случай, когда своих данных по городу ещё мало. Средняя по городу
# из ЦСБ за I квартал 2026, приведённая к трёхкомнатной квартире коэффициентом
# 1.15 — по августовским данным для Тель-Авива медиана трёшек примерно на
# столько выше общей медианы города.
REFERENCE_3ROOM = {
    "Tel Aviv": 8500,
    "Ramat Gan": 6700,
    "Givatayim": 6700,
    "Bnei Brak": 5100,
    "Bat Yam": 5000,
    "Holon": 5600,
    "Herzliya": 7500,
    "Jerusalem": 6700,
    "Haifa": 4000,
}

# Как цена зависит от числа комнат. Коэффициент к трёхкомнатной.
ROOM_FACTOR = {1.0: 0.55, 1.5: 0.65, 2.0: 0.75, 2.5: 0.87,
               3.0: 1.0, 3.5: 1.13, 4.0: 1.28, 4.5: 1.42, 5.0: 1.55}

MIN_SAMPLE = 8          # меньше — своей медиане не верим
CACHE_TTL_SEC = 3600

# Границы. Подбирались по живым данным: настоящие «дёшево, но реально» лежат
# в 0.7–0.85 от медианы, а всё, что ниже 0.55, на проверке оказывалось мусором.
SUSPICIOUS_BELOW = 0.55
CHEAP_BELOW = 0.85
EXPENSIVE_ABOVE = 1.25

_cache = {"at": 0.0, "medians": {}}


def _bucket(rooms: Optional[float]) -> Optional[float]:
    """Округляем до половины комнаты — так группы не рассыпаются в пыль."""
    if rooms is None:
        return None
    value = round(float(rooms) * 2) / 2
    return min(max(value, 1.0), 5.0)


async def medians(store, days: int = 120, force: bool = False) -> dict:
    """{(город, комнаты): медиана} по собственной базе, с кэшем на час."""
    now = time.time()
    if not force and _cache["medians"] and now - _cache["at"] < CACHE_TTL_SEC:
        return _cache["medians"]

    cur = await store._db.execute(
        "SELECT city, rooms, price FROM listing_facts"
        " WHERE price IS NOT NULL AND city IS NOT NULL AND rooms IS NOT NULL"
        "   AND (deal_type IS NULL OR deal_type = 'rent')"
        "   AND extracted_at >= datetime('now', ?)", (f"-{days} days",))
    groups: dict = {}
    for city, rooms, price in await cur.fetchall():
        groups.setdefault((city, _bucket(rooms)), []).append(price)

    result = {key: statistics.median(values)
              for key, values in groups.items() if len(values) >= MIN_SAMPLE}
    _cache.update({"at": now, "medians": result})
    log.info("медианы пересчитаны: %d групп из %d", len(result), len(groups))
    return result


def expected_price(city: Optional[str], rooms: Optional[float], own: dict) -> Optional[float]:
    """Сколько такая квартира стоит на рынке. Сначала свои данные, потом ориентир."""
    bucket = _bucket(rooms)
    if city is None or bucket is None:
        return None
    if (city, bucket) in own:
        return own[(city, bucket)]
    base = REFERENCE_3ROOM.get(city)
    if base is None:
        return None
    return base * ROOM_FACTOR.get(bucket, 1.0)


def assess(facts: dict, own: dict) -> Optional[dict]:
    """Как цена объявления соотносится с рынком.

    Возвращает None, когда сравнивать не с чем — это «нет данных», а не «норма».
    """
    price = facts.get("price")
    expected = expected_price(facts.get("city"), facts.get("rooms"), own)
    if not price or not expected:
        return None
    ratio = price / expected
    if ratio < SUSPICIOUS_BELOW:
        verdict = "suspicious"
    elif ratio < CHEAP_BELOW:
        verdict = "cheap"
    elif ratio > EXPENSIVE_ABOVE:
        verdict = "expensive"
    else:
        verdict = "market"
    return {"ratio": round(ratio, 2), "expected": round(expected),
            "verdict": verdict, "diff_pct": round((ratio - 1) * 100)}


def describe(assessment: Optional[dict]) -> str:
    """Короткая подпись для карточки и дайджеста."""
    if not assessment:
        return ""
    pct = abs(assessment["diff_pct"])
    return {
        "suspicious": f"подозрительно дёшево: на {pct}% ниже рынка",
        "cheap": f"на {pct}% ниже рынка",
        "market": "по рынку",
        "expensive": f"на {pct}% выше рынка",
    }[assessment["verdict"]]
