"""Холостой прогон боевого пути: сбор → пайплайн → сопоставление.

Всё как в main.py, кроме двух вещей: бот не опрашивается и ничего не
отправляется. Нужен, чтобы проверить пайплайн на живом потоке, не разослав
при этом сообщений.

    python3 scripts/dryrun.py --seconds 60 --db ~/dira-data/dryrun.db
"""

import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collectors.telegram_v2 import TelegramMonitor   # noqa: E402
from core import pipeline, settings                  # noqa: E402
from core.sources import channels_for                # noqa: E402
from core.store import Store                         # noqa: E402
from db.migrate import migrate                       # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
for noisy in ("telethon", "httpx", "anthropic"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("dryrun")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.expanduser("~/dira-data/dryrun.db"))
    ap.add_argument("--seconds", type=int, default=45)
    ap.add_argument("--backfill-days", type=int, default=1)
    ap.add_argument("--enrich", type=int, default=0, help="сколько объявлений дозаполнить моделью")
    args = ap.parse_args()

    migrate(args.db, verbose=False)
    store = await Store(args.db).connect()

    user = await store.ensure_user(TEST_TELEGRAM_ID, "tester", "Игорь")
    if not await store.profiles_of(user["telegram_id"]):
        await store.create_profile(
            user["telegram_id"], "Основной",
            cities=["Tel Aviv", "Ramat Gan", "Givatayim", "Bnei Brak"],
            price_max=8000, price_ideal=7500, rooms_min=2.5,
            req_mamad="allow_unknown", delivery_mode="digest", digest_hour=9)

    cities = set()
    for profile in await store.active_profiles():
        cities.update(profile.get("cities") or [])
    channels = channels_for(cities)
    log.info("каналы: %s", ", ".join(channels))

    counters = {"new": 0, "duplicate": 0, "repost": 0, "matches": 0}

    async def on_listing(raw):
        result = await pipeline.process(store, raw)
        counters[result["status"]] = counters.get(result["status"], 0) + 1
        counters["matches"] += result.get("matches", 0)
        if result["status"] == "repost":
            log.info("повтор: %s ₪ → %s ₪", result.get("old_price"), result.get("new_price"))

    monitor = TelegramMonitor(channels)
    await monitor.start(on_listing)
    log.info("состояние монитора: %s", monitor.is_healthy())

    log.info("backfill за %d дн…", args.backfill_days)
    await monitor.backfill(days=args.backfill_days, per_channel=200)
    log.info("после backfill: %s", counters)

    if args.enrich:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
        result = await pipeline.enrich_pending(store, client, settings.CLAUDE_MODEL,
                                               limit=args.enrich)
        log.info("дозаполнение: %s", result)

    log.info("слушаю ещё %d секунд…", args.seconds)
    await asyncio.sleep(args.seconds)

    log.info("итог: %s", counters)
    log.info("статистика базы: %s", await store.stats())
    for profile in await store.active_profiles():
        queue = await store.queue_for(profile["id"], limit=100)
        log.info("профиль %s: в очереди на отправку %d", profile["id"], len(queue))
    log.info("монитор: %s", monitor.is_healthy())
    await monitor.stop()
    await store.close()


if __name__ == "__main__":
    asyncio.run(main())
