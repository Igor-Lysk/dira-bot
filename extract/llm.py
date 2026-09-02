"""LLM-слой извлечения: добирает то, что не взяли регулярки.

Модель здесь **только читает текст и возвращает факты**. Она не знает критериев
поиска, не выставляет оценок и не решает, подходит объявление или нет. Всё это
делает код в `core/match.py`.

Так сделано не из вкуса, а по результатам разбора v1. Там критерии лежали в
промпте, а модель возвращала вердикт — и на корпусе из 748 объявлений она
поставила «мамад есть» 226 раз, из которых 70 (31%) текстом не подтверждаются:
в 57 случаях в объявлении упомянут только миклат — общее убежище в здании,
которое в том же промпте капсом запрещено считать мамадом, — а в 13 случаях про
защищённое помещение не сказано вообще ничего. Модель дописывала факт по
единственному обязательному критерию поиска. Значит вердикт ей доверять нельзя,
а извлечение — можно, если спрашивать узко и проверять ответ.

Три правила этого слоя:

1. **Спрашиваем только недостающее.** Что взяли регулярки — не переспрашиваем:
   дешевле, быстрее и нечему разъезжаться. На корпусе это 80–90% цены и комнат.
2. **Регулярки главнее.** При расхождении побеждает детерминированный слой;
   значение модели пишется только в пустое поле.
3. **«Не знаю» — законный ответ.** В промпте это сказано прямо, и ответ null
   не считается ошибкой. Именно попытка всегда дать ответ и породила выдуманные
   мамады в v1.
"""

import json
import logging
import re
from typing import Optional, Tuple

from .schema import Facts, SCHEMA_VERSION

log = logging.getLogger(__name__)

# Цены Anthropic за миллион токенов. Нужны, чтобы расход считался, а не
# оценивался по памяти, как в v1 («примерно $1–2»).
PRICES_USD_PER_MTOK = {
    "claude-haiku-4-5-20251001": (1.00, 5.00),
}
DEFAULT_PRICE = (1.00, 5.00)

SYSTEM = (
    "Ты извлекаешь факты из объявлений об аренде жилья в Израиле. "
    "Текст может быть на иврите, русском или английском, часто вперемешку. "
    "Ты не оцениваешь объявление и не решаешь, подходит ли оно кому-либо — "
    "только читаешь, что в нём написано. Отвечаешь одним JSON-объектом без пояснений."
)

# Описания полей: что именно спрашиваем и как отвечать. Формулировки важны —
# каждая закрывает конкретную ошибку, встреченную на корпусе.
FIELD_HINTS = {
    "price": "аренда в шекелях за месяц, число. НЕ ваад-байт, НЕ арнона, НЕ залог, "
             "НЕ цена за ночь или за сутки. Если цена указана за ночь — null",
    "rooms": "число комнат по израильскому счёту (гостиная считается комнатой). "
             "Если написано «2 спальни» — это 3 комнаты",
    "area_sqm": "площадь в квадратных метрах, число",
    "floor": "этаж квартиры, число (первый/цокольный = 0)",
    "total_floors": "сколько всего этажей в доме",
    "city": "город одним словом на английском: Tel Aviv, Ramat Gan, Givatayim, "
            "Bnei Brak, Bat Yam, Holon, Herzliya, Jerusalem, Haifa и т.д.",
    "district": "район или квартал",
    "street": "улица и номер дома, как написано в объявлении",
    "entry_date": "дата въезда в формате YYYY-MM-DD; «сразу»/«מיידי» — строка \"now\"",
    "lease_months": "минимальный срок аренды в месяцах, число",
    "deal_type": "rent — сдаётся целая квартира; sublet — краткосрочно или на лето; "
                 "shared — сдаётся КОМНАТА в квартире с соседями; sale — продажа",
    "furnished": "full — полностью с мебелью, partial — частично, none — пустая",
    "commission": "\"none\" если прямо сказано без комиссии/без посредников; "
                  "иначе сумма или процент строкой",
    "price_includes_bills": "yes если цена включает счета и коммунальные, иначе no",
    "contact_type": "agent если пишет маклер или агентство, private если хозяин",
    "mamad": "yes ТОЛЬКО если в тексте прямо сказано, что защищённая комната "
             "(ממ\"ד / мамад / safe room) находится ВНУТРИ квартиры. "
             "מקלט (миклат) — это общее убежище в здании, это НЕ мамад, для него null. "
             "Если формулировка «מרחב מוגן» без указания, в квартире оно или в подъезде — null. "
             "Если про защищённое помещение не сказано ничего — null",
    "miklat": "yes если упомянуто общее убежище в здании или рядом",
    "elevator": "yes/no", "balcony": "yes/no", "parking": "yes/no",
    "storage": "yes/no", "air_conditioning": "yes/no",
    "pets_allowed": "yes если с животными можно, no если прямо запрещено",
    "garden": "yes/no", "renovated": "yes/no", "immediate_entry": "yes/no",
    "no_broker": "yes если прямо написано «без посредников» / «ללא תיווך»",
}

