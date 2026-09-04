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

from core import market as market_mod
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
# Мера — доля общих слов (Жаккар).
#
# Порог поднят с 0.45 до 0.80 после живого случая: агент публикует квартиры по
# шаблону, и «Raziel 60» с «Raziel 39» — разные квартиры в соседних домах —
# совпали на 0.76. Их склеило в одну запись, и цена в истории заскакала
# 2600 → 2400 → 2600 при каждом скане. Настоящий перепост того же текста даёт
# 0.9 и выше, так что порог 0.8 их не теряет.
#
# Цена ошибки несимметрична: пропущенный перепост — это лишний дубль в ленте,
# а ложная склейка прячет квартиру и портит историю цен.
REPOST_TEXT_SIMILARITY = 0.80


def _tokens(text: str) -> set:
    return {w for w in re.findall(r"[\w\u0590-\u05ff]{3,}", (text or "").lower())}


def text_similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def listing_id(url: Optional[str], text: str, source_id: Optional[str] = None) -> str:
    """Устойчивый идентификатор объявления.

    Сначала — идентификатор из самого источника: он точен и не зависит от того,
    как выглядит ссылка. Это не мелочь: у Komo адрес объявления вида
    `/code/nadlan/details/?modaaNum=123`, и отбрасывание query-строки (нужное
    для Yad2, где в хвосте болтается разметка кампании) схлопывало все семьдесят
    семь объявлений в одно. Первый прогон дал 76 «дубликатов» из 77.
    """
    if source_id:
        return hashlib.sha256(source_id.encode()).hexdigest()[:20]
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

    Три ключа по убыванию надёжности: отпечаток текста ловит точные
    перепечатки; связка «телефон + комнаты» переживает переписанный заголовок,
    на котором ломался отпечаток v1; связка «улица + цена + комнаты» ловит
    случай, когда одну квартиру публикуют разные маклеры со своими номерами.
    Два последних ключа подтверждаются похожестью текста — без неё у агента с
    десятком трёхкомнатных квартир склеивается всё подряд.
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
        # Разные улицы — разные квартиры, сколько бы ни совпал текст. Это
        # запрет, а не довод: у шаблонных объявлений одного агента текст
        # совпадает всегда.
        if facts.street and row.get("prev_street") and facts.street != row["prev_street"]:
            continue
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

    # Третий ключ — сам объект: улица, цена и комнаты вместе. Нужен там, где
    # телефон не совпадает: одну и ту же квартиру публикуют разные маклеры, и
    # в живой выдаче она пришла дважды подряд — «אוסישקין 44, 8500 ₪, 3к» от
    # двух агентов с разными номерами. Требование полного совпадения адреса,
    # цены и комнат плюс похожий текст делает ложную склейку маловероятной:
    # это должны быть две разные квартиры в одном доме за одни деньги.
    if not (facts.street and facts.price and facts.rooms is not None):
        return None
    cur = await store._db.execute(
        "SELECT l.*, f.price AS prev_price, f.street AS prev_street"
        " FROM listings l JOIN listing_facts f ON f.listing_id = l.id"
        " WHERE f.street = ? AND f.price = ? AND f.rooms = ?"
        "   AND l.collected_at >= datetime('now', ?)"
        " ORDER BY l.collected_at DESC LIMIT 5",
        (facts.street, facts.price, facts.rooms, f"-{REPOST_WINDOW_DAYS} days"))
    for row in await cur.fetchall():
        row = dict(row)
        if text_similarity(raw_text, row.get("raw_text") or "") >= REPOST_TEXT_SIMILARITY:
            return row
    return None


