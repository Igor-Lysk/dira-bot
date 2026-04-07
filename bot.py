"""Dira Bot — Telegram bot (aiogram 3).

Sends listing alerts with inline feedback buttons.
Handles user commands: /top, /stats, /preferences, /pause, /resume.
"""

import asyncio
import logging
from typing import Optional

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from aiogram.filters import Command
from aiogram.enums import ParseMode

import config
from database import Database

log = logging.getLogger(__name__)
router = Router()

# Module-level state (set by init())
_bot: Optional[Bot] = None
_dp: Optional[Dispatcher] = None
_db: Optional[Database] = None
_paused: bool = False


# ── Init ──────────────────────────────────────────────────────────────────────

def init(db: Database) -> tuple[Bot, Dispatcher]:
    """Create bot + dispatcher, register handlers."""
    global _bot, _dp, _db
    _db = db
    _bot = Bot(token=config.BOT_TOKEN)
    _dp = Dispatcher()
    _dp.include_router(router)
    return _bot, _dp


def get_bot() -> Bot:
    assert _bot is not None, "Call bot.init() first"
    return _bot


# ── Message formatting ────────────────────────────────────────────────────────

def format_alert(listing: dict, analysis: dict) -> str:
    """Format a listing alert message (Markdown)."""
    score = analysis.get("score", 0)
    rec = analysis.get("recommendation", "SKIP")

    if score >= 8:
        icon = "\U0001f7e2"   # green
    elif score >= 6:
        icon = "\U0001f7e1"   # yellow
    else:
        icon = "\U0001f534"   # red

    source = listing.get("source", "?")
    source_label = {"telegram": "Telegram", "yad2": "Yad2", "facebook": "Facebook"}.get(
        source, source
    )

    location = analysis.get("location", "")
    price = analysis.get("price_found") or listing.get("price", "?")
    rooms = analysis.get("rooms_found") or listing.get("rooms", "?")

    # Mamad
    mamad = analysis.get("has_mamad")
    if mamad is True:
        mamad_s = '\u2705 \u05de\u05de"\u05d3'
    elif mamad is False:
        mamad_s = '\u274c \u05e0\u05d5 \u05de\u05de"\u05d3'
    else:
        mamad_s = '\u2753 \u05de\u05de"\u05d3?'

    # Furnished
    furn = analysis.get("furnished", "unknown")
    furn_map = {
        "full": "\U0001f6cb Full",
        "partial": "\U0001fa91 Partial",
        "empty": "\U0001f4e6 Empty",
        "unknown": "\u2753",
    }
    furn_s = furn_map.get(furn, "\u2753")

    # Station
    station = analysis.get("near_station")
    if station is True:
        station_s = "\U0001f689 Near station"
    elif station is False:
        station_s = "\U0001f6b6 Far from station"
    else:
        station_s = "\u2753 Station?"

    parts = [
        f"{icon} *[{score}/10] {rec}* \u2014 {source_label}",
        "",
        f"\U0001f4cd {location}" if location else "",
    ]

    if isinstance(price, (int, float)) and price > 0:
        parts.append(f"\U0001f4b0 {int(price):,} \u20aa/\u043c\u0435\u0441")
    elif price and str(price) != "?":
        parts.append(f"\U0001f4b0 {price} \u20aa/\u043c\u0435\u0441")

    if rooms and str(rooms) != "?":
        parts.append(f"\U0001f6cf {rooms} \u043a\u043e\u043c\u043d.")

    parts += ["", mamad_s, furn_s, station_s]

    pros = analysis.get("pros", [])
    issues = analysis.get("issues", [])
    if pros:
        parts += ["", "\u2705 " + " \u2022 ".join(pros[:3])]
    if issues:
        parts += ["\u26a0\ufe0f " + " \u2022 ".join(issues[:2])]

    summary = analysis.get("summary", "")
    if summary:
        parts += ["", f"_{summary}_"]

    url = listing.get("url", "")
    if url:
        parts += ["", f"\U0001f517 [Open]({url})"]

    return "\n".join(p for p in parts if p is not None)


