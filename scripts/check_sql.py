"""Прогон всех запросов Store по пустой базе.

Смысл ровно один: SQL здесь собирается из кусков — условие свежести зависит от
набора источников, фильтры ленты от кнопки, состояния от вызова, — и ошибка в
склейке видна только в момент выполнения. Один такой остаток («AND (…) OR (…))»
с лишней скобкой) прожил в очереди доставки до первой мгновенной отправки:
дайджест ходит раз в сутки, и падение было бы замечено утром, в единственный
момент, когда оно важно.

Проверяется синтаксис и имена столбцов, не поведение — база пуста, все выборки
возвращают ничего. Ошибка означает, что запрос сломан для всех.

    python3 scripts/check_sql.py
"""

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.app import SORTS, FILTERS          # noqa: E402
from core.store import Store                # noqa: E402
from db.migrate import migrate              # noqa: E402


async def main() -> int:
    path = os.path.join(tempfile.mkdtemp(), "check.db")
    migrate(path, verbose=False)
    store = await Store(path).connect()

    checks = [
        ("admins", store.admins()),
        ("get_user", store.get_user(1)),
        ("profiles_of", store.profiles_of(1)),
        ("active_profiles", store.active_profiles()),
        ("get_profile", store.get_profile(1)),
        ("listing_exists", store.listing_exists("x")),
        ("find_by_fingerprint", store.find_by_fingerprint("x")),
        ("pending_listings", store.pending_listings()),
        ("get_facts", store.get_facts("x")),
        ("price_history", store.price_history("x")),
        ("queue_for", store.queue_for(1)),
        ("sent_today", store.sent_today(1)),
        ("spend", store.spend()),
        ("stats", store.stats()),
        ("mark_seen", store.mark_seen("homeless", ["a", "b"])),
    ]
    # лента: каждая сортировка против каждого фильтра
    for order, _ in SORTS:
        for flt, _ in FILTERS:
            checks.append((f"feed {order}/{flt}", store.feed(1, order=order, flt=flt)))

    failed = 0
    for name, coro in checks:
        try:
            await coro
        except Exception as e:                          # noqa: BLE001
            failed += 1
            print(f"✗ {name}: {e}")
    await store.close()
    print(f"{len(checks) - failed} из {len(checks)} запросов исполнились")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
