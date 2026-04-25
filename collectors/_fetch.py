"""Provider-fallback HTTP fetcher for anti-bot-protected sites (Yad2, Madlan).

Tries each configured proxy provider in order. The first one returning
a non-blocked HTTP 200 wins. When a provider returns 4xx (quota out) or
serves an anti-bot interstitial, we move on to the next one.

This is the *reactive* strategy — no quota tracking, just probe-and-fall-through.
That keeps things simple and self-healing: when monthly quotas reset, the
top-priority provider just starts working again.

Provider order (cheapest/fastest first):
  1. ScraperAPI  — fastest response, biggest free quota
  2. Scrape.do   — backup with full JS rendering on free tier
  3. ScrapingBee — last resort (free tier is small with JS)

(FlareSolverr can be appended later as a final, infinite-quota backup.)
"""

import logging
import urllib.parse
from typing import Optional

import httpx

import config

log = logging.getLogger(__name__)


def _scraperapi_url(url: str) -> Optional[str]:
    if not config.SCRAPERAPI_KEY:
        return None
    return (
        "https://api.scraperapi.com/"
        f"?api_key={config.SCRAPERAPI_KEY}"
        f"&url={urllib.parse.quote(url, safe='')}"
    )


def _scrapedo_url(url: str) -> Optional[str]:
    if not config.SCRAPEDO_KEY:
        return None
    # render=true → headless Chrome, geoCode=il → Israel residential exit
    return (
        "https://api.scrape.do/"
        f"?token={config.SCRAPEDO_KEY}"
        f"&url={urllib.parse.quote(url, safe='')}"
        "&render=true"
        "&geoCode=il"
    )


def _scrapingbee_url(url: str) -> Optional[str]:
    if not config.SCRAPINGBEE_KEY:
        return None
    return (
        "https://app.scrapingbee.com/api/v1/"
        f"?api_key={config.SCRAPINGBEE_KEY}"
        f"&url={urllib.parse.quote(url, safe='')}"
        "&render_js=true"
        "&country_code=il"
    )


# Order matters — first that succeeds wins.
PROVIDERS = [
    ("scraperapi", _scraperapi_url, 60),
    ("scrapedo", _scrapedo_url, 90),
    ("scrapingbee", _scrapingbee_url, 90),
]


def _is_blocked(resp: httpx.Response, expect_marker: Optional[str] = None) -> bool:
    """Detect anti-bot interstitials served with HTTP 200.

    Reblaze (used by Yad2) returns short HTML pages containing __uzdbm_ JS vars.
    If caller passes expect_marker (e.g. '__NEXT_DATA__'), we additionally
    require it to be present.
    """
    if resp.status_code != 200:
        return True
    body = resp.text
    if "__uzdbm_" in body and len(body) < 8000:
        return True  # Reblaze challenge page
    if expect_marker and expect_marker not in body:
        return True
    return False


async def fetch(
    url: str,
    headers: Optional[dict] = None,
    expect_marker: Optional[str] = None,
) -> Optional[httpx.Response]:
    """Fetch URL via the provider chain.

    Returns the first httpx.Response that's HTTP 200 and not anti-bot-blocked.
    Returns None when every configured provider fails — caller logs/handles.

    Args:
        url: The target URL to fetch.
        headers: Optional headers forwarded to the proxy (most providers do
            pass them through to the target; some ignore custom headers).
        expect_marker: Optional substring required in the response body
            (e.g. '__NEXT_DATA__'). Useful to catch silently-wrong responses.
    """
    async with httpx.AsyncClient(
        headers=headers or {},
        follow_redirects=True,
    ) as client:
        for name, build_url, timeout in PROVIDERS:
            proxy_url = build_url(url)
            if proxy_url is None:
                continue  # provider not configured
            try:
                resp = await client.get(proxy_url, timeout=timeout)
            except Exception as e:
                log.warning("fetch via %s failed: %s", name, e)
                continue

            if _is_blocked(resp, expect_marker):
                log.warning(
                    "fetch via %s blocked (HTTP %s, %d bytes) — trying next",
                    name, resp.status_code, len(resp.text),
                )
                continue

            log.info("fetch via %s OK (%d bytes)", name, len(resp.text))
            return resp

    log.error("fetch: all providers failed for %s", url[:100])
    return None
