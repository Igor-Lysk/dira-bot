"""Dira Bot — Yad2 collector via Apify (swerve~yad2-scraper).

Uses Apify residential proxies to bypass Yad2 ShieldSquare anti-bot.
Primary Yad2 source when running on server. Runs every 2 hours.
"""

import asyncio
import logging
from typing import Optional

import httpx

import config
from collectors.base import BaseCollector

log = logging.getLogger(__name__)

APIFY_BASE = "https://api.apify.com/v2"
ACTOR_ID = "swerve~yad2-scraper"
POLL_INTERVAL = 5   # seconds between status checks
MAX_POLLS = 60      # 5 minutes max wait


def _normalize(item: dict) -> Optional[dict]:
    """Convert swerve~yad2-scraper item to standard raw listing format."""
    listing_id = item.get("listingId") or item.get("id")
    if not listing_id:
        return None

    url = item.get("url") or f"https://www.yad2.co.il/item/{listing_id}"
    price = item.get("price")
    rooms = item.get("rooms")
    sqm = item.get("areaSqm") or item.get("squareMeter")
    floor = item.get("floor")
    neighbourhood = item.get("neighbourhood") or item.get("neighborhood", "")
    has_mamad = item.get("hasSecureRoom")
    description = item.get("listingDescription", "")

    parts = []
    if price:
        parts.append(f"מחיר: {price:,} ₪/חודש" if isinstance(price, int) else f"מחיר: {price} ₪/חודש")
    if rooms:
        parts.append(f"חדרים: {rooms}")
    if sqm:
        parts.append(f"שטח: {sqm} מ\"ר")
    if neighbourhood:
        parts.append(f"שכונה: {neighbourhood}")
    if floor is not None:
        parts.append(f"קומה: {floor}")
    if has_mamad:
        parts.append('ממ"ד: כן')
    if description:
        parts.append(f"תיאור: {description[:500]}")

    raw_text = "\n".join(parts) if parts else description

    return {
        "source_id": f"yad2_{listing_id}",
        "raw_text": raw_text,
        "url": url,
        "price": price,
        "rooms": rooms,
        "address": neighbourhood,
        "city": "תל אביב",
        "has_mamad_hint": has_mamad,
        "sqm": sqm,
        "floor": floor,
    }


class ApifyYad2Collector(BaseCollector):
    """Fetches Yad2 rental listings via Apify (bypasses ShieldSquare)."""

    source_name = "yad2"

    async def collect(self) -> list[dict]:
        if not config.APIFY_TOKEN:
            log.warning("APIFY_TOKEN not set, skipping Apify Yad2")
            return []

        actor_input = {
            "city": "Tel Aviv",
            "listingType": "rent",
            "minRooms": int(config.MIN_ROOMS),
            "maxPrice": config.MAX_PRICE,
        }

        try:
            items = await self._run_actor(actor_input)
        except Exception as e:
            log.exception("Apify Yad2 error: %s", e)
            return []

        results = []
        for item in items:
            raw = _normalize(item)
            if raw:
                results.append(raw)

        log.info("Apify Yad2: fetched %d listings", len(results))
        return results

    async def _run_actor(self, actor_input: dict) -> list[dict]:
        """Start run, poll until done, return dataset items."""
        headers = {"Authorization": f"Bearer {config.APIFY_TOKEN}"}

        async with httpx.AsyncClient(timeout=30) as client:
            # Start the run
            resp = await client.post(
                f"{APIFY_BASE}/acts/{ACTOR_ID}/runs",
                headers=headers,
                json=actor_input,
            )
            resp.raise_for_status()
            run = resp.json()["data"]
            run_id = run["id"]
            log.info("Apify Yad2: run started (%s)", run_id)

        # Poll in a fresh client to avoid timeout issues
        async with httpx.AsyncClient(timeout=30) as client:
            status = "RUNNING"
            for _ in range(MAX_POLLS):
                await asyncio.sleep(POLL_INTERVAL)
                r = await client.get(
                    f"{APIFY_BASE}/acts/{ACTOR_ID}/runs/{run_id}",
                    headers=headers,
                )
                r.raise_for_status()
                status = r.json()["data"].get("status", "RUNNING")
                if status not in ("RUNNING", "READY", "ABORTING"):
                    break

            if status != "SUCCEEDED":
                log.warning("Apify Yad2: run %s → %s", run_id, status)
                return []

            # Fetch results
            r = await client.get(
                f"{APIFY_BASE}/acts/{ACTOR_ID}/runs/{run_id}/dataset/items",
                headers=headers,
                params={"format": "json", "limit": 200},
            )
            r.raise_for_status()
            return r.json()
