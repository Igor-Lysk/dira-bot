"""Сравнение двух моделей на слое дозаполнения фактов (extract/llm.py).

Нужен при смене модели: один и тот же промпт, одни и те же объявления,
два ответа рядом. Регулярки отрабатывают один раз, модели получают
одинаковый список недостающих полей.

Что смотрим:
* сколько полей каждая модель заполнила и где они расходятся;
* мамад «yes» без подтверждения в тексте — главная ошибка v1 (F-1);
* сбои: исключения API и ответы, которые не разобрались в JSON;
* измеренный расход.

Запуск (из repo/, ключ берётся из .env):
    python3 scripts/compare_models.py claude-haiku-4-5-20251001 claude-haiku-5-5 \\
        --fb-sample 300 --out ../research/model-compare
"""

import argparse
import asyncio
import copy
import json
import os
import random
import re
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from extract import extract                                  # noqa: E402
from extract.llm import build_prompt, fill_gaps              # noqa: E402
from extract.schema import BOOL_FIELDS, VALUE_FIELDS         # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.normpath(os.path.join(HERE, "..", "fixtures"))
FIELDS = [*VALUE_FIELDS, *BOOL_FIELDS]

# Слова, без которых «мамад есть» текстом не подтверждён.
MAMAD_WORDS = re.compile(r'ממ["״\']?ד|מרחב מוגן|мамад|mamad|safe ?room|защищ[её]нн',
                         re.IGNORECASE)


