"""Насколько быстро обновляется та часть источника, которую мы видим.

Отсюда берётся срок в `core.sources.MAX_AGE_DAYS`. Для источников, где мы
читаем не всю доску, а первую страницу, нельзя просто взять «семь дней»:
объявление может висеть там неделями и всё это время быть актуальным.

Метод: сравниваем то, что лежит в базе, с текущей выдачей. Доля пропавших за
известный промежуток даёт скорость обновления слота, а срок жизни слота с
запасом в полтора раза — искомый порог.

    python3 scripts/measure_turnover.py
"""

import asyncio
import os
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collectors import homeless, komo                # noqa: E402
from core import settings                            # noqa: E402
from core.sources import max_age_days                # noqa: E402

CITIES = ["Tel Aviv", "Ramat Gan", "Givatayim", "Bnei Brak"]


async def measure(name, fetch, conn):
    known = {r[0]: r[1] for r in conn.execute(
        "SELECT source_id, collected_at FROM listings WHERE source=?", (name,))}
    if not known:
        print(f"{name}: в базе пусто, измерять нечего")
        return
    items = await fetch(CITIES)
    live = {i["source_id"] for i in items}
    gone = len(set(known) - live)

    first = min(known.values())
    span_days = max((datetime.now() - datetime.fromisoformat(first)).total_seconds() / 86400, 0.5)
    if gone == 0:
        print(f"{name}: за {span_days:.1f} дн. не пропало ни одного из {len(known)} — "
              f"порог можно не менять (сейчас {max_age_days(name)} дн.)")
        return
    per_day = gone / span_days
    lifetime = len(known) / per_day
    print(f"{name}: {len(known)} известных, сейчас в выдаче {len(live)}, пропало {gone} "
          f"за {span_days:.1f} дн.")
    print(f"   слот обновляется за ~{lifetime:.0f} дн. → порог с запасом "
          f"{lifetime * 1.5:.0f} дн. (сейчас {max_age_days(name)})")


async def main():
    conn = sqlite3.connect(settings.DB_PATH)
    await measure("komo", komo.collect, conn)
    await measure("homeless", homeless.collect, conn)
    conn.close()


if __name__ == "__main__":
    asyncio.run(main())
