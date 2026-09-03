"""Метаданные источников: какой канал про какой город.

Зачем. Первый живой прогон показал дыру в правиле «нет данных — не нарушение».
Для признаков квартиры оно верное: не написали про лифт — не повод выбрасывать.
Для города — нет. Объявления из @flamingorent (Хайфа и Крайот) не называют город
в тексте, потому что читателю канала он и так понятен. В результате они проходили
фильтр «Тель-Авив» как «город неизвестен» и занимали верх выдачи.

Отсюда две поправки:

1. Канал знает свой регион. Если в тексте города нет, берём подсказку отсюда —
   это не догадка, а факт об источнике.
2. Город в профиле получает те же три положения, что и остальные признаки
   (см. `core.match`): по умолчанию объявления без опознанного города в выдачу
   не идут, потому что город — самый весомый фильтр из всех.
"""

# region — используется как город по умолчанию, когда в тексте города нет.
# aggregator — канал перепечатывает объявления из других мест (в том числе из
#   Facebook) и часто сам расставляет теги. Не повод приглушать: это расширение
#   охвата, пересечения снимает дедупликация.
TELEGRAM_CHANNELS = {
    "Israel_arenda":            {"region": None,       "aggregator": True,
                                 "note": "вся страна, самый активный"},
    "jeremy_public":            {"region": "Tel Aviv",  "aggregator": True,
                                 "note": "агент, частично покрывает Facebook"},
    "jeremy_public_ramat_gan":  {"region": "Ramat Gan", "aggregator": True,
                                 "note": "агент, Рамат-Ган"},
    "aptfornew":                {"region": "Tel Aviv",  "aggregator": False,
                                 "note": "квартиры репатриантам, ТА и окрестности"},
    "snyat_kvartiruy":          {"region": None,        "aggregator": False,
                                 "note": "вся страна"},
    "isra_home_arenda":         {"region": None,        "aggregator": False,
                                 "note": "вся страна, редкие посты"},
    "flamingorent":             {"region": "Haifa",     "aggregator": False,
                                 "note": "Хайфа и Крайот — для поиска по Гуш-Дану бесполезен"},
    "ambery_longrent_telaviv":  {"region": "Tel Aviv",  "aggregator": False,
                                 "note": "последний пост 1 июля 2026 — канал мёртв"},
}

# Каналы, которые стоит отключить: мёртвые или про чужой регион. Решение по
# составу списка отложено (Q9), пока просто помечены.
STALE = {"ambery_longrent_telaviv"}


def region_of(channel: str):
    """Город по умолчанию для канала, если в тексте объявления города нет."""
    meta = TELEGRAM_CHANNELS.get(channel or "")
    return meta.get("region") if meta else None


def channels_for(cities) -> list:
    """Какие каналы имеет смысл читать для заданного набора городов.

    Канал без региона читаем всегда — он про всю страну. Канал с регионом
    читаем, только если этот регион кому-то нужен. Это то же правило, что для
    Yad2: не сканируем то, что никто не рассматривает.
    """
    wanted = set(cities or [])
    out = []
    for name, meta in TELEGRAM_CHANNELS.items():
        if name in STALE:
            continue
        region = meta.get("region")
        if region is None or not wanted or region in wanted:
            out.append(name)
    return out


# ── города: как они называются в каждом источнике ────────────────────────────
# Ключ — наше внутреннее английское имя (оно же приходит из визарда и лежит в
# профилях). Значения — то, чем город зовётся у Yad2 (числовой код) и у
# Homeless (ивритское название в URL).
#
# Города, которых здесь нет, просто не сканируются на этом источнике: это то же
# правило, что и для каналов — не запрашиваем то, что никто не ищет. В v1 в
# конфиге лежали шесть городов, а URL Yad2 был захардкожен на Тель-Авив, и пять
# из шести не запрашивались ни разу (F-10).

