"""Миграции схемы: нумерованные .sql-файлы и указатель версии в самой базе.

Почему не Alembic. Botkin построен на SQLAlchemy + Postgres, там Alembic на
своём месте. Dira — это aiosqlite и обычный SQL, ORM в проекте нет и не нужен
ради двух-пяти пользователей. Тянуть Alembic пришлось бы вместе с SQLAlchemy,
то есть две крупные зависимости ради того, что здесь укладывается в сорок строк.

Что важно сохранить из Alembic: версия схемы хранится в самой базе, миграции
применяются по порядку и ровно один раз, а v1-подход `CREATE TABLE IF NOT EXISTS`
(который не умеет добавлять колонки и приводит к тихому расхождению схем)
больше не используется.

Версия лежит в `PRAGMA user_version` — встроенное целое, ради него не нужна
отдельная таблица.

    python3 db/migrate.py data/dira.db          применить всё, чего не хватает
    python3 db/migrate.py data/dira.db --status показать текущую версию
"""

import os
import re
import sqlite3
import sys

MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")
_NAME_RE = re.compile(r"^(\d{3})_[\w\-]+\.sql$")


def available():
    """[(версия, путь)] по возрастанию."""
    out = []
    for name in sorted(os.listdir(MIGRATIONS_DIR)):
        m = _NAME_RE.match(name)
        if m:
            out.append((int(m.group(1)), os.path.join(MIGRATIONS_DIR, name)))
    return out


def current_version(conn) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(db_path: str, verbose: bool = True) -> int:
    os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        version = current_version(conn)
        applied = 0
        for number, path in available():
            if number <= version:
                continue
            with open(path, encoding="utf-8") as f:
                sql = f.read()
            # Каждая миграция — одна транзакция: либо применилась целиком,
            # либо база осталась на прежней версии.
            try:
                conn.executescript("BEGIN;\n" + sql + f"\nPRAGMA user_version={number};\nCOMMIT;")
            except Exception:
                conn.rollback()
                raise
            applied += 1
            if verbose:
                print(f"применена {os.path.basename(path)}")
        if verbose and not applied:
            print(f"нечего применять, версия схемы {version}")
        return current_version(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    path = sys.argv[1]
    if "--status" in sys.argv:
        c = sqlite3.connect(path)
        print(f"версия схемы: {current_version(c)}")
        print("доступны:", ", ".join(os.path.basename(p) for _, p in available()))
        c.close()
    else:
        print(f"версия схемы: {migrate(path)}")
