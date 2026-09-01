"""Сквозная проверка: корпус → факты → база → отбор по двум разным профилям.

Показывает главное утверждение архитектуры v2 на живых данных: извлечение
происходит один раз на объявление, а отбор — отдельно для каждого профиля,
обычным SQL, без обращения к модели.

    python3 scripts/smoke_match.py [сколько объявлений]
"""

import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.migrate import migrate                      # noqa: E402
from extract import extract                         # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.normpath(os.path.join(HERE, "..", "fixtures"))

BOOL_COLS = ("mamad", "miklat", "elevator", "balcony", "parking", "storage",
             "air_conditioning", "pets_allowed", "garden", "renovated",
             "immediate_entry", "no_broker")


def load_corpus(limit):
    for name in ("fb-raw-v1.jsonl", "listings-v1.jsonl"):
        path = os.path.join(FIX, name)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                text = row.get("text") or ""
                if len(text) < 30:
                    continue
                yield name, row, text
                limit -= 1
                if limit <= 0:
                    return


def fill(conn, limit):
    n = 0
    for source_name, row, text in load_corpus(limit):
        f = extract(text)
        lid = f.fingerprint[:40] if f.fingerprint else str(n)
        src = "facebook" if source_name.startswith("fb") else "telegram"
        try:
            conn.execute(
                "INSERT INTO listings (id, source, url, raw_text, fingerprint, status)"
                " VALUES (?,?,?,?,?,'extracted')",
                (lid, src, row.get("url"), text, f.fingerprint))
        except sqlite3.IntegrityError:
            continue        # дубликат по отпечатку — ровно то, что нужно
        d = f.as_dict()
        cols = ["listing_id", "price", "rooms", "area_sqm", "floor", "total_floors",
                "city", "district", "deal_type", "furnished", "mamad_evidence",
                *BOOL_COLS]
        vals = [lid] + [d.get(c) for c in cols[1:]]
        conn.execute(
            f"INSERT INTO listing_facts ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            vals)
        if f.price:
            conn.execute("INSERT INTO price_history (listing_id, price, source) VALUES (?,?,?)",
                         (lid, f.price, src))
        n += 1
    conn.commit()
    return n


def add_profile(conn, tg_id, name, **kw):
    conn.execute("INSERT OR IGNORE INTO users (telegram_id, first_name) VALUES (?,?)", (tg_id, name))
    cols = ["user_id", "name"] + list(kw)
    vals = [tg_id, name] + [json.dumps(v, ensure_ascii=False) if isinstance(v, list) else v
                            for v in kw.values()]
    cur = conn.execute(
        f"INSERT INTO search_profiles ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
    conn.commit()
    return cur.lastrowid


# Требование к признаку разворачивается в кусок WHERE. Вся суть трёх состояний
# помещается в эти три строки.
def _req(col, mode):
    if mode == "required":
        return f"f.{col} = 'yes'"
    if mode == "allow_unknown":
        extra = " OR f.mamad_evidence IS NOT NULL" if col == "mamad" else ""
        return f"(f.{col} = 'yes' OR f.{col} IS NULL{extra})"
    return "1=1"


def match(conn, profile_id):
    p = dict(conn.execute("SELECT * FROM search_profiles WHERE id=?", (profile_id,)).fetchone())
    cities = json.loads(p["cities"])
    where, args = ["l.status = 'extracted'"], []

    if cities:
        where.append("f.city IN (%s)" % ",".join("?" * len(cities)))
        args += cities
    if p["price_max"] is not None:
        where.append("f.price IS NOT NULL AND f.price <= ?")
        args.append(p["price_max"])
    if p["rooms_min"] is not None:
        # комнаты неизвестны — не отбрасываем: это «нет данных», а не «мало комнат»
        where.append("(f.rooms IS NULL OR f.rooms >= ?)")
        args.append(p["rooms_min"])
    if p["exclude_shared"]:
        where.append("(f.deal_type IS NULL OR f.deal_type <> 'shared')")
    if p["exclude_sublet"]:
        where.append("(f.deal_type IS NULL OR f.deal_type <> 'sublet')")
    where.append("(f.deal_type IS NULL OR f.deal_type <> 'sale')")
    for col, key in (("mamad", "req_mamad"), ("elevator", "req_elevator"),
                     ("parking", "req_parking"), ("balcony", "req_balcony"),
                     ("pets_allowed", "req_pets")):
        where.append(_req(col, p[key]))

    # Ранг считается тут же, обычной арифметикой: запас по бюджету, полнота
    # данных, явный мамад. Никакой модели.
    rank = """
        (CASE WHEN ? IS NOT NULL AND f.price IS NOT NULL
              THEN MAX(0.0, MIN(1.0, (? - f.price) * 1.0 / ?)) ELSE 0 END) * 3
      + (CASE WHEN f.mamad = 'yes' THEN 2 WHEN f.mamad_evidence IS NOT NULL THEN 1 ELSE 0 END)
      + (CASE WHEN f.rooms IS NOT NULL THEN 1 ELSE 0 END)
      + (CASE WHEN f.area_sqm IS NOT NULL THEN 0.5 ELSE 0 END)
      + (CASE WHEN f.floor IS NOT NULL THEN 0.5 ELSE 0 END)
    """
    cap = p["price_max"] or 0
    sql = (f"SELECT l.id, f.price, f.rooms, f.city, f.mamad, f.mamad_evidence,"
           f" ROUND({rank}, 2) AS rank FROM listings l JOIN listing_facts f ON f.listing_id = l.id"
           f" WHERE {' AND '.join(where)} ORDER BY rank DESC, l.collected_at DESC")
    return conn.execute(sql, [p["price_max"], cap, cap or 1] + args).fetchall()


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 4000
    path = os.path.join(tempfile.gettempdir(), "dira_smoke.db")
    if os.path.exists(path):
        os.remove(path)
    print(f"версия схемы: {migrate(path, verbose=False)}")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")

    n = fill(conn, limit)
    print(f"загружено объявлений: {n} (дубликаты по отпечатку отброшены)\n")

    igor = add_profile(conn, 1, "Игорь",
                       cities=["Tel Aviv", "Ramat Gan", "Givatayim", "Bnei Brak"],
                       price_max=8000, rooms_min=2.5, req_mamad="required")
    friend = add_profile(conn, 2, "Знакомый (тест)",
                         cities=["Tel Aviv"], price_max=5000, rooms_min=1,
                         req_mamad="allow_unknown", req_pets="ignore")

    for pid, title in ((igor, "Игорь"), (friend, "Знакомый (тестовые критерии)")):
        rows = match(conn, pid)
        print(f"=== {title} — подошло {len(rows)} ===")
        for r in rows[:5]:
            mam = "мамад" if r["mamad"] == "yes" else ("неясно" if r["mamad_evidence"] else "—")
            rooms = f"{r['rooms']:g}" if r["rooms"] else "?"
            print(f"  ранг {r['rank']:>5}  {r['price'] or '?':>6} ₪  {rooms:>4} комн  "
                  f"{r['city'] or '?':<12} {mam}")
        print()
    conn.close()


if __name__ == "__main__":
    main()
