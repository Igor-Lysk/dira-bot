"""Жалобы на объявления и ответы на них.

Раньше кнопка «данные неверны» записывала жалобу и отвечала «разберу заново» —
на этом разговор заканчивался. Человек не узнавал, помогло ли, и для него это
было неотличимо от кнопки, которая ничего не делает. Здесь жалоба доводится до
ответа: что было, что стало, откуда это в тексте объявления.

Два вида жалоб по тому, как на них отвечать:

* **на поле** (цена, комнаты, мамад, адрес) — поле разбирается заново, ответ
  приходит после повторного разбора, беззвучно, ответом на карточку;
* **остальные** (снято, дубль, агент, не подходит, другое) — ответ сразу, в том
  же сообщении, где человек выбирал причину.
"""

import html
import json
import logging
import re
from html import escape
from typing import Optional

from core.match import ALLOW_UNKNOWN, FEATURE_REQUIREMENTS, IGNORE, REQUIRED, match
from core.store import Store

log = logging.getLogger(__name__)

# Порядок — порядок кнопок, по две в ряд.
KINDS = [
    ("price", "Цена"), ("rooms", "Комнаты"),
    ("mamad", "Мамад"), ("address", "Адрес"),
    ("removed", "Снято / не открывается"), ("duplicate", "Это дубль"),
    ("agent", "Агент / комиссия"), ("notfit", "Не подходит мне"),
    ("other", "Другое"),
]
LABELS = dict(KINDS)

# Жалобы на поле: разбираем заново и отвечаем, когда разбор пройдёт.
REPARSE_KINDS = ("price", "rooms", "mamad", "address")

FIELDS_OF = {
    "price": ["price"],
    "rooms": ["rooms"],
    "mamad": ["mamad", "mamad_evidence"],
    "address": ["street", "district", "city"],
    "agent": ["contact_type", "no_broker", "commission"],
}

# Повторный разбор идёт проходом дозаполнения раз в десять минут. Если за час
# не прошёл — дальше ждать бессмысленно, честнее сказать об этом.
WAIT_LIMIT_MIN = 60
# Сколько ждать фразу после «Другое». Дольше — человек, скорее всего, передумал,
# и его следующее сообщение к жалобе уже не относится.
TEXT_WAIT_MIN = 30

SOURCE_NAMES = {"komo": "Komo", "homeless": "Homeless", "telegram": "Telegram",
                "yad2": "Yad2", "facebook": "Facebook"}

FEATURE_LABELS = {
    "req_mamad": "Мамад", "req_elevator": "Лифт", "req_parking": "Парковка",
    "req_balcony": "Балкон", "req_pets": "Животные", "req_furnished": "Мебель",
}


# ── журнал ───────────────────────────────────────────────────────────────────

def snapshot(kind: str, facts: dict) -> dict:
    """Значения спорных полей на момент жалобы — чтобы потом сказать «было / стало»."""
    return {name: facts.get(name) for name in FIELDS_OF.get(kind, [])}


async def record(store: Store, user_id: int, listing_id: str, kind: str, *,
                 before: dict = None, detail: str = None, chat_id: int = None,
                 card_msg_id: int = None, status: str = "waiting",
                 reply: str = None) -> int:
    cur = await store._db.execute(
        "INSERT INTO feedback (user_id, listing_id, kind, detail, before, chat_id,"
        " card_msg_id, status, reply, closed_at)"
        " VALUES (?,?,?,?,?,?,?,?,?, CASE WHEN ? = 'closed' THEN datetime('now') END)",
        (user_id, listing_id, kind, detail, json.dumps(before or {}, ensure_ascii=False),
         chat_id, card_msg_id, status, reply, status))
    await store._db.commit()
    return cur.lastrowid


async def close(store: Store, feedback_id: int, reply: Optional[str], detail: str = None):
    await store._db.execute(
        "UPDATE feedback SET status = 'closed', closed_at = datetime('now'), reply = ?,"
        " detail = COALESCE(?, detail) WHERE id = ?", (reply, detail, feedback_id))
    await store._db.commit()


async def awaiting_text(store: Store, user_id: int) -> Optional[dict]:
    """Жалоба «Другое», к которой человек ещё не написал текст."""
    cur = await store._db.execute(
        "SELECT * FROM feedback WHERE user_id = ? AND status = 'awaiting_text'"
        "   AND created_at >= datetime('now', ?) ORDER BY id DESC LIMIT 1",
        (user_id, f"-{TEXT_WAIT_MIN} minutes"))
    row = await cur.fetchone()
    return dict(row) if row else None


