"""Доступ к базе. Асинхронный, поверх aiosqlite, без ORM.

Один принцип на весь модуль: **всё, что отдаётся наружу, уже разделено по
пользователям**. В v1 бот был на одного человека, и «показать топ» означало
«показать топ». Здесь любой запрос за объявлениями идёт через профиль, а
действия и статусы никогда не смешиваются между пользователями.
"""

import json
from typing import Any, Optional

import aiosqlite

# Списки и словари в SQLite лежат строками JSON. Разворачиваем на границе,
# чтобы дальше по коду с ними работали как с обычными списками.
_JSON_FIELDS = {"cities", "districts", "stop_words", "phones", "media",
                "reasons", "payload", "onboarding_data"}


def _row(row: Optional[aiosqlite.Row]) -> Optional[dict]:
    if row is None:
        return None
    d = dict(row)
    for key in _JSON_FIELDS & d.keys():
        if isinstance(d[key], str):
            try:
                d[key] = json.loads(d[key])
            except (ValueError, TypeError):
                pass
    return d


def _rows(rows) -> list:
    return [_row(r) for r in rows]


def _presence_sql() -> str:
    """Источники, у которых свежесть определяется присутствием в выдаче."""
    from core.sources import PRESENCE_SOURCES
    return ",".join(f"'{name}'" for name in PRESENCE_SOURCES) or "''"


def _dump(value: Any) -> Any:
    return json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value


