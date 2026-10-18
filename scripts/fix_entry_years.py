"""Разовая правка года в датах въезда, которые модель записала до решения 0010.

Модель не знала текущей даты и ставила год из обучения («с 3 апреля» →
2024-04-03). Новые ответы правит `extract.llm.fix_entry_year`; этот скрипт
прогоняет ту же функцию по уже сохранённым фактам.

Трогаем только текстовые источники (Telegram, Facebook): там дату въезда
пишет исключительно модель. У Homeless и Komo она может прийти со страницы
доски с полным годом (`collectors/details.py`), и отличить её от догадки
модели по базе нельзя — такие строки не трогаем.

Опорная дата — момент публикации объявления, а не сегодня: «с 3 апреля» в
посте от 25 марта — это этот апрель.

    python3 scripts/fix_entry_years.py --db /app/data/dira.db          # показать
    python3 scripts/fix_entry_years.py --db /app/data/dira.db --apply  # записать
"""

import argparse
import os
import sqlite3
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from extract.llm import fix_entry_year          # noqa: E402

TEXT_SOURCES = ("telegram", "facebook")


def ref_date(row) -> date:
    for key in ("posted_at", "collected_at"):
        raw = row[key]
        if raw:
            try:
                return date.fromisoformat(str(raw)[:10])
            except ValueError:
                continue
    return date.today()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.getenv("DB_PATH", "data/dira.db"))
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    marks = ",".join("?" * len(TEXT_SOURCES))
    rows = db.execute(
        "SELECT f.listing_id, f.entry_date, l.raw_text, l.posted_at, l.collected_at, l.source"
        " FROM listing_facts f JOIN listings l ON l.id = f.listing_id"
        " WHERE f.source_layer = 'mixed' AND f.entry_date GLOB '[0-9][0-9][0-9][0-9]-*'"
        f" AND l.source IN ({marks})", TEXT_SOURCES).fetchall()

    changes = []
    for r in rows:
        new = fix_entry_year(r["entry_date"], r["raw_text"] or "", ref_date(r))
        if new != r["entry_date"]:
            changes.append((r["listing_id"], r["entry_date"], new, ref_date(r)))

    print(f"дат въезда из текстовых источников: {len(rows)}, к правке: {len(changes)}")
    for lid, old, new, ref in changes:
        print(f"  #{lid}: {old} → {new or 'нет данных'} (опубликовано {ref})")

    if args.apply and changes:
        with db:
            db.executemany("UPDATE listing_facts SET entry_date = ? WHERE listing_id = ?",
                           [(new, lid) for lid, _, new, _ in changes])
        print("записано")
    elif changes:
        print("\nэто предпросмотр; --apply запишет")


if __name__ == "__main__":
    main()
