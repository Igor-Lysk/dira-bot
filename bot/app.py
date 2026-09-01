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
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message, TelegramObject)

from bot import cards, wizard
from core.store import Store

log = logging.getLogger(__name__)
router = Router()

SORTS = [("rank", "по релевантности"), ("price", "по цене"), ("fresh", "по свежести"),
         ("rooms", "по комнатам"), ("sqm_price", "по цене за м²")]


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


def _feed_kb(profile_id: int, order: str, offset: int, has_more: bool) -> InlineKeyboardMarkup:
    rows, row = [], []
    for code, label in SORTS:
        mark = "• " if code == order else ""
        row.append(InlineKeyboardButton(text=mark + label, callback_data=f"f:{code}:0"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    if has_more:
        rows.append([InlineKeyboardButton(text="Показать ещё",
                                          callback_data=f"f:{order}:{offset + 5}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── онбординг ────────────────────────────────────────────────────────────────

async def _ask(target, user: dict, store: Store):
    """Показать текущий шаг визарда."""
    data = user.get("onboarding_data") or {}
    key = user.get("onboarding_step") or wizard.first_step()
    q = wizard.question(key, data)
    text = f"<b>[{q['progress']}] {q['title']}</b>\n\n{q['text']}"
    if q["hint"]:
        text += f"\n\n<i>{q['hint']}</i>"
    sender = target.answer if isinstance(target, Message) else target.message.edit_text
    await sender(text, parse_mode=ParseMode.HTML, reply_markup=_wizard_kb(q))


async def _finish(target, user: dict, store: Store):
    data = user.get("onboarding_data") or {}
    fields = wizard.to_profile(data)
    profiles = await store.profiles_of(user["telegram_id"])
    if profiles:
        await store.update_profile(profiles[0]["id"], **fields)
    else:
        await store.create_profile(user["telegram_id"], "Основной", **fields)
    await store.set_user(user["telegram_id"], onboarding_step="done")
    text = ("<b>Готово, профиль сохранён</b>\n\n" + wizard.summary(data) +
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
    if key == wizard.DONE:
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
    if key == wizard.DONE:
        await _finish(message, user, store)
    else:
        await _ask(message, user, store)


# ── лента ────────────────────────────────────────────────────────────────────

async def _send_feed(target, store: Store, profile: dict, order: str, offset: int):
    rows = await store.feed(profile["id"], order=order, limit=5, offset=offset)
    more = len(await store.feed(profile["id"], order=order, limit=1, offset=offset + 5)) > 0
    if not rows:
        text = ("Пока пусто. Бот собирает объявления и пришлёт, как только "
                "появится подходящее." if offset == 0 else "Дальше ничего нет.")
        await (target.answer(text) if isinstance(target, Message)
               else target.message.answer(text))
        return
    label = dict(SORTS).get(order, order)
    header = f"<b>Найдено — {label}</b>"
    send = target.answer if isinstance(target, Message) else target.message.answer
    await send(header, parse_mode=ParseMode.HTML)
    for facts in rows:
        await send(cards.card(facts), parse_mode=ParseMode.HTML,
                   disable_web_page_preview=True, reply_markup=_card_kb(facts["listing_id"]))
    await send("Сортировка:", reply_markup=_feed_kb(profile["id"], order, offset, more))


@router.message(Command("feed"))
async def cmd_feed(message: Message, user: dict, store: Store):
    profiles = await store.profiles_of(user["telegram_id"])
    if not profiles:
        await message.answer("Сначала настроим критерии: /start")
        return
    await _send_feed(message, store, profiles[0], "rank", 0)


@router.callback_query(F.data.startswith("f:"))
async def on_feed_page(callback: CallbackQuery, user: dict, store: Store):
    _, order, offset = callback.data.split(":", 2)
    profiles = await store.profiles_of(user["telegram_id"])
    if not profiles:
        await callback.answer("Сначала /start"); return
    await callback.answer()
    await _send_feed(callback, store, profiles[0], order, int(offset))


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
    data = {
        "cities": p["cities"], "price_max": p["price_max"], "price_ideal": p["price_ideal"],
        "rooms_min": p["rooms_min"], "req_mamad": p["req_mamad"],
        "req_elevator": p["req_elevator"], "req_pets": p["req_pets"],
        "delivery_mode": p["delivery_mode"], "digest_hour": p["digest_hour"],
        "stop_words": p["stop_words"],
    }
    state = "на паузе" if p["is_paused"] else "работает"
    await message.answer(
        f"<b>Критерии поиска</b> ({state})\n\n{wizard.summary(data)}\n\n"
        f"Изменить всё — /setup. Пауза — /pause, снять — /resume.",
        parse_mode=ParseMode.HTML)


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
