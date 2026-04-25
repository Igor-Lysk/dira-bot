"""Dira Bot — task scheduler (APScheduler).

Orchestrates periodic tasks:
- Yad2 direct scraping (Phase 2)
- Apify result fetching (Phase 3)
- Daily digest generation
- Preference learning
"""

import logging
from datetime import datetime
from typing import Callable, Awaitable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

import config
from database import Database
from analyzer import generate_digest, learn_preferences
from bot import send_digest, send_text

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, db: Database, on_listing: Callable[[dict], Awaitable[None]] = None):
        self._db = db
        self._on_listing = on_listing
        self._sched = AsyncIOScheduler(timezone=config.TIMEZONE)

    def start(self):
        """Register all periodic jobs and start the scheduler."""

        if self._on_listing:
            # Kick free scrapers immediately on start so we don't wait up to 2h
            # for the first batch after a restart. Facebook stays on schedule —
            # it's paid-per-event and restarts shouldn't re-burn credits.
            now = datetime.now()

            if config.APIFY_TOKEN:
                # Apify Yad2 every 2h (bypasses ShieldSquare on server)
                self._sched.add_job(
                    self._collect_apify_yad2,
                    IntervalTrigger(hours=2),
                    id="apify_yad2_collect",
                    replace_existing=True,
                    next_run_time=now,
                )
                # Apify Facebook every 12h (paid-per-event actor — keep events low)
                self._sched.add_job(
                    self._collect_apify_facebook,
                    IntervalTrigger(hours=12),
                    id="apify_facebook_collect",
                    replace_existing=True,
                )

            # Any anti-bot proxy provider configured? collectors/_fetch.py handles
            # the fallback chain (ScraperAPI → Scrape.do → ScrapingBee).
            has_proxy = any([
                config.SCRAPERAPI_KEY,
                config.SCRAPEDO_KEY,
                config.SCRAPINGBEE_KEY,
            ])

            if has_proxy:
                # Direct Yad2 every 30 min via the proxy chain
                self._sched.add_job(
                    self._collect_yad2,
                    IntervalTrigger(minutes=30),
                    id="yad2_direct_collect",
                    replace_existing=True,
                    next_run_time=now,
                )
                # Madlan every 2h via the proxy chain
                self._sched.add_job(
                    self._collect_madlan,
                    IntervalTrigger(hours=2),
                    id="madlan_collect",
                    replace_existing=True,
                    next_run_time=now,
                )

            elif not config.APIFY_TOKEN:
                # No proxy at all: direct Yad2 (works locally, may be blocked on server)
                self._sched.add_job(
                    self._collect_yad2,
                    IntervalTrigger(minutes=30),
                    id="yad2_collect",
                    replace_existing=True,
                    next_run_time=now,
                )

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

        # Healthchecks.io heartbeat ping
        if config.HEALTHCHECK_URL:
            self._sched.add_job(
                self._healthcheck_ping,
                IntervalTrigger(minutes=config.HEALTHCHECK_INTERVAL_MIN),
                id="healthcheck_ping",
                replace_existing=True,
                next_run_time=datetime.now(),
            )

        self._sched.start()
        if not self._on_listing:
            sources = "digest+preferences"
        else:
            parts = []
            if config.APIFY_TOKEN:
                parts += ["apify_yad2", "apify_facebook"]
            proxy_names = []
            if config.SCRAPERAPI_KEY:
                proxy_names.append("scraperapi")
            if config.SCRAPEDO_KEY:
                proxy_names.append("scrapedo")
            if config.SCRAPINGBEE_KEY:
                proxy_names.append("scrapingbee")
            if proxy_names:
                parts += [f"yad2[{'+'.join(proxy_names)}]", "madlan"]
            elif not config.APIFY_TOKEN:
                parts += ["yad2_direct"]
            parts += ["digest", "preferences"]
            sources = "+".join(parts)
        log.info("Scheduler started (%s) | digest at %s:00", sources, config.DIGEST_HOUR)

    async def _collect_apify_yad2(self):
        """Fetch Yad2 listings via Apify (bypasses anti-bot on server)."""
        from collectors.apify_yad2 import ApifyYad2Collector
        collector = ApifyYad2Collector()
        try:
            items = await collector.collect()
            for raw in items:
                raw["source"] = "yad2"
                if self._on_listing:
                    await self._on_listing(raw)
        except Exception as e:
            log.exception("Apify Yad2 collection error: %s", e)

    async def _collect_apify_facebook(self):
        """Fetch Facebook group posts via Apify."""
        from collectors.apify_facebook import ApifyFacebookCollector
        collector = ApifyFacebookCollector()
        try:
            items = await collector.collect()
            for raw in items:
                raw["source"] = "facebook"
                if self._on_listing:
                    await self._on_listing(raw)
        except Exception as e:
            log.exception("Apify Facebook collection error: %s", e)

    async def _collect_madlan(self):
        """Fetch Madlan.co.il listings (5 cities, no anti-bot needed)."""
        from collectors.madlan import MadlanCollector
        collector = MadlanCollector()
        try:
            items = await collector.collect()
            for raw in items:
                raw["source"] = "madlan"
                if self._on_listing:
                    await self._on_listing(raw)
        except Exception as e:
            log.exception("Madlan collection error: %s", e)

    async def _collect_yad2(self):
        """Fetch Yad2 listings via direct page scraping (fallback, works locally)."""
        from collectors.yad2_page import Yad2PageCollector
        collector = Yad2PageCollector()
        try:
            items = await collector.collect()
            for raw in items:
                raw["source"] = "yad2"
                if self._on_listing:
                    await self._on_listing(raw)
        except Exception as e:
            log.exception("Yad2 collection error: %s", e)

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

    async def _healthcheck_ping(self):
        import httpx
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.get(config.HEALTHCHECK_URL)
        except Exception as e:
            log.warning("Healthcheck ping failed: %s", e)

    def stop(self):
        self._sched.shutdown(wait=False)