async def expire_awaiting(store: Store):
    """Не дождались текста — закрываем, чтобы жалоба не висела вечно."""
    await store._db.execute(
        "UPDATE feedback SET status = 'closed', closed_at = datetime('now')"
        " WHERE status = 'awaiting_text' AND created_at < datetime('now', ?)",
        (f"-{TEXT_WAIT_MIN} minutes",))
    await store._db.commit()


async def hide_for(store: Store, profile: Optional[dict], listing_id: str):
    if profile:
        await store.set_match_state(profile["id"], listing_id, "hidden")


# ── оформление ───────────────────────────────────────────────────────────────

def _money(value) -> str:
    return f"{int(value):,}".replace(",", " ") + " ₪"


def _address(d: dict) -> Optional[str]:
    parts, seen = [], set()
    for value in (d.get("street"), d.get("district"), d.get("city")):
        if value and value not in seen:
            parts.append(value)
            seen.add(value)
    return ", ".join(parts) or None


def shown(kind: str, d: dict) -> Optional[str]:
    """Как значение поля выглядит для человека. None — в объявлении не указано."""
    if kind == "price":
        return _money(d["price"]) if d.get("price") else None
    if kind == "rooms":
        return f"{d['rooms']:g}" if d.get("rooms") is not None else None
    if kind == "mamad":
        if d.get("mamad") == "yes":
            return "есть"
        if d.get("mamad") == "no":
            return "нет"
        if d.get("mamad_evidence"):
            return "упомянуто защищённое помещение, но неясно, в квартире ли"
        return None
    if kind == "address":
        return _address(d)
    return None


WHAT = {"price": "цена", "rooms": "число комнат", "mamad": "мамад", "address": "адрес"}
WHAT_ACC = {"price": "цену", "rooms": "число комнат", "mamad": "мамад", "address": "адрес"}


def title(facts: dict) -> str:
    """Короткое имя объявления со ссылкой: ответ приходит отдельным сообщением."""
    name = " · ".join(x for x in (_address(facts),
                                  _money(facts["price"]) if facts.get("price") else None) if x)
    name = escape(name or "объявление")
    url = facts.get("url")
    return f'<a href="{escape(url, quote=True)}">{name}</a>' if url else name


def quote(raw_text: str, needles: list, width: int = 60) -> Optional[str]:
    """Кусок текста объявления вокруг первого найденного значения."""
    # В тексте с досок остаются HTML-сущности (&quot; в «מ"ר»): без раскодирования
    # они после экранирования покажутся человеку буквально.
    text = re.sub(r"\s+", " ", html.unescape(raw_text or ""))
    for needle in needles:
        if not needle:
            continue
        i = text.find(needle)
        if i < 0:
            continue
        a, b = max(0, i - width), min(len(text), i + len(needle) + width)
        return ("…" if a else "") + text[a:b].strip() + ("…" if b < len(text) else "")
    return None


def _needles(kind: str, d: dict) -> list:
    if kind == "price" and d.get("price"):
        v = int(d["price"])
        return [f"{v:,}", f"{v:,}".replace(",", " "), str(v)]
    if kind == "rooms" and d.get("rooms") is not None:
        r = d["rooms"]
        return [f"{r:.1f} חדר", f"{r:g} חדר", f"{r:g} комн", f"{r:g}-комн", f"{r:g} rooms"]
    if kind == "mamad":
        return [d.get("mamad_evidence"), 'ממ"ד', "ממ״ד", "ממד", "מרחב מוגן",
                "мамад", "Мамад", "mamad", "Mamad"]
    if kind == "address":
        return [d.get("street")]
    return []


def reparse_outcome(kind: str, before: dict, facts: dict, dropped: bool) -> str:
    old, new = shown(kind, before), shown(kind, facts)
    what = WHAT[kind]
    if old == new:
        if new is None:
            body = f"Перепроверил: {WHAT_ACC[kind]} в объявлении так и не нашёл."
        else:
            body = f"Перепроверил: {what} в объявлении — {escape(new)}."
            snippet = quote(facts.get("raw_text"), _needles(kind, facts))
            if snippet:
                body += f"\n<i>«{escape(snippet)}»</i>"
    elif new is None:
        body = (f"{what.capitalize()}: было {escape(old)}, при перепроверке в тексте "
                f"этого не нашёл — убрал из карточки.")
    elif old is None:
        body = f"{what.capitalize()}: было не указано, нашёл в тексте — {escape(new)}."
    else:
        body = f"{what.capitalize()}: было {escape(old)}, стало {escape(new)} — исправил."
    if dropped:
        body += "\n\nПод твои критерии оно теперь не подходит — из ленты убрал."
    return body


