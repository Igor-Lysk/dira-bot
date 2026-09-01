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
