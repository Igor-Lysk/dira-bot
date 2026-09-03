"""Сопоставление фактов с профилем поиска и ранжирование. Без обращения к модели.

Здесь живёт вся логика отбора. Она детерминированная, объяснимая и запускается
отдельно для каждого профиля поверх одной общей таблицы фактов — в этом и состоит
главный сдвиг v2 по сравнению с v1, где критерии были текстом внутри промпта.

Два правила, из которых следует всё остальное:

1. **«Нет данных» — не «нет».** Отсутствие поля никогда не считается нарушением
   требования. Отсекать по нему можно только если пользователь явно попросил
   (`required`), и это его осознанный выбор, а не поведение по умолчанию.

2. **Ранг объясним.** Каждое слагаемое подписано и возвращается вместе с числом.
   Пользователь должен видеть, почему объявление наверху, а мы — чинить ранжирование,
   не гадая. В v1 ранга не было вовсе: сортировали по оценке модели, которая в 55%
   случаев выдавала 8 или 9.
"""

from dataclasses import dataclass, field
from typing import Optional

REQUIRED = "required"            # только явное «есть»
ALLOW_UNKNOWN = "allow_unknown"  # «есть» либо нет данных — посмотрю сам
IGNORE = "ignore"

# Признак профиля → поле фактов.
FEATURE_REQUIREMENTS = {
    "req_mamad": "mamad",
    "req_elevator": "elevator",
    "req_parking": "parking",
    "req_balcony": "balcony",
    "req_pets": "pets_allowed",
    "req_furnished": "furnished",
}


@dataclass
class MatchResult:
    matched: bool
    rank: float = 0.0
    reasons: list = field(default_factory=list)   # [(подпись, вклад)]
    rejected_by: Optional[str] = None             # какое правило не прошло

    def explain(self) -> str:
        if not self.matched:
            return f"не подходит: {self.rejected_by}"
        parts = [f"{name} {value:+.1f}" for name, value in self.reasons if value]
        return f"ранг {self.rank:.2f} · " + ", ".join(parts)


def _feature_ok(value, mode) -> bool:
    if mode == REQUIRED:
        return value == "yes"
    if mode == ALLOW_UNKNOWN:
        return value != "no"          # «есть» или нет данных
    return True