PROMPT = """Извлеки из объявления только перечисленные ниже поля.

ГЛАВНОЕ ПРАВИЛО: если в тексте про поле не сказано — верни null.
Не догадывайся, не выводи из косвенных признаков, не заполняй «наиболее вероятным».
null — это правильный и ожидаемый ответ, а не неудача.

ПОЛЯ:
{fields}

ОБЪЯВЛЕНИЕ:
---
{text}
---

Верни ровно один JSON-объект с перечисленными ключами и ничего больше."""


def build_prompt(text: str, facts: Facts, max_chars: int = 4000) -> Optional[str]:
    """Промпт на недостающие поля. None — если спрашивать нечего."""
    missing = [f for f in facts.missing_fields() if f in FIELD_HINTS]
    if not missing:
        return None
    fields = "\n".join(f"- {name}: {FIELD_HINTS[name]}" for name in missing)
    return PROMPT.format(fields=fields, text=text[:max_chars])


# ── разбор ответа ────────────────────────────────────────────────────────────

# ממ"ד содержит обычную двойную кавычку, и она рвёт JSON изнутри строки.
# Баг из v1, чинится тем же способом: подменяем на ивритский гершаим U+05F4.
_HEBREW_ABBREV = [
    ('ממ"ד', "ממ״ד"), ('צה"ל', "צה״ל"), ('ת"א', "ת״א"),
    ('ר"ג', "ר״ג"), ('ב"ב', "ב״ב"), ('שכ"ד', "שכ״ד"),
    ('מ"ר', "מ״ר"), ('חד"ש', "חד״ש"),
]


def _sanitize_hebrew(text: str) -> str:
    for bad, good in _HEBREW_ABBREV:
        text = text.replace(bad, good)
    return text


def _try(text: str):
    for candidate in (text, _sanitize_hebrew(text)):
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def parse_response(raw: str) -> dict:
    """Достать JSON из ответа модели. Пустой словарь, если не вышло."""
    text = (raw or "").strip()
    result = _try(text)
    if result is not None:
        return result

    if "```" in text:
        for part in text.split("```"):
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("{"):
                result = _try(part)
                if result is not None:
                    return result

    start = text.find("{")
    if start >= 0:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    result = _try(text[start:i + 1])
                    if result is not None:
                        return result
                    break

    log.warning("не разобрал ответ модели (%d символов): %r", len(text), text[:80])
    return {}


# ── слияние ──────────────────────────────────────────────────────────────────

_TRISTATE_FIELDS = {
    "mamad", "miklat", "elevator", "balcony", "parking", "storage",
    "air_conditioning", "pets_allowed", "garden", "renovated",
    "immediate_entry", "no_broker", "price_includes_bills",
}
_NUMERIC = {"price": int, "rooms": float, "area_sqm": int, "floor": int,
            "total_floors": int, "lease_months": int}
