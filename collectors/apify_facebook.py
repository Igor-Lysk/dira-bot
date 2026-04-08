"""Dira Bot — Facebook groups collector via Apify (apify~facebook-groups-scraper).

Collects rental posts from 7 Israeli Facebook groups.
Runs every 2 hours. Pre-filters by rent keywords before Claude analysis.
"""

import asyncio
import logging
from typing import Optional

import httpx

import config
from collectors.base import BaseCollector

log = logging.getLogger(__name__)

APIFY_BASE = "https://api.apify.com/v2"
ACTOR_ID = "apify~facebook-groups-scraper"
POLL_INTERVAL = 5
MAX_POLLS = 90      # 7.5 minutes max (Facebook is slower)
POSTS_PER_GROUP = 20


def _is_relevant(text: str) -> bool:
    """Pre-filter: must have rent keyword and no exclusion keyword."""
    if not text:
        return False
    t = text.lower()
    has_rent = any(kw.lower() in t for kw in config.RENT_KEYWORDS)
    excluded = any(kw.lower() in t for kw in config.EXCLUDE_KEYWORDS)
    return has_rent and not excluded


def _normalize(item: dict) -> Optional[dict]:
    """Convert Facebook post dict to standard raw listing format."""
    post_id = item.get("postId") or item.get("id")
    if not post_id:
        return None

    text = item.get("postText") or item.get("text") or ""
    if not _is_relevant(text):
        return None

    url = item.get("url") or item.get("postUrl") or ""

    return {
        "source_id": f"fb_{post_id}",
        "raw_text": text,
        "url": url,
        "price": None,   # Claude will extract
        "rooms": None,
        "address": None,
        "city": None,
    }


class ApifyFacebookCollector(BaseCollector):
    """Collects rental posts from Facebook groups via Apify."""

    source_name = "facebook"

    async def backfill(self) -> list[dict]:
        """Fetch last ~100 posts per group (one-time historical load)."""
        return await self._collect_with_limit(100)

    async def collect(self) -> list[dict]:
        if not config.APIFY_TOKEN:
            log.warning("APIFY_TOKEN not set, skipping Apify Facebook")
            return []

        if not config.FB_GROUPS:
            return []

        return await self._collect_with_limit(POSTS_PER_GROUP)

    async def _collect_with_limit(self, limit: int) -> list[dict]:
        if not config.APIFY_TOKEN or not config.FB_GROUPS:
            return []

        actor_input = {
            "startUrls": [{"url": g} for g in config.FB_GROUPS],
            "resultsLimit": limit,
        }

        try:
            items = await self._run_actor(actor_input)
        except Exception as e:
            log.exception("Apify Facebook error: %s", e)
            return []

        results = []
        for item in items:
            raw = _normalize(item)
            if raw:
                results.append(raw)

        log.info("Apify Facebook: %d relevant posts (from %d raw)", len(results), len(items))
        return results



    async def _run_actor(self, actor_input: dict) -> list[dict]:
        """Start run, poll until done, return dataset items."""
        headers = {"Authorization": f"Bearer {config.APIFY_TOKEN}"}

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{APIFY_BASE}/acts/{ACTOR_ID}/runs",
                headers=headers,
                json=actor_input,
            )
            resp.raise_for_status()
            run_id = resp.json()["data"]["id"]
            log.info("Apify Facebook: run started (%s)", run_id)

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
                log.warning("Apify Facebook: run %s → %s", run_id, status)
                return []

            r = await client.get(
                f"{APIFY_BASE}/acts/{ACTOR_ID}/runs/{run_id}/dataset/items",
                headers=headers,
                params={"format": "json", "limit": 500},
            )
            r.raise_for_status()
            return r.json()
