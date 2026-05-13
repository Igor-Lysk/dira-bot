# dira-bot

> Autonomous apartment-hunting bot for Tel Aviv. Scrapes Telegram, Yad2, and Madlan in real time; scores every listing with an LLM; pings me on Telegram with inline thumbs-up/down feedback.

**Status:** archived — the bot found my apartment in May 2026 and was retired. This repo is preserved as a working reference.

## Why I built this

Apartment hunting in Tel Aviv is brutal: dozens of channels, hundreds of daily posts in Hebrew, Russian, and English, agents reposting the same listing under different prices, and the good ones are gone within 30 minutes. I wanted to spend my attention on viewing apartments, not on doomscrolling Telegram. So I built a pipeline that watches every public source 24/7, filters out the noise, asks Claude Haiku whether each listing fits my criteria, and only pings me when something is worth a click.

## What it did in 33 days

| Metric | Value |
|---|---|
| Uptime | 7 April → 10 May 2026 (33 days) |
| Listings collected | **748** |
| Sources | Telegram channels: 682 · Yad2: 66 |
| LLM analyses | 748 (Claude Haiku 4.5) |
| Sent as instant alert (score ≥ 7) | 132 |
| Sent as "maybe" (score 5–6) | 88 |
| Filtered out | 528 |
| Apartments physically visited | ~8 |
| Apartments rented | **1** ✅ |

## Architecture

Everything runs in a single `asyncio` event loop — no threads, no subprocesses.

```
 Telegram channels  ──┐
 (Telethon, real-time)│
                      │
 Yad2 page scraper    │
 (proxy chain, 1h)    ├──► process_new_listing  ──►  Claude Haiku  ──►  Telegram alert
                      │      (main.py)               (analyzer.py)       (bot.py)
 Apify Yad2 (2h)      │           │
 Apify Facebook (12h) ┘           ▼
                              SQLite (database.py)
                                  │
                            APScheduler (scheduler.py)
                             ├── 20:00 daily digest
                             └── learn-preferences every 6h
```

### Pipeline per listing (`main.py:process_new_listing`)

1. **Sublet filter** — regex catches "саблет / sublet / שותפ", with rollback if "не саблет" appears.
2. **Pre-Claude rooms filter** — regex pulls the explicit room count (Hebrew `חדרים`, Russian `комнат`, English `bedrooms`). Anything < 2.5 → SKIP without calling the LLM. Saves money and latency.
3. **Cross-source dedup** — `db.find_similar(price ± 300, rooms, city)` prevents one apartment posted to both Yad2 and Telegram from triggering two alerts.
4. **ID + fingerprint dedup** — SHA-256 of URL or text body.
5. **LLM analysis** — Claude Haiku returns structured JSON: `{score: 0-10, decision: SEND/MAYBE/SKIP, summary, red_flags}`.
6. **Alert** — `SEND` or `MAYBE` triggers a Telegram message with two inline buttons: 👍 and 👎. Feedback flows back into the criteria-learning loop.

### Collectors

| Collector | File | Interval | Notes |
|---|---|---|---|
| `TelegramMonitor` | `collectors/telegram_monitor.py` | real-time | Telethon `StringSession`; `backfill(days=7)` on start |
| `Yad2PageCollector` | `collectors/yad2_page.py` | 60 min | Parses `__NEXT_DATA__` JSON; uses proxy chain |
| `MadlanCollector` | `collectors/madlan.py` | 2 h | Disabled — anti-bot returns empty payloads even with FlareSolverr |
| `ApifyYad2Collector` | `collectors/apify_yad2.py` | 2 h | Activated when `APIFY_TOKEN` is set |
| `ApifyFacebookCollector` | `collectors/apify_facebook.py` | 12 h | Pay-per-event (~$3 / 1k); FB groups list in `config.FB_GROUPS` |

### Proxy fallback chain (`collectors/_fetch.py`)

For Yad2 / Madlan I cycle through providers until one returns 200 OK:

```
ScraperAPI ──► Scrape.do ──► ScrapingBee ──► FlareSolverr (local Docker, lazy-started)
```

FlareSolverr is the last-resort fallback because spinning up its Chromium is expensive. The lifecycle manager starts it on demand and stops it as soon as the request completes — saves ~600 MB of resident RAM on the VPS during idle hours.

### Search criteria (locked in `analyzer.py`)