def load_env(path=".env"):
    env = {}
    for line in open(path, encoding="utf-8-sig"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def load_jsonl(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def corpus(fb_sample: int, seed: int):
    rows = []
    for r in load_jsonl(os.path.join(FIX, "listings-v1.jsonl")):
        if len(r.get("text") or "") >= 30:
            rows.append(("v1", r["text"]))
    if fb_sample:
        fb = [r["text"] for r in load_jsonl(os.path.join(FIX, "fb-raw-v1.jsonl"))
              if len(r.get("text") or "") >= 30]
        random.Random(seed).shuffle(fb)
        rows += [("fb", t) for t in fb[:fb_sample]]
    return rows


def norm(v):
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str):
        return v.strip().lower()
    return v


async def run_one(client, model, text, base, sem):
    async with sem:
        t0 = time.monotonic()
        facts, usage = await fill_gaps(text, copy.deepcopy(base), client, model)
        usage["seconds"] = round(time.monotonic() - t0, 2)
        return facts.as_dict(), usage


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("model_a")
    ap.add_argument("model_b")
    ap.add_argument("--fb-sample", type=int, default=300)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--out", default="compare-out")
    ap.add_argument("--budget", type=int, default=0,
                    help="секунд на этот запуск; результаты дописываются, повторный запуск продолжает")
    args = ap.parse_args()

    import anthropic
    env = load_env()
    client = anthropic.AsyncAnthropic(api_key=env["ANTHROPIC_API_KEY"], max_retries=5)
    sem = asyncio.Semaphore(args.concurrency)

    rows = corpus(args.fb_sample, args.seed)
    if args.limit:
        rows = rows[:args.limit]
    jobs, seen = [], set()
    for src, text in rows:
        if text in seen:            # в корпусе есть перепосты — спрашиваем один раз
            continue
        seen.add(text)
        base = extract(text)
        if build_prompt(text, base) is None:
            continue
        jobs.append((src, text, base))
    print(f"корпус: {len(rows)}, спрашиваем модель: {len(jobs)}", flush=True)

    os.makedirs(args.out, exist_ok=True)
    out_path = os.path.join(args.out, "results.jsonl")
    results = list(load_jsonl(out_path)) if os.path.exists(out_path) else []
    done = {r["text"] for r in results}
    todo = [j for j in jobs if j[1] not in done]
    print(f"уже готово: {len(results)}, осталось: {len(todo)}", flush=True)

    async def both(src, text, base):
        a, b = await asyncio.gather(
            run_one(client, args.model_a, text, base, sem),
            run_one(client, args.model_b, text, base, sem))
        return {"src": src, "text": text, "rules": base.as_dict(), "a": a, "b": b}

    started = time.monotonic()
    batch = max(args.concurrency, 1)
    with open(out_path, "a", encoding="utf-8") as out:
        for i in range(0, len(todo), batch):
            if args.budget and time.monotonic() - started > args.budget:
                break
            for r in await asyncio.gather(*(both(*j) for j in todo[i:i + batch])):
                results.append(r)
                out.write(json.dumps(r, ensure_ascii=False) + "\n")
            out.flush()
    if len(results) < len(jobs):
        print(f"готово {len(results)}/{len(jobs)} — запусти ещё раз, чтобы продолжить")
        return

    # ── сводка ───────────────────────────────────────────────────────────────
    stats = {k: Counter() for k in ("a", "b")}
    filled = {k: Counter() for k in ("a", "b")}
    agree, differ, only_a, only_b = Counter(), Counter(), Counter(), Counter()
    diffs = {}
    mamad_unsupported = {"a": [], "b": []}
    for r in results:
        rules = r["rules"]
        for k in ("a", "b"):
            facts, usage = r[k]
            if not usage.get("ok"):
                stats[k]["errors"] += 1
                continue
            stats[k]["calls"] += 1
            stats[k]["in"] += usage["input_tokens"]
            stats[k]["out"] += usage["output_tokens"]
            stats[k]["cost_micro"] += int(usage["cost_usd"] * 1e6)
            stats[k]["seconds_x100"] += int(usage["seconds"] * 100)
            added = [f for f in FIELDS if rules.get(f) is None and facts.get(f) is not None]
            if not added:
                stats[k]["nothing_added"] += 1
            for f in added:
                filled[k][f] += 1
            if rules.get("mamad") is None and facts.get("mamad") == "yes" \
                    and not MAMAD_WORDS.search(r["text"]):
                mamad_unsupported[k].append(r["text"][:200].replace("\n", " "))
        fa, ua = r["a"]
        fb_, ub = r["b"]
        if not (ua.get("ok") and ub.get("ok")):
            continue
        for f in FIELDS:
            if rules.get(f) is not None:
                continue
            va, vb = fa.get(f), fb_.get(f)
            if va is None and vb is None:
                continue
            if va is None:
                only_b[f] += 1
            elif vb is None:
                only_a[f] += 1
            elif norm(va) == norm(vb):
                agree[f] += 1
            else:
                differ[f] += 1
                diffs.setdefault(f, []).append((va, vb, r["text"][:160].replace("\n", " ")))

    names = {"a": args.model_a, "b": args.model_b}
    lines = [f"# {args.model_a} против {args.model_b}", "",
             f"Корпус: {len(rows)} объявлений (listings-v1 целиком"
             f"{f' + {args.fb_sample} случайных из fb-raw, seed {args.seed}' if args.fb_sample else ''}),"
             f" к модели ушло {len(jobs)} — у остальных регулярки взяли всё.", "",
             "| | " + " | ".join(names.values()) + " |", "|---|---|---|"]
    def row(label, fn):
        lines.append(f"| {label} | " + " | ".join(fn(k) for k in ("a", "b")) + " |")
    row("успешных вызовов", lambda k: str(stats[k]["calls"]))
    row("ошибок API", lambda k: str(stats[k]["errors"]))
    row("ничего не дописала", lambda k: str(stats[k]["nothing_added"]))
    row("полей дописано всего", lambda k: str(sum(filled[k].values())))
    row("токенов вход / выход", lambda k: f'{stats[k]["in"]:,} / {stats[k]["out"]:,}')
    row("расход, $", lambda k: f'{stats[k]["cost_micro"] / 1e6:.3f}')
    row("$ на 1000 объявлений", lambda k: f'{stats[k]["cost_micro"] / 1e6 / max(stats[k]["calls"], 1) * 1000:.2f}')
    row("сек на вызов (среднее)", lambda k: f'{stats[k]["seconds_x100"] / 100 / max(stats[k]["calls"], 1):.1f}')
    row("мамад yes без слова в тексте", lambda k: str(len(mamad_unsupported[k])))

    lines += ["", "## По полям (только поля, которые регулярки не взяли)", "",
              f"| поле | дописала A | дописала B | совпали | разошлись | только A | только B |",
              "|---|---|---|---|---|---|---|"]
    for f in FIELDS:
        if filled["a"][f] or filled["b"][f]:
            lines.append(f"| {f} | {filled['a'][f]} | {filled['b'][f]} | {agree[f]} | "
                         f"{differ[f]} | {only_a[f]} | {only_b[f]} |")
    lines += ["", f"A = {args.model_a}, B = {args.model_b}", "", "## Примеры расхождений", ""]
    for f, items in sorted(diffs.items(), key=lambda x: -len(x[1])):
        lines.append(f"**{f}** ({len(items)})")
        for va, vb, t in items[:5]:
            lines.append(f"- A=`{va}` B=`{vb}` — {t}")
        lines.append("")
    for k in ("a", "b"):
        if mamad_unsupported[k]:
            lines.append(f"## Мамад без подтверждения — {names[k]}")
            lines += [f"- {t}" for t in mamad_unsupported[k][:10]]
            lines.append("")

    report = "\n".join(lines)
    with open(os.path.join(args.out, "report.md"), "w", encoding="utf-8") as f:
        f.write(report)
    print(report)


if __name__ == "__main__":
    asyncio.run(main())
