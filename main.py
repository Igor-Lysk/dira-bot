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
import sys

import config
from database import Database, make_id, make_fingerprint
from collectors.telegram_monitor import TelegramMonitor
from analyzer import analyze_listing
from bot import init as init_bot, send_alert, send_text
from scheduler import Scheduler

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


# ── Processing pipeline ──────────────────────────────────────────────────────

async def process_new_listing(raw: dict, db: Database):
    """Pipeline: dedup → save → analyze → alert."""
    text = raw.get("raw_text", "")
    url = raw.get("url")
    source = raw.get("source", "telegram")
    source_id = raw.get("source_id", "")

    listing_id = make_id(url, text)

    # Save to DB (dedup by ID + fingerprint)
    is_new = await db.add_listing(
        listing_id=listing_id,
        source=source,
        source_id=source_id,
        raw_text=text,
        url=url,
    )

    if not is_new:
        log.debug("Duplicate listing %s, skipping", listing_id)
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

    # 3. Scheduler
    scheduler = Scheduler(db)
    scheduler.start()

    # 4. Telegram Monitor (Telethon)
    monitor = TelegramMonitor()

    async def on_listing(raw: dict):
        raw["source"] = "telegram"
        await process_new_listing(raw, db)

    await monitor.start(on_listing)

    # Send startup message
    stats = await db.get_stats()
    await send_text(
        f"\U0001f680 Dira Bot started!\n\n"
        f"Monitoring {len(config.TG_CHANNELS)} Telegram channels\n"
        f"DB: {stats['total_listings']} listings, "
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
