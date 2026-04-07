"""Dira Bot — task scheduler (APScheduler).

Orchestrates periodic tasks:
- Yad2 direct scraping (Phase 2)
- Apify result fetching (Phase 3)
- Daily digest generation
- Preference learning
"""

import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

import config
from database import Database
from analyzer import generate_digest, learn_preferences
from bot import send_digest, send_text

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, db: Database):
        self._db = db
        self._sched = AsyncIOScheduler(timezone=config.TIMEZONE)

    def start(self):
        """Register all periodic jobs and start the scheduler."""

        # Daily digest at 20:00 Israel time
        self._sched.add_job(
            self._daily_digest,
            CronTrigger(hour=config.DIGEST_HOUR, minute=0, timezone=config.TIMEZONE),
            id="daily_digest",
            replace_existing=True,
        )

        # Learn preferences every 6 hours (if enough feedback)
        self._sched.add_job(
            self._learn_preferences,
            IntervalTrigger(hours=6),
            id="learn_preferences",
            replace_existing=True,
        )

        self._sched.start()
        log.info("Scheduler started (digest at %s:00, preferences every 6h)",
                 config.DIGEST_HOUR)

    async def _daily_digest(self):
        """Generate and send daily digest."""
        log.info("Generating daily digest...")
        try:
            top = await self._db.get_top_listings(limit=5, days=1)
            if not top:
                # Fall back to weekly if no daily results
                top = await self._db.get_top_listings(limit=5, days=7)

            if not top:
                await send_text(
                    "\U0001f4ed Daily digest: no new good listings today. "
                    "I'll keep looking!"
                )
                return

            # Merge listing + analysis data for digest
            digest_data = []
            for row in top:
                digest_data.append({
                    "score": row.get("score"),
                    "summary": row.get("summary", ""),
                    "price": row.get("price"),
                    "rooms": row.get("rooms"),
                    "address": row.get("address", ""),
                    "has_mamad": row.get("has_mamad"),
                    "furnished": row.get("furnished"),
                    "url": row.get("url", ""),
                })

            result = await generate_digest(digest_data, count=5, days=1)
            digest_text = result.get("digest", "No digest available.")

            header = f"\U0001f4cb *Daily Digest \u2014 {datetime.now().strftime('%d.%m')}*\n\n"
            await send_digest(header + digest_text)

            trends = result.get("trends", [])
            if trends:
                await send_text(
                    "\U0001f4c8 Trends: " + " | ".join(trends)
                )

        except Exception as e:
            log.exception("Digest error: %s", e)

    async def _learn_preferences(self):
        """Analyze user feedback and learn preferences."""
        feedback = await self._db.get_all_feedback()
        if len(feedback) < 5:
            return  # not enough data yet

        log.info("Learning preferences from %d feedbacks...", len(feedback))
        try:
            result = await learn_preferences(feedback)
            prefs = result.get("preferences", [])

            for p in prefs:
                await self._db.save_preference(
                    key=p["key"],
                    value=p["value"],
                    confidence=p.get("confidence", 0.5),
                    learned_from=[],
                )

            if prefs:
                log.info("Learned %d preferences", len(prefs))

        except Exception as e:
            log.exception("Preference learning error: %s", e)

    def stop(self):
        self._sched.shutdown(wait=False)
