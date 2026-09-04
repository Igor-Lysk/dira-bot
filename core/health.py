"""Самопроверка: показатели, по которым видно, что что-то пошло не так.

Написано после дорогого урока. Дозаполнение крутило одни и те же объявления по
кругу трое суток, и строка в логе всё это время выглядела ровно так же, как при
нормальной работе: «дозаполнено 25». Ошибка была не в том, что её никто не
заметил, а в том, что заметить её было нечем — ни одна величина не измерялась.

Отсюда правило, по которому собран этот модуль: у каждой повторяющейся работы
должна быть величина с ожидаемым значением. Не «нет ошибок в логе», а «вызовов
модели на объявление — 1.05 при допустимых 1.5». Проверки нарочно
сформулированы как утверждения о числах, а не как поиск исключений: цикл,
молчащий сборщик и застрявшая очередь ошибок не выбрасывают.

Отчёт уходит администратору только когда что-то не сходится, и не чаще раза в
сутки на каждую проверку — иначе его перестают читать. Полная картина по
запросу: команда /health.
"""

import logging
import os
from dataclasses import dataclass
from typing import Optional

from core.store import Store

log = logging.getLogger(__name__)

# Ожидания. Каждое число — не вкус, а граница, за которой поведение перестаёт
# объясняться нормальной работой.
MAX_CALLS_PER_LISTING = 1.5      # 1.0 в идеале; выше — признак повтора
MIN_CALLS_TO_JUDGE = 20          # на десятке вызовов среднее ничего не значит
SPEND_SPIKE_FACTOR = 3.0         # во столько раз сутки дороже обычного
SPEND_SPIKE_FLOOR = 0.5          # ниже этого скачки не стоят внимания, $
MAX_QUEUE = 300                  # объявлений в очереди к модели
MAX_PENDING = 50                 # застрявших в повторной обработке
QUIET_COLLECTORS_HOURS = 8       # столько без единого нового объявления
MAX_STUCK_ATTEMPTS = 20          # объявлений, упёршихся в предел попыток
MAX_DB_MB = 500


@dataclass
class Check:
    name: str
    ok: bool
    line: str

    def render(self) -> str:
        return ("✓ " if self.ok else "⚠ ") + self.line


async def _one(db, sql: str, *params):
    cur = await db.execute(sql, params)
    row = await cur.fetchone()
    return row[0] if row else None


