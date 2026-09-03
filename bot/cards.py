"""Оформление сообщений: карточка объявления и дайджест.

Состав карточки взят не из головы, а из того, чем пользовались в v1: адрес,
цена, комнаты, мамад. Плюс дата въезда — единственное, чего там не хватало
(из-за неё однажды состоялась поездка впустую). Остальные факты прячутся под
кнопку «подробнее», чтобы сообщение оставалось читаемым в ленте.

Кнопок 👍/👎 нет: в v1 они собрали 7 нажатий на 220 сообщений, а их задачу
теперь выполняет сортировка. Вместо них — действия, которые полезны самому
человеку: скрыть, отметить статус, сообщить об ошибке в данных.
"""

from html import escape

from core import market as market_mod

TRI = {"yes": "есть", "no": "нет", None: "нет данных"}

STATES = [
    ("saved", "🔖 В избранное"),
    ("contacted", "✍️ Написал"),
    ("waiting", "⏳ Жду ответа"),
    ("visit", "🚪 Еду смотреть"),
    ("rejected", "✖️ Не подошло"),
]


def _price(facts: dict) -> str:
    price = facts.get("price")
    if not price:
        return "цена не указана"
    out = f"{price:,}".replace(",", " ") + " ₪/мес"
    area = facts.get("area_sqm")
    if area:
        out += f" · {round(price / area)} ₪/м²"
    return out


def _mamad(facts: dict) -> str:
    if facts.get("mamad") == "yes":
        return "мамад есть"
    if facts.get("mamad") == "no":
        return "мамада нет"
    if facts.get("mamad_evidence"):
        # Ровно тот случай, ради которого заведено третье состояние: в тексте
        # написано «מרחב מוגן», но не сказано, в квартире это или в подъезде.
        return f"защищённое помещение упомянуто ({escape(facts['mamad_evidence'])}), " \
               f"но неясно, в квартире или в доме"
    return "про мамад не написано"


def _address(facts: dict) -> str:
    # Район часто совпадает с городом (для Рамат-Гана и Гиватаима это одно и то
    # же слово) — не повторяем его дважды.
    parts, seen = [], set()
    for value in (facts.get("street"), facts.get("district"), facts.get("city")):
        if value and value not in seen:
            parts.append(value); seen.add(value)
    return ", ".join(parts) or "адрес не указан"


def _entry(facts: dict) -> str:
    value = facts.get("entry_date")
    if not value:
        return "дата въезда не указана"
    return "въезд сразу" if value == "now" else f"въезд {value}"


def card(facts: dict, rank: float = None, reasons: list = None,
         price_history: list = None, own_medians: dict = None) -> str:
    """Короткая карточка. HTML-разметка Telegram."""
    rooms = facts.get("rooms")
    floor = facts.get("floor")
    head = [f"<b>{escape(_address(facts))}</b>", _price(facts)]

    line = []
    if rooms:
        line.append(f"{rooms:g} комн.")
    if facts.get("area_sqm"):
        line.append(f"{facts['area_sqm']} м²")
    if floor is not None:
        total = facts.get("total_floors")
        line.append(f"этаж {floor}" + (f" из {total}" if total else ""))
    if line:
        head.append(" · ".join(line))

    if own_medians is not None:
        assessment = market_mod.assess(facts, own_medians)
        label = market_mod.describe(assessment)
        if assessment and assessment["verdict"] != "market":
            head.append(f"{label} (медиана по району {assessment['expected']} ₪)")

    head.append(_mamad(facts))
    head.append(_entry(facts))

    # История цены появляется, только когда объявление публиковали повторно.
    # Падающая цена — сигнал к торгу, поэтому она в карточке, а не в деталях.
    if price_history and len(price_history) > 1:
        first, last = price_history[0]["price"], price_history[-1]["price"]
        if first != last:
            arrow = "снизилась" if last < first else "выросла"
            head.append(f"⚠️ публиковалось повторно, цена {arrow}: {first} → {last} ₪")
        else:
            head.append("⚠️ публиковалось повторно, цена та же")

    if rank is not None and reasons:
        top = ", ".join(name for name, value in reasons if value and value > 0)
        if top:
            head.append(f"<i>почему наверху: {escape(top)}</i>")

    url = facts.get("url")
    if url:
        head.append(f'<a href="{escape(url)}">открыть объявление</a>')
    return "\n".join(head)


