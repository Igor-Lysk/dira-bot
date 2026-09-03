"""Варианты дайджеста.

Дайджест — это не «список того, что нашлось», а ответ на вопрос «стоит ли мне
сейчас что-то делать». Отсюда три требования, которые проверяются каждым
вариантом:

* видно за три секунды, есть ли ради чего открывать;
* из дайджеста можно попасть в объявление, не листая ленту;
* понятно, что изменилось со вчера, а не только что есть вообще.

Варианты различаются тем, чем жертвуют: плотностью, действиями или структурой.
Собраны отдельным модулем, чтобы можно было отправить их себе, посмотреть в
чате и выбрать, а не обсуждать словами.
"""

from html import escape

CITY_RU = {
    "Tel Aviv": "Тель-Авив", "Ramat Gan": "Рамат-Ган", "Givatayim": "Гиватаим",
    "Bnei Brak": "Бней-Брак", "Bat Yam": "Бат-Ям", "Holon": "Холон",
    "Herzliya": "Герцлия", "Petah Tikva": "Петах-Тиква", "Jerusalem": "Иерусалим",
    "Haifa": "Хайфа",
}


def _price(facts) -> str:
    p = facts.get("price")
    return f"{p:,}".replace(",", " ") + " ₪" if p else "цена ?"


def _rooms(facts) -> str:
    r = facts.get("rooms")
    return f"{r:g} комн" if r else "? комн"


def _where(facts, short=False) -> str:
    city = CITY_RU.get(facts.get("city"), facts.get("city") or "")
    if short:
        return facts.get("street") or facts.get("district") or city
    parts = [facts.get("street"), city]
    return ", ".join(p for p in parts if p) or "адрес не указан"


def _mamad(facts) -> str:
    if facts.get("mamad") == "yes":
        return "мамад"
    if facts.get("mamad_evidence"):
        return "мамад?"
    return ""


def _link(facts, text) -> str:
    url = facts.get("url")
    return f'<a href="{escape(url)}">{escape(text)}</a>' if url else escape(text)


# ── А. Компактный список ─────────────────────────────────────────────────────
# Плотнее некуда: строка на объявление, всё существенное и ссылка. Жертвует
# структурой — при десяти городах читается хуже.

def compact(items: list, title: str = "Новое за сегодня") -> str:
    if not items:
        return f"<b>{title}</b>\n\nНичего нового."
    lines = [f"<b>{title} — {len(items)}</b>", ""]
    for i, f in enumerate(items, 1):
        tail = _mamad(f)
        line = f"{i}. {_price(f)} · {_rooms(f)} · {_where(f)}"
        if tail:
            line += f" · {tail}"
        lines.append(_link(f, line))
    lines += ["", "Вся лента и сортировки — /feed"]
    return "\n".join(lines)


# ── Б. С группировкой по городам ─────────────────────────────────────────────
# Отвечает на вопрос «где сегодня появилось», а не только «что». Полезно, когда
# городов несколько и они не равноценны.

def by_city(items: list, title: str = "Новое за сегодня") -> str:
    if not items:
        return f"<b>{title}</b>\n\nНичего нового."
    groups = {}
    for f in items:
        groups.setdefault(CITY_RU.get(f.get("city"), f.get("city") or "без города"), []).append(f)
    lines = [f"<b>{title} — {len(items)}</b>"]
    for city, group in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        lines += ["", f"<b>{escape(city)}</b> · {len(group)}"]
        for f in group:
            tail = _mamad(f)
            line = f"· {_price(f)} · {_rooms(f)} · {_where(f, short=True)}"
            if tail:
                line += f" · {tail}"
            lines.append(_link(f, line))
    lines += ["", "Вся лента и сортировки — /feed"]
    return "\n".join(lines)


# ── Г. Таблицей ──────────────────────────────────────────────────────────────
# Самое плотное представление: колонки выравниваются, глаз сравнивает цены
# по вертикали. Жертвует ссылками — в моноширинном блоке они не кликаются,
# поэтому под таблицей идут номера со ссылками.

def table(items: list, title: str = "Новое за сегодня") -> str:
    if not items:
        return f"<b>{title}</b>\n\nНичего нового."
    rows = ["  ц е н а   комн  м²   район"]
    for i, f in enumerate(items, 1):
        price = f"{f['price']:>6,}".replace(",", " ") if f.get("price") else "     ?"
        rooms = f"{f['rooms']:>4g}" if f.get("rooms") else "   ?"
        area = f"{f['area_sqm']:>3}" if f.get("area_sqm") else "  ?"
        where = (_where(f, short=True) or "")[:18]
        mark = "✓" if f.get("mamad") == "yes" else ("?" if f.get("mamad_evidence") else " ")
        rows.append(f"{i:>2} {price} {rooms} {area} {mark} {where}")
    links = " · ".join(_link(f, str(i)) for i, f in enumerate(items, 1))
    return (f"<b>{title} — {len(items)}</b>\n<pre>" + "\n".join(escape(r) for r in rows) +
            "</pre>\n" + links + "\n\nВся лента — /feed")


# ── В. Топ карточками + хвост ────────────────────────────────────────────────
# Не текст, а сценарий: три полноценные карточки с кнопками действий и одна
# строка про остальное. Жертвует полнотой ради того, что по верхним трём можно
# сразу что-то сделать, не открывая ленту.

def top_head(items: list, shown: int, title: str = "Новое за сегодня") -> str:
    return (f"<b>{title} — {len(items)}</b>\n"
            f"Показываю {shown} лучших по релевантности, остальное в /feed.")


def top_tail(items: list, shown: int) -> str:
    rest = items[shown:]
    if not rest:
        return ""
    cheapest = min((f for f in rest if f.get("price")), key=lambda f: f["price"], default=None)
    line = f"Ещё {len(rest)} — /feed"
    if cheapest:
        line += f"\nСамое дешёвое из них: {_price(cheapest)} · {_where(cheapest)}"
    return line
