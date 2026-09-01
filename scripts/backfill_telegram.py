"""Первичный сбор истории из Telegram-каналов в базу v2.

Нужен потому, что real-time мониторинг начинает видеть объявления только с
момента запуска: без backfill бот стартует с пустой лентой, а квартиры уходят
за часы. В v1 это уже было и работало, здесь то же самое, но результат кладётся
в новую схему с фактами.

Модель не вызывается: раскладываем детерминированным слоем, поля, которые он не
взял, остаются пустыми и дозаполняются позже.

    python3 scripts/backfill_telegram.py --days 3 --db data/dira.db
"""

import argparse
import asyncio
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.migrate import migrate                     # noqa: E402
from extract import extract                        # noqa: E402

CHANNELS = [
    "Israel_arenda", "jeremy_public", "jeremy_public_ramat_gan", "aptfornew",
    "snyat_kvartiruy", "isra_home_arenda", "flamingorent", "ambery_longrent_telaviv",
]

# Грубый фильтр «похоже на объявление» — тот же, что в v1: дешёвый способ не
# тащить в базу переписку и объявления о поиске работы.
RENT_WORDS = re.compile(
    r"להשכרה|שכירות|דירה|חדרים|חד'|аренда|сдам|сдаётся|сдается|снять|квартир|комнат|"
    r"for rent|apartment|flat|studio|rooms",
    re.IGNORECASE)
EXCLUDE_WORDS = re.compile(
    r"למכירה|for sale|продаётся|продается|продам|вакансия|работа\s+в|ищу\s+работу",
    re.IGNORECASE)

BOOL_COLS = ("mamad", "miklat", "elevator", "balcony", "parking", "storage",
             "air_conditioning", "pets_allowed", "garden", "renovated",
             "immediate_entry", "no_broker")
FACT_COLS = ("price", "rooms", "area_sqm", "floor", "total_floors", "city",
             "district", "street", "entry_date", "lease_months", "deal_type",
             "furnished", "mamad_evidence", *BOOL_COLS)


def looks_like_listing(text: str) -> bool:
    if not text or len(text) < 40:
        return False
    if EXCLUDE_WORDS.search(text):
        return False
    return bool(RENT_WORDS.search(text))


def load_env(path=".env"):
    env = {}
    for line in open(path, encoding="utf-8-sig"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def listing_id(url: str, text: str) -> str:
    base = (url or "").split("?")[0].rstrip("/") or text[:500]
    return hashlib.sha256(base.encode()).hexdigest()[:20]


def store(conn, lid, channel, url, text, posted_at, facts) -> bool:
    """Вернуть True, если объявление новое."""
    d = facts.as_dict()
    try:
        conn.execute(
            "INSERT INTO listings (id, source, source_id, channel, url, raw_text,"
            " fingerprint, posted_at, status) VALUES (?,?,?,?,?,?,?,?,'extracted')",
            (lid, "telegram", url, channel, url, text, facts.fingerprint, posted_at))
    except sqlite3.IntegrityError:
        return False
    cols = ["listing_id", *FACT_COLS, "phones", "source_layer"]
    vals = [lid] + [d.get(c) for c in FACT_COLS] + [json.dumps(d.get("phones") or []), "rules"]
    conn.execute(f"INSERT INTO listing_facts ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
    if facts.price:
        conn.execute("INSERT INTO price_history (listing_id, price, source) VALUES (?,?,'telegram')",
                     (lid, facts.price))
    return True


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--db", default="data/dira.db")
    ap.add_argument("--limit", type=int, default=400, help="сообщений на канал")
    args = ap.parse_args()

    from telethon import TelegramClient
    from telethon.sessions import StringSession

    env = load_env()
    version = migrate(args.db, verbose=False)
    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA foreign_keys=ON")
    print(f"база {args.db}, версия схемы {version}")

    client = TelegramClient(StringSession(env["TELEGRAM_SESSION_STRING"]),
                            int(env["TELEGRAM_API_ID"]), env["TELEGRAM_API_HASH"])
    await client.connect()
    if not await client.is_user_authorized():
        print("сессия не авторизована"); return

    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=args.days)
    total_seen = total_new = 0
    for name in CHANNELS:
        seen = new = 0
        try:
            entity = await client.get_entity(name)
            async for msg in client.iter_messages(entity, limit=args.limit):
                if msg.date < cutoff:
                    break
                text = msg.text or ""
                if not looks_like_listing(text):
                    continue
                seen += 1
                url = f"https://t.me/{name}/{msg.id}"
                if store(conn, listing_id(url, text), name, url, text,
                         msg.date.isoformat(), extract(text)):
                    new += 1
            conn.commit()
        except Exception as e:                      # noqa: BLE001
            print(f"  @{name}: ошибка {type(e).__name__}: {e}")
            continue
        print(f"  @{name:<26} объявлений {seen:>4}, новых {new:>4}")
        total_seen += seen
        total_new += new

    print(f"\nвсего: {total_seen} похожих на объявления, {total_new} новых в базе")
    await client.disconnect()
    conn.close()


if __name__ == "__main__":
    asyncio.run(main())
