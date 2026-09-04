"""Визард настройки профиля поиска — чистая логика, без aiogram.

Состояние живёт в базе (`users.onboarding_step` и `users.onboarding_data`), а не
в памяти процесса: рестарт бота не должен стирать наполовину заполненный профиль.
Паттерн взят из Botkin, где он уже отработал.

Логика намеренно отделена от Telegram: сюда приходит строка ответа, отсюда
уходит следующий вопрос. Это позволяет прогнать весь визард тестом, не поднимая
бота, — а заодно переиспользовать его в Mini App, если он появится.

Свободный текстовый ввод критериев (описал поиск фразой — модель разобрала)
сознательно не делается: неточная формулировка даёт тихую ошибку в отборе,
которую человек заметит через неделю пропущенных квартир. Явные кнопки муторнее
ровно один раз — при настройке (решение D5).
"""

import json
import re
from typing import Optional, Tuple

# Три положения признака. Те же, что в core.match.
TRISTATE = [
    ("required", "обязательно"),
    ("allow_unknown", "можно без данных"),
    ("ignore", "неважно"),
]

CITY_CHOICES = [
    ("Tel Aviv", "Тель-Авив"),
    ("Ramat Gan", "Рамат-Ган"),
    ("Givatayim", "Гиватаим"),
    ("Bnei Brak", "Бней-Брак"),
    ("Bat Yam", "Бат-Ям"),
    ("Holon", "Холон"),
    ("Herzliya", "Герцлия"),
    ("Petah Tikva", "Петах-Тиква"),
]

PRICE_CHOICES = [4000, 5000, 6000, 7000, 8000, 9000, 10000]

# Тихие часы задаются диапазоном «с — по», час по израильскому времени.
# Интервал может пересекать полночь: 23–8 это ночь, а не двадцать один час.
QUIET_CHOICES = [
    ("23-8", "23:00 – 8:00"),
    ("22-9", "22:00 – 9:00"),
    ("0-7", "00:00 – 7:00"),
    ("none", "Не нужны, присылать круглосуточно"),
]
CAP_CHOICES = [20, 50, 100]
ROOMS_CHOICES = [1, 1.5, 2, 2.5, 3, 3.5, 4]


def _parse_number(text: str) -> Optional[float]:
    text = (text or "").replace(",", ".").replace(" ", "").replace("₪", "")
    m = re.search(r"\d+(?:\.\d+)?", text)
    return float(m.group()) if m else None


def parse_quiet(text: str):
    """«23-8», «с 23 до 8», «23:00 8:00» → (23, 8). Иначе None."""
    if (text or "").strip() == "none":
        return None
    nums = re.findall(r"\d{1,2}", (text or "").replace(":00", " "))
    if len(nums) != 2:
        return None
    start, end = int(nums[0]), int(nums[1])
    if not (0 <= start <= 23 and 0 <= end <= 23) or start == end:
        return None
    return start, end