def _feedback_keyboard(listing_id: str) -> InlineKeyboardMarkup:
    """Create inline keyboard with feedback buttons."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="\U0001f44d", callback_data=f"fb:like:{listing_id}"),
        InlineKeyboardButton(text="\U0001f44e", callback_data=f"fb:dislike:{listing_id}"),
        InlineKeyboardButton(text="\U0001f4de", callback_data=f"fb:contact:{listing_id}"),
        InlineKeyboardButton(text="\U0001f5d1", callback_data=f"fb:irrelevant:{listing_id}"),
    ]])


# ── Send alerts ───────────────────────────────────────────────────────────────

async def send_alert(listing: dict, analysis: dict):
    """Send a listing alert to the user."""
    if _paused:
        log.info("Paused, skipping alert for %s", listing.get("id"))
        return

    text = format_alert(listing, analysis)
    listing_id = listing.get("id", "unknown")

    try:
        await get_bot().send_message(
            chat_id=config.CHAT_ID,
            text=text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_feedback_keyboard(listing_id),
            disable_web_page_preview=True,
        )
    except Exception as e:
        log.error("Failed to send alert: %s", e)
        # Retry without markdown
        try:
            plain = text.replace("*", "").replace("_", "")
            await get_bot().send_message(
                chat_id=config.CHAT_ID,
                text=plain,
                reply_markup=_feedback_keyboard(listing_id),
            )
        except Exception as e2:
            log.error("Failed to send plain alert: %s", e2)


async def send_digest(text: str):
    """Send a digest message."""
    try:
        await get_bot().send_message(
            chat_id=config.CHAT_ID,
            text=text,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
        )
    except Exception as e:
        log.error("Failed to send digest: %s", e)


async def send_text(text: str):
    """Send a simple text message."""
    try:
        await get_bot().send_message(chat_id=config.CHAT_ID, text=text)
    except Exception as e:
        log.error("Failed to send text: %s", e)


# ── Callback: feedback buttons ────────────────────────────────────────────────

@router.callback_query(F.data.startswith("fb:"))
async def on_feedback(callback: CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("?")
        return

    _, rating, listing_id = parts
    await _db.save_feedback(listing_id, rating)

    labels = {
        "like": "\U0001f44d Saved!",
        "dislike": "\U0001f44e Noted",
        "contact": "\U0001f4de Good luck!",
        "irrelevant": "\U0001f5d1 Skipped",
    }
    await callback.answer(labels.get(rating, "OK"))
    log.info("Feedback: %s → %s", listing_id, rating)


# ── Commands ──────────────────────────────────────────────────────────────────

@router.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "\U0001f3e0 *Dira Bot*\n\n"
        "I monitor apartment listings in Tel Aviv area and send you the best matches.\n\n"
        "Commands:\n"
        "/top \u2014 Top 5 listings\n"
        "/stats \u2014 Collection stats\n"
        "/preferences \u2014 Learned preferences\n"
        "/pause \u2014 Pause alerts\n"
        "/resume \u2014 Resume alerts",
        parse_mode=ParseMode.MARKDOWN,
    )


@router.message(Command("top"))
async def cmd_top(message: Message):
    top = await _db.get_top_listings(limit=5, days=14)
    if not top:
        await message.answer("\U0001f914 No good listings found yet.")
        return

    lines = ["\U0001f3c6 *Top 5 listings:*\n"]
    for i, r in enumerate(top, 1):
        score = r.get("score", "?")
        summary = r.get("summary", "No summary")
        url = r.get("url", "")
        price = r.get("price", "?")
        lines.append(
            f"{i}. [{score}/10] {summary}\n"
            f"   \U0001f4b0 {price} \u20aa | \U0001f517 [link]({url})\n"
        )

    await message.answer("\n".join(lines), parse_mode=ParseMode.MARKDOWN,
                         disable_web_page_preview=True)


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    stats = await _db.get_stats()
    by_src = stats.get("by_source", {})
    src_lines = "\n".join(f"  {k}: {v}" for k, v in by_src.items())
    text = (
        f"\U0001f4ca *Stats:*\n\n"
        f"Total listings: {stats['total_listings']}\n"
        f"By source:\n{src_lines}\n\n"
        f"Analyzed: {stats['analyzed']}\n"
        f"SEND: {stats['sent']} | MAYBE: {stats['maybe']}\n"
        f"Feedbacks: {stats['feedbacks']}"
    )
    await message.answer(text, parse_mode=ParseMode.MARKDOWN)


@router.message(Command("preferences"))
async def cmd_preferences(message: Message):
    prefs = await _db.get_preferences()
    if not prefs:
        await message.answer(
            "\U0001f914 No preferences learned yet. "
            "Rate some listings first (use \U0001f44d/\U0001f44e buttons)."
        )
        return

    lines = ["\U0001f9e0 *Learned preferences:*\n"]
    for p in prefs:
        conf = float(p.get("confidence", 0))
        lines.append(f"\u2022 {p['key']}: {p['value']} ({conf:.0%})")

    await message.answer("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


@router.message(Command("pause"))
async def cmd_pause(message: Message):
    global _paused
    _paused = True
    await message.answer("\u23f8 Alerts paused. /resume to continue.")


@router.message(Command("resume"))
async def cmd_resume(message: Message):
    global _paused
    _paused = False
    await message.answer("\u25b6\ufe0f Alerts resumed!")