def match(facts: dict, profile: dict, market: dict = None) -> MatchResult:
    """Подходит ли объявление профилю, и насколько.

    facts — строка listing_facts как словарь. profile — строка search_profiles.
    market — оценка цены относительно рынка из `core.market.assess`, если есть.
    """
    # ── жёсткие правила ──────────────────────────────────────────────────────
    deal = facts.get("deal_type")

    if deal == "sale":
        return MatchResult(False, rejected_by="продажа, а не аренда")
    if profile.get("exclude_shared", 1) and deal == "shared":
        return MatchResult(False, rejected_by="комната в квартире с соседями")
    if profile.get("exclude_sublet", 1) and deal == "sublet":
        return MatchResult(False, rejected_by="саблет")

    cities = profile.get("cities") or []
    city = facts.get("city")
    if cities:
        if city is None:
            # Город — единственный признак, где «нет данных» по умолчанию НЕ
            # проходит. Причина из живого прогона: объявления из хайфского
            # канала не называют город (читателю он очевиден) и проходили
            # тель-авивский фильтр как «неизвестно», занимая верх выдачи.
            # Подсказку из метаданных канала подставляет вызывающий код
            # (core.sources.region_of), сюда факты приходят уже с ней.
            if profile.get("allow_unknown_city", 0):
                pass
            else:
                return MatchResult(False, rejected_by="город не опознан")
        elif city not in cities:
            return MatchResult(False, rejected_by=f"город {city} не в списке")

    price = facts.get("price")
    price_max = profile.get("price_max")
    if price_max is not None and price is not None and price > price_max:
        return MatchResult(False, rejected_by=f"{price} ₪ дороже потолка {price_max} ₪")

    rooms = facts.get("rooms")
    rooms_min = profile.get("rooms_min")
    if rooms_min is not None and rooms is not None and rooms < rooms_min:
        return MatchResult(False, rejected_by=f"{rooms:g} комн. меньше минимума {rooms_min:g}")
    rooms_max = profile.get("rooms_max")
    if rooms_max is not None and rooms is not None and rooms > rooms_max:
        return MatchResult(False, rejected_by=f"{rooms:g} комн. больше максимума {rooms_max:g}")

    area_min = profile.get("area_min")
    area = facts.get("area_sqm")
    if area_min is not None and area is not None and area < area_min:
        return MatchResult(False, rejected_by=f"{area} м² меньше минимума {area_min} м²")

    floor = facts.get("floor")
    if floor is not None:
        if profile.get("floor_min") is not None and floor < profile["floor_min"]:
            return MatchResult(False, rejected_by=f"этаж {floor} ниже допустимого")
        if profile.get("floor_max") is not None and floor > profile["floor_max"]:
            return MatchResult(False, rejected_by=f"этаж {floor} выше допустимого")

    for key, fact_field in FEATURE_REQUIREMENTS.items():
        mode = profile.get(key, IGNORE)
        if mode == IGNORE:
            continue
        value = facts.get(fact_field)
        # мамад — особый случай: неоднозначная формулировка («מרחב מוגן» без
        # указания, в квартире оно или в подъезде) при allow_unknown засчитывается,
        # потому что её как раз и можно уточнить у хозяина
        if fact_field == "mamad" and mode == ALLOW_UNKNOWN and facts.get("mamad_evidence"):
            continue
        if not _feature_ok(value, mode):
            return MatchResult(False, rejected_by=f"{fact_field}: требуется {mode}, в объявлении {value or 'нет данных'}")

    text = (facts.get("raw_text") or "").lower()
    for word in (profile.get("stop_words") or []):
        if word.lower() in text:
            return MatchResult(False, rejected_by=f"стоп-слово «{word}»")

    # ── ранг ─────────────────────────────────────────────────────────────────
    reasons = []

    # Запас по бюджету — от желаемой цены, а не от потолка.
    ideal = profile.get("price_ideal") or price_max
    if price is not None and ideal:
        if price <= ideal:
            reasons.append(("в бюджете", 2.0))
        else:
            over = (price - ideal) / max(ideal, 1)
            reasons.append(("дороже желаемого", -round(min(2.0, over * 4), 2)))

    # Дешевизна сама по себе — не достоинство. Сравниваем с рынком: цена ниже
    # медианы по городу и числу комнат на 15–45% это хорошая находка, а вдвое
    # ниже рынка — почти всегда мусорное объявление. До этой поправки наверх
    # дайджеста выходили четырёхкомнатные в Рамат-Гане за 2 716 ₪ при медиане
    # 6 500, и объяснение ранга у них было такое же, как у настоящих находок.
    if market:
        if market["verdict"] == "suspicious":
            reasons.append((f"подозрительно дёшево для района ({market['ratio']} от медианы)", -3.0))
        elif market["verdict"] == "cheap":
            reasons.append((f"дешевле рынка на {abs(market['diff_pct'])}%", 1.2))
        elif market["verdict"] == "expensive":
            reasons.append((f"дороже рынка на {market['diff_pct']}%", -0.5))

    if facts.get("mamad") == "yes":
        reasons.append(("мамад подтверждён", 2.0))
    elif facts.get("mamad_evidence"):
        reasons.append(("защищённое помещение упомянуто, но неясно чьё", 0.8))

    if rooms is not None and rooms_min is not None and rooms >= rooms_min + 1:
        reasons.append(("комнат с запасом", 0.5))

    # Полнота данных: объявление, где всё указано, экономит время и обычно
    # написано хозяином, а не выгружено пачкой
    filled = sum(1 for f in ("price", "rooms", "area_sqm", "floor", "street", "entry_date")
                 if facts.get(f) is not None)
    if filled >= 5:
        reasons.append(("объявление подробное", 0.7))
    elif filled <= 2:
        reasons.append(("мало данных", -0.5))

    if facts.get("no_broker") == "yes":
        reasons.append(("без посредника", 0.6))
    if facts.get("commission") and facts.get("commission") != "none":
        reasons.append(("есть комиссия", -0.4))

    if facts.get("immediate_entry") == "yes":
        reasons.append(("въезд сразу", 0.3))
    if facts.get("elevator") == "no" and (floor or 0) >= 3:
        reasons.append(("высокий этаж без лифта", -0.6))

    rank = round(sum(v for _, v in reasons), 2)
    return MatchResult(True, rank=rank, reasons=reasons)
