"""Dira Bot — main entry point.

Starts all components:
1. SQLite database
2. Telethon monitor (real-time Telegram channel monitoring)
3. aiogram bot (alerts + commands + feedback)
4. APScheduler (digest, preferences)

All run concurrently in a single asyncio event loop.
"""

import asyncio
import logging
import re
import sys

import config
from database import Database, make_id, make_fingerprint
from collectors.telegram_monitor import TelegramMonitor
from analyzer import analyze_listing
from bot import init as init_bot, send_alert, send_text
from scheduler import Scheduler

# ── Rooms pre-filter ─────────────────────────────────────────────────────────
#
# Claude occasionally ignores the "rooms < 2.5 → SKIP score 0" hard rule in
# the prompt, especially when all other criteria (mamad, price, city) look
# great. This deterministic regex filter runs BEFORE Claude so a listing with
# a clearly stated room count below 2.5 never reaches the LLM at all.
#
# It only fires when we can extract an explicit room count with confidence.
# Ambiguous listings (no room count mentioned) pass through to Claude as usual.

_ROOMS_HEBREW_RE = re.compile(
    r"""
    (?:
        (\d+(?:[.,]\d)?)\s*חד(?:רים|ר\b|')   # "2 חדרים" / "2 חד'"
    )
    |
    (?:
        חד(?:רים|ר\b|')                        # label before number:
        [^0-9]{0,8}                             # "חדרים:** 2" / "חדרים: 2"
        (\d+(?:[.,]\d)?)
    )
    """,
    re.VERBOSE | re.UNICODE,
)
_ROOMS_RUSSIAN_RE = re.compile(
    r'(\d+(?:[.,]\d)?)\s*[-–]?\s*комнат',  # "2 комнаты", "3-комнатная"
    re.IGNORECASE | re.UNICODE,
)
_ROOMS_ENGLISH_RE = re.compile(
    r'(\d+(?:[.,]\d)?)\s*(?:bed\s*rooms?|br\b)',
    re.IGNORECASE,
)


def _extract_rooms(text: str) -> float | None:
    """Return the minimum explicitly-stated room count, or None if uncertain.

    Taking the minimum is the conservative choice: if a listing says both
    '2 חדרים' and '3 חדרים' (unusual but possible in copy-paste posts),
    we'd rather investigate the lower figure.
    """
    rooms: list[float] = []
    for pattern in (_ROOMS_HEBREW_RE, _ROOMS_RUSSIAN_RE, _ROOMS_ENGLISH_RE):
        for m in pattern.finditer(text):
            for g in m.groups():
                if g is not None:
                    try:
                        rooms.append(float(g.replace(",", ".")))
                    except ValueError:
                        pass
    return min(rooms) if rooms else None

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("dira")

# Silence noisy libraries
logging.getLogger("telethon").setLevel(logging.WARNING)
logging.getLogger("aiogram").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
# Show DEBUG from telegram_monitor to diagnose real-time event reception
logging.getLogger("collectors.telegram_monitor").setLevel(logging.DEBUG)


# ── Sublet detector ──────────────────────────────────────────────────────────
#
# Sublet = аренда комнаты в квартире вместе с другими жильцами.
# Нельзя просто заблокировать слово "саблет" — встречаются формулировки
# "не саблет" / "no sublet" (продавец подчёркивает, что это НЕ саблет).
#
# Алгоритм: есть признак саблета И нет отрицания рядом.

_SUBLET_RE = re.compile(
    r"саблет|sublet|שותפ|roommate|flatmate|שכירות\s+משותפת"
    r"|подселени[ея]|сожитель|комнат[уы]\s+в\s+квартир",
    re.IGNORECASE,
)
_NOT_SUBLET_RE = re.compile(
    r"(?:не|без|no[tn]?|לא|without)\s{0,8}"
    r"(?:саблет|sublet|שותפ|roommate|сожител)",
    re.IGNORECASE,
)


def _is_sublet(text: str) -> bool:
    """True if listing is a sublet/room-share offer (not an apartment)."""
    if not _SUBLET_RE.search(text):
        return False
    # Explicit negation like "не саблет" / "not a sublet" → it's a real apartment
    if _NOT_SUBLET_RE.search(text):
        return False
    return True


# ── Processing pipeline ──────────────────────────────────────────────────────