async def collect(store: Store, db_path: Optional[str] = None) -> list:
    """Все проверки разом. Ничего не чинит и никому не пишет — только считает."""
    db = store._db
    checks = []

    # 1. Повторные обращения к модели. Та самая величина, которой не было.
    calls = await _one(db, "SELECT COUNT(*) FROM llm_usage"
                           " WHERE created_at >= datetime('now','-1 day')") or 0
    uniq = await _one(db, "SELECT COUNT(DISTINCT listing_id) FROM llm_usage"
                          " WHERE created_at >= datetime('now','-1 day')") or 0
    ratio = calls / uniq if uniq else 0
    if calls < MIN_CALLS_TO_JUDGE:
        checks.append(Check("llm_ratio", True,
                            f"обращений к модели за сутки: {calls} — мало для суждения"))
    else:
        checks.append(Check("llm_ratio", ratio <= MAX_CALLS_PER_LISTING,
                            f"вызовов модели на объявление: {ratio:.2f} "
                            f"(допустимо {MAX_CALLS_PER_LISTING}; "
                            f"{calls} вызовов на {uniq} объявлений)"))

    # 2. Расход суток против обычного. Ловит то же самое с другой стороны:
    #    цикл виден и как повтор, и как счёт.
    today = await _one(db, "SELECT COALESCE(SUM(cost_usd),0) FROM llm_usage"
                           " WHERE created_at >= datetime('now','-1 day')") or 0
    cur = await db.execute(
        "SELECT COALESCE(SUM(cost_usd),0) FROM llm_usage"
        " WHERE created_at >= datetime('now','-8 days')"
        "   AND created_at < datetime('now','-1 day')"
        " GROUP BY date(created_at) ORDER BY 1")
    days = [r[0] for r in await cur.fetchall()]
    typical = days[len(days) // 2] if days else 0
    spike = bool(typical and today > SPEND_SPIKE_FLOOR and today > typical * SPEND_SPIKE_FACTOR)
    checks.append(Check("spend", not spike,
                        f"расход за сутки: ${today:.3f}" +
                        (f" при обычных ${typical:.3f}" if typical
                         else " (сравнивать пока не с чем)")))

    # 3. Очередь к модели. Должна убывать; постоянно полная означает, что
    #    что-то в неё возвращается.
    #
    #    Считается ровно тем же условием, что и сама очередь, включая города
    #    активных профилей. Первая версия города не учитывала и показывала 84
    #    при реальной очереди в ноль: объявления из Хайфы к модели не идут и
    #    лежать в этом числе будут вечно. Показатель, который не совпадает с
    #    тем, что он измеряет, — это следующая ошибка того же рода.
    profiles = await store.active_profiles()
    cities = sorted({c for p in profiles for c in (p.get("cities") or [])})
    city_clause, params = "", []
    if cities:
        placeholders = ",".join("?" * len(cities))
        city_clause = f" AND (f.city IS NULL OR f.city IN ({placeholders}))"
        params = cities
    queue = await _one(db,
        "SELECT COUNT(*) FROM listings l JOIN listing_facts f ON f.listing_id = l.id"
        " WHERE f.llm_at IS NULL AND f.source_layer <> 'source'"
        "   AND l.status = 'extracted' AND l.junk_reason IS NULL"
        "   AND f.llm_attempts < 3" + city_clause, *params) or 0
    checks.append(Check("queue", queue <= MAX_QUEUE,
                        f"в очереди к модели: {queue} (порог {MAX_QUEUE})"))

    # 4. Приток объявлений. Молчащий сборщик выглядит как спокойный день.
    last = await _one(db, "SELECT MAX(collected_at) FROM listings")
    hours = None
    if last:
        hours = await _one(db, "SELECT ROUND((julianday('now') - julianday(?)) * 24, 1)", last)
    checks.append(Check("intake", bool(hours is not None and hours <= QUIET_COLLECTORS_HOURS),
                        f"последнее объявление собрано {hours} ч назад"
                        f" (порог {QUIET_COLLECTORS_HOURS})" if hours is not None
                        else "объявлений в базе нет"))

    # 5. Застрявшие в повторной обработке.
    pending = await _one(db, "SELECT COUNT(*) FROM listings WHERE status = 'pending'") or 0
    checks.append(Check("pending", pending <= MAX_PENDING,
                        f"ждут повторной обработки: {pending} (порог {MAX_PENDING})"))

    # 6. Объявления, упёршиеся в предел попыток за последние сутки. Считаем
    #    только свежие: накопленное за всю историю никогда не уменьшается, и
    #    такая проверка через неделю превращается в постоянно горящую лампочку,
    #    на которую перестают смотреть.
    stuck = await _one(db, "SELECT COUNT(*) FROM listing_facts"
                           " WHERE llm_attempts >= 3 AND llm_at >= datetime('now','-1 day')") or 0
    checks.append(Check("attempts", stuck < MAX_STUCK_ATTEMPTS,
                        f"упёрлись в предел попыток за сутки: {stuck} (порог {MAX_STUCK_ATTEMPTS})"))

    # 7. Доставка. Профиль с непустой очередью, которому сутки ничего не ушло, —
    #    это либо тихие часы длиной в день, либо поломка.
    silent = await _one(db,
        "SELECT COUNT(*) FROM search_profiles p WHERE p.is_paused = 0 AND p.is_enabled = 1"
        "   AND EXISTS (SELECT 1 FROM matches m WHERE m.profile_id = p.id AND m.state = 'new')"
        "   AND NOT EXISTS (SELECT 1 FROM matches m WHERE m.profile_id = p.id"
        "                     AND m.sent_at >= datetime('now','-1 day'))") or 0
    checks.append(Check("delivery", silent == 0,
                        f"профилей с непустой очередью и без отправок за сутки: {silent}"))

    # 8. Размер базы.
    if db_path and os.path.exists(db_path):
        mb = os.path.getsize(db_path) / 1024 / 1024
        checks.append(Check("db_size", mb <= MAX_DB_MB, f"база: {mb:.1f} МБ (порог {MAX_DB_MB})"))

    # 9. Мусор — справочно, порога нет.
    junk = await _one(db, "SELECT COUNT(*) FROM listings WHERE junk_reason IS NOT NULL") or 0
    checks.append(Check("junk", True, f"признано не объявлениями: {junk}"))

    return checks


def report(checks: list, only_bad: bool = False) -> str:
    lines = [c.render() for c in checks if not only_bad or not c.ok]
    return "\n".join(lines)


def failures(checks: list) -> list:
    return [c.name for c in checks if not c.ok]
