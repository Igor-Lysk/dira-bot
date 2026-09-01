"""Прогон детерминированного слоя на корпусах из ../fixtures.

Что меряем и чего НЕ меряем — важно не путать:

* **Покрытие** (coverage) — доля объявлений, где поле вообще извлеклось.
  Меряется без разметки и отвечает на главный вопрос этапа 1а: вокруг каких
  полей вообще имеет смысл строить фильтры.

* **Согласие с v1** по мамаду — единственное поле, которое v1 сохраняла в базу
  (`analyses.has_mamad`, проставлено Claude). Расхождения — самый интересный
  материал для ручного разбора.

* Точность по остальным полям НЕ меряется: размеченного эталона нет.
  `tel_aviv_final.xlsx` сгенерирован тем же парсером, что мы портировали,
  поэтому сверка с ним показывает только точность порта, а не качество разбора.

Запуск:  python3 scripts/bench_extract.py
"""

import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from extract import extract                      # noqa: E402
from extract.schema import BOOL_FIELDS, VALUE_FIELDS  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.normpath(os.path.join(HERE, "..", "fixtures"))

FIELDS = [*VALUE_FIELDS, *BOOL_FIELDS]


def load_jsonl(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def coverage(rows, label):
    n = 0
    filled = Counter()
    values = {f: Counter() for f in BOOL_FIELDS}
    cities = Counter()
    for r in rows:
        text = r.get("text") or ""
        if len(text) < 30:
            continue
        n += 1
        f = extract(text).as_dict()
        for key in FIELDS:
            if f.get(key) is not None:
                filled[key] += 1
        for key in BOOL_FIELDS:
            values[key][f.get(key) or "unknown"] += 1
        cities[f.get("city") or "-"] += 1

    print(f"\n=== {label} — {n} объявлений ===\n")
    print(f"{'поле':<20}{'извлечено':>10}{'доля':>8}   {'из них no':>10}")
    print("-" * 52)
    for key in FIELDS:
        pct = filled[key] / n * 100 if n else 0
        no = values[key]["no"] if key in values else ""
        no_s = f"{no}" if no != "" else ""
        print(f"{key:<20}{filled[key]:>10}{pct:>7.1f}%   {no_s:>10}")

    print("\nгорода (топ-10):")
    for city, cnt in cities.most_common(10):
        print(f"  {city:<16}{cnt:>6}  {cnt/n*100:>5.1f}%")
    return n


def mamad_agreement():
    """Сверка с тем, что проставила Claude в v1 (единственное сохранённое поле)."""
    path = os.path.join(FIX, "listings-v1.jsonl")
    if not os.path.exists(path):
        return
    agree = Counter()
    examples = {"rules_yes_llm_no": [], "rules_none_llm_yes": []}
    for r in load_jsonl(path):
        llm = r.get("v1", {}).get("has_mamad")
        text = r.get("text") or ""
        if len(text) < 30:
            continue
        f = extract(text)
        rules_val = "yes" if f.mamad == "yes" else ("no" if f.mamad == "no" else "unknown")
        llm_val = {1: "yes", 0: "no", None: "unknown"}.get(llm, "unknown")
        agree[(rules_val, llm_val)] += 1
        if rules_val == "yes" and llm_val in ("no", "unknown") and len(examples["rules_yes_llm_no"]) < 3:
            examples["rules_yes_llm_no"].append(text[:180].replace("\n", " "))
        if rules_val == "unknown" and llm_val == "yes" and len(examples["rules_none_llm_yes"]) < 3:
            examples["rules_none_llm_yes"].append(text[:180].replace("\n", " "))

    total = sum(agree.values())
    same = sum(v for (a, b), v in agree.items() if a == b)
    print(f"\n=== Мамад: regex против Claude из v1 — {total} объявлений ===\n")
    print(f"{'regex':<10}{'claude':<10}{'кол-во':>8}")
    print("-" * 30)
    for (a, b), v in sorted(agree.items(), key=lambda x: -x[1]):
        print(f"{a:<10}{b:<10}{v:>8}")
    print(f"\nсовпало: {same}/{total} = {same/total*100:.1f}%")
    for key, items in examples.items():
        if items:
            print(f"\n{key}:")
            for t in items:
                print(f"  · {t}")


if __name__ == "__main__":
    fb = os.path.join(FIX, "fb-raw-v1.jsonl")
    tg = os.path.join(FIX, "listings-v1.jsonl")
    if os.path.exists(fb):
        coverage(load_jsonl(fb), "Facebook (FB_scrapper, сырые посты)")
    if os.path.exists(tg):
        coverage(load_jsonl(tg), "Telegram + Yad2 (корпус v1)")
    mamad_agreement()
