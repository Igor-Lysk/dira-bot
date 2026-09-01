"""Схема фактов объявления.

Главный принцип v2 (решение D3): у каждого признака ТРИ состояния, а не два.

    YES     — в тексте явно сказано, что признак есть
    NO      — в тексте явно сказано, что признака нет ("ללא מעלית", "без лифта")
    UNKNOWN — в тексте про это ничего нет

UNKNOWN — полноценное значение, а не пропуск. Один пользователь отсекает такие
объявления, другой оставляет, чтобы посмотреть лично и спросить у хозяина.
Поэтому "не упомянуто" никогда не сворачивается в "нет".
"""

from dataclasses import dataclass, field, asdict
from typing import Optional, List

YES = "yes"
NO = "no"
UNKNOWN = None          # нет данных

TRISTATE = (YES, NO, UNKNOWN)

# Признаки, у которых есть все три состояния.
BOOL_FIELDS = (
    "mamad",            # ממ"ד внутри квартиры (миклат в подъезде — это НЕ mamad)
    "miklat",           # общее убежище в здании/районе
    "elevator",
    "balcony",
    "parking",
    "storage",
    "air_conditioning",
    "pets_allowed",
    "garden",
    "renovated",
    "immediate_entry",
    "no_broker",        # прямо сказано "без посредников" / "от хозяина"
)

# Числовые и строковые поля: значение либо None (нет данных).
VALUE_FIELDS = (
    "price",            # ₪ в месяц
    "rooms",            # израильский счёт, гостиная считается комнатой
    "area_sqm",
    "floor",
    "total_floors",
    "city",
    "district",
    "street",
    "furnished",        # full | partial | none | None
    "deal_type",        # rent | sale | sublet | shared | None
    "entry_date",
)

SCHEMA_VERSION = 1      # растёт при изменении набора полей или логики извлечения


@dataclass
class Facts:
    """Факты одного объявления. Всё, чего нет в тексте, остаётся None."""

    # значения
    price: Optional[int] = None
    rooms: Optional[float] = None
    area_sqm: Optional[int] = None
    floor: Optional[int] = None
    total_floors: Optional[int] = None
    city: Optional[str] = None
    district: Optional[str] = None
    street: Optional[str] = None
    furnished: Optional[str] = None
    deal_type: Optional[str] = None
    entry_date: Optional[str] = None

    # трёхзначные признаки
    mamad: Optional[str] = None
    mamad_evidence: Optional[str] = None   # найденная фраза, если защищённое помещение упомянуто неоднозначно
    miklat: Optional[str] = None
    elevator: Optional[str] = None
    balcony: Optional[str] = None
    parking: Optional[str] = None
    storage: Optional[str] = None
    air_conditioning: Optional[str] = None
    pets_allowed: Optional[str] = None
    garden: Optional[str] = None
    renovated: Optional[str] = None
    immediate_entry: Optional[str] = None
    no_broker: Optional[str] = None

    # служебное
    phones: List[str] = field(default_factory=list)
    fingerprint: Optional[str] = None
    source_layer: str = "rules"     # rules | llm | mixed
    schema_version: int = SCHEMA_VERSION

    def as_dict(self) -> dict:
        return asdict(self)

    def missing_fields(self) -> List[str]:
        """Поля, по которым нет данных — кандидаты на добор через LLM."""
        d = self.as_dict()
        return [k for k in (*VALUE_FIELDS, *BOOL_FIELDS) if d.get(k) is None]