async def process_new_listing(raw: dict, db: Database):
    """Pipeline: dedup → save → analyze → alert."""
    text = raw.get("raw_text", "")
    url = raw.get("url")
    source = raw.get("source", "telegram")
    source_id = raw.get("source_id", "")

    listing_id = make_id(url, text)

    # Pre-filter: sublets (shared apartment / room rental)
    if _is_sublet(text):
        log.debug("Sublet filtered: %s", listing_id)
        return

    # Cross-source dedup: same apt posted on Yad2 + Telegram + Facebook
    if await db.find_similar(
        price=raw.get("price"),
        rooms=raw.get("rooms"),
        city=raw.get("city"),
    ):
        log.debug("Cross-source duplicate (price/rooms/city match): %s", listing_id)
        return

    # Save to DB (dedup by ID + fingerprint)
    is_new = await db.add_listing(
        listing_id=listing_id,
        source=source,
        source_id=source_id,
        raw_text=text,
        url=url,
        price=raw.get("price"),
        rooms=raw.get("rooms"),
        address=raw.get("address"),
        city=raw.get("city"),
    )

    if not is_new:
        log.debug("Duplicate listing %s, skipping", listing_id)
        return

    # Pre-filter: reject listings with a clearly sub-minimum room count.
    # This runs BEFORE Claude so prompt-level rules can't be rationalized away.
    detected_rooms = _extract_rooms(text)
    if detected_rooms is not None and detected_rooms < config.MIN_ROOMS:
        log.info(
            "Rooms pre-filter: %.4g rooms < %.4g minimum — SKIP (listing %s)",
            detected_rooms, config.MIN_ROOMS, listing_id[:12],
        )
        await db.save_analysis(listing_id, {
            "score": 0,
            "recommendation": "SKIP",
            "summary": f"Авто-фильтр: {detected_rooms:g} комн. < {config.MIN_ROOMS:g} (минимум)",
            "pros": [],
            "issues": [f"Только {detected_rooms:g} комнат — ниже минимума {config.MIN_ROOMS:g}"],
            "rooms_found": detected_rooms,
            "has_mamad": None,
            "price_found": None,
            "location": "",
        })
        return

    log.info("New listing %s from %s — analyzing...", listing_id, source)

    # Get learned preferences for context
    prefs = await db.get_preferences()

    # Analyze with Claude
    analysis = await analyze_listing(text, source=source, preferences=prefs)

    score = analysis.get("score", 0)
    rec = analysis.get("recommendation", "SKIP")
    summary = analysis.get("summary", "")

    log.info("  → %s (score %d): %s", rec, score, summary[:80])

    # Save analysis
    await db.save_analysis(listing_id, analysis)

    # Update listing with extracted data
    # (price, rooms, etc. from Claude's analysis)
    # Note: we could update the listing row here, but for simplicity
    # we keep extracted data in the analyses table.

    # Send alert if passes threshold
    listing_data = {
        "id": listing_id,
        "source": source,
        "url": url,
        "price": analysis.get("price_found"),
        "rooms": analysis.get("rooms_found"),
    }

    if rec == "SEND" or score >= config.SEND_THRESHOLD:
        await send_alert(listing_data, analysis)
    elif rec == "MAYBE" or score >= config.MAYBE_THRESHOLD:
        await send_alert(listing_data, analysis)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    log.info("=" * 50)
    log.info("  Dira Bot starting...")
    log.info("=" * 50)

    # 1. Database
    db = Database(config.DB_PATH)
    await db.connect()
    log.info("Database ready: %s", config.DB_PATH)

    # 2. Telegram Bot (aiogram)
    bot, dp = init_bot(db)
    log.info("Bot initialized")

    # 4. Telegram Monitor (Telethon)
    monitor = TelegramMonitor()

    async def on_listing(raw: dict):
        if "source" not in raw:
            raw["source"] = "telegram"
        await process_new_listing(raw, db)

    await monitor.start(on_listing)

    # 3. Scheduler (needs on_listing for Yad2 polling). It also gets a
    # reference to the monitor so the healthchecks.io ping job can stop
    # pinging if Telethon goes silent — that's our watchdog for the silent
    # disconnect bug observed Apr 27.
    scheduler = Scheduler(db, on_listing=on_listing, telegram_monitor=monitor)
    scheduler.start()

    # Backfill: load recent history from all sources in background
    async def run_backfill():
        log.info("Starting backfill (Telegram 7 days)...")
        await monitor.backfill(days=7)

        if config.APIFY_TOKEN:
            # Facebook backfill: only run once (residential proxies are expensive).
            # Skip if we already have Facebook posts in the DB.
            fb_stats = await db.get_stats()
            fb_count = fb_stats.get("by_source", {}).get("facebook", 0)
            if fb_count == 0:
                log.info("Facebook backfill: fetching ~15 posts/group (first-time only)...")
                from collectors.apify_facebook import ApifyFacebookCollector
                fb = ApifyFacebookCollector()
                items = await fb.backfill()
                for raw in items:
                    raw["source"] = "facebook"
                    await on_listing(raw)
                log.info("Facebook backfill: done (%d items fetched)", len(items))
            else:
                log.info("Facebook backfill: skipped (already have %d FB posts in DB)", fb_count)

        log.info("Backfill complete.")

    asyncio.create_task(run_backfill())

    # Send startup message
    stats = await db.get_stats()
    source_lines = [f"  \U0001f4f1 Telegram: {len(config.TG_CHANNELS)} channels (live)"]
    if config.APIFY_TOKEN:
        source_lines.append("  \U0001f3e0 Yad2: via Apify, every 2h")
        source_lines.append("  \U0001f4c4 Facebook: 7 groups via Apify, every 12h")
    if config.SCRAPERAPI_KEY:
        source_lines.append("  \U0001f3e0 Yad2: via ScraperAPI, every 30 min")
        source_lines.append("  \U0001f3e0 Madlan: 5 cities via ScraperAPI, every 2h")
    elif not config.APIFY_TOKEN:
        source_lines.append("  \U0001f3e0 Yad2: direct, every 30 min")

    await send_text(
        f"\U0001f680 Dira Bot started!\n\n"
        f"Sources:\n"
        + "\n".join(source_lines) +
        f"\n\nDB: {stats['total_listings']} listings, "
        f"{stats['sent']} sent, {stats['maybe']} maybe\n\n"
        f"Commands: /top /stats /preferences /pause /resume"
    )

    # 5. Run bot polling + Telethon in parallel
    try:
        # aiogram polling runs in the background
        polling_task = asyncio.create_task(
            dp.start_polling(bot, handle_signals=False)
        )

        # Telethon keeps running via its event loop
        # We just wait forever (both aiogram and telethon run concurrently)
        await asyncio.Event().wait()

    except (KeyboardInterrupt, SystemExit):
        log.info("Shutting down...")
    finally:
        scheduler.stop()
        await monitor.stop()
        await bot.session.close()
        await db.close()
        log.info("Dira Bot stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