async def process(store: Store, raw: dict) -> dict:
    """Обработать одно сырое объявление. Возвращает, что с ним стало."""
    text = raw.get("raw_text") or ""
    url = raw.get("url")
    lid = listing_id(url, text, raw.get("source_id"))

    if await store.listing_exists(lid):
        return {"status": "duplicate", "listing_id": lid}

    facts = extract(text)

    # Факты, пришедшие от самого источника (Yad2, Homeless), важнее того, что мы
    # вытащили из текста: там это поля базы, а не наша догадка по строке. Заодно
    # такие объявления не попадают к модели — спрашивать нечего.
    hints = raw.get("facts") or {}
    for name, value in hints.items():
        if value is not None and hasattr(facts, name):
            setattr(facts, name, value)
    if hints:
        facts.source_layer = "source"

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
    own = await market_mod.medians(store)          # кэшируется на час
    assessment = market_mod.assess(facts, own)
    created = 0
    for profile in await store.active_profiles():
        result = match(facts, profile, market=assessment)
        if result.matched:
            if await store.add_match(profile["id"], lid, result.rank, result.reasons):
                created += 1
    return created


async def rematch_profile(store: Store, profile_id: int, days: int = 14, limit: int = 500) -> int:
    """Прогнать уже собранные объявления по одному профилю.

    Нужно потому, что сопоставление происходит в момент обработки объявления:
    всё, что бот собрал до того, как человек настроил профиль, иначе осталось бы
    невидимым. Без этого первый `/feed` после онбординга пустой, хотя в базе уже
    сотня объявлений. Вызывается при создании и при любом изменении профиля.
    """
    profile = await store.get_profile(profile_id)
    if not profile:
        return 0
    cur = await store._db.execute(
        "SELECT id FROM listings WHERE collected_at >= datetime('now', ?)"
        " ORDER BY collected_at DESC LIMIT ?", (f"-{days} days", limit))
    ids = [r[0] for r in await cur.fetchall()]
    own = await market_mod.medians(store)
    created = 0
    for lid in ids:
        facts = await store.get_facts(lid)
        if not facts:
            continue
        result = match(facts, profile, market=market_mod.assess(facts, own))
        if result.matched:
            if await store.add_match(profile_id, lid, result.rank, result.reasons):
                created += 1
    log.info("профиль %s: пересчёт по %d объявлениям, новых совпадений %d",
             profile_id, len(ids), created)
    return created


async def fetch_details(store: Store, limit: int = 15) -> dict:
    """Дочитать страницы объявлений, которые кому-то подошли.

    Отбор именно такой: страница нужна не для всех, а для тех, что человек
    увидит. На них есть описание от хозяина и комиссия — единственные деньги
    в сделке, которых нет ни в одной строке таблицы.
    """
    from collectors import details as details_mod

    cur = await store._db.execute(
        "SELECT DISTINCT l.id, l.url, l.source FROM listings l"
        " JOIN matches m ON m.listing_id = l.id"
        " WHERE l.details_at IS NULL AND l.url IS NOT NULL"
        "   AND l.source IN ('homeless','komo')"
        " ORDER BY m.rank DESC LIMIT ?", (limit,))
    rows = await cur.fetchall()
    if not rows:
        return {"fetched": 0}

    filled = 0
    client = await details_mod.make_client()
    try:
        for lid, url, source in rows:
            page = await details_mod.fetch(client, url, source)
            await store._db.execute(
                "UPDATE listings SET details_at = datetime('now') WHERE id = ?", (lid,))
            if not page:
                continue
            if page["description"]:
                # описание дописываем к тексту: LLM-слой и фильтр стоп-слов
                # работают по нему же
                await store._db.execute(
                    "UPDATE listings SET raw_text = raw_text || ? WHERE id = ?",
                    ("\n\n" + page["description"], lid))
            if page["facts"]:
                existing = await store.get_facts(lid)
                merged = {k: v for k, v in page["facts"].items()
                          if v is not None and existing.get(k) is None
                          or k == "commission"}
                if merged:
                    await store.save_facts(lid, merged)
                    filled += 1
            if page["description"]:
                # У объявления появился текст, которого модель ещё не видела.
                # Возвращаем строку фактов в слой «только правила», чтобы её
                # подобрал проход дозаполнения: комиссия, мебель, лифт живут
                # именно в описании, а не в подписях доски.
                await store.save_facts(lid, {"source_layer": "rules"})
            await match_listing(store, lid)
        await store._db.commit()
    finally:
        await client.aclose()
    return {"fetched": len(rows), "filled": filled}


