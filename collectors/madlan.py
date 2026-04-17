"""Dira Bot — Madlan.co.il collector.

Madlan is Israel's second-largest real estate portal (owned by Yad2 group
but separate inventory). It's less bot-protected than Yad2 — standard
browser headers are enough on a residential IP, or Apify if blocked.

Runs every 2 hours via scheduler (same cadence as Apify Yad2).
"""

import logging
import re
import json
import urllib.parse

import httpx

import config
from collectors.base import BaseCollector

log = logging.getLogger(__name__)

SEARCH_URL = "https://www.madlan.co.il/for-rent/tel-aviv-yafo-israel"
CITIES = [
    ("tel-aviv-yafo-israel",   "תל אביב יפו"),
    ("ramat-gan-israel",       "רמת גן"),
    ("givatayim-israel",       "גבעתיים"),
    ("bat-yam-israel",         "בת ים"),
    ("holon-israel",           "חולון"),
]

def _scraper_url(url: str) -> str:
    """Wrap URL with ScraperAPI proxy if key is configured (bypasses server IP block)."""
    if config.SCRAPERAPI_KEY:
        return f"https://api.scraperapi.com/?api_key={config.SCRAPERAPI_KEY}&url={urllib.parse.quote(url, safe='')}"
    return url


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Site": "none",
}


def _parse_page(html: str, city_heb: str) -> list[dict]:
    """Extract listings from Madlan's __NEXT_DATA__ JSON."""
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if not m:
        return []

    try:
        nd = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []

    # Navigate to listings — structure: props.pageProps.searchResults or similar
    try:
        page_props = nd["props"]["pageProps"]
    except (KeyError, TypeError):
        return []

    # Madlan stores listings in different places depending on page version
    listings_raw = (
        page_props.get("listings")
        or page_props.get("searchResults", {}).get("listings")
        or page_props.get("data", {}).get("listings")
        or []
    )

    # Also check dehydratedState (same pattern as Yad2)
    if not listings_raw:
        try:
            queries = page_props["dehydratedState"]["queries"]
            for q in queries:
                d = q.get("state", {}).get("data", {})
                if isinstance(d, dict):
                    listings_raw = (
                        d.get("listings")
                        or d.get("results")
                        or []
                    )
                    if listings_raw:
                        break
        except (KeyError, TypeError):
            pass

    if not listings_raw:
        log.debug("Madlan: no listings found in NEXT_DATA for %s", city_heb)
        return []

    results = []
    for item in listings_raw:
        if not isinstance(item, dict):
            continue

        listing_id = (
            item.get("id")
            or item.get("listingId")
            or item.get("token")
        )
        if not listing_id:
            continue

        price = item.get("price") or item.get("monthlyPrice")
        rooms = item.get("rooms") or item.get("roomsCount")
        sqm = item.get("squareMeter") or item.get("squareMeters") or item.get("size")
        floor = item.get("floor")

        # Address
        addr = item.get("address") or {}
        if isinstance(addr, dict):
            street = addr.get("street", {})
            street = street.get("longName", "") if isinstance(street, dict) else str(street or "")
            hood = addr.get("neighborhood", {})
            hood = hood.get("longName", "") if isinstance(hood, dict) else str(hood or "")
            city_name = addr.get("city", {})
            city_name = city_name.get("longName", city_heb) if isinstance(city_name, dict) else str(city_name or city_heb)
        else:
            street = hood = ""
            city_name = city_heb

        url = item.get("url") or item.get("listingUrl") or ""
        if url and not url.startswith("http"):
            url = f"https://www.madlan.co.il{url}"

        # Features
        props = item.get("properties") or item.get("listingProperties") or {}
        has_mamad = props.get("securityRoom") or props.get("mamad") or False
        furnished = props.get("furniture") or props.get("furnished") or False
        parking = props.get("parking") or False

        desc = item.get("description") or item.get("text") or ""

        # Build human-readable text
        parts = []
        if price:
            parts.append(f"מחיר: {price:,} ₪/חודש" if isinstance(price, int) else f"מחיר: {price} ₪/חודש")
        if rooms:
            parts.append(f"חדרים: {rooms}")
        if sqm:
            parts.append(f"שטח: {sqm} מ\"ר")
        if street or hood:
            parts.append(f"כתובת: {street}, {hood}, {city_name}".strip(", "))
        if floor is not None:
            parts.append(f"קומה: {floor}")
        if has_mamad:
            parts.append('ממ"ד: כן')
        if desc:
            parts.append(f"תיאור: {desc[:400]}")

        raw_text = "\n".join(parts) if parts else desc

        results.append({
            "source_id": f"madlan_{listing_id}",
            "raw_text": raw_text,
            "url": url,
            "price": price if isinstance(price, (int, float)) else None,
            "rooms": rooms if isinstance(rooms, (int, float)) else None,
            "address": f"{street}, {hood}".strip(", ") or None,
            "city": city_name,
            "has_mamad_hint": bool(has_mamad),
            "sqm": sqm,
            "floor": floor,
        })

    return results


class MadlanCollector(BaseCollector):
    """Scrapes Madlan.co.il rental listings (no anti-bot on residential IPs)."""

    source_name = "madlan"

    async def collect(self) -> list[dict]:
        results = []
        filters = f"?dealType=rent&minRooms={int(config.MIN_ROOMS)}&maxPrice={config.MAX_PRICE}"

        via = "ScraperAPI" if config.SCRAPERAPI_KEY else "direct"
        async with httpx.AsyncClient(
            headers=HEADERS,
            follow_redirects=True,
            timeout=60,  # ScraperAPI can be slower
        ) as client:
            for city_slug, city_heb in CITIES:
                raw_url = f"https://www.madlan.co.il/for-rent/{city_slug}{filters}"
                url = _scraper_url(raw_url)
                try:
                    r = await client.get(url)
                    if r.status_code != 200:
                        log.warning("Madlan %s: HTTP %s (via %s)", city_heb, r.status_code, via)
                        continue
                    if "__NEXT_DATA__" not in r.text:
                        log.warning("Madlan %s: no NEXT_DATA (blocked?) via %s", city_heb, via)
                        continue

                    items = _parse_page(r.text, city_heb)
                    log.info("Madlan %s: %d listings", city_heb, len(items))
                    results.extend(items)

                except Exception as e:
                    log.warning("Madlan %s error: %s", city_heb, e)

        log.info("Madlan total: %d listings across %d cities", len(results), len(CITIES))
        return results
