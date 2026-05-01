"""One-shot re-analysis of recent DB listings against the current criteria.

Use case: criteria in analyzer.py / config.py changed; we want to surface
listings that were previously SKIP'd but now match (and would have been
sent to chat under the new rules).

Scope:
- Only listings collected in the last N days (default 7) — older are stale
- Only those whose latest analysis is NOT SEND/MAYBE — already-sent listings
  are skipped so we don't double-alert

Run inside the dira-bot container so env vars / DB / Anthropic API are wired:
    docker exec dira-bot python scripts/reanalyze.py [--days N] [--dry-run]
"""

import argparse
import asyncio
import logging
import sys

# Project root is mounted as /app inside the container
sys.path.insert(0, "/app")

import config
from analyzer import analyze_listing
from bot import init as init_bot, send_alert
from database import Database

log = logging.getLogger("reanalyze")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)


async def run(days: int, dry_run: bool):
    db = Database(config.DB_PATH)
    await db.connect()

    # Initialise the aiogram bot — send_alert needs a configured Bot instance
    if not dry_run:
        init_bot(db)

    cur = await db._db.execute(
        """
        SELECT l.id, l.source, l.raw_text, l.url, l.price, l.rooms,
               l.address, l.city, a.recommendation, a.score
        FROM listings l
        LEFT JOIN analyses a ON a.listing_id = l.id
        WHERE datetime(l.collected_at) > datetime('now', ?)
          AND (a.recommendation IS NULL OR a.recommendation NOT IN ('SEND', 'MAYBE'))
        ORDER BY l.collected_at DESC
        """,
        (f"-{days} day",),
    )
    rows = [dict(r) for r in await cur.fetchall()]
    log.info("Re-analysing %d listings (last %d days, excluding already-sent)", len(rows), days)

    prefs = await db.get_preferences()

    counts = {"SEND": 0, "MAYBE": 0, "SKIP": 0, "ERROR": 0}
    new_alerts = 0
    for i, r in enumerate(rows, 1):
        text = r["raw_text"] or ""
        if not text.strip():
            counts["SKIP"] += 1
            continue

        try:
            analysis = await analyze_listing(text, source=r["source"], preferences=prefs)
        except Exception as e:
            log.warning("[%d/%d] analyze failed for %s: %s", i, len(rows), r["id"][:12], e)
            counts["ERROR"] += 1
            continue

        rec = analysis.get("recommendation", "SKIP")
        score = analysis.get("score", 0)
        old_rec = r.get("recommendation") or "—"
        counts[rec] = counts.get(rec, 0) + 1

        log.info(
            "[%d/%d] %s: %s/10 %s (was %s)  %s",
            i, len(rows), r["id"][:12], score, rec, old_rec,
            (analysis.get("summary") or "")[:60],
        )

        if dry_run:
            continue

        # Persist the new analysis (INSERT OR REPLACE — overwrites old record)
        await db.save_analysis(r["id"], analysis)

        # Push to chat if we now consider it worth seeing
        if rec in ("SEND", "MAYBE"):
            listing_data = {
                "id": r["id"],
                "source": r["source"],
                "url": r["url"],
                "price": analysis.get("price_found") or r.get("price"),
                "rooms": analysis.get("rooms_found") or r.get("rooms"),
            }
            try:
                await send_alert(listing_data, analysis)
                new_alerts += 1
            except Exception as e:
                log.warning("send_alert failed for %s: %s", r["id"][:12], e)

    log.info("Done. counts=%s alerts_sent=%d", counts, new_alerts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--dry-run", action="store_true",
                    help="don't write analyses or send alerts; just log")
    args = ap.parse_args()

    asyncio.run(run(args.days, args.dry_run))


if __name__ == "__main__":
    main()