async def enrich_pending(store: Store, client, model: str, limit: int = 25) -> dict:
    """Дозаполнить факты моделью и пересчитать совпадения.

    Идёт отдельным проходом, а не внутри `process`, по двум причинам: сбор не
    должен ждать сети, и при недоступности модели объявления просто копятся в
    очереди вместо того, чтобы теряться.
    """
    from extract.llm import fill_gaps
    from extract.schema import Facts

    # Дозаполняем не всё подряд, а то, что кому-то может пригодиться. Иначе
    # деньги уходят на объявления из чужих городов: канал по Хайфе читается,
    # пока хоть один профиль её ищет, но его объявления никому не нужны.
    profiles = await store.active_profiles()
    if not profiles:
        return {"enriched": 0, "failed": 0, "cost_usd": 0.0, "skipped_no_profiles": True}
    cities = sorted({c for p in profiles for c in (p.get("cities") or [])})

    city_clause, params = "", []
    if cities:
        placeholders = ",".join("?" * len(cities))
        # город неизвестен — оставляем: как раз модель его чаще всего и определяет
        city_clause = f" AND (f.city IS NULL OR f.city IN ({placeholders}))"
        params = cities

    cur = await store._db.execute(
        "SELECT l.id FROM listings l JOIN listing_facts f ON f.listing_id = l.id"
        " WHERE f.source_layer = 'rules' AND l.status = 'extracted'" + city_clause +
        " ORDER BY EXISTS (SELECT 1 FROM matches m WHERE m.listing_id = l.id) DESC,"
        "          l.collected_at DESC LIMIT ?", (*params, limit))
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


# Какие поля таблицы стоят за жалобой пользователя. Мамад идёт вместе со своим
# признаком-уликой, адрес — целиком, потому что улица без города бесполезна.
DISPUTED_FIELDS = {
    "price": ["price"],
    "rooms": ["rooms"],
    "mamad": ["mamad", "mamad_evidence"],
    "address": ["street", "district", "city"],
    "other": [],
}


async def mark_disputed(store: Store, listing_id: str, field: str) -> bool:
    """Жалоба «данные неверны»: стереть спорное поле и вернуть объявление в разбор.

    Прежняя версия кнопки ставила статус `pending` — и всё. Часовой проход
    возвращал статус обратно, а дозаполнение брало только объявления со слоем
    `rules`, то есть уже разобранное моделью не трогало вовсе. Нажатие ничего
    не меняло, кроме записи в журнале.

    Стереть поле — не потеря, а постановка вопроса заново: модель спрашивается
    ровно о пустых полях, поэтому очищенная цена гарантированно уедет к ней на
    следующем проходе. Заодно объявление остаётся в выдаче: пустое поле у нас
    никогда не считается нарушением критериев.
    """
    row = await store.get_facts(listing_id)
    if not row:
        return False
    columns = DISPUTED_FIELDS.get(field, [])
    fresh = {k: row.get(k) for k in FACT_COLUMNS if k in row}
    fresh["phones"] = row.get("phones") or []
    fresh["schema_version"] = row.get("schema_version", 1)
    for name in columns:
        fresh[name] = None
    # Слой сбрасываем всегда: даже жалоба «другое» должна вернуть объявление
    # на второй круг, иначе кнопка снова окажется пустышкой.
    fresh["source_layer"] = "rules"
    await store.save_facts(listing_id, fresh)
    await store.set_status(listing_id, "extracted")
    await match_listing(store, listing_id)
    return True


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
