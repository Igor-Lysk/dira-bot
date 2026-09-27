"""Ручная чистка: проверить страницы всех свежих объявлений Komo, ушедших с
первой страницы, и отметить снятые.

Плановая задача (`job_alive`) проверяет только то, что лежит у кого-то в
подборке, по двадцать страниц за проход. Этот скрипт — для разовой чистки всей
базы: после появления проверки, после долгого простоя, после жалобы.

    python3 scripts/check_removed.py            все свежие, ушедшие с первой страницы
    python3 scripts/check_removed.py --matched  только те, что у кого-то в подборке

Пауза между запросами та же, что у плановой задачи: несколько сотен страниц
идут полчаса, и это нормально.
"""

import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import settings                   # noqa: E402
from core.pipeline import check_removed     # noqa: E402
from core.store import Store                 # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--matched", action="store_true",
                        help="только объявления, которые у кого-то в подборке")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    store = await Store(settings.DB_PATH).connect()
    try:
        # recheck_hours=0 — проверить и то, что плановая задача смотрела недавно
        result = await check_removed(store, limit=0, recheck_hours=0,
                                     pause_sec=settings.ALIVE_PAUSE_SEC,
                                     only_matched=args.matched)
    finally:
        await store.close()
    print(result)
    return 1 if result.get("blocked") else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
