# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Dira Bot — autonomous apartment-hunting bot for Tel Aviv. Monitors Telegram channels, Yad2, Madlan, and Facebook for rental listings, analyzes each with Claude (Haiku), and sends Telegram alerts with 👍/👎 feedback buttons. Deployed 24/7 on a Berlin VPS.

**Search criteria (hardcoded in `analyzer.py` prompt):** Tel Aviv area, ≤7,000 ₪/month, 2+ rooms, ממ"ד required, furnished, near train station, move-in May 2026, 6+ months.

---

## Running & deployment

```bash
# Local run (uses .env in project root)
python main.py

# Build and start Docker
docker-compose up --build

# Deploy to VPS (git push triggers post-receive hook → docker-compose up)
git push berlin master

# View live logs on server
ssh -i ~/.ssh/REDACTED igor@REDACTED \
  "DOCKER_HOST=unix:///run/user/1003/docker.sock docker logs dira-bot -f"

# Check container status
ssh -i ~/.ssh/REDACTED igor@REDACTED \
  "DOCKER_HOST=unix:///run/user/1003/docker.sock docker ps"
```

**Server `.env`** lives at `~/workspace/dira-bot/.env` on the VPS — it is not in git. Add new secrets there manually via SSH **and** to the local `.env`.

---

## Architecture

Everything runs in a **single asyncio event loop**. No threads, no subprocesses.

```
Telegram channels (real-time)  ─┐
Yad2 via ScraperAPI (30 min)   ─┤──► process_new_listing() ──► Claude ──► send_alert()
Madlan via ScraperAPI (2h)     ─┤         (main.py)           analyzer.py    bot.py
Apify Yad2/Facebook (2h/12h)  ─┘
                                          │
                                          ▼
                                     SQLite DB
                                    (database.py)
                                          │
                                 APScheduler (scheduler.py)
                                  ├── daily digest 20:00
                                  └── learn preferences 6h
```

### Processing pipeline (`process_new_listing` in `main.py`)

Every listing from every source goes through this sequence:

1. **Sublet filter** — regex matches "саблет/sublet/שותפ" etc., but cancels on negation like "не саблет" within 8 chars (`_SUBLET_RE` / `_NOT_SUBLET_RE`)
2. **Cross-source dedup** — `db.find_similar(price±300, rooms, city)` prevents the same apartment from Yad2 + Telegram + Facebook firing 3 alerts. Only fires when all 3 fields are non-null
3. **ID + fingerprint dedup** — SHA-256 of URL (preferred) or text; text fingerprint strips whitespace/punctuation
4. **Claude analysis** — score 0-10, SEND/MAYBE/SKIP recommendation, structured JSON
5. **Alert** — SEND (score ≥7) or MAYBE (score 5-6) → `send_alert()`

### Collectors

All extend `BaseCollector` (`collectors/base.py`). The scheduler wires them up:

| Collector | File | Trigger | Notes |
|-----------|------|---------|-------|
| `TelegramMonitor` | `telegram_monitor.py` | real-time events | Uses Telethon StringSession; `backfill(days=7)` runs once on start |
| `Yad2PageCollector` | `yad2_page.py` | every 30 min | Parses `__NEXT_DATA__` JSON from page; routes via ScraperAPI when `SCRAPERAPI_KEY` set |
| `MadlanCollector` | `madlan.py` | every 2h | Same `__NEXT_DATA__` approach; routes via ScraperAPI |
| `ApifyYad2Collector` | `apify_yad2.py` | every 2h | Only if `APIFY_TOKEN` set |
| `ApifyFacebookCollector` | `apify_facebook.py` | every 12h | Only if `APIFY_TOKEN` set; **paid-per-event** (~$3/1000 results) |

**Scheduler selection logic** (`scheduler.py`):
- `APIFY_TOKEN` set → Apify Yad2 + Apify Facebook
- `SCRAPERAPI_KEY` set → direct Yad2 + Madlan (both wrapped through ScraperAPI)
- Both set → all four run; DB dedup prevents duplicate alerts
- Neither → direct Yad2 only (works locally, 403 on server)

### Facebook backfill guard

`run_backfill()` in `main.py` checks `db.get_stats()["by_source"]["facebook"] > 0` before running the Apify Facebook backfill. This prevents re-burning Apify credits on every restart. The Facebook actor is **paid-per-event** — the initial backfill of 100 posts × 15 groups burned ~$4.75 on first deploy (April 8, 2026).

---

## Key implementation details

### Hebrew JSON gotcha

`ממ"ד` (mamad) contains a literal double-quote (Hebrew gershayim, U+0022) that breaks JSON parsing. `_sanitize_hebrew_json()` in `analyzer.py` replaces it with U+05F4 before parsing. This runs as a fallback in `_try_parse()`.

### ScraperAPI integration

`_scraper_url(url)` in `yad2_page.py` and `madlan.py` wraps any URL:
```python
f"https://api.scraperapi.com/?api_key={config.SCRAPERAPI_KEY}&url={urllib.parse.quote(url, safe='')}"
```
Timeout must be **60s** (not 20s) — ScraperAPI adds latency. Free tier: 5,000 req/month. Current usage: ~3,240/month (Yad2 30min + Madlan 5 cities 2h).

### Apify cost model

`apify~facebook-groups-scraper` charges **per event (post returned)**, not per request or bandwidth. At ~$3/1000 events, current settings (5 posts × 7 groups × 2 runs/day) cost ~$0.63/month. Monthly limit resets on the 1st — if exhausted, both Apify collectors return 403.

### Scoring thresholds

Defined in `config.py`:
- `SEND_THRESHOLD = 7` → instant alert
- `MAYBE_THRESHOLD = 5` → alert marked with "?"
- Both trigger `send_alert()` (handled in `process_new_listing`)

---

## Config / environment

All settings flow through `config.py` which loads `.env`. Key variables:

| Variable | Purpose |
|----------|---------|
| `TELEGRAM_BOT_TOKEN` | aiogram bot |
| `TELEGRAM_CHAT_ID` | where to send alerts |
| `TELEGRAM_API_ID/HASH/SESSION_STRING` | Telethon user session |
| `ANTHROPIC_API_KEY` | Claude API |
| `CLAUDE_MODEL` | default `claude-haiku-4-5-20251001` |
| `APIFY_TOKEN` | enables Apify collectors |
| `SCRAPERAPI_KEY` | enables ScraperAPI proxy for Yad2/Madlan |

Adding a new Telegram channel: add username (without `@`) to `TG_CHANNELS` in `config.py`.
Adding a Facebook group: add URL to `FB_GROUPS` (affects Apify cost — each group adds ~$0.09/month at current settings).
