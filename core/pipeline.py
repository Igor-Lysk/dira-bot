"""Обработка объявления: от сырого текста до совпадений по профилям.

Порядок шагов выбран так, чтобы дорогое стояло последним, а необратимое —
после проверок:

    дедуп → сохранить → правила → сопоставить → (модель только если нужно)

Про повторные публикации. В v1 повтор просто выбрасывался как дубликат, и вместе
с ним терялась информация о том, что квартира висит уже третью неделю, а цена
упала. Здесь повтор обновляет запись и возвращает её в выдачу с пометкой —
падающая цена это прямой сигнал к торгу (решение D9).

Про сбои. Ошибка модели не считается результатом: объявление получает статус
`pending` и вернётся в очередь. В v1 ошибка записывалась как «проанализировано,
score 0», и объявление терялось навсегда (F-11).
"""

import hashlib
import logging
import re
from typing import Optional

from core.match import match
from core.sources import region_of
from core.store import Store
from extract import extract
from extract.schema import BOOL_FIELDS, VALUE_FIELDS

log = logging.getLogger(__name__)

FACT_COLUMNS = [*VALUE_FIELDS, *BOOL_FIELDS, "mamad_evidence", "commission",
                "price_includes_bills", "contact_type"]

# Насколько назад ищем прежнюю публикацию той же квартиры.
REPOST_WINDOW_DAYS = 45

# Насколько тексты должны совпадать, чтобы считать объявления одной квартирой.
# Мера — доля общих слов (Жаккар). Перепост с переписанным заголовком сохраняет
# основную часть описания, а два разных объекта одного маклера — нет.
REPOST_TEXT_SIMILARITY = 0.45


def _tokens(text: str) -> set:
    return {w for w in re.findall(r"[\w\u0590-\u05ff]{3,}", (text or "").lower())}


def text_similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def listing_id(url: Optional[str], text: str) -> str:
    base = (url or "").split("?")[0].rstrip("/") or text[:500]
    return hashlib.sha256(base.encode()).hexdigest()[:20]


def facts_to_row(facts) -> dict:
    d = facts.as_dict()
    row = {k: d.get(k) for k in FACT_COLUMNS if k in d}
    row["phones"] = d.get("phones") or []
    row["source_layer"] = d.get("source_layer", "rules")
    row["schema_version"] = d.get("schema_version", 1)
    return row


async def _find_repost(store: Store, facts, fingerprint: str, raw_text: str = "") -> Optional[dict]:
    """Та же квартира под другим сообщением.

    Сначала по отпечатку текста — ловит точные перепечатки. Затем по связке
    «телефон + количество комнат», которая переживает переписанный заголовок:
    именно на нём ломался отпечаток v1, где бралось только начало текста.
    """
    same_text = await store.find_by_fingerprint(fingerprint)
    if same_text:
        return same_text
    if not facts.phones or facts.rooms is None:
        return None
    placeholders = ",".join("?" * len(facts.phones))
    cur = await store._db.execute(
        f"SELECT l.*, f.price AS prev_price, f.street AS prev_street"
        f" FROM listings l JOIN listing_facts f ON f.listing_id = l.id"
        f" WHERE f.rooms = ? AND l.collected_at >= datetime('now', ?)"
        f"   AND EXISTS (SELECT 1 FROM json_each(f.phones) WHERE value IN ({placeholders}))"
        f" ORDER BY l.collected_at DESC LIMIT 5",
        (facts.rooms, f"-{REPOST_WINDOW_DAYS} days", *facts.phones))
    for row in await cur.fetchall():
        row = dict(row)
        # Телефона и числа комнат мало: у агента десятки трёхкомнатных квартир с
        # одним номером. Живой прогон сразу дал ложную склейку 3800 ₪ и 7499 ₪ —
        # это два разных объекта одного маклера. Поэтому нужен ещё один признак:
        # та же улица либо цена в пределах 20%.
        same_street = bool(facts.street and row.get("prev_street")
                           and facts.street == row["prev_street"])
        old, new = row.get("prev_price"), facts.price
        close_price = bool(old and new and abs(old - new) <= 0.2 * max(old, new))
        if not (same_street or close_price):
            continue
        # Последняя проверка — сам текст. Цена и число комнат совпадают у многих
        # разных квартир одного маклера; описание — нет.
        if text_similarity(raw_text, row.get("raw_text") or "") >= REPOST_TEXT_SIMILARITY:
            return row
    return None