def details(facts: dict) -> str:
    """Полный список фактов — под кнопкой «подробнее».

    Показываются и пустые поля: «нет данных» это ответ, а не отсутствие ответа,
    и по нему видно, о чём спрашивать хозяина."""
    rows = [
        ("Мебель", {"full": "полностью", "partial": "частично",
                    "none": "пустая"}.get(facts.get("furnished"), "нет данных")),
        ("Лифт", TRI.get(facts.get("elevator"), "нет данных")),
        ("Балкон", TRI.get(facts.get("balcony"), "нет данных")),
        ("Парковка", TRI.get(facts.get("parking"), "нет данных")),
        ("Кладовка", TRI.get(facts.get("storage"), "нет данных")),
        ("Кондиционер", TRI.get(facts.get("air_conditioning"), "нет данных")),
        ("Животные", TRI.get(facts.get("pets_allowed"), "нет данных")),
        ("После ремонта", TRI.get(facts.get("renovated"), "нет данных")),
        ("Миклат в доме", TRI.get(facts.get("miklat"), "нет данных")),
        ("Комиссия", facts.get("commission") or "нет данных"),
        ("Срок аренды", f"{facts['lease_months']} мес." if facts.get("lease_months") else "нет данных"),
        ("Источник", f"{facts.get('source', '?')} · {facts.get('channel') or '—'}"),
    ]
    return "\n".join(f"{name}: {value}" for name, value in rows)


def digest_line(facts: dict, own_medians: dict = None) -> str:
    """Одна строка дайджеста. Всё, что помещается на экран телефона, и ничего сверх.

    Порядок: цена · комнаты (отношение к рынку) · метраж · адрес · мамад.
    Оценка стоит вплотную к комнатам, потому что она про цену именно за такую
    квартиру: «3.5к (−38% к рынку)» читается как одно утверждение, а вынесенная
    в конец строки оценка теряла связь с тем, к чему относится.
    """
    price = facts.get("price")
    parts = [f"{price:,}".replace(",", " ") + " ₪" if price else "цена ?"]

    rooms = f"{facts['rooms']:g}к" if facts.get("rooms") else None
    market_note = ""
    if own_medians is not None:
        assessment = market_mod.assess(facts, own_medians)
        if assessment and assessment["verdict"] == "cheap":
            market_note = f" (−{abs(assessment['diff_pct'])}% к рынку)"
        elif assessment and assessment["verdict"] == "suspicious":
            market_note = " (подозрительно дёшево)"
    if rooms:
        parts.append(rooms + market_note)
    elif market_note:
        parts.append(market_note.strip(" ()"))

    if facts.get("area_sqm"):
        parts.append(f"{facts['area_sqm']} м²")
    parts.append(_address(facts))
    if facts.get("mamad") == "yes":
        parts.append("мамад")
    elif facts.get("mamad_evidence"):
        parts.append("мамад?")
    return " · ".join(escape(str(p)) for p in parts if p)


def digest(items: list, total: int = None, title: str = "Новое за сутки",
           own_medians: dict = None) -> str:
    """Компактный список: строка на объявление, вся строка — ссылка.

    Формат выбран под чтение с телефона: без карточек, без таблиц, без
    объяснений ранга. Реальный поток при настроенном профиле — 5–10 объявлений
    в день, и на экран они помещаются целиком.
    """
    if not items:
        return f"<b>{title}</b>\n\nСегодня ничего нового не нашлось."
    total = total if total is not None else len(items)
    lines = [f"<b>{title}: {total}</b>", ""]
    for facts in items:
        line = digest_line(facts, own_medians)
        url = facts.get("url")
        lines.append(f"• <a href=\"{escape(url)}\">{line}</a>" if url else f"• {line}")
    if total > len(items):
        lines.append("")
        lines.append(f"<i>Ещё {total - len(items)} — /feed</i>")
    return "\n".join(lines)