class Store:
    def __init__(self, path: str):
        self._path = path
        self._db: Optional[aiosqlite.Connection] = None

    async def connect(self):
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        try:
            await self._db.execute("PRAGMA journal_mode=WAL")
        except aiosqlite.OperationalError:
            await self._db.execute("PRAGMA journal_mode=DELETE")
        await self._db.execute("PRAGMA foreign_keys=ON")
        return self

    async def close(self):
        if self._db:
            await self._db.close()

    # ── пользователи ─────────────────────────────────────────────────────────

    async def ensure_user(self, telegram_id: int, username: str = "", first_name: str = "") -> dict:
        """Зарегистрировать при первом обращении, иначе обновить отметку активности.

        Открытая регистрация: любой, кто написал боту, заводится сам. Закрывать
        доступ можно флагом is_active, а не белым списком в конфиге."""
        await self._db.execute(
            "INSERT INTO users (telegram_id, username, first_name, last_seen_at)"
            " VALUES (?,?,?,datetime('now'))"
            " ON CONFLICT(telegram_id) DO UPDATE SET"
            "   username=excluded.username, last_seen_at=datetime('now')",
            (telegram_id, username, first_name))
        await self._db.commit()
        cur = await self._db.execute("SELECT * FROM users WHERE telegram_id=?", (telegram_id,))
        return _row(await cur.fetchone())

    async def admins(self) -> list:
        """Кому идут служебные сообщения: о нехватке памяти, о сбоях сбора.

        Обычным пользователям это знать незачем — их волнует лента, а не то,
        сколько на сервере свободно."""
        cur = await self._db.execute(
            "SELECT telegram_id FROM users WHERE is_admin=1 AND is_active=1")
        return [r[0] for r in await cur.fetchall()]

    async def get_user(self, telegram_id: int) -> Optional[dict]:
        cur = await self._db.execute("SELECT * FROM users WHERE telegram_id=?", (telegram_id,))
        return _row(await cur.fetchone())

    async def set_user(self, telegram_id: int, **fields):
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        await self._db.execute(f"UPDATE users SET {sets} WHERE telegram_id=?",
                               [_dump(v) for v in fields.values()] + [telegram_id])
        await self._db.commit()

    # ── профили ──────────────────────────────────────────────────────────────

    async def create_profile(self, user_id: int, name: str = "Основной", **fields) -> int:
        cols = ["user_id", "name", *fields]
        vals = [user_id, name, *[_dump(v) for v in fields.values()]]
        cur = await self._db.execute(
            f"INSERT INTO search_profiles ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
        await self._db.commit()
        return cur.lastrowid

    async def get_profile(self, profile_id: int) -> Optional[dict]:
        cur = await self._db.execute("SELECT * FROM search_profiles WHERE id=?", (profile_id,))
        return _row(await cur.fetchone())

    async def profiles_of(self, user_id: int) -> list:
        cur = await self._db.execute(
            "SELECT * FROM search_profiles WHERE user_id=? ORDER BY id", (user_id,))
        return _rows(await cur.fetchall())

    async def active_profiles(self) -> list:
        """Все профили, которым сейчас положено получать объявления."""
        cur = await self._db.execute(
            "SELECT p.* FROM search_profiles p JOIN users u ON u.telegram_id = p.user_id"
            " WHERE p.is_enabled = 1 AND p.is_paused = 0 AND u.is_active = 1")
        return _rows(await cur.fetchall())

    async def update_profile(self, profile_id: int, **fields):
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        await self._db.execute(
            f"UPDATE search_profiles SET {sets}, updated_at=datetime('now') WHERE id=?",
            [_dump(v) for v in fields.values()] + [profile_id])
        await self._db.commit()

    # ── объявления и факты ───────────────────────────────────────────────────

    async def listing_exists(self, listing_id: str) -> bool:
        cur = await self._db.execute("SELECT 1 FROM listings WHERE id=?", (listing_id,))
        return await cur.fetchone() is not None

    async def find_by_fingerprint(self, fingerprint: str) -> Optional[dict]:
        cur = await self._db.execute(
            "SELECT * FROM listings WHERE fingerprint=? ORDER BY collected_at DESC LIMIT 1",
            (fingerprint,))
        return _row(await cur.fetchone())

    async def add_listing(self, **fields) -> bool:
        cols = list(fields)
        try:
            await self._db.execute(
                f"INSERT INTO listings ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                [_dump(v) for v in fields.values()])
        except aiosqlite.IntegrityError:
            return False
        await self._db.commit()
        return True

    async def mark_seen(self, source: str, source_ids: list) -> dict:
        """Отметить, какие объявления источника есть в свежем скане.

        Увиденные обнуляют счётчик промахов, невидимые его увеличивают. После
        трёх промахов подряд объявление перестаёт попадать в выдачу — но
        остаётся в базе: история цен и медианы по району от этого выигрывают.
        """
        if not source_ids:
            return {"seen": 0, "missed": 0}
        placeholders = ",".join("?" * len(source_ids))
        await self._db.execute(
            f"UPDATE listings SET last_seen_at = datetime('now'), missed_scans = 0"
            f" WHERE source = ? AND source_id IN ({placeholders})",
            (source, *source_ids))
        cur = await self._db.execute(
            f"UPDATE listings SET missed_scans = missed_scans + 1"
            f" WHERE source = ? AND source_id NOT IN ({placeholders})",
            (source, *source_ids))
        await self._db.commit()
        gone = await self._db.execute(
            "SELECT COUNT(*) FROM listings WHERE source = ? AND missed_scans >= ?",
            (source, 3))
        return {"seen": len(source_ids), "hidden": (await gone.fetchone())[0]}

    async def set_status(self, listing_id: str, status: str, error: str = None):
        """Пометить состояние обработки.

        `pending` при сбое, а не `failed`: объявление должно вернуться в очередь.
        В v1 ошибка модели записывалась как результат анализа, и объявление
        терялось навсегда (F-11)."""
        await self._db.execute(
            "UPDATE listings SET status=?, last_error=?, attempts=attempts+1 WHERE id=?",
            (status, error, listing_id))
        await self._db.commit()

    async def pending_listings(self, limit: int = 50, max_attempts: int = 5) -> list:
        """Что нужно (пере)обработать: новые и те, что сорвались."""
        cur = await self._db.execute(
            "SELECT * FROM listings WHERE status IN ('new','pending') AND attempts < ?"
            " ORDER BY collected_at LIMIT ?", (max_attempts, limit))
        return _rows(await cur.fetchall())

    async def save_facts(self, listing_id: str, facts: dict):
        facts = {k: v for k, v in facts.items() if k != "listing_id"}
        cols = ["listing_id", *facts]
        await self._db.execute(
            f"INSERT OR REPLACE INTO listing_facts ({','.join(cols)})"
            f" VALUES ({','.join('?' * len(cols))})",
            [listing_id] + [_dump(v) for v in facts.values()])
        await self._db.commit()

    async def get_facts(self, listing_id: str) -> Optional[dict]:
        cur = await self._db.execute(
            "SELECT l.url, l.channel, l.source, l.raw_text, l.media, f.*"
            " FROM listings l LEFT JOIN listing_facts f ON f.listing_id = l.id"
            " WHERE l.id=?", (listing_id,))
        return _row(await cur.fetchone())

    async def add_price(self, listing_id: str, price: int, source: str = None):
        await self._db.execute(
            "INSERT INTO price_history (listing_id, price, source) VALUES (?,?,?)",
            (listing_id, price, source))
        await self._db.commit()

    async def price_history(self, listing_id: str) -> list:
        cur = await self._db.execute(
            "SELECT price, seen_at FROM price_history WHERE listing_id=? ORDER BY seen_at",
            (listing_id,))
        return _rows(await cur.fetchall())

    # ── совпадения ───────────────────────────────────────────────────────────

    async def add_match(self, profile_id: int, listing_id: str, rank: float, reasons: list) -> bool:
        try:
            await self._db.execute(
                "INSERT INTO matches (profile_id, listing_id, rank, reasons) VALUES (?,?,?,?)",
                (profile_id, listing_id, rank, json.dumps(reasons, ensure_ascii=False)))
        except aiosqlite.IntegrityError:
            return False
        await self._db.commit()
        return True

    async def queue_for(self, profile_id: int, limit: int = 50) -> list:
        """Что ещё не отправлено этому профилю, по убыванию ранга."""
        cur = await self._db.execute(
            "SELECT m.rank, m.reasons, l.*, f.* FROM matches m"
            " JOIN listings l ON l.id = m.listing_id"
            " LEFT JOIN listing_facts f ON f.listing_id = m.listing_id"
            " WHERE m.profile_id=? AND m.state='new'"
            f"   AND ((l.source IN ({_presence_sql()}) AND l.missed_scans < 3)"
            f"     OR (l.source NOT IN ({_presence_sql()})"
            f"         AND COALESCE(l.posted_at, l.collected_at) >="
            f"             date('now', '-{self.MAX_AGE_DAYS} days')))"
            " ORDER BY m.rank DESC LIMIT ?",
            (profile_id, limit))
        return _rows(await cur.fetchall())

    # `new` входит в ленту наравне с отправленным: человек спрашивает «что
    # нашлось», а не «что мне уже прислали». Без этого /feed показывал «пока
    # пусто» при 129 подобранных объявлениях, ждущих утреннего дайджеста —
    # первое, что вылезло при живой проверке бота.
    FEED_STATES = ("new", "sent", "saved", "contacted", "waiting", "visit")

    # Объявления старше этого срока в ленту не идут. Нужно прежде всего новому
    # пользователю: без отсечения его первая лента — это сотни объявлений, из
    # которых половина снята месяц назад. В базе нашлось объявление от августа
    # прошлого года. Считаем от даты публикации, а где её нет — от момента,
    # когда мы объявление увидели.
    MAX_AGE_DAYS = 7

    async def feed(self, profile_id: int, order: str = "rank", limit: int = 5,
                   offset: int = 0, states: tuple = FEED_STATES,
                   flt: str = "all") -> list:
        """Лента с сортировкой и быстрым фильтром.

        Фильтры намеренно простые и их три: они должны отвечать на вопросы,
        которые возникают при листании («а где с мамадом?»), а не заменять
        настройку профиля."""
        orders = {
            "rank": "m.rank DESC",
            "price": "f.price IS NULL, f.price ASC",
            "fresh": "l.collected_at DESC",
            "rooms": "f.rooms IS NULL, f.rooms DESC",
            "sqm_price": "f.price IS NULL OR f.area_sqm IS NULL, "
                         "CAST(f.price AS REAL) / NULLIF(f.area_sqm, 0) ASC",
        }
        # Свежесть считается по-разному: где доску видно целиком, объявление
        # живо, пока оно в выдаче; где нет — по дате (решение 0005).
        from core.sources import MISSED_SCANS_TO_HIDE, PRESENCE_SOURCES
        presence = ",".join(f"'{s}'" for s in PRESENCE_SOURCES) or "''"
        age = (f" AND ((l.source IN ({presence}) AND l.missed_scans < {MISSED_SCANS_TO_HIDE})"
               f"   OR (l.source NOT IN ({presence})"
               f"       AND COALESCE(l.posted_at, l.collected_at) >= "
               f"           date('now', '-{self.MAX_AGE_DAYS} days')))")
        filters = {
            "all": "",
            "mamad": " AND (f.mamad = 'yes' OR f.mamad_evidence IS NOT NULL)",
            "cheap": " AND m.rank > 2.5",
            "photo": " AND l.media IS NOT NULL AND l.media <> '[]'",
        }
        placeholders = ",".join("?" * len(states))
        cur = await self._db.execute(
            f"SELECT m.rank, m.state, l.*, f.* FROM matches m"
            f" JOIN listings l ON l.id = m.listing_id"
            f" LEFT JOIN listing_facts f ON f.listing_id = m.listing_id"
            f" WHERE m.profile_id=? AND m.state IN ({placeholders})"
            f"{age}{filters.get(flt, '')}"
            f" ORDER BY {orders.get(order, orders['rank'])} LIMIT ? OFFSET ?",
            (profile_id, *states, limit, offset))
        return _rows(await cur.fetchall())

    async def set_match_state(self, profile_id: int, listing_id: str, state: str):
        await self._db.execute(
            "UPDATE matches SET state=?, state_at=datetime('now')"
            " WHERE profile_id=? AND listing_id=?", (state, profile_id, listing_id))
        await self._db.commit()

    async def mark_sent(self, profile_id: int, listing_ids: list):
        await self._db.executemany(
            "UPDATE matches SET state='sent', sent_at=datetime('now')"
            " WHERE profile_id=? AND listing_id=?",
            [(profile_id, lid) for lid in listing_ids])
        await self._db.commit()

    async def sent_today(self, profile_id: int) -> int:
        cur = await self._db.execute(
            "SELECT COUNT(*) FROM matches WHERE profile_id=? AND sent_at >= date('now')",
            (profile_id,))
        return (await cur.fetchone())[0]

    # ── действия и расход ────────────────────────────────────────────────────

    async def log_action(self, user_id: int, action: str, listing_id: str = None, payload: dict = None):
        await self._db.execute(
            "INSERT INTO user_actions (user_id, listing_id, action, payload) VALUES (?,?,?,?)",
            (user_id, listing_id, action, json.dumps(payload or {}, ensure_ascii=False)))
        await self._db.commit()

    async def log_llm(self, purpose: str, model: str, usage: dict, listing_id: str = None):
        await self._db.execute(
            "INSERT INTO llm_usage (purpose, model, listing_id, input_tokens,"
            " output_tokens, cost_usd) VALUES (?,?,?,?,?,?)",
            (purpose, model, listing_id, usage.get("input_tokens", 0),
             usage.get("output_tokens", 0), usage.get("cost_usd", 0)))
        await self._db.commit()

    async def spend(self, days: int = 30) -> dict:
        cur = await self._db.execute(
            "SELECT COUNT(*) calls, SUM(input_tokens) tin, SUM(output_tokens) tout,"
            " ROUND(SUM(cost_usd), 4) usd FROM llm_usage"
            " WHERE created_at >= datetime('now', ?)", (f"-{days} days",))
        return _row(await cur.fetchone()) or {}

    async def stats(self) -> dict:
        out = {}
        for key, sql in {
            "listings": "SELECT COUNT(*) FROM listings",
            "extracted": "SELECT COUNT(*) FROM listing_facts",
            "pending": "SELECT COUNT(*) FROM listings WHERE status IN ('new','pending')",
            "users": "SELECT COUNT(*) FROM users WHERE is_active=1",
            "profiles": "SELECT COUNT(*) FROM search_profiles WHERE is_enabled=1",
            "matches": "SELECT COUNT(*) FROM matches",
        }.items():
            cur = await self._db.execute(sql)
            out[key] = (await cur.fetchone())[0]
        return out
