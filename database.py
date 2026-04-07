"""Dira Bot — async SQLite database (schema + CRUD).

Uses aiosqlite for non-blocking access from the async event loop.
"""

import aiosqlite
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ── helpers ───────────────────────────────────────────────────────────────────

def make_id(url: str | None, text: str) -> str:
    """Stable listing ID from URL (preferred) or text fingerprint."""
    if url and url.strip() not in ("", "nan", "None"):
        base = url.split("?")[0].rstrip("/")
        return hashlib.sha256(base.encode()).hexdigest()[:20]
    return hashlib.sha256(text[:500].encode()).hexdigest()[:20]


def make_fingerprint(text: str) -> str:
    """Cross-source dedup fingerprint: lowercase, no whitespace/punctuation, first 400 chars."""
    cleaned = re.sub(r"https?://\S+", "", text)
    cleaned = re.sub(r"[\s\W]+", "", cleaned)
    return cleaned[:400].lower()


# ── Database class ────────────────────────────────────────────────────────────

class Database:
    def __init__(self, db_path: str):
        self._path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def connect(self):
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._create_tables()

    async def close(self):
        if self._db:
            await self._db.close()

    # ── schema ────────────────────────────────────────────────────────────────

    async def _create_tables(self):
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS listings (
                id            TEXT PRIMARY KEY,
                source        TEXT NOT NULL,          -- yad2 | facebook | telegram
                source_id     TEXT,                   -- original ID in source
                raw_text      TEXT NOT NULL,
                url           TEXT,
                price         INTEGER,
                rooms         REAL,
                address       TEXT,
                city          TEXT,
                fingerprint   TEXT,
                collected_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_listings_source
                ON listings(source);
            CREATE INDEX IF NOT EXISTS idx_listings_fingerprint
                ON listings(fingerprint);
            CREATE INDEX IF NOT EXISTS idx_listings_collected
                ON listings(collected_at);

            CREATE TABLE IF NOT EXISTS analyses (
                listing_id      TEXT PRIMARY KEY REFERENCES listings(id),
                score           INTEGER,
                recommendation  TEXT,               -- SEND | MAYBE | SKIP
                has_mamad       INTEGER,            -- 1/0/NULL
                furnished       TEXT,               -- full | partial | empty | unknown
                near_station    INTEGER,            -- 1/0/NULL
                pros            TEXT,               -- JSON array
                issues          TEXT,               -- JSON array
                summary         TEXT,
                analyzed_at     TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS feedback (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_id  TEXT NOT NULL REFERENCES listings(id),
                rating      TEXT NOT NULL,           -- like | dislike | contact | irrelevant
                comment     TEXT,
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_feedback_listing
                ON feedback(listing_id);

            CREATE TABLE IF NOT EXISTS preferences (
                key           TEXT PRIMARY KEY,
                value         TEXT,
                confidence    REAL DEFAULT 0.5,
                learned_from  TEXT,                  -- JSON array of listing IDs
                updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
            );
        """)
        await self._db.commit()

    # ── listings CRUD ─────────────────────────────────────────────────────────

    async def listing_exists(self, listing_id: str) -> bool:
        cur = await self._db.execute(
            "SELECT 1 FROM listings WHERE id = ?", (listing_id,)
        )
        return (await cur.fetchone()) is not None

    async def fingerprint_exists(self, fp: str) -> bool:
        cur = await self._db.execute(
            "SELECT 1 FROM listings WHERE fingerprint = ?", (fp,)
        )
        return (await cur.fetchone()) is not None

    async def add_listing(
        self,
        listing_id: str,
        source: str,
        raw_text: str,
        url: str = None,
        source_id: str = None,
        price: int = None,
        rooms: float = None,
        address: str = None,
        city: str = None,
        fingerprint: str = None,
    ) -> bool:
        """Insert listing. Returns True if new, False if duplicate."""
        fp = fingerprint or make_fingerprint(raw_text)

        # Check both ID and fingerprint for dedup
        if await self.listing_exists(listing_id):
            return False
        if await self.fingerprint_exists(fp):
            return False

        await self._db.execute(
            """INSERT INTO listings
               (id, source, source_id, raw_text, url, price, rooms, address, city, fingerprint)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (listing_id, source, source_id, raw_text, url, price, rooms, address, city, fp),
        )
        await self._db.commit()
        return True

    async def get_listing(self, listing_id: str) -> Optional[dict]:
        cur = await self._db.execute(
            "SELECT * FROM listings WHERE id = ?", (listing_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    # ── analyses CRUD ─────────────────────────────────────────────────────────

    async def save_analysis(self, listing_id: str, analysis: dict):
        await self._db.execute(
            """INSERT OR REPLACE INTO analyses
               (listing_id, score, recommendation, has_mamad, furnished,
                near_station, pros, issues, summary)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                listing_id,
                analysis.get("score"),
                analysis.get("recommendation"),
                analysis.get("has_mamad"),
                analysis.get("furnished"),
                analysis.get("near_station"),
                json.dumps(analysis.get("pros", []), ensure_ascii=False),
                json.dumps(analysis.get("issues", []), ensure_ascii=False),
                analysis.get("summary", ""),
            ),
        )
        await self._db.commit()

    async def get_analysis(self, listing_id: str) -> Optional[dict]:
        cur = await self._db.execute(
            "SELECT * FROM analyses WHERE listing_id = ?", (listing_id,)
        )
        row = await cur.fetchone()
        if not row:
            return None
        d = dict(row)
        d["pros"] = json.loads(d.get("pros") or "[]")
        d["issues"] = json.loads(d.get("issues") or "[]")
        return d

    # ── feedback CRUD ─────────────────────────────────────────────────────────

    async def save_feedback(self, listing_id: str, rating: str, comment: str = None):
        await self._db.execute(
            "INSERT INTO feedback (listing_id, rating, comment) VALUES (?, ?, ?)",
            (listing_id, rating, comment),
        )
        await self._db.commit()

    async def get_all_feedback(self) -> list[dict]:
        cur = await self._db.execute(
            """SELECT f.*, l.raw_text, a.score, a.summary
               FROM feedback f
               JOIN listings l ON l.id = f.listing_id
               LEFT JOIN analyses a ON a.listing_id = f.listing_id
               ORDER BY f.created_at DESC"""
        )
        return [dict(r) for r in await cur.fetchall()]

    # ── top listings ──────────────────────────────────────────────────────────

    async def get_top_listings(self, limit: int = 5, days: int = 7) -> list[dict]:
        cur = await self._db.execute(
            """SELECT l.*, a.*
               FROM listings l
               JOIN analyses a ON a.listing_id = l.id
               WHERE a.recommendation IN ('SEND', 'MAYBE')
                 AND l.collected_at >= datetime('now', ?)
               ORDER BY a.score DESC, l.collected_at DESC
               LIMIT ?""",
            (f"-{days} days", limit),
        )
        rows = [dict(r) for r in await cur.fetchall()]
        for r in rows:
            r["pros"] = json.loads(r.get("pros") or "[]")
            r["issues"] = json.loads(r.get("issues") or "[]")
        return rows

    # ── stats ─────────────────────────────────────────────────────────────────

    async def get_stats(self) -> dict:
        total = (await (await self._db.execute("SELECT COUNT(*) FROM listings")).fetchone())[0]
        by_source = {}
        cur = await self._db.execute(
            "SELECT source, COUNT(*) as cnt FROM listings GROUP BY source"
        )
        for row in await cur.fetchall():
            by_source[row["source"]] = row["cnt"]

        analyzed = (await (await self._db.execute("SELECT COUNT(*) FROM analyses")).fetchone())[0]
        sent = (await (await self._db.execute(
            "SELECT COUNT(*) FROM analyses WHERE recommendation = 'SEND'"
        )).fetchone())[0]
        maybe = (await (await self._db.execute(
            "SELECT COUNT(*) FROM analyses WHERE recommendation = 'MAYBE'"
        )).fetchone())[0]
        feedbacks = (await (await self._db.execute("SELECT COUNT(*) FROM feedback")).fetchone())[0]

        return {
            "total_listings": total,
            "by_source": by_source,
            "analyzed": analyzed,
            "sent": sent,
            "maybe": maybe,
            "feedbacks": feedbacks,
        }

    # ── preferences ───────────────────────────────────────────────────────────

    async def save_preference(self, key: str, value: str, confidence: float, learned_from: list):
        await self._db.execute(
            """INSERT OR REPLACE INTO preferences (key, value, confidence, learned_from)
               VALUES (?, ?, ?, ?)""",
            (key, value, confidence, json.dumps(learned_from)),
        )
        await self._db.commit()

    async def get_preferences(self) -> list[dict]:
        cur = await self._db.execute("SELECT * FROM preferences ORDER BY confidence DESC")
        return [dict(r) for r in await cur.fetchall()]