STEPS = [
    {
        "key": "cities",
        "kind": "multi",
        "title": "Города",
        "question": "В каких городах ищем? Отметь все подходящие и нажми «Готово».",
        "options": CITY_CHOICES,
        "hint": "Соседние города дают заметно больше вариантов при тех же деньгах: "
                "на нашем корпусе добавление Рамат-Гана, Гиватаима, Бат-Яма и Холона "
                "к Тель-Авиву увеличивает выборку примерно на 40%.",
    },
    {
        "key": "price_max",
        "kind": "number",
        "title": "Потолок цены",
        "question": "Максимальная аренда в шекелях за месяц? Выбери или напиши своё число.",
        "options": [(str(p), f"{p:,}".replace(",", " ") + " ₪") for p in PRICE_CHOICES],
        "hint": "Это аренда без коммунальных: суммы ваад-байта и арноны мы из цены вычитаем, "
                "чтобы они не путались с арендной платой.",
    },
    {
        "key": "price_ideal",
        "kind": "number",
        "title": "Желаемая цена",
        "question": "А сколько хотелось бы платить? Дороже этой суммы объявления не отсекаются, "
                    "но опускаются в списке.",
        "options": [("skip", "Так же, как потолок")],
        "optional": True,
    },
    {
        "key": "rooms_min",
        "kind": "number",
        "title": "Комнаты",
        "question": "Минимум комнат? Счёт израильский: гостиная считается комнатой, "
                    "то есть «3 комнаты» это две спальни и салон.",
        "options": [(str(r), f"{r:g}") for r in ROOMS_CHOICES],
    },
    {
        "key": "req_mamad",
        "kind": "tristate",
        "title": "Мамад",
        "question": "Мамад — укреплённая комната внутри квартиры. Насколько он обязателен?",
        "hint": "«Можно без данных» стоит выбирать осознанно: примерно в 18% объявлений "
                "написано «מרחב מוגן» без уточнения, комната это в квартире или объём в "
                "подъезде. С требованием «обязательно» такие в выдачу не попадут, "
                "а с «можно без данных» попадут — и уточнить можно у хозяина.",
    },
    {
        "key": "req_elevator",
        "kind": "tristate",
        "title": "Лифт",
        "question": "Лифт нужен?",
    },
    {
        "key": "req_no_commission",
        "kind": "tristate",
        "title": "Комиссия маклера",
        "question": "Комиссия — это обычно месячная аренда сверху. Искать только без неё?",
        "hint": "«Обязательно» оставит объявления, где прямо написано «ללא תיווך». "
                "«Можно без данных» добавит те, где про комиссию не сказано — а это "
                "большинство: структурного поля под неё нет ни на одной доске, "
                "она встречается только в тексте.",
    },
    {
        "key": "req_pets",
        "kind": "tristate",
        "title": "Животные",
        "question": "Нужно, чтобы разрешали животных?",
        "hint": "Разрешение на животных пишут редко — примерно в 8% объявлений. "
                "С «обязательно» выдача сильно сузится.",
    },
    {
        "key": "delivery_mode",
        "kind": "choice",
        "title": "Как присылать",
        "question": "Присылать сразу или собирать в дайджест?",
        "options": [
            ("realtime", "Сразу, как появится"),
            ("digest", "Раз в день, дайджестом"),
        ],
        "hint": "Сразу — если хочешь написать хозяину первым. Хорошие варианты в Тель-Авиве "
                "уходят за час, но и сообщений будет больше.",
    },
    {
        "key": "digest_hour",
        "kind": "number",
        "title": "Время дайджеста",
        "question": "Во сколько присылать дайджест? Час по израильскому времени.",
        "options": [(str(h), f"{h}:00") for h in (8, 9, 12, 18, 20, 21)],
        # Шаг скрывается, только когда точно известно, что он не нужен. Иначе
        # счётчик «3/9» на середине превращался бы в «9/10» — мелочь, но она
        # выглядит как сбой.
        "hidden_if": lambda data: data.get("delivery_mode") == "realtime",
    },
    {
        "key": "quiet",
        "kind": "range",
        "title": "Тихие часы",
        "question": "Когда не беспокоить? Найденное за это время не пропадёт — "
                    "придёт, как только тихие часы закончатся.",
        "options": QUIET_CHOICES,
        "hint": "Можно написать свой диапазон, например «23-7».",
        "hidden_if": lambda data: data.get("delivery_mode") != "realtime",
    },
    {
        "key": "max_per_day",
        "kind": "number",
        "title": "Предел за сутки",
        "question": "Сколько объявлений за сутки считать нормой? Это страховка от "
                    "лавины, а не способ уменьшить поток.",
        "options": [(str(c), str(c)) for c in CAP_CHOICES],
        "hint": "Реальный поток даже при широких критериях — около 20 подходящих в "
                "сутки, и приходят они списками, а не по одному. До предела дело "
                "доходит только при ошибке в критериях или догоняющем сборе после "
                "простоя: тогда всё сверх него ждёт в /feed, а сюда придёт "
                "предупреждение.",
        "hidden_if": lambda data: data.get("delivery_mode") != "realtime",
    },
    {
        "key": "stop_words",
        "kind": "list",
        "title": "Стоп-слова",
        "question": "Слова, при которых объявление можно сразу отбрасывать. "
                    "Через запятую, или пропусти.",
        "options": [("skip", "Пропустить")],
        "optional": True,
        "hint": "Например: «חזית», «ללא מעלית», имя надоевшего агента.",
    },
]