# ── «снято» ──────────────────────────────────────────────────────────────────

async def handle_removed(store: Store, profile: Optional[dict], listing_id: str,
                         facts: dict) -> str:
    """Проверить страницу, если источник это позволяет (решение 0008).

    Подтвердилось — снимаем у всех. Не подтвердилось или проверить нельзя —
    убираем только у пожаловавшегося: его слова хватает для его ленты, но не
    для чужих."""
    from collectors import details as details_mod

    source = facts.get("source")
    name = SOURCE_NAMES.get(source, source or "источнике")
    if source in details_mod.REMOVAL_SOURCES and facts.get("url"):
        client = await details_mod.make_client()
        try:
            verdict = await details_mod.check_alive(client, facts["url"], source)
        finally:
            await client.aclose()
        if verdict == "removed":
            await store.mark_removed(listing_id)
            return "Да, объявление снято — убрал его из ленты у всех. Спасибо!"
        await hide_for(store, profile, listing_id)
        if verdict == "alive":
            return (f"Страница на {name} открывается, объявление пока на месте. "
                    f"Из твоей ленты убрал.")
        return "Проверить страницу сейчас не получилось. Из твоей ленты убрал."
    await hide_for(store, profile, listing_id)
    return f"Проверить, снято ли объявление на {name}, бот не умеет. Из твоей ленты убрал."


# ── «дубль» ──────────────────────────────────────────────────────────────────

async def similar(store: Store, facts: dict, listing_id: str, limit: int = 5) -> list:
    """Кандидаты в оригинал: тот же город и комнаты, цена рядом."""
    if not facts.get("city") or facts.get("rooms") is None:
        return []
    params = [listing_id, facts["city"], facts["rooms"]]
    price_clause = ""
    if facts.get("price"):
        price_clause = " AND f.price BETWEEN ? AND ?"
        params += [facts["price"] - 300, facts["price"] + 300]
    cur = await store._db.execute(
        "SELECT l.id, l.source, l.url, f.price, f.street FROM listings l"
        " JOIN listing_facts f ON f.listing_id = l.id"
        " WHERE l.id <> ? AND f.city = ? AND f.rooms = ?" + price_clause +
        " ORDER BY (f.street IS NOT NULL AND f.street = ?) DESC,"
        "          ABS(COALESCE(f.price, 0) - ?) LIMIT ?",
        (*params, facts.get("street"), facts.get("price") or 0, limit))
    return [dict(r) for r in await cur.fetchall()]


def similar_text(items: list) -> str:
    if not items:
        return "Похожих в базе не нашёл."
    lines = []
    for it in items:
        price = _money(it["price"]) if it.get("price") else "цена ?"
        where = escape(it.get("street") or "улица ?")
        link = (f'<a href="{escape(it["url"], quote=True)}">{escape(it["source"])}</a>'
                if it.get("url") else escape(it["source"]))
        lines.append(f"• <code>{it['id']}</code> {link}, {where}, {price}")
    return "Похожие:\n" + "\n".join(lines)


# ── «агент / комиссия» ───────────────────────────────────────────────────────

_AGENT_NEEDLES = ["דמי תיווך", "תיווך", "מתווך", "סוכנות", "комисси", "маклер",
                  "агент", "посредник", "broker", "agent", "commission"]


