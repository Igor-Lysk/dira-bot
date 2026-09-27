"""Перепроверить все подобранные объявления по текущим фактам.

Разовая чистка после правки 0010: до неё факты, дописанные моделью или
страницей объявления, только добавляли совпадения и никогда не убирали. В
лентах остались квартиры, которые по нынешним данным не проходят критерии.

Только убирает: новых совпадений по старым объявлениям не заводит.

    python3 scripts/recheck_matches.py
"""

import asyncio
import collections
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import settings                   # noqa: E402
from core.pipeline import recheck_listing   # noqa: E402
from core.store import Store                # noqa: E402


async def main() -> int:
    store = await Store(settings.DB_PATH).connect()
    try:
        cur = await store._db.execute("SELECT DISTINCT listing_id FROM matches")
        ids = [r[0] for r in await cur.fetchall()]
        totals = collections.Counter()
        for lid in ids:
            for outcome in (await recheck_listing(store, lid, add_new=False)).values():
                totals[outcome] += 1
    finally:
        await store.close()
    print(f"объявлений: {len(ids)}", dict(totals))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
