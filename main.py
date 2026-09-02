"""Dira Bot v2 — точка входа.

Всё в одном asyncio-цикле, как и в v1: потоков нет, subprocess нет.

    сбор (Telethon, realtime + backfill)
        → pipeline.process: дедуп, правила, сопоставление с профилями
        → enrich (раз в 10 минут): модель дозаполняет недостающее
        → delivery (раз в 5 минут): realtime либо дайджест по часам профиля

Планировщик отдельно подбирает то, что сорвалось, и пингует healthchecks.io —
но только пока Telethon жив. Это watchdog из v1: тогда цикл обновлений тихо
завершился, контейнер выглядел здоровым, и бот молчал 36 часов.
"""

import asyncio
import logging
import sys
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from bot.app import build
from collectors.telegram_v2 import TelegramMonitor
from core import delivery, pipeline, settings
from core.sources import channels_for
from core.store import Store
from db.migrate import migrate

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)])
# httpx2 — так называет свой логгер клиент anthropic; без него в логи
# сыплется по строке на каждый вызов модели.
for noisy in ("telethon", "aiogram", "httpx", "httpx2", "httpcore",
              "apscheduler", "anthropic"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("dira")


async def wanted_channels(store: Store) -> list:
    """Читаем только те каналы, чей регион кому-то нужен.

    То же правило, что для городов Yad2: не сканируем то, что никто не
    рассматривает. Города берутся из профилей, а не из константы в конфиге."""
    cities = set()
    for profile in await store.active_profiles():
        cities.update(profile.get("cities") or [])
    return channels_for(cities)


async def main():
    log.info("Dira Bot v2 запускается")
    version = migrate(settings.DB_PATH, verbose=False)
    store = await Store(settings.DB_PATH).connect()
    log.info("база %s, версия схемы %s", settings.DB_PATH, version)

    bot, dp = build(settings.BOT_TOKEN, store)

    client = None
    if settings.ANTHROPIC_API_KEY:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    else:
        log.warning("ANTHROPIC_API_KEY не задан — работаем только на регулярках")

    channels = await wanted_channels(store)
    log.info("каналы: %s", ", ".join(channels) or "нет активных профилей")
    monitor = TelegramMonitor(channels)

    async def on_listing(raw: dict):
        result = await pipeline.process(store, raw)
        if result["status"] == "new" and result["matches"]:
            log.info("новое объявление %s → совпадений: %d",
                     result["listing_id"][:10], result["matches"])
        elif result["status"] == "repost" and result.get("price_changed"):
            log.info("повтор %s: цена %s → %s", result["listing_id"][:10],
                     result.get("old_price"), result.get("new_price"))

    await monitor.start(on_listing)

    scheduler = AsyncIOScheduler(timezone=settings.TIMEZONE)

    async def job_enrich():
        if client is None:
            return
        result = await pipeline.enrich_pending(store, client, settings.CLAUDE_MODEL,
                                               limit=settings.ENRICH_BATCH)
        if result["enriched"] or result["failed"]:
            log.info("дозаполнено %d, сбоев %d, потрачено $%s",
                     result["enriched"], result["failed"], result["cost_usd"])

    async def job_retry():
        if client is None:
            return
        result = await pipeline.retry_pending(store, client, settings.CLAUDE_MODEL)
        if result.get("retried"):
            log.info("повторная обработка: %s", result)

    async def job_deliver():
        stats = await delivery.deliver_all(bot, store)
        if stats["realtime"] or stats["digest"]:
            log.info("отправлено: realtime %d, дайджест %d",
                     stats["realtime"], stats["digest"])

    async def job_healthcheck():
        # Пинг гасится, если Telethon нездоров: тогда healthchecks.io сам
        # пришлёт алерт. Молчание бота должно быть заметно снаружи.
        ok, reason = monitor.is_healthy()
        if not ok:
            log.warning("healthcheck пропущен — telegram нездоров: %s", reason)
            return
        if not settings.HEALTHCHECK_URL:
            return
        import httpx
        try:
            async with httpx.AsyncClient(timeout=10) as http:
                await http.get(settings.HEALTHCHECK_URL)
        except Exception as e:                        # noqa: BLE001
            log.warning("healthcheck не отправился: %s", e)

    # first_delay — через сколько секунд после старта задача выполнится впервые.
    # Без этого интервальная задача ждёт полный период: после каждого рестарта
    # факты десять минут лежали бы недозаполненными, а выдача стояла бы пять.
    # misfire_grace_time=None — задача никогда не считается просроченной; в v1
    # старт «сейчас» с грейсом по умолчанию молча пропускал первый час.
    now = datetime.now()
    for func, minutes, job_id, first_delay in (
        (job_enrich, settings.ENRICH_INTERVAL_MIN, "enrich", 90),
        (job_deliver, settings.DELIVERY_INTERVAL_MIN, "deliver", 150),
        (job_retry, settings.RETRY_INTERVAL_MIN, "retry", 600),
        (job_healthcheck, settings.HEALTHCHECK_INTERVAL_MIN, "healthcheck", 30),
    ):
        scheduler.add_job(func, IntervalTrigger(minutes=minutes), id=job_id,
                          replace_existing=True, misfire_grace_time=None,
                          next_run_time=now + timedelta(seconds=first_delay))
    scheduler.start()
    log.info("планировщик запущен")

    async def run_backfill():
        count = await monitor.backfill(days=settings.BACKFILL_DAYS)
        log.info("backfill: %d сообщений обработано", count)

    asyncio.create_task(run_backfill())

    try:
        await dp.start_polling(bot, handle_signals=False)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        log.info("остановка")
        scheduler.shutdown(wait=False)
        await monitor.stop()
        await bot.session.close()
        await store.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