- **Cities** (equal weight): Tel Aviv, Ramat Gan, Givatayim, Bnei Brak. Bat Yam / Holon get −1 point.
- **Rooms:** ≥ 2.5 (Israeli convention: the living room counts as a room).
- **Price:** ≤ 8,000 ₪ / month (≤ 7,500 is bonus −1 point).
- **Mamad** (ממ"ד, reinforced safe room inside the apartment): required. Miklat (building-level shelter) does not qualify.

Hard rejects applied before scoring:
- Wrong city → score 0
- Sale, not rent → score 0
- Sublet / roommate / room share → score 0
- Lease < 6 months → score 0
- Price > 8,000 ₪ → score 0
- Rooms < 2.5 → score 0
- Only miklat, no mamad → score 0
- Mamad not mentioned at all → score ≤ 3 → SKIP

Alert thresholds: SEND ≥ 7, MAYBE 5–6.

### Database (`database.py`)

SQLite, single `listings` table with `raw_text` preserved for retroactive re-analysis. After parameter changes I re-ran the entire 748-row corpus through `scripts/reanalyze.py` to compare old vs. new decisions. Cheap because cached Claude responses are reused for unchanged criteria.

### Watchdog

External liveness check via Healthchecks.io. The bot pings `HEALTHCHECK_URL` every 10 minutes; if pings stop, Healthchecks sends a Telegram alert. This caught two silent Telethon disconnects that I would have otherwise missed for hours.

## Stack

Python 3.12 · `aiogram` 3 (bot framework) · `telethon` (channel monitor) · `anthropic` SDK · `httpx` · `apscheduler` · `aiosqlite` · `playwright` (FlareSolverr fallback) · Docker + Docker Compose.

## Deployment

The bot ran on a small remote Linux VPS (rootless Docker, systemd user services, git bare repo with a post-receive hook for push-to-deploy). It was **not** run on a personal computer — that would have been pointless given the 24/7 listening loop. The repo ships with a `Dockerfile` and `docker-compose.yml` so any Linux host with Docker can run it; the `_flaresolverr_lifecycle.py` module talks to the host docker socket (mounted into the container) to start/stop the FlareSolverr fallback container on demand.

## Setup

```bash
git clone https://github.com/Igor-Lysk/dira-bot.git
cd dira-bot
cp .env.example .env
# Fill in the values — see "Required keys" below.
docker compose up -d
```

### Required keys

| Variable | Where to get it |
|---|---|
| `TELEGRAM_BOT_TOKEN` | [@BotFather](https://t.me/BotFather) — `/newbot` |
| `TELEGRAM_CHAT_ID` | Your own user ID (start a chat with the bot, hit `/start`, check logs) |
| `TELEGRAM_API_ID` + `TELEGRAM_API_HASH` | [my.telegram.org](https://my.telegram.org) → API development tools |
| `TELEGRAM_SESSION_STRING` | Run `python session_string_generator.py` once and follow the prompts |
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) |

### Optional keys (graceful degradation)

| Variable | Effect when missing |
|---|---|
| `APIFY_TOKEN` | Apify collectors skip cleanly; Telegram + Yad2 still run |
| `SCRAPERAPI_KEY` / `SCRAPEDO_KEY` / `SCRAPINGBEE_KEY` | Falls through to the next provider, then FlareSolverr |
| `HEALTHCHECK_URL` | Watchdog disabled |

## Project layout

```
dira-bot/
├── main.py              # asyncio event loop, listing pipeline
├── bot.py               # aiogram 3 — alerts and inline feedback
├── analyzer.py          # Claude Haiku prompt + JSON parsing
├── database.py          # SQLite layer, dedup, criteria learning
├── scheduler.py         # APScheduler (digest, learn loop, scrapers)
├── config.py            # env-driven config + criteria + channel list
├── collectors/
│   ├── telegram_monitor.py
│   ├── yad2_page.py
│   ├── madlan.py
│   ├── apify_yad2.py
│   ├── apify_facebook.py
│   ├── _fetch.py        # proxy chain
│   └── _flaresolverr_lifecycle.py
├── scripts/
│   └── reanalyze.py     # re-score the whole DB after rule changes
├── data/                # SQLite DB lives here (gitignored)
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
└── README.md
```

## Bonus repository: FB_scrapper

Apify's Facebook scraper turned out to be the most expensive collector by far, and the free tier ran out fast. For dense Facebook coverage I wrote a separate scraper using Playwright with a persistent Chrome profile — it lives in [FB_scrapper](https://github.com/Igor-Lysk/FB_scrapper). The two projects share the same scoring philosophy but are independent so you can run either alone.

## Lessons learned

- **LLMs are perfect for fuzzy real-estate filtering.** Hebrew + Russian + English in one inbox, sometimes one post; classical regex would have been a maintenance nightmare. Claude Haiku at $0.25 / $1.25 per million tokens cost me about $1.20 for the whole run.
- **A proxy fallback chain beats picking a "best" provider.** ScraperAPI, Scrape.do, and ScrapingBee each fail in different ways at different times of day. Trying them in order recovered ~30 % of requests that would otherwise hav