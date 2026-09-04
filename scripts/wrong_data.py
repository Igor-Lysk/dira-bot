"""Жалобы «данные неверны» — что именно люди поправляли.

Смысл кнопки не в том, чтобы починить одно объявление, а в том, чтобы починить
парсер. Для этого жалобы нужно читать пачкой: три жалобы на цену в объявлениях
одного канала — это не три случайности, а один неучтённый формат.

    python3 scripts/wrong_data.py [путь-к-базе]
"""

import json
import os
import sqlite3
import sys

path = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/dira-data/dira.db")
conn = sqlite3.connect(path)
conn.row_factory = sqlite3.Row

rows = conn.execute(
    "SELECT a.created_at, a.payload, a.listing_id, l.source, l.channel, l.url,"
    "       f.price, f.rooms, f.mamad, f.street, f.city, substr(l.raw_text, 1, 300) AS head"
    "  FROM user_actions a"
    "  LEFT JOIN listings l ON l.id = a.listing_id"
    "  LEFT JOIN listing_facts f ON f.listing_id = a.listing_id"
    " WHERE a.action = 'wrong_data' ORDER BY a.created_at DESC").fetchall()

if not rows:
    print("жалоб нет")
    raise SystemExit

by_field, by_source = {}, {}
for r in rows:
    field = (json.loads(r["payload"] or "{}") or {}).get("field", "не указано")
    by_field[field] = by_field.get(field, 0) + 1
    key = f"{r['source']} · {r['channel'] or '—'}"
    by_source[key] = by_source.get(key, 0) + 1

print(f"жалоб всего: {len(rows)}")
print("по полям:  ", ", ".join(f"{k} — {v}" for k, v in sorted(by_field.items(), key=lambda x: -x[1])))
print("по каналам:", ", ".join(f"{k} — {v}" for k, v in sorted(by_source.items(), key=lambda x: -x[1])))
print()
for r in rows[:20]:
    field = (json.loads(r["payload"] or "{}") or {}).get("field", "?")
    print(f"— {r['created_at']} · {field} · {r['source']}/{r['channel'] or '—'}")
    print(f"  разбор: цена {r['price']}, комнат {r['rooms']}, мамад {r['mamad']}, "
          f"{r['street'] or '?'}, {r['city'] or '?'}")
    print(f"  {r['url']}")
    print(f"  текст: {' '.join((r['head'] or '').split())[:200]}")
    print()