async def process(store: Store, raw: dict) -> dict:
    """Обработать одно сырое объявление. Возвращает, что с ним стало."""
    text = raw.get("raw_text") or ""
    url = raw.get("url")
    lid = listing_id(url, text)

    if await store.listing_exists(lid):
        return {"status": "duplicate", "listing_id": lid}

    facts = extract(text)

    # город из метаданных канала, если в тексте его нет
    if facts.city is None:
        hint = region_of(raw.get("channel"))
        if hint:
            facts.city = hint

    repost = await _find_repost(store, facts, facts.fingerprint or "", text)
    if repost:
        return await _handle_repost(store, repost, facts, raw, lid)

    await store.add_listing(
        id=lid, source=raw.get("source", "telegram"), source_id=raw.get("source_id"),
        channel=raw.get("channel"), url=url, raw_text=text, media=raw.get("media") or [],
        fingerprint=facts.fingerprint, posted_at=raw.get("posted_at"), status="extracted")
    await store.save_facts(lid, facts_to_row(facts))
    if facts.price:
        await store.add_price(lid, facts.price, raw.get("source"))

    created = await match_listing(store, lid)
    return {"status": "new", "listing_id": lid, "matches": created}


async def _handle_repost(store: Store, previous: dict, facts, raw: dict, new_id: str) -> dict:
    """Повтор: обновляем прежнюю запись, а не заводим новую."""
    old_id = previous["id"]
    old = await store.get_facts(old_id)
    old_price = old.get("price") if old else None

    await store.save_facts(old_id, facts_to_row(facts))
    await store._db.execute(
        "UPDATE listings SET url=?, raw_text=?, collected_at=datetime('now'),"
        " status='extracted' WHERE id=?",
        (raw.get("url") or previous.get("url"), raw.get("raw_text"), old_id))
    await store._db.commit()

    changed = False
    if facts.price and facts.price != old_price:
        await store.add_price(old_id, facts.price, raw.get("source"))
        changed = True

    # Возвращаем в выдачу: изменившаяся цена — новость, повтор без изменений — тоже
    # сигнал (квартира всё ещё свободна), но менее срочный.
    await store._db.execute(
        "UPDATE matches SET state='new' WHERE listing_id=? AND state='sent'", (old_id,))
    await store._db.commit()
    await match_listing(store, old_id)
    return {"status": "repost", "listing_id": old_id, "price_changed": changed,
            "old_price": old_price, "new_price": facts.price}


async def match_listing(store: Store, lid: str) -> int:
    """Прогнать объявление по всем активным профилям."""
    facts = await store.get_facts(lid)
    if not facts:
        return 0
    created = 0
    for profile in await store.active_profiles():
        result = match(facts, profile)
        if result.matched:
            if await store.add_match(profile["id"], lid, result.rank, result.reasons):
                created += 1
    return created


async def enrich_pending(store: Store, client, model: str, limit: int = 25) -> dict:
    """Дозаполнить факты моделью и пересчитать совпадения.

    Идёт отдельным проходом, а не внутри `process`, по двум причинам: сбор не
    должен ждать сети, и при недоступности модели объявления просто копятся в
    очереди вместо того, чтобы теряться.
    """
    from extract.llm import fill_gaps
    from extract.schema import Facts

    cur = await store._db.execute(
        "SELECT l.id FROM listings l JOIN listing_facts f ON f.listing_id = l.id"
        " WHERE f.source_layer = 'rules' AND l.status = 'extracted'"
        " ORDER BY l.collected_at DESC LIMIT ?", (limit,))
    ids = [r[0] for r in await cur.fetchall()]

    done = failed = 0
    spent = 0.0
    for lid in ids:
        row = await store.get_facts(lid)
        facts = Facts()
        for name in FACT_COLUMNS:
            if hasattr(facts, name) and row.get(name) is not None:
                setattr(facts, name, row[name])
        facts, usage = await fill_gaps(row["raw_text"], facts, client, model)
        if usage.get("skipped"):
            await store.save_facts(lid, {**facts_to_row(facts), "source_layer": "mixed"})
            continue
        if not usage.get("ok"):
            failed += 1
            await store.set_status(lid, "pending", str(usage.get("error"))[:200])
            continue
        await store.log_llm("extract", model, usage, lid)
        spent += usage.get("cost_usd", 0)
        await store.save_facts(lid, facts_to_row(facts))
        await match_listing(store, lid)     # факты изменились — пересчитываем
        done += 1
    return {"enriched": done, "failed": failed, "cost_usd": round(spent, 4)}


async def retry_pending(store: Store, client, model: str, limit: int = 25) -> dict:
    """Вернуть в работу то, что сорвалось. Без этого прохода `pending` — тупик."""
    rows = await store.pending_listings(limit=limit)
    for row in rows:
        await store._db.execute("UPDATE listings SET status='extracted' WHERE id=?", (row["id"],))
    await store._db.commit()
    if not rows:
        return {"retried": 0}
    result = await enrich_pending(store, client, model, limit=len(rows))
    return {"retried": len(rows), **result}
