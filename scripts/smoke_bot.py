"""Сквозная проверка всего стека без Telegram.

Проходит визард программно, сохраняет профиль, прогоняет по нему все собранные
объявления, складывает совпадения и читает ленту в разных сортировках. Ровно то,
что будет делать бот, только вместо кнопок — вызовы функций.

    python3 scripts/smoke_bot.py ~/dira-data/dira.db
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import cards, wizard              # noqa: E402
from core.match import match               # noqa: E402
from core.sources import region_of         # noqa: E402
from core.store import Store               # noqa: E402
from db.migrate import migrate             # noqa: E402

ANSWERS = {
    "cities": ["Tel Aviv", "Ramat Gan", "Givatayim", "done"],
    "price_max": ["8000"], "price_ideal": ["7500"], "rooms_min": ["2.5"],
    "req_mamad": ["allow_unknown"], "req_elevator": ["ignore"], "req_pets": ["ignore"],
    "delivery_mode": ["digest"], "digest_hour": ["9"], "stop_words": ["skip"],
}


async def run_wizard(store: Store, user_id: int) -> dict:
    data, key, steps = {}, wizard.first_step(), 0
    while key != wizard.DONE and steps < 40:
        steps += 1
        for answer in ANSWERS[key]:
            accepted, error = wizard.apply(key, data, answer)
            assert not error, f"{key}: {error}"
            await store.set_user(user_id, onboarding_data=data, onboarding_step=key)
            if accepted:
                key = wizard.step_after(key, data)
                break
    await store.set_user(user_id, onboarding_step="done")
    print(f"визард пройден за {steps} шагов\n{wizard.summary(data)}\n")
    return data


async def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/dira-data/dira.db")
    print("версия схемы:", migrate(path, verbose=False))
    store = await Store(path).connect()

    user = await store.ensure_user(TEST_TELEGRAM_ID, "tester", "Игорь")
    data = await run_wizard(store, user["telegram_id"])

    existing = await store.profiles_of(user["telegram_id"])
    fields = wizard.to_profile(data)
    if existing:
        await store.update_profile(existing[0]["id"], **fields)
        profile_id = existing[0]["id"]
    else:
        profile_id = await store.create_profile(user["telegram_id"], "Основной", **fields)
    profile = await store.get_profile(profile_id)

    # прогон всех объявлений по профилю
    cur = await store._db.execute("SELECT id FROM listings")
    ids = [r[0] for r in await cur.fetchall()]
    matched = rejected = 0
    reasons_top = {}
    for listing_id in ids:
        facts = await store.get_facts(listing_id)
        if facts.get("city") is None:
            hint = region_of(facts.get("channel"))
            if hint:
                facts["city"] = hint
        result = match(facts, profile)
        if result.matched:
            await store.add_match(profile_id, listing_id, result.rank, result.reasons)
            matched += 1
        else:
            rejected += 1
            head = (result.rejected_by or "").split(":")[0]
            reasons_top[head] = reasons_top.get(head, 0) + 1

    print(f"объявлений: {len(ids)} · подошло: {matched} · отсеяно: {rejected}")
    print("причины отказа:", dict(sorted(reasons_top.items(), key=lambda x: -x[1])[:4]), "\n")

    # лента в разных сортировках — как её увидит пользователь
    for order in ("rank", "price", "fresh"):
        rows = await store.feed(profile_id, order=order, limit=3, states=("new", "sent"))
        print(f"— сортировка {order}:")
        for facts in rows:
            price = f"{facts['price']} ₪" if facts.get("price") else "цена ?"
            rooms = f"{facts['rooms']:g} комн" if facts.get("rooms") else "? комн"
            print(f"    {price:>10} · {rooms:>8} · {facts.get('city') or '?':<10} "
                  f"· ранг {facts.get('rank')}")
        print()

    # карточка первого объявления — то, что уйдёт в чат
    top = await store.feed(profile_id, order="rank", limit=1, states=("new",))
    if top:
        print("карточка:\n")
        print(cards.card(top[0], rank=top[0].get("rank")))
        print("\nстатус «написал»…")
        await store.set_match_state(profile_id, top[0]["listing_id"], "contacted")
        await store.log_action(user["telegram_id"], "state_change",
                               top[0]["listing_id"], {"state": "contacted"})
        print("готово, статистика:", await store.stats())

    await store.close()


if __name__ == "__main__":
    asyncio.run(main())
