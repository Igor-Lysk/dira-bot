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
         price_history: list = None) -> str:
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


def digest(items: list, title: str = "Новые квартиры") -> str:
    """Дайджест: компактный список с самым важным.

    Формат намеренно простой: одна строка на объявление плюс ссылка. Подробный
    разбор того, каким дайджест должен быть, отложен в отдельную задачу — здесь
    рабочий минимум, чтобы режим доставки существовал."""
    if not items:
        return f"<b>{title}</b>\n\nЗа сегодня ничего нового не нашлось."
    lines = [f"<b>{title}</b> — {len(items)}\n"]
    for i, facts in enumerate(items, 1):
        rooms = f"{facts['rooms']:g} комн" if facts.get("rooms") else "? комн"
        price = f"{facts['price']:,}".replace(",", " ") + " ₪" if facts.get("price") else "цена ?"
        mamad = "мамад" if facts.get("mamad") == "yes" else (
            "мамад?" if facts.get("mamad_evidence") else "")
        url = facts.get("url") or ""
        head = f"{i}. {price} · {rooms} · {escape(_address(facts))}"
        if mamad:
            head += f" · {mamad}"
        lines.append(f'<a href="{escape(url)}">{head}</a>' if url else head)
    return "\n".join(lines)