STEP_INDEX = {s["key"]: i for i, s in enumerate(STEPS)}
DONE = "done"


def _visible(step: dict, data: dict) -> bool:
    cond = step.get("hidden_if")
    return not cond(data) if cond else True


def visible_keys(data: dict) -> list:
    """Шаги, применимые к этому профилю. Нужно и визарду, и меню /settings:
    предлагать «время дайджеста» тому, кто выбрал мгновенную доставку, — способ
    получить настройку, которая ни на что не влияет."""
    return [step["key"] for step in STEPS if _visible(step, data)]


def first_step() -> str:
    return STEPS[0]["key"]


def step_before(key: str, data: dict) -> Optional[str]:
    """Предыдущий видимый шаг. None — если это первый.

    Нужен для кнопки «назад»: без неё опечатка на третьем шаге означала
    пройти визард заново.
    """
    visible = [step["key"] for step in STEPS if _visible(step, data)]
    if key not in visible:
        return None
    index = visible.index(key)
    return visible[index - 1] if index > 0 else None


def step_after(key: str, data: dict) -> str:
    """Следующий видимый шаг. Пропускает те, чьё условие не выполнено."""
    start = STEP_INDEX.get(key, -1) + 1
    for step in STEPS[start:]:
        if _visible(step, data):
            return step["key"]
    return DONE


def progress(key: str, data: dict) -> str:
    visible = [s for s in STEPS if _visible(s, data)]
    keys = [s["key"] for s in visible]
    return f"{keys.index(key) + 1}/{len(keys)}" if key in keys else ""


def question(key: str, data: dict) -> dict:
    """Что показать пользователю на этом шаге."""
    step = next(s for s in STEPS if s["key"] == key)
    options = step.get("options")
    if step["kind"] == "tristate":
        options = TRISTATE
    return {
        "key": key,
        "title": step["title"],
        "progress": progress(key, data),
        "text": step["question"],
        "hint": step.get("hint"),
        "options": options or [],
        "kind": step["kind"],
        "optional": step.get("optional", False),
        "selected": data.get(key) if step["kind"] == "multi" else None,
    }


def apply(key: str, data: dict, answer: str) -> Tuple[bool, Optional[str]]:
    """Записать ответ. Возвращает (принято, текст ошибки).

    Данные меняются на месте: они же лежат в users.onboarding_data и сохраняются
    после каждого шага, чтобы рестарт ничего не терял."""
    step = next(s for s in STEPS if s["key"] == key)
    answer = (answer or "").strip()
    kind = step["kind"]

    if answer == "skip" and step.get("optional"):
        data[key] = None
        return True, None

    if kind == "multi":
        # каждый ответ переключает город; завершает шаг отдельная кнопка «Готово»
        chosen = list(data.get(key) or [])
        if answer == "done":
            if not chosen:
                return False, "Выбери хотя бы один город."
            return True, None
        valid = {code for code, _ in step["options"]}
        if answer not in valid:
            return False, "Не понял город. Выбери кнопкой."
        chosen.remove(answer) if answer in chosen else chosen.append(answer)
        data[key] = chosen
        return False, None            # шаг остаётся открытым

    if kind == "number":
        value = _parse_number(answer)
        if value is None:
            return False, "Нужно число. Можно выбрать кнопкой или написать своё."
        if key in ("price_max", "price_ideal") and not (1000 <= value <= 50000):
            return False, "Цена выглядит странно. Ожидаю от 1000 до 50000 ₪."
        if key == "rooms_min" and not (0.5 <= value <= 10):
            return False, "Комнат ожидаю от 0.5 до 10."
        if key == "digest_hour" and not (0 <= value <= 23):
            return False, "Час от 0 до 23."
        if key == "max_per_day" and not (5 <= value <= 500):
            return False, "Ожидаю от 5 до 500 объявлений в сутки."
        data[key] = int(value) if key != "rooms_min" else value
        return True, None

    if kind == "range":
        if answer == "none":
            data[key] = None
            return True, None
        parsed = parse_quiet(answer)
        if parsed is None:
            return False, "Нужны два часа, например «23-8»."
        data[key] = f"{parsed[0]}-{parsed[1]}"
        return True, None

    if kind == "tristate":
        if answer not in {code for code, _ in TRISTATE}:
            return False, "Выбери один из трёх вариантов."
        data[key] = answer
        return True, None

    if kind == "choice":
        if answer not in {code for code, _ in step["options"]}:
            return False, "Выбери вариант кнопкой."
        data[key] = answer
        return True, None

    if kind == "list":
        words = [w.strip() for w in re.split(r"[,;\n]", answer) if w.strip()]
        data[key] = words
        return True, None

    return False, "Не понял ответ."