_ENUMS = {
    "deal_type": {"rent", "sale", "sublet", "shared"},
    "furnished": {"full", "partial", "none"},
    "contact_type": {"agent", "private"},
}


def _clean(name: str, value):
    """Привести значение к схеме. Мусор молча отбрасываем — лучше «нет данных»,
    чем правдоподобное враньё в поле, по которому потом отбирают."""
    if value is None or value == "" or value == "null":
        return None
    if name in _TRISTATE_FIELDS:
        v = str(value).strip().lower()
        return v if v in ("yes", "no") else None
    if name in _NUMERIC:
        try:
            v = _NUMERIC[name](str(value).replace(",", "").replace(" ", ""))
        except (TypeError, ValueError):
            return None
        if name == "price" and not (500 <= v <= 200000):
            return None
        if name == "rooms" and not (0.5 <= v <= 15):
            return None
        if name == "area_sqm" and not (10 <= v <= 1000):
            return None
        if name in ("floor", "total_floors") and not (0 <= v <= 80):
            return None
        return v
    if name in _ENUMS:
        v = str(value).strip().lower()
        return v if v in _ENUMS[name] else None

    text = str(value).strip()
    if not text:
        return None
    # Названия мест не бывают голыми числами. Живой прогон дал район «2» —
    # модель разложила «צוריאל 2» на улицу и «район», и в карточке получилось
    # «צוריאל, 2, Ramat Gan».
    if name in ("district", "city", "street") and text.isdigit():
        return None
    if name in ("district", "city") and len(text) < 3:
        return None
    # «צוריאל, 2» — модель иногда отделяет номер дома запятой, и в карточке это
    # читается как два разных поля адреса.
    if name == "street":
        text = re.sub(r",\s*(\d+[א-ת]?)$", r" \1", text)
    return text


def merge(facts: Facts, payload: dict) -> Facts:
    """Дописать в пустые поля. Значения регулярок не трогаем — они надёжнее."""
    changed = False
    for name, value in (payload or {}).items():
        if not hasattr(facts, name) or getattr(facts, name) is not None:
            continue
        cleaned = _clean(name, value)
        if cleaned is None:
            continue
        setattr(facts, name, cleaned)
        changed = True
    if changed:
        facts.source_layer = "mixed"
        facts.schema_version = SCHEMA_VERSION
    return facts


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    price_in, price_out = PRICES_USD_PER_MTOK.get(model, DEFAULT_PRICE)
    return round(input_tokens / 1e6 * price_in + output_tokens / 1e6 * price_out, 6)


async def fill_gaps(text: str, facts: Facts, client, model: str) -> Tuple[Facts, dict]:
    """Дозаполнить факты через модель. Возвращает (факты, расход).

    Ошибка модели не считается результатом: факты возвращаются как есть, а вызов
    помечается неуспешным, чтобы объявление ушло в повторную обработку, а не
    осело в базе как «проанализировано» (баг F-11 из v1).
    """
    prompt = build_prompt(text, facts)
    if prompt is None:
        return facts, {"skipped": True}

    try:
        message = await client.messages.create(
            model=model, max_tokens=900, system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:                      # noqa: BLE001
        log.warning("вызов модели не удался: %s", e)
        return facts, {"ok": False, "error": str(e)}

    payload = parse_response(message.content[0].text)
    facts = merge(facts, payload)
    usage = getattr(message, "usage", None)
    tokens_in = getattr(usage, "input_tokens", 0)
    tokens_out = getattr(usage, "output_tokens", 0)
    return facts, {
        "ok": True, "model": model,
        "input_tokens": tokens_in, "output_tokens": tokens_out,
        "cost_usd": cost_usd(model, tokens_in, tokens_out),
        "fields_asked": len(facts.missing_fields()),
    }