async def apply_agent(store: Store, listing_id: str, facts: dict) -> tuple:
    """Отметить посредника. Возвращает (текст ответа, нашлось ли это в тексте).

    Решено верить человеку, даже если текст молчит: про посредника часто
    узнаёшь только по телефону. Отметка общая — кто ищет без комиссии, это
    объявление больше не получит."""
    from collectors.details import commission as fee_in
    from core.pipeline import facts_to_row
    from extract import extract

    text = facts.get("raw_text") or ""
    rules = facts_to_row(extract(text))
    fee = fee_in(text)
    seen = (rules.get("contact_type") == "agent" or rules.get("no_broker") == "no"
            or fee not in (None, "none"))
    if fee not in (None, "none"):
        commission = fee
    elif facts.get("commission") not in (None, "none"):
        commission = facts["commission"]
    else:
        commission = "есть, со слов пользователя"
    await store.save_facts(listing_id, {"contact_type": "agent", "no_broker": "no",
                                        "commission": commission})
    if seen:
        body = "Перепроверил: посредник в тексте есть — отметил."
        snippet = quote(text, _AGENT_NEEDLES)
        if snippet:
            body += f"\n<i>«{escape(snippet)}»</i>"
    else:
        body = "В тексте про посредника ничего нет, но отметил с твоих слов."
    return body + "\nКто ищет без комиссии, больше его не получит.", seen


# ── «не подходит мне» ────────────────────────────────────────────────────────

def notfit_options(profile: dict) -> list:
    """Кнопки — только те критерии, которые человек сам задал."""
    opts = []
    if profile.get("cities"):
        opts.append(("city", "Город"))
    if profile.get("price_max") is not None:
        opts.append(("price", "Цена"))
    if profile.get("rooms_min") is not None or profile.get("rooms_max") is not None:
        opts.append(("rooms", "Комнаты"))
    if profile.get("area_min") is not None:
        opts.append(("area", "Площадь"))
    if profile.get("floor_min") is not None or profile.get("floor_max") is not None:
        opts.append(("floor", "Этаж"))
    for key in FEATURE_REQUIREMENTS:
        if profile.get(key, IGNORE) != IGNORE:
            opts.append((key, FEATURE_LABELS.get(key, key)))
    if profile.get("req_no_commission", IGNORE) != IGNORE:
        opts.append(("req_no_commission", "Комиссия"))
    if profile.get("exclude_shared", 1):
        opts.append(("shared", "Соседи"))
    if profile.get("exclude_sublet", 1):
        opts.append(("sublet", "Саблет"))
    if profile.get("stop_words"):
        opts.append(("stop", "Стоп-слова"))
    return opts


def _mode_text(mode: str) -> str:
    return {REQUIRED: "«только если есть»",
            ALLOW_UNKNOWN: "«подходит и без данных»"}.get(mode, "«не важно»")


def _range(lo, hi, unit: str = "") -> str:
    if lo is not None and hi is not None:
        return f"от {lo:g} до {hi:g}{unit}"
    if lo is not None:
        return f"от {lo:g}{unit}"
    return f"до {hi:g}{unit}"


def explain_notfit(code: str, facts: dict, profile: dict) -> str:
    """Почему объявление прошло этот критерий."""
    result = match(facts, profile)
    if not result.matched:
        # Подобрано под прежние настройки или факты уже поправили
        return (f"Сейчас оно под твои критерии уже не проходит ({escape(result.rejected_by)}) — "
                f"видимо, подобрано до того, как что-то поменялось.")

    if code == "city":
        city = facts.get("city")
        if city is None:
            return ("Город в объявлении не опознан, а у тебя включено «подходит и без "
                    "города». Строже — в /settings.")
        return f"Город в объявлении — {escape(city)}, он есть в твоём списке."
    if code == "price":
        if not facts.get("price"):
            return "Цена в объявлении не указана — такие не отсекаются, решаешь сам."
        return (f"Цена в объявлении — {_money(facts['price'])} при твоём потолке "
                f"{_money(profile['price_max'])}.")
    if code == "rooms":
        want = _range(profile.get("rooms_min"), profile.get("rooms_max"))
        if facts.get("rooms") is None:
            return f"Число комнат не указано — такие не отсекаются. У тебя {want}."
        return f"Комнат в объявлении — {facts['rooms']:g}, у тебя {want}."
    if code == "area":
        if facts.get("area_sqm") is None:
            return "Площадь не указана — такие не отсекаются."
        return f"Площадь в объявлении — {facts['area_sqm']} м², у тебя от {profile['area_min']} м²."
    if code == "floor":
        want = _range(profile.get("floor_min"), profile.get("floor_max"))
        if facts.get("floor") is None:
            return f"Этаж не указан — такие не отсекаются. У тебя {want}."
        return f"Этаж в объявлении — {facts['floor']}, у тебя {want}."
    if code in FEATURE_REQUIREMENTS:
        label = FEATURE_LABELS.get(code, code)
        mode = profile.get(code, IGNORE)
        field = FEATURE_REQUIREMENTS[code]
        value = facts.get(field)
        if field == "mamad" and value is None and facts.get("mamad_evidence"):
            return (f"{label}: упомянуто защищённое помещение, но неясно, в квартире ли. "
                    f"У тебя {_mode_text(mode)} — такое проходит. Строже — в /settings.")
        if value is None:
            return (f"{label}: в объявлении нет данных, а у тебя {_mode_text(mode)}. "
                    f"Строже — в /settings.")
        shown_value = {"yes": "есть", "no": "нет", "full": "полностью",
                       "partial": "частично", "none": "нет"}.get(value, value)
        return f"{label}: в объявлении — {escape(str(shown_value))}."
    if code == "req_no_commission":
        fee = facts.get("commission")
        if fee is None:
            return (f"Про комиссию в объявлении ничего нет, а у тебя "
                    f"{_mode_text(profile.get('req_no_commission'))}. Строже — в /settings.")
        return "В объявлении сказано, что без комиссии." if fee == "none" \
            else f"Комиссия в объявлении: {escape(fee)}."
    if code == "shared":
        return ("В объявлении не сказано, что это комната с соседями, — бот принял его "
                "за обычную аренду.")
    if code == "sublet":
        return "В объявлении не сказано, что это саблет, — бот принял его за обычную аренду."
    if code == "stop":
        words = ", ".join(profile.get("stop_words") or [])
        return f"Ни одно из твоих стоп-слов ({escape(words)}) в тексте не встретилось."
    return "Объявление прошло все твои критерии."