def to_profile(data: dict) -> dict:
    """Перевести собранные ответы в поля search_profiles."""
    profile = {
        "cities": data.get("cities") or [],
        "price_max": data.get("price_max"),
        "price_ideal": data.get("price_ideal") or data.get("price_max"),
        "rooms_min": data.get("rooms_min"),
        "req_mamad": data.get("req_mamad", "ignore"),
        "req_elevator": data.get("req_elevator", "ignore"),
        "req_pets": data.get("req_pets", "ignore"),
        "req_no_commission": data.get("req_no_commission", "ignore"),
        "delivery_mode": data.get("delivery_mode", "digest"),
        "stop_words": data.get("stop_words") or [],
    }
    if data.get("delivery_mode") == "digest":
        profile["digest_hour"] = data.get("digest_hour", 9)
    else:
        # Тихие часы имеют смысл только при мгновенной доставке: дайджест и так
        # уходит в выбранный час.
        quiet = parse_quiet(data.get("quiet") or "none")
        profile["quiet_from"] = quiet[0] if quiet else None
        profile["quiet_to"] = quiet[1] if quiet else None
        profile["max_per_day"] = data.get("max_per_day") or 50
    return profile


def summary(data: dict) -> str:
    """Человеческое описание профиля — показывается в конце и в /settings."""
    names = dict(CITY_CHOICES)
    tri = dict(TRISTATE)
    cities = ", ".join(names.get(c, c) for c in (data.get("cities") or [])) or "не выбраны"
    lines = [
        f"Города: {cities}",
        f"Цена: до {data.get('price_max')} ₪" +
        (f", желательно до {data['price_ideal']}" if data.get("price_ideal") else ""),
        f"Комнат: от {data.get('rooms_min'):g}" if data.get("rooms_min") else "Комнат: любое",
        f"Мамад: {tri.get(data.get('req_mamad'), 'неважно')}",
        f"Лифт: {tri.get(data.get('req_elevator'), 'неважно')}",
        f"Животные: {tri.get(data.get('req_pets'), 'неважно')}",
        f"Комиссия: {tri.get(data.get('req_no_commission'), 'неважно')}",
    ]
    if data.get("delivery_mode") == "realtime":
        lines.append("Присылать: сразу")
        quiet = parse_quiet(data.get("quiet") or "none")
        lines.append(f"Тихие часы: {quiet[0]}:00 – {quiet[1]}:00" if quiet
                     else "Тихие часы: не заданы")
        lines.append(f"Предел за сутки: {data.get('max_per_day') or 50} объявлений")
    else:
        lines.append(f"Присылать: дайджестом в {data.get('digest_hour', 9)}:00")
    if data.get("stop_words"):
        lines.append("Стоп-слова: " + ", ".join(data["stop_words"]))
    return "\n".join(lines)
