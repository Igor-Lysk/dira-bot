"""Provider-fallback HTTP fetcher for anti-bot-protected sites (Yad2, Madlan).

Tries each configured provider in order. The first one returning a non-blocked
HTTP 200 wins. When a provider returns 4xx/5xx or serves an anti-bot
interstitial, we move on to the next one.

This is the *reactive* strategy — no quota tracking, just probe-and-fall-through.
That keeps things simple and self-healing: when monthly quotas reset, the
top-priority provider just starts working again.

Provider order (cheapest/fastest first):
  1. ScraperAPI     — fastest response, biggest free quota
  2. Scrape.do      — backup with full JS rendering on free tier
  3. ScrapingBee    — small free tier with JS rendering
  4. FlareSolverr   — local headless Chrome, infinite quota but slow (5-15s)
"""

import logging
import urllib.parse
from typing import Optional, Callable, Awaitable

import httpx

import config

log = logging.getLogger(__name__)

FLARESOLVERR_URL = "http://flaresolverr:8191/v1"

# ── Provider implementations ──────────────────────────────────────────────────
# Each provider is an async function (client, url, headers) -> Optional[Response].
# Returns None when the provider is not configured. Returns httpx.Response on
# *attempt* (success or failure) so the caller can inspect status / body.

ProviderFn = Callable[[httpx.AsyncClient, str, dict], Awaitable[Optional[httpx.Response]]]


async def _via_scraperapi(client, url, headers):
    if not config.SCRAPERAPI_KEY:
        return None
    proxy = (
        "https://api.scraperapi.com/"
        f"?api_key={config.SCRAPERAPI_KEY}"
        f"&url={urllib.parse.quote(url, safe='')}"
    )
    return await client.get(proxy, headers=headers, timeout=60)


async def _via_scrapedo(client, url, headers):
    if not config.SCRAPEDO_KEY:
        return None
    proxy = (
        "https://api.scrape.do/"
        f"?token={config.SCRAPEDO_KEY}"
        f"&url={urllib.parse.quote(url, safe='')}"
        "&render=true"
        "&geoCode=il"
    )
    return await client.get(proxy, headers=headers, timeout=90)


async def _via_scrapingbee(client, url, headers):
    if not config.SCRAPINGBEE_KEY:
        return None
    proxy = (
        "https://app.scrapingbee.com/api/v1/"
        f"?api_key={config.SCRAPINGBEE_KEY}"
        f"&url={urllib.parse.quote(url, safe='')}"
        "&render_js=true"
        "&country_code=il"
    )
    return await client.get(proxy, headers=headers, timeout=90)


async def _via_flaresolverr(client, url, headers):
    """Self-hosted headless Chrome bypass. Slow (~5-15s) but no quota.

    Lifecycle: container is started lazily on each call and stopped
    immediately after the request completes. We're called at most once
    per hour (when paid providers fail), so keeping Chrome warm
    in-between would just waste ~280 MB RAM. See _flaresolverr_lifecycle.
    """
    from collectors import _flaresolverr_lifecycle as fs

    if not await fs.ensure_started():
        return None  # cold-start failed or docker socket missing

    try:
        payload = {
            "cmd": "request.get",
            "url": url,
            "maxTimeout": 60000,
        }
        try:
            r = await client.post(FLARESOLVERR_URL, json=payload, timeout=90)
        except httpx.ConnectError:
            return None  # raced container shutdown; treat as unavailable
        r.raise_for_status()

        data = r.json()
        if data.get("status") != "ok":
            log.warning("flaresolverr error: %s", data.get("message"))
            return httpx.Response(
                status_code=502,
                text=data.get("message", ""),
                request=httpx.Request("GET", url),
            )
        sol = data["solution"]
        return httpx.Response(
            status_code=sol.get("status", 200),
            text=sol.get("response", ""),
            request=httpx.Request("GET", url),
        )
    finally:
        # Always stop the container, even if the request errored — we
        # don't want a leaked Chrome eating RAM until the next attempt.
        fs.stop_in_background()


PROVIDERS: list[tuple[str, ProviderFn]] = [
    ("scraperapi", _via_scraperapi),
    ("scrapedo", _via_scrapedo),
    ("scrapingbee", _via_scrapingbee),
    ("flaresolverr", _via_flaresolverr),
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

    Returns the first httpx.Response that is HTTP 200 and not anti-bot-blocked.
    Returns None when every configured provider fails — caller logs/handles.
    """
    headers = headers or {}
    async with httpx.AsyncClient(follow_redirects=True) as client:
        for name, provider in PROVIDERS:
            try:
                resp = await provider(client, url, headers)
            except Exception as e:
                log.warning("fetch via %s failed: %s", name, e)
                continue

            if resp is None:
                continue  # provider not configured

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
