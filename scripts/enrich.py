"""Дозаполнение фактов моделью: то, что не взяли регулярки.

Второй слой извлечения. Спрашивает только недостающие поля, вердикта не
запрашивает, значения не по схеме отбрасывает. Расход пишется в `llm_usage`,
чтобы стоимость была измеренной, а не оценочной.

    python3 scripts/enrich.py --db ~/dira-data/dira.db --limit 20
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.store import Store                        # noqa: E402
from extract.llm import fill_gaps                   # noqa: E402
from extract.schema import Facts, BOOL_FIELDS, VALUE_FIELDS  # noqa: E402

TRACKED = [*VALUE_FIELDS, *BOOL_FIELDS, "mamad_evidence"]


def load_env(path=".env"):
    env = {}
    for line in open(path, encoding="utf-8-sig"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def facts_from_row(row: dict) -> Facts:
    f = Facts()
    for name in TRACKED:
        if hasattr(f, name) and row.get(name) is not None:
            setattr(f, name, row[name])
    return f


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.expanduser("~/dira-data/dira.db"))
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--dry-run", action="store_true", help="только показать, сколько будет спрошено")
    args = ap.parse_args()

    env = load_env()
    model = env.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
    import anthropic
    client = anthropic.AsyncAnthropic(api_key=env["ANTHROPIC_API_KEY"])

    store = await Store(args.db).connect()
    cur = await store._db.execute(
        "SELECT l.id FROM listings l JOIN listing_facts f ON f.listing_id = l.id"
        " WHERE f.source_layer = 'rules' ORDER BY l.collected_at DESC LIMIT ?", (args.limit,))
    ids = [r[0] for r in await cur.fetchall()]
    print(f"объявлений к дозаполнению: {len(ids)}\n")

    before = {name: 0 for name in TRACKED}
    after = {name: 0 for name in TRACKED}
    total_cost = 0.0
    failures = 0

    for i, listing_id in enumerate(ids, 1):
        row = await store.get_facts(listing_id)
        facts = facts_from_row(row)
        for name in TRACKED:
            if getattr(facts, name, None) is not None:
                before[name] += 1
        if args.dry_run:
            continue

        facts, usage = await fill_gaps(row["raw_text"], facts, client, model)
        if usage.get("ok"):
            await store.log_llm("extract", model, usage, listing_id)
            total_cost += usage.get("cost_usd", 0)
        elif not usage.get("skipped"):
            failures += 1
            # объявление возвращается в очередь, а не помечается разобранным
            await store.set_status(listing_id, "pending", usage.get("error", "")[:200])
            continue

        for name in TRACKED:
            if getattr(facts, name, None) is not None:
                after[name] += 1
        data = {k: v for k, v in facts.as_dict().items()
                if k not in ("phones", "fingerprint")}
        data["phones"] = facts.phones
        await store.save_facts(listing_id, data)
        if i % 10 == 0:
            print(f"  обработано {i}/{len(ids)}, потрачено ${total_cost:.4f}")

    print(f"\n{'поле':<20}{'было':>7}{'стало':>7}{'+':>6}")
    print("-" * 40)
    for name in TRACKED:
        if after[name] != before[name]:
            print(f"{name:<20}{before[name]:>7}{after[name]:>7}{after[name] - before[name]:>+6}")
    n = len(ids) or 1
    print(f"\nпотрачено: ${total_cost:.4f} на {n} объявлений "
          f"(${total_cost / n:.5f} за штуку), сбоев: {failures}")
    print("расход в базе:", await store.spend())
    await store.close()


if __name__ == "__main__":
    asyncio.run(main())
