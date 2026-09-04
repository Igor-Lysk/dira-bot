"""Telegram-бот v2 на aiogram: онбординг, настройки, лента, статусы.

Отличия от v1, каждое — следствие разбора:

* адресат берётся из базы, а не из `CHAT_ID` в окружении: бот многопользовательский;
* пауза, тихие часы и режим доставки живут в профиле, а не в глобальной
  переменной модуля, поэтому переживают рестарт;
* кнопок 👍/👎 нет, вместо них статусы просмотра и «данные неверны»;
* состояние визарда хранится в базе — рестарт не стирает наполовину
  заполненный профиль.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message, TelegramObject)

from bot import cards, wizard
from core import market as market_mod
from core.store import Store

log = logging.getLogger(__name__)
router = Router()

SORTS = [("rank", "по релевантности"), ("price", "по цене"), ("fresh", "по свежести"),
         ("rooms", "по комнатам"), ("sqm_price", "по цене за м²")]
FILTERS = [("all", "все"), ("mamad", "с мамадом"), ("cheap", "дешевле рынка")]


class AuthMiddleware(BaseMiddleware):
    """Открытая регистрация: кто написал — тот и завёлся.

    Закрывать доступ можно флагом `is_active`, а не белым списком в конфиге, —
    иначе каждый новый пользователь требует передеплоя. Паттерн из Botkin.
    """

    def __init__(self, store: Store):
        self.store = store

    async def __call__(self, handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
                       event: TelegramObject, data: Dict[str, Any]) -> Any:
        tg_user = data.get("event_from_user")
        if tg_user is None:
            return await handler(event, data)
        user = await self.store.ensure_user(
            tg_user.id, tg_user.username or "", tg_user.first_name or "")
        if not user.get("is_active", 1):
            if isinstance(event, Message):
                await event.answer("Доступ к боту закрыт. Напиши администратору, если это ошибка.")
            return
        data["user"] = user
        data["store"] = self.store
        return await handler(event, data)


# ── клавиатуры ───────────────────────────────────────────────────────────────

def _wizard_kb(q: dict) -> InlineKeyboardMarkup:
    rows, row = [], []
    selected = set(q.get("selected") or [])
    for code, label in q["options"]:
        text = ("✅ " if code in selected else "") + label
        row.append(InlineKeyboardButton(text=text, callback_data=f"w:{q['key']}:{code}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    if q["kind"] == "multi":
        rows.append([InlineKeyboardButton(text="Готово", callback_data=f"w:{q['key']}:done")])
    if q["optional"] and q["kind"] != "multi":
        rows.append([InlineKeyboardButton(text="Пропустить", callback_data=f"w:{q['key']}:skip")])
    if q.get("back"):
        rows.append([InlineKeyboardButton(text="← Назад", callback_data=f"w:{q['key']}:__back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _card_kb(listing_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔖 В избранное", callback_data=f"s:saved:{listing_id}"),
         InlineKeyboardButton(text="✍️ Написал", callback_data=f"s:contacted:{listing_id}")],
        [InlineKeyboardButton(text="🚪 Еду смотреть", callback_data=f"s:visit:{listing_id}"),
         InlineKeyboardButton(text="✖️ Не подошло", callback_data=f"s:rejected:{listing_id}")],
        [InlineKeyboardButton(text="ℹ️ Подробнее", callback_data=f"d:{listing_id}"),
         InlineKeyboardButton(text="⚠️ Данные неверны", callback_data=f"e:{listing_id}")],
    ])


def _feed_kb(order: str, flt: str, offset: int, has_more: bool) -> InlineKeyboardMarkup:
    """Сортировки, фильтры и «показать ещё». Текущий выбор помечен точкой."""
    rows, row = [], []
    for code, label in SORTS:
        row.append(InlineKeyboardButton(text=("• " if code == order else "") + label,
                                        callback_data=f"f:{code}:{flt}:0"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text=("• " if code == flt else "") + label,
                                      callback_data=f"f:{order}:{code}:0")
                 for code, label in FILTERS])
    if has_more:
        rows.append([InlineKeyboardButton(text="Показать ещё",
                                          callback_data=f"f:{order}:{flt}:{offset + 5}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)



# Поля, которые можно править по одному из /settings. Ключ — шаг визарда.
EDITABLE = [
    ("cities", "Города"), ("price_max", "Потолок цены"), ("price_ideal", "Желаемая цена"),
    ("rooms_min", "Комнаты"), ("req_mamad", "Мамад"), ("req_elevator", "Лифт"),
    ("req_pets", "Животные"), ("req_no_commission", "Комиссия"), ("delivery_mode", "Как присылать"),
    ("digest_hour", "Время дайджеста"), ("quiet", "Тихие часы"),
    ("max_per_day", "Предел за сутки"), ("stop_words", "Стоп-слова"),
]

# Шаг визарда → поля профиля, которые он задаёт. По умолчанию имя совпадает,
# но «тихие часы» это два поля, а смена режима доставки тянет за собой время
# дайджеста и ограничения мгновенной отправки.
PROFILE_FIELDS = {
    "quiet": {"quiet_from", "quiet_to"},
    "delivery_mode": {"delivery_mode", "digest_hour", "quiet_from", "quiet_to", "max_per_day"},
}


def _profile_data(p: dict, **extra) -> dict:
    """Профиль из базы в том виде, в каком его понимает визард."""
    quiet = (f"{p['quiet_from']}-{p['quiet_to']}"
             if p.get("quiet_from") is not None and p.get("quiet_to") is not None else "none")
    data = {
        "cities": p["cities"], "price_max": p["price_max"], "price_ideal": p["price_ideal"],
        "rooms_min": p["rooms_min"], "req_mamad": p["req_mamad"],
        "req_elevator": p["req_elevator"], "req_pets": p["req_pets"],
        "req_no_commission": p["req_no_commission"],
        "delivery_mode": p["delivery_mode"], "digest_hour": p["digest_hour"],
        "quiet": None if quiet == "none" else quiet,
        "max_per_day": p["max_per_day"], "stop_words": p["stop_words"],
    }
    data.update(extra)
    return data


async def _save_single_field(target, user: dict, store: Store):
    """Записать одно поле профиля и вернуться к настройкам.

    Раньше поправить потолок цены значило пройти все десять шагов заново —
    достаточно, чтобы человек этого не делал вовсе.
    """
    data = dict(user.get("onboarding_data") or {})
    key = data.pop("_edit_only", None)
    profiles = await store.profiles_of(user["telegram_id"])
    if profiles and key:
        fields = wizard.to_profile(data)
        # пишем только то поле, которое правили, плюс связанное с ним
        keep = PROFILE_FIELDS.get(key, {key})
        await store.update_profile(profiles[0]["id"],
                                   **{k: v for k, v in fields.items() if k in keep})
        from core.pipeline import rematch_profile
        await rematch_profile(store, profiles[0]["id"])
    await store.set_user(user["telegram_id"], onboarding_step="done", onboarding_data=data)
    sender = target.answer if isinstance(target, Message) else target.message.answer
    await sender("Сохранено. Ещё что-нибудь поправить — /settings")


def _settings_kb(data: dict) -> InlineKeyboardMarkup:
    rows, row = [], []
    allowed = set(wizard.visible_keys(data))
    for key, label in EDITABLE:
        if key not in allowed:
            continue
        row.append(InlineKeyboardButton(text=label, callback_data=f"edit:{key}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── онбординг ────────────────────────────────────────────────────────────────

async def _ask(target, user: dict, store: Store):
    """Показать текущий шаг визарда."""
    data = user.get("onboarding_data") or {}
    key = user.get("onboarding_step") or wizard.first_step()
    q = wizard.question(key, data)
    # в режиме правки одного поля ни прогресса, ни «назад» не нужно
    editing = bool(data.get("_edit_only"))
    q["back"] = not editing and wizard.step_before(key, data) is not None
    head = q["title"] if editing else f"[{q['progress']}] {q['title']}"
    text = f"<b>{head}</b>\n\n{q['text']}"
    if q["hint"]:
        text += f"\n\n<i>{q['hint']}</i>"
    sender = target.answer if isinstance(target, Message) else target.message.edit_text
    await sender(text, parse_mode=ParseMode.HTML, reply_markup=_wizard_kb(q))


async def _finish(target, user: dict, store: Store):
    data = user.get("onboarding_data") or {}
    fields = wizard.to_profile(data)
    profiles = await store.profiles_of(user["telegram_id"])
    if profiles:
        profile_id = profiles[0]["id"]
        await store.update_profile(profile_id, **fields)
    else:
        # Граница накопленного: всё, что уже лежит в базе, уходит в /feed, а
        # мгновенная доставка начинается с этой секунды. Иначе первое, что
        # человек получает после настройки, — двести объявлений разом.
        fields["backlog_before"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        profile_id = await store.create_profile(user["telegram_id"], "Основной", **fields)
    await store.set_user(user["telegram_id"], onboarding_step="done")

    # Сопоставление идёт в момент обработки объявления, поэтому всё собранное до
    # настройки профиля иначе осталось бы невидимым: первый /feed был бы пустым
    # при полной базе.
    from core.pipeline import rematch_profile
    found = await rematch_profile(store, profile_id)
    text = ("<b>Готово, профиль сохранён</b>\n\n" + wizard.summary(data) +
            (f"\n\nПо этим критериям уже нашлось: {found}." if found else
             "\n\nПодходящего пока нет — пришлю, как появится.") +
            "\n\nМеняется в /settings. Что уже нашлось — /feed.")
    sender = target.answer if isinstance(target, Message) else target.message.edit_text
    await sender(text, parse_mode=ParseMode.HTML)


@router.message(Command("start"))
async def cmd_start(message: Message, user: dict, store: Store):
    if user.get("onboarding_step") == "done":
        await message.answer(
            "С возвращением. /feed — что нашлось, /settings — критерии, /stats — статистика.")
        return
    await store.set_user(user["telegram_id"],
                         onboarding_step=user.get("onboarding_step") or wizard.first_step())
    user = await store.get_user(user["telegram_id"])
    await message.answer(
        "Ищу квартиры в аренду и присылаю подходящие.\n\n"
        "Сейчас настроим, что именно искать — десять коротких вопросов. "
        "Ответы сохраняются на каждом шаге, так что можно прерваться и вернуться.",
        parse_mode=ParseMode.HTML)
    await _ask(message, user, store)


@router.message(Command("setup"))
async def cmd_setup(message: Message, user: dict, store: Store):
    """Пройти визард заново."""
    await store.set_user(message.from_user.id, onboarding_step=wizard.first_step(),
                         onboarding_data="{}")
    user = await store.get_user(message.from_user.id)
    await _ask(message, user, store)


@router.callback_query(F.data.startswith("w:"))
async def on_wizard(callback: CallbackQuery, user: dict, store: Store):
    _, key, answer = callback.data.split(":", 2)
    data = user.get("onboarding_data") or {}

    if answer == "__back":
        previous = wizard.step_before(key, data)
        if previous:
            await store.set_user(user["telegram_id"], onboarding_step=previous)
            user = await store.get_user(user["telegram_id"])
        await callback.answer()
        await _ask(callback, user, store)
        return

    accepted, error = wizard.apply(key, data, answer)
    await store.set_user(user["telegram_id"], onboarding_data=data)
    if error:
        await callback.answer(error, show_alert=True)
        return
    await callback.answer()
    if accepted:
        key = wizard.step_after(key, data)
        await store.set_user(user["telegram_id"], onboarding_step=key)
    user = await store.get_user(user["telegram_id"])
    if (user.get("onboarding_data") or {}).get("_edit_only") and accepted:
        await _save_single_field(callback, user, store)
    elif key == wizard.DONE:
        await _finish(callback, user, store)
    else:
        await _ask(callback, user, store)


@router.message(F.text, ~F.text.startswith("/"))
async def on_text(message: Message, user: dict, store: Store):
    """Свободный текст принимается только там, где визард его ждёт — число или список."""
    key = user.get("onboarding_step")
    if not key or key == "done":
        await message.answer("Не понял. /feed — лента, /settings — критерии.")
        return
    data = user.get("onboarding_data") or {}
    accepted, error = wizard.apply(key, data, message.text)
    await store.set_user(user["telegram_id"], onboarding_data=data)
    if error:
        await message.answer(error)
        return
    if accepted:
        key = wizard.step_after(key, data)
        await store.set_user(user["telegram_id"], onboarding_step=key)
    user = await store.get_user(user["telegram_id"])
    if (user.get("onboarding_data") or {}).get("_edit_only") and accepted:
        await _save_single_field(message, user, store)
    elif key == wizard.DONE:
        await _finish(message, user, store)
    else:
        await _ask(message, user, store)


# ── лента ────────────────────────────────────────────────────────────────────

async def _send_feed(target, store: Store, profile: dict, order: str, offset: int,
                     flt: str = "all"):
    rows = await store.feed(profile["id"], order=order, limit=5, offset=offset, flt=flt)
    more = len(await store.feed(profile["id"], order=order, limit=1,
                                offset=offset + 5, flt=flt)) > 0
    if not rows:
        text = ("Пока пусто. Бот собирает объявления и пришлёт, как только "
                "появится подходящее." if offset == 0 else "Дальше ничего нет.")
        await (target.answer(text) if isinstance(target, Message)
               else target.message.answer(text))
        return
    label = dict(SORTS).get(order, order)
    flt_label = dict(FILTERS).get(flt, "")
    header = f"<b>Найдено — {label}</b>" + (f" · {flt_label}" if flt != "all" else "")
    send = target.answer if isinstance(target, Message) else target.message.answer
    await send(header, parse_mode=ParseMode.HTML)
    # оценку «дешевле рынка» показываем и в ленте, а не только в дайджесте:
    # это первое, на что смотрят, решая, открывать ли объявление
    own = await market_mod.medians(store)
    for facts in rows:
        await send(cards.card(facts, own_medians=own, show_age=True), parse_mode=ParseMode.HTML,
                   disable_web_page_preview=True, reply_markup=_card_kb(facts["listing_id"]))
    await send("Сортировка и фильтры:", reply_markup=_feed_kb(order, flt, offset, more))


@router.message(Command("feed"))
async def cmd_feed(message: Message, user: dict, store: Store):
    profiles = await store.profiles_of(user["telegram_id"])
    if not profiles:
        await message.answer("Сначала настроим критерии: /start")
        return
    p = profiles[0]
    # порядок и фильтр берём те, что человек выбрал в прошлый раз
    await _send_feed(message, store, p, p.get("feed_order") or "rank", 0,
                     p.get("feed_filter") or "all")


@router.callback_query(F.data.startswith("f:"))
async def on_feed_page(callback: CallbackQuery, user: dict, store: Store):
    _, order, flt, offset = callback.data.split(":", 3)
    profiles = await store.profiles_of(user["telegram_id"])
    if not profiles:
        await callback.answer("Сначала /start"); return
    await store.update_profile(profiles[0]["id"], feed_order=order, feed_filter=flt)
    await callback.answer()
    await _send_feed(callback, store, profiles[0], order, int(offset), flt)


# ── действия на карточке ─────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("s:"))
async def on_state(callback: CallbackQuery, user: dict, store: Store):
    _, state, listing_id = callback.data.split(":", 2)
    profiles = await store.profiles_of(user["telegram_id"])
    if profiles:
        await store.set_match_state(profiles[0]["id"], listing_id, state)
    await store.log_action(user["telegram_id"], "state_change", listing_id, {"state": state})
    await callback.answer(dict(cards.STATES).get(state, "Отмечено"))


@router.callback_query(F.data.startswith("d:"))
async def on_details(callback: CallbackQuery, user: dict, store: Store):
    facts = await store.get_facts(callback.data.split(":", 1)[1])
    await callback.answer()
    await callback.message.answer(cards.details(facts or {}))


@router.callback_query(F.data.startswith("e:"))
async def on_wrong_data(callback: CallbackQuery, user: dict, store: Store):
    """«Данные неверны» — помечаем факты на переизвлечение.

    Самый ценный вид обратной связи: он чинит парсер и не требует никакого
    обучения. Попадает в регрессионный набор."""
    listing_id = callback.data.split(":", 1)[1]
    await store.set_status(listing_id, "pending", "помечено пользователем как неверное")
    await store.log_action(user["telegram_id"], "wrong_data", listing_id)
    await callback.answer("Спасибо, перепроверим разбор этого объявления", show_alert=True)


# ── настройки и статистика ───────────────────────────────────────────────────

@router.message(Command("settings"))
async def cmd_settings(message: Message, user: dict, store: Store):
    profiles = await store.profiles_of(user["telegram_id"])
    if not profiles:
        await message.answer("Профиля ещё нет: /start")
        return
    p = profiles[0]
    data = _profile_data(p)
    state = "на паузе" if p["is_paused"] else "работает"
    await message.answer(
        f"<b>Критерии поиска</b> ({state})\n\n{wizard.summary(data)}\n\n"
        f"Что поправить? Всё сразу — /setup. Пауза — /pause, снять — /resume.",
        parse_mode=ParseMode.HTML, reply_markup=_settings_kb(data))


@router.callback_query(F.data.startswith("edit:"))
async def on_edit_field(callback: CallbackQuery, user: dict, store: Store):
    """Правка одного поля: подставляем текущие значения и открываем один шаг."""
    key = callback.data.split(":", 1)[1]
    profiles = await store.profiles_of(user["telegram_id"])
    if not profiles:
        await callback.answer("Сначала /start"); return
    p = profiles[0]
    data = _profile_data(p, _edit_only=key)
    await store.set_user(user["telegram_id"], onboarding_step=key, onboarding_data=data)
    user = await store.get_user(user["telegram_id"])
    await callback.answer()
    await _ask(callback, user, store)


@router.message(Command("pause"))
async def cmd_pause(message: Message, user: dict, store: Store):
    for p in await store.profiles_of(user["telegram_id"]):
        await store.update_profile(p["id"], is_paused=1)
    await message.answer("Пауза. Ничего присылать не буду, /resume — вернуть.")


@router.message(Command("resume"))
async def cmd_resume(message: Message, user: dict, store: Store):
    for p in await store.profiles_of(user["telegram_id"]):
        await store.update_profile(p["id"], is_paused=0)
    await message.answer("Продолжаю искать.")


@router.message(Command("stats"))
async def cmd_stats(message: Message, user: dict, store: Store):
    s = await store.stats()
    spend = await store.spend(30)
    await message.answer(
        f"<b>Статистика</b>\n\n"
        f"Объявлений собрано: {s['listings']}\n"
        f"Разобрано: {s['extracted']}, в очереди: {s['pending']}\n"
        f"Совпадений: {s['matches']}\n"
        f"Пользователей: {s['users']}, профилей: {s['profiles']}\n\n"
        f"Расход модели за 30 дней: ${spend.get('usd') or 0} "
        f"({spend.get('calls') or 0} вызовов)",
        parse_mode=ParseMode.HTML)


def build(token: str, store: Store):
    bot = Bot(token=token)
    dp = Dispatcher()
    middleware = AuthMiddleware(store)
    dp.message.middleware(middleware)
    dp.callback_query.middleware(middleware)
    dp.include_router(router)
    return bot, dp