CITY_CODES = {
    "Tel Aviv":    {"yad2": "5000", "homeless": "תל אביב יפו", "komo": "תל אביב יפו"},
    "Ramat Gan":   {"yad2": "8600", "homeless": "רמת גן", "komo": "רמת גן"},
    "Givatayim":   {"yad2": "6900", "homeless": "גבעתיים", "komo": "גבעתיים"},
    "Bnei Brak":   {"yad2": "7900", "homeless": "בני ברק", "komo": "בני ברק"},
    "Bat Yam":     {"yad2": "6600", "homeless": "בת ים", "komo": "בת ים"},
    "Holon":       {"yad2": "6400", "homeless": "חולון", "komo": "חולון"},
    "Herzliya":    {"homeless": "הרצליה", "komo": "הרצליה"},
    "Petah Tikva": {"homeless": "פתח תקווה", "komo": "פתח תקווה"},
    "Jerusalem":   {"homeless": "ירושלים", "komo": "ירושלים"},
    "Haifa":       {"homeless": "חיפה", "komo": "חיפה"},
}


def source_cities(cities, source: str) -> list:
    """[(наше имя, код источника)] для тех городов, которые источник знает."""
    out = []
    for name in cities or []:
        code = CITY_CODES.get(name, {}).get(source)
        if code:
            out.append((name, code))
    return out


# Обратная карта: ивритское написание → наше имя. Нужна потому, что доски
# отдают в выдаче по городу и соседние населённые пункты тоже: в списке по
# Бней-Браку попалось объявление с районом «שאר העיר חולון», и город был
# подписан по запросу, а не по объявлению.
_HEBREW_TO_NAME = {}
for _name, _codes in CITY_CODES.items():
    for _key in ("homeless", "komo"):
        _value = _codes.get(_key)
        if _value:
            _HEBREW_TO_NAME[_value] = _name
_HEBREW_TO_NAME.update({
    "תל אביב": "Tel Aviv",
    "תל אביב-יפו": "Tel Aviv",
    "יפו": "Tel Aviv",
})


def city_from_hebrew(value: str, default: str = None) -> str:
    """Наше имя города по тому, как он написан в объявлении."""
    value = (value or "").strip()
    if value in _HEBREW_TO_NAME:
        return _HEBREW_TO_NAME[value]
    for hebrew, name in _HEBREW_TO_NAME.items():
        if hebrew and hebrew in value:
            return name
    return default


# ── как понимать, что объявление ещё живо ───────────────────────────────────
# Признак зависит от того, видим ли мы доску целиком (решение 0005).
#
#   presence — читаем всю доску, поэтому пропажа из выдачи означает снятие;
#   date     — либо объявление не снимается никогда (Telegram), либо мы видим
#              только верхушку и пропажа ничего не доказывает (Komo).
FRESHNESS = {
    "telegram": "date",
    "homeless": "presence",     # проходим все страницы
    "komo": "date",             # пагинация через AJAX, читаем только первую страницу
    "yad2": "date",
    "facebook": "date",
}
PRESENCE_SOURCES = tuple(k for k, v in FRESHNESS.items() if v == "presence")
MISSED_SCANS_TO_HIDE = 3        # три промаха подряд, чтобы разовый сбой не выкосил ленту

# Сколько дней объявление считается свежим — для источников, где признак «дата».
#
# Число не выдумано, а выведено из того, как долго объявление держится в той
# части источника, которую мы видим. У Komo мы читаем первую страницу, 20
# объявлений на город; за сутки из выдачи ушло 5 из 82, то есть слот
# обновляется примерно за 16 дней. С запасом в полтора раза — 24 дня. Ставить
# Komo те же 7 дней, что и Telegram, было бы неверно: мы выбрасывали бы
# объявления, которые доска всё ещё показывает.
#
# У Telegram другая природа: пост не исчезает никогда, и семь дней — это оценка
# того, сколько живёт актуальность самого объявления.
#
# Пересчитать: python3 scripts/measure_turnover.py
MAX_AGE_DAYS = {
    "telegram": 7,
    "komo": 24,
    "yad2": 14,
    "facebook": 7,
}
DEFAULT_MAX_AGE_DAYS = 7


def max_age_days(source: str) -> int:
    return MAX_AGE_DAYS.get(source, DEFAULT_MAX_AGE_DAYS)
