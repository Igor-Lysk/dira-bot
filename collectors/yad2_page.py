"""Dira Bot — Yad2 collector via __NEXT_DATA__ page scraping.

Fetches the Yad2 rental listing page and extracts structured data
from the embedded React Query dehydrated state. No API key required.

Anti-bot notes:
- One request per collection cycle (30 min intervals)
- Rotates User-Agent
- Returns empty list on CAPTCHA block (skips cycle silently)
"""

import asyncio
import gzip
import json
import logging
import random
import re
from typing import Optional

import config
from collectors.base import BaseCollector
from collectors._fetch import fetch

log = logging.getLogger(__name__)

# Yad2 URL with search params matching our criteria
YAD2_URL = (
    "https://www.yad2.co.il/realestate/rent"
    "?city=5000"          # Tel Aviv
    "&area=1"
    "&region=3"
    f"&minPrice=0"
    f"&maxPrice={config.MAX_PRICE}"
    f"&minRooms={int(config.MIN_ROOMS)}"
    "&maxRooms=10"
)

# Tag IDs that indicate mamad
MAMAD_TAG_IDS = {1009}
MAMAD_TAG_NAMES = {"ממ\"ד", 'ממ"ד', "ממד", "ממ'ד"}

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
]


def _has_mamad(tags: list) -> Optional[bool]:
    for tag in tags:
        if tag.get("id") in MAMAD_TAG_IDS:
            return True
        if tag.get("name", "") in MAMAD_TAG_NAMES:
            return True
    return None  # unknown


def _item_to_text(item: dict) -> str:
    """Build Hebrew/mixed text description from structured Yad2 listing."""
    addr = item.get("address", {})
    city      = addr.get("city", {}).get("text", "")
    neigh     = addr.get("neighborhood", {}).get("text", "")
    street    = addr.get("street", {}).get("text", "")
    house_num = addr.get("house", {}).get("number", "")
    floor_num = addr.get("house", {}).get("floor", "")

    det = item.get("additionalDetails", {})
    rooms     = det.get("roomsCount", "")
    sqm       = det.get("squareMeter", "")
    prop_type = det.get("property", {}).get("text", "")

    price     = item.get("price", "")
    ad_type   = item.get("adType", "")
    tags      = [t.get("name", "") for t in item.get("tags", []) if t.get("name")]

    parts = []
    if price:
        parts.append(f"מחיר: {price:,} ₪/חודש" if isinstance(price, int) else f"מחיר: {price} ₪/חודש")
    if rooms:
        parts.append(f"חדרים: {rooms}")
    if sqm:
        parts.append(f"שטח: {sqm} מ\"ר")
    if prop_type:
        parts.append(f"סוג נכס: {prop_type}")
    if city:
        parts.append(f"עיר: {city}")
    if neigh:
        parts.append(f"שכונה: {neigh}")
    if street:
        loc = f"רחוב: {street}"
        if house_num:
            loc += f" {house_num}"
        parts.append(loc)
    if floor_num:
        parts.append(f"קומה: {floor_num}")
    if ad_type == "agency":
        parts.append("מתווך: כן")
    if tags:
        parts.append(f"תכונות: {', '.join(tags)}")

    return "\n".join(parts)


class Yad2PageCollector(BaseCollector):
    """Collects Yad2 rental listings by scraping the search results page."""

    source_name = "yad2"

    async def collect(self) -> list[dict]:
        """Fetch one page of Yad2 listings. Returns [] on block/error."""
        headers = {
            "User-Agent": random.choice(_USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Cache-Control": "no-cache",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }

        resp = await fetch(YAD2_URL, headers=headers, expect_marker="__NEXT_DATA__")
        if resp is None:
            return []

        try:
            items = self._parse_feed(resp.text)
        except Exception as e:
            log.exception("Yad2 parse error: %s", e)
            return []

        log.info("Yad2: fetched %d listings", len(items))
        return items

    def _parse_feed(self, html: str) -> list[dict]:
        """Extract listing dicts from embedded __NEXT_DATA__ JSON."""
        idx = html.find("__NEXT_DATA__")
        brace = html.index("{", idx)

        # Find matching closing brace
        depth, end = 0, brace
        for i in range(brace, min(brace + 8_000_000, len(html))):
            c = html[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break

        nd = json.loads(html[brace:end])
        dehy = nd["props"]["pageProps"]["dehydratedState"]

        # Find the realestate-rent-feed query
        feed_query = next(
            (q for q in dehy.get("queries", [])
             if "realestate-rent-feed" in str(q.get("queryKey", ""))),
            None,
        )
        if not feed_query:
            log.warning("Yad2: feed query not found in dehydratedState")
            return []

        feed_data = feed_query["state"]["data"]
        pagination = feed_data.get("pagination", {})
        log.info("Yad2 pagination: total=%s pages=%s",
                 pagination.get("total"), pagination.get("totalPages"))

        results = []
        for section in ["private", "agency", "platinum"]:
            raw_items = feed_data.get(section, [])
            if not isinstance(raw_items, list):
                continue
            for item in raw_items:
                raw = self._normalize(item, section)
                if raw:
                    results.append(raw)

        return results

    def _normalize(self, item: dict, section: str) -> Optional[dict]:
        """Convert Yad2 item dict to our standard raw listing format."""
        token = item.get("token")
        if not token:
            return None

        price = item.get("price")
        details = item.get("additionalDetails", {})
        rooms = details.get("roomsCount")
        sqm = details.get("squareMeter")

        addr = item.get("address", {})
        city = addr.get("city", {}).get("text", "")
        neigh = addr.get("neighborhood", {}).get("text", "")
        street = addr.get("street", {}).get("text", "")
        house = addr.get("house", {}).get("number", "")
        floor = addr.get("house", {}).get("floor")

        tags_raw = item.get("tags", [])
        tags = [t.get("name", "") for t in tags_raw if t.get("name")]
        has_mamad = _has_mamad(tags_raw)

        url = f"https://www.yad2.co.il/item/{token}"

        # Build human-readable text for Claude analysis
        raw_text = _item_to_text(item)

        address_str = " ".join(filter(None, [street, str(house) if house else "", neigh, city]))

        return {
            "source_id": f"yad2_{token}",
            "raw_text": raw_text,
            "url": url,
            "price": price,
            "rooms": rooms,
            "address": address_str.strip(),
            "city": city,
            "has_mamad_hint": has_mamad,  # pre-computed from tags, passed to analyzer
            "tags": tags,
            "floor": floor,
            "sqm": sqm,
            "section": section,  # private / agency / platinum
        }