NOTFIT_LABELS = {
    "city": "Город", "price": "Цена", "rooms": "Комнаты", "area": "Площадь",
    "floor": "Этаж", **FEATURE_LABELS, "req_no_commission": "Комиссия",
    "shared": "Соседи", "sublet": "Саблет", "stop": "Стоп-слова",
}


def notfit_label(code: str) -> str:
    return NOTFIT_LABELS.get(code, code)


# ── ответ после повторного разбора ───────────────────────────────────────────

async def process_waiting(bot, store: Store) -> dict:
    """Ответить на жалобы на поля, по которым прошёл повторный разбор.

    Разбор закончен, когда у объявления снова стоит отметка модели: жалоба её
    снимает (pipeline.mark_disputed), проход дозаполнения ставит — и когда
    спросил модель, и когда решил, что спрашивать незачем."""
    from aiogram.types import ReplyParameters

    from core.pipeline import recheck_listing

    await expire_awaiting(store)
    cur = await store._db.execute(
        "SELECT *, created_at < datetime('now', ?) AS overdue FROM feedback"
        " WHERE status = 'waiting' ORDER BY id", (f"-{WAIT_LIMIT_MIN} minutes",))
    rows = [dict(r) for r in await cur.fetchall()]
    answered = 0
    for fb in rows:
        facts = await store.get_facts(fb["listing_id"]) if fb["listing_id"] else None
        if facts and facts.get("llm_at") is None and not fb["overdue"]:
            continue                                  # разбор ещё не прошёл

        if not facts:
            body = "Объявление уже не в базе — поправлять нечего."
        elif facts.get("llm_at") is None:
            body = ("Перепроверить автоматически не вышло. Жалобу записал — "
                    "разберусь вручную.")
        else:
            changes = await recheck_listing(store, fb["listing_id"])
            profiles = await store.profiles_of(fb["user_id"])
            dropped = bool(profiles) and changes.get(profiles[0]["id"]) in ("dropped", "stale")
            before = json.loads(fb["before"] or "{}")
            body = reparse_outcome(fb["kind"], before, facts, dropped)

        text = f"По твоей жалобе на {title(facts) if facts else 'объявление'}:\n\n{body}"
        try:
            await bot.send_message(
                fb["chat_id"] or fb["user_id"], text, parse_mode="HTML",
                disable_notification=True, disable_web_page_preview=True,
                reply_parameters=(ReplyParameters(message_id=fb["card_msg_id"],
                                                  allow_sending_without_reply=True)
                                  if fb["card_msg_id"] else None))
            answered += 1
        except Exception as e:                        # noqa: BLE001
            # Человек мог заблокировать бота. Закрываем всё равно, иначе
            # жалоба будет пытаться уйти каждые пять минут.
            log.warning("ответ на жалобу %s не ушёл: %s", fb["id"], e)
        await close(store, fb["id"], text)
    return {"answered": answered}
