"""Детерминированный слой извлечения: текст → Facts.

Слой работает ДО обращения к LLM. Всё, что он извлёк уверенно, модель больше
не спрашивают (закрывает F-06: в v1 половину вызовов можно было не делать).

Что здесь добавлено по сравнению с портированным кодом FB_scrapper:

1. **Явные отрицания.** В FB_scrapper отрицания просто вычищались из текста
   (`strip_negations`), чтобы не давать ложных срабатываний. Для v2 этого мало:
   "ללא מעלית" должно давать `elevator = NO`, а не отсутствие признака.
   Иначе теряется разница между "лифта нет" и "про лифт не написано" — а это
   ровно та разница, ради которой введены три состояния (D3).

2. **Этаж.** В FB_scrapper его не было; здесь разбираются формы
   "קומה 3", "קומה 3 מתוך 5", "3rd floor", "1 этаж", "קומת קרקע".

3. **Тип сделки.** Аренда / продажа / саблет / комната в квартире — отдельным
   полем, а не фильтром на входе.
"""

import re
from typing import Optional

from . import rules
from .schema import Facts, YES, NO

# ── явные отрицания ──────────────────────────────────────────────────────────
# Ключ — поле схемы, значение — что именно отрицается. Отрицание засчитывается,
# только если стоит непосредственно перед признаком (до 3 слов-разделителей),
# либо внутри слэш-перечисления вида "אין מעלית/ממד/מקלט".

_NEG_WORD = r'(?:ללא|אין|בלי|לא|no\b|without|not\b|без|нет)'

_NEG_PATTERNS = {
    "elevator":         r'מעלית\w*|elevator|lift|лифт\w*',
    "parking":          r'חנ[יי]ה\w*|חניון|parking|парков\w*',
    "mamad":            r'ממ["\'״״]?ד|מרחב\s+מוגן|safe.?room',
    "miklat":           r'מקלט\w*|shelter|убежищ\w*|укрыти\w*',
    "balcony":          r'מרפסת\w*|balcony|балкон\w*',
    "air_conditioning": r'מזגן\w*|מיזוג\w*|ממוזג\w*|a/?c\b|air.?cond|кондиц\w*',
    "storage":          r'מחסן|storage|кладовк\w*',
    "garden":           r'גינה\w*|חצר\w*|garden|yard|сад\b|двор\b',
    "pets_allowed":     r'בעלי\s+חיים|בע["\'״]?ח\b|חיות\s+מחמד|pets?\b|животн\w*|питомц\w*',
}

# "אין מעלית/ממד/מקלט" — отрицание распространяется на весь слэш-список.
def _negated(txt: str, target: str) -> bool:
    direct = re.compile(
        _NEG_WORD + r'[\s,:]{0,3}(?:\S+/){0,4}\s*(?:' + target + r')',
        re.IGNORECASE,
    )
    if direct.search(txt):
        return True
    # обратный порядок: "מעלית אין" / "лифта нет"
    reverse = re.compile(
        r'(?:' + target + r')[\s,:]{0,3}(?:אין|нет|отсутств\w*)',
        re.IGNORECASE,
    )
    return bool(reverse.search(txt))


# ── этаж ─────────────────────────────────────────────────────────────────────

_FLOOR_OF_RE = re.compile(
    r'קומה\s*(\d{1,2})\s*(?:מתוך|/)\s*(\d{1,2})', re.IGNORECASE)
_FLOOR_RE = re.compile(
    r'קומה\s*(\d{1,2})|(\d{1,2})\s*(?:st|nd|rd|th)?\s*floor|'
    r'(\d{1,2})\s*[-–]?\s*(?:й\s*)?этаж',
    re.IGNORECASE)
_GROUND_RE = re.compile(r'קומת\s*קרקע|ground\s*floor|первый\s*этаж', re.IGNORECASE)


def _extract_floor(txt: str):
    m = _FLOOR_OF_RE.search(txt)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = _FLOOR_RE.search(txt)
    if m:
        v = next((g for g in m.groups() if g), None)
        if v is not None and 0 <= int(v) <= 60:
            return int(v), None
    if _GROUND_RE.search(txt):
        return 0, None
    return None, None



# ── комната в квартире с соседями ────────────────────────────────────────────
#
# Первый же сквозной прогон вытащил наверх «6-комнатную квартиру в Тель-Авиве
# за 2000 ₪». Цена извлеклась верно — это аренда КОМНАТЫ в квартире с соседями,
# а «6 комнат» относится ко всей квартире. Такие объявления в ленте поиска
# квартиры бесполезны и вдобавок всплывают на первые места, потому что дешёвые.
#
# Портированный is_shared_apartment ловит «דירת שותפים» и «חדר בשותפות», но не
# ловит самую частую формулировку: «מפנה את החדר שלי» — «освобождаю свою комнату».
# Здесь список расширен. Тот же класс объявлений, что в v1 отсекался фильтром
# саблетов, только формулировки другие.

_SHARED_EXTRA = re.compile(
    r'מפנה\s+(?:את\s+)?(?:ה)?חדר|'          # освобождаю комнату
    r'מתפנה\s+(?:ה)?חדר|'                    # освобождается комната
    r'חדר\s+ב?דירת\s+\d|'                   # комната в N-комнатной квартире
    r'חדר\s+ב?דירה\s+(?:מרווחת|משותפת)|'
    r'מחפש\w*\s+(?:שותפ\w*|מחליף\w*)|'      # ищу соседа / замену
    r'שותפ(?:ים|ה|ות)\b|'
    r'сда[её]тся\s+комнат|сда[юм]\s+комнат|'
    r'ищу\s+(?:соседа|сосед\w*|подселени|замену)|'
    r'комнат\w*\s+в\s+(?:квартире|доме)\s+с\s+сосед|'
    r'room\s+in\s+(?:a\s+)?(?:shared|\d)',
    re.IGNORECASE)

# «не саблет» / «не подселение» — продавец сам подчёркивает обратное
_NOT_SHARED = re.compile(
    r'(?:לא|ללא)\s+שותפ|не\s+саблет|не\s+подселени|not\s+a\s+sublet|НЕ\s+дел[её]нка',
    re.IGNORECASE)


def _is_shared(txt: str) -> bool:
    if _NOT_SHARED.search(txt):
        return False
    return bool(rules.is_shared_apartment(txt) or _SHARED_EXTRA.search(txt))


# ── тип сделки ───────────────────────────────────────────────────────────────

_SALE_RE = re.compile(r'למכירה|for\s+sale|продаётся|продается|продам', re.IGNORECASE)
# «סבלט» без алефа — самое частое написание, портированный фильтр его не знал.
# Посуточная аренда опознаётся отдельно: «מחיר לילה 2000 ₪» — это цена за НОЧЬ,
# и без этой проверки она попадает в базу как месячная аренда шестикомнатного
# дома за 2000 ₪ и уходит на первое место в ленте, потому что дешевле всех.
_SUBLET_RE = re.compile(
    r'ס[א]?בלט|sublet|саблет|краткосроч\w*|short.?term|'
    r'לתקופה\s+קצרה|для\s+туристов|посуточн\w*',
    re.IGNORECASE)

_PER_NIGHT_RE = re.compile(
    r'מחיר\s+לילה|ל?לילה\b|per\s+night|/\s*night|за\s+ночь|за\s+сутки|בלילה\b|'
    r'מחיר\s+ליום|per\s+day',
    re.IGNORECASE)


def is_per_night(txt: str) -> bool:
    """Цена указана за ночь или за сутки, а не за месяц."""
    return bool(_PER_NIGHT_RE.search(txt))


def _deal_type(txt: str) -> Optional[str]:
    if _is_shared(txt):
        return "shared"
    if _SUBLET_RE.search(txt):
        return "sublet"
    if _SALE_RE.search(txt):
        return "sale"
    if re.search(r'להשכרה|שכירות|for\s+rent|аренда|сдам|сдаётся|сдается', txt, re.IGNORECASE):
        return "rent"
    return None


# ── площадь ──────────────────────────────────────────────────────────────────

_SQM_RE = re.compile(r'(\d{2,3})\s*(?:מ["\'״]?ר\b|sqm\b|מטר(?:\s+רבוע)?\b|кв\.?\s*м)', re.IGNORECASE)


def _area(txt: str):
    m = _SQM_RE.search(txt)
    if m:
        v = int(m.group(1))
        if 15 <= v <= 500:
            return v
    return None






# ── цена: добор после портированного парсера ─────────────────────────────────
#
# Оригинальный extract_price писался под тель-авивский Facebook, где аренда
# начинается примерно от 4000 ₪. Поэтому в проходах без ключевого слова стоит
# нижняя граница 3000. На корпусе Telegram это отсекало реальные объявления:
# «💰 ₪2500 включая счета» в Хайфе — нормальная цена, а не опечатка.
#
# Здесь добор с нижней границей 1500, но только когда рядом есть валюта или
# денежный маркер. Без этого условия в цену немедленно попадают куски телефонов
# (+972-52-957-3304) — на этом корпусе это самая частая ложная сработка.

_CUR_TOK = r'(?:ש[\'"\u05f4]?ח\b|₪|שקל\w*|שח\b|шек\w*|NIS\b|ILS\b|руб)'
_MONEY_MARK = r'(?:💰|💵|💸|🏷)'

_PRICE_FALLBACK = [
    re.compile(_CUR_TOK + r'\s*(\d[\d,\. ]{2,8})', re.IGNORECASE),          # ₪2500
    re.compile(r'(\d[\d,\. ]{2,8})\s*' + _CUR_TOK, re.IGNORECASE),          # 2500 ₪
    re.compile(_MONEY_MARK + r'[^\d\n]{0,12}(\d[\d,\. ]{2,8})'),           # 💰 2500
]


def _price(txt: str):
    v = rules.extract_price(txt)
    if v is not None:
        return v
    for rx in _PRICE_FALLBACK:
        for m in rx.finditer(txt):
            raw = m.group(1).replace(",", "").replace(".", "").replace(" ", "")
            if not raw.isdigit():
                continue
            n = int(raw)
            if 1500 <= n <= 200000:
                return n
    return None


# ── комнаты: русские и ивритские формы, которых нет в портированном парсере ──
#
# Замеры на корпусе Telegram показали 57% покрытия против 88% на ивритском
# Facebook. Разрыв объясняется двумя вещами, обе чинятся регулярками:
#
# 1. Дефис. «2-комнатная», «2-х комнатная», «4‑комнатная» — в оригинале между
#    числом и словом допускались только пробелы, поэтому самая частая русская
#    форма не ловилась вообще. Отдельно: в постах встречается неразрывный дефис
#    U+2011 и разные тире, их тоже надо принимать.
#
# 2. Спальни. «2 חדרי שינה» — это две СПАЛЬНИ, а по израильскому счёту такая
#    квартира трёхкомнатная: гостиная считается комнатой. Пересчёт спален в
#    комнаты (+1 за салон) — та же логика, из-за отсутствия которой в v1
#    двухкомнатные квартиры проходили как подходящие.

_DASH = r'[\s\-\u2010-\u2015]*'

_ROOMS_RU_RE = re.compile(
    r'(\d+(?:[.,]\d)?)' + _DASH + r'(?:х|ти)?' + _DASH + r'комнат',
    re.IGNORECASE)

_BEDROOMS_HE_RE = re.compile(r'(\d+(?:[.,]\d)?)\s*חדרי\s+שינה')
_BEDROOMS_EN_RE = re.compile(r'(\d+)' + _DASH + r'bed\s?rooms?\b', re.IGNORECASE)

_STUDIO_RE = re.compile(r'студи[яию]|\bstudio\b|סטודיו', re.IGNORECASE)

# «Комнаты: 3», «Комнат: 2», «Количество комнат: 3» — подпись стоит перед числом.
# Самая частая форма в русскоязычных каналах, и её оригинальный парсер не брал:
# он ожидал число слева от слова.
_ROOMS_LABEL_RE = re.compile(
    r'(?:кол-?в[оа]\s+)?комнат\w*\s*[:：]?\s*(\d+(?:[.,]\d)?)', re.IGNORECASE)

# «Трёхкомнатная», «2-х комнатная» словами. Встречается реже цифр, но регулярно.
# То же самое на иврите: «חדרים: 3». Портированный парсер знал только обратный
# порядок («3 חדרים»), а подпись перед числом — 66 объявлений в корпусе, больше
# всех прочих пропусков вместе взятых. Допускаем звёздочки и пробелы между
# словом и числом: в постах встречается «חדרים:** 2».
_ROOMS_LABEL_HE_RE = re.compile(
    "\u05d7\u05d3(?:\u05e8\u05d9\u05dd|\u05e8)"      # חדרים / חדר
    "[\'\"\u05f4]?\\s*[:\uff1a]?[\\s*_]{0,4}(\\d+(?:[.,]\\d)?)")

_WORD_ROOMS = {
    "одно": 1, "двух": 2, "двух-": 2, "трех": 3, "трёх": 3, "четырех": 4,
    "четырёх": 4, "пяти": 5, "шести": 6,
}
_WORD_ROOMS_RE = re.compile(
    r'\b(одно|двух|тр[её]х|четыр[её]х|пяти|шести)[\s\-]?комнат', re.IGNORECASE)

# «2 спальни», «с 3 спальнями» — по израильскому счёту это на комнату больше:
# спальни плюс салон. Та же поправка, что для «חדרי שינה».
_BEDROOMS_RU_RE = re.compile(r'(\d+)\s*спал[ья]\w*', re.IGNORECASE)

# Комнаты, которые комнатами не считаются. Без этого «1 ванная комната» и
# «защищённая комната» превращали квартиру в однокомнатную.
_NOT_A_ROOM_RE = re.compile(
    r'(?:ванн\w*|душев\w*|служебн\w*|подсобн\w*|защищённ\w*|защищенн\w*|'
    r'гардеробн\w*|детск\w*)\s+комнат', re.IGNORECASE)


def _rooms(txt: str):
    """Комнаты по израильскому счёту: гостиная считается комнатой."""
    # сначала убираем «ванная комната» и подобное, чтобы они не считались
    clean = _NOT_A_ROOM_RE.sub(" ", txt)

    m = _ROOMS_LABEL_HE_RE.search(clean)
    if m:
        value = float(m.group(1).replace(",", "."))
        if 1 <= value <= 10:
            return value

    m = _ROOMS_LABEL_RE.search(clean)
    if m:
        value = float(m.group(1).replace(",", "."))
        tail = clean[m.end():m.end() + 20]
        if re.match(r"\s*спал", tail, re.IGNORECASE):
            value += 1                      # «Комнат: 2 спальни» — это 3 комнаты
        if 1 <= value <= 10:
            return value

    m = _WORD_ROOMS_RE.search(clean)
    if m:
        key = m.group(1).lower().replace("ё", "е")
        value = {"одно": 1, "двух": 2, "трех": 3, "четырех": 4,
                 "пяти": 5, "шести": 6}.get(key)
        if value:
            return float(value)

    m = _BEDROOMS_RU_RE.search(clean)
    if m:
        value = float(m.group(1)) + 1
        if 1 <= value <= 10:
            return value

    txt = clean
    m = _ROOMS_RU_RE.search(txt)
    if m:
        v = float(m.group(1).replace(",", "."))
        if 1 <= v <= 10:
            return v

    for rx in (_BEDROOMS_HE_RE, _BEDROOMS_EN_RE):
        m = rx.search(txt)
        if m:
            v = float(m.group(1).replace(",", ".")) + 1   # + салон
            if 1 <= v <= 10:
                return v

    v = rules.extract_rooms(txt)
    if v is not None:
        return v

    if _STUDIO_RE.search(txt):
        return 1.0
    return None


# ── убежище: мамад, миклат и «непонятно чьё» ────────────────────────────────
# Портированный classify_safe_room писался под ивритоязычный Facebook. На
# русскоязычном корпусе Telegram он почти слеп: из 748 объявлений маркер
# «ממ"ד» встречается 9 раз, зато «מרחב מוגן» — 131 раз, а русское «мамад» — 38.
# Поэтому здесь: добавлены русские и английские формы, и главное — отдельное
# поле mamad_evidence.
#
# «מרחב מוגן» без уточнения — это НЕ «нет данных» и НЕ «есть мамад»: это
# «защищённое помещение упомянуто, но непонятно, в квартире оно или в доме».
# Таких объявлений 18% корпуса, выбрасывать их нельзя, засчитывать за мамад —
# тоже. Поэтому mamad остаётся UNKNOWN, а evidence хранит найденную фразу, и
# фильтр профиля получает три осмысленных положения:
#     обязателен  ·  допускаю неясное  ·  неважно

_MAMAD_RU_EN = re.compile(
    r'мамад\w*|мам["\'״]?д|safe.?room|защищённ\w*\s+комнат\w*|защищенн\w*\s+комнат\w*',
    re.IGNORECASE)
_MAMAD_NEG_RU = re.compile(r'(?:без|нет|no|without)\s+мамад\w*', re.IGNORECASE)
_AMBIG_RE = re.compile(r'מרחב\s+מוגן|защищённ\w*\s+помещени\w*|защищенн\w*\s+помещени\w*',
                       re.IGNORECASE)


def _safe_room(txt: str):
    """Вернуть (mamad, miklat, evidence)."""
    tag = rules.classify_safe_room(txt)

    mamad = miklat = None
    evidence = None

    if tag == "mamad":
        mamad = YES
    elif tag == "miklat":
        miklat = YES
    elif tag == "safe room":
        m = _AMBIG_RE.search(txt)
        evidence = m.group(0) if m else "מרחב מוגן"

    # русские и английские формы, которых портированный классификатор не знает
    if mamad is None:
        m = _MAMAD_RU_EN.search(txt)
        if m and not _MAMAD_NEG_RU.search(txt):
            mamad = YES
            evidence = evidence or m.group(0)

    if mamad is None and evidence is None:
        m = _AMBIG_RE.search(txt)
        if m:
            evidence = m.group(0)

    # русское «миклат» портированный классификатор тоже не знает
    if miklat is None and mamad is None:
        if re.search(r'миклат\w*', txt, re.IGNORECASE) and not re.search(
                r'(?:без|нет)\s+миклат', txt, re.IGNORECASE):
            miklat = YES

    return mamad, miklat, evidence


# ── город ────────────────────────────────────────────────────────────────────
# В FB_scrapper города искали только среди «не Тель-Авив», потому что все 24
# группы были по Гуш-Дану и умолчанием служил Тель-Авив. В v2 источники шире
# (Telegram, Yad2, доски), поэтому Тель-Авив тоже надо распознавать явно, а
# заодно и города вне зоны интереса: город — жёсткий фильтр, и половина отказов
# в v1 была именно по нему.

_CITY_PATTERNS = [
    ("Tel Aviv",    r'תל[\s\-]?אביב|ת["\'״]א\b|tel[\s\-]?aviv|tlv\b|тель[\s\-]?авив'),
    ("Ramat Gan",   r'רמת\s*גן|ramat.?gan|\bר["״]ג\b|рамат[\s\-]?ган'),
    ("Givatayim",   r'גבעתיים|givatayim|гиватаим'),
    ("Bnei Brak",   r'בני\s*ברק|bnei.?brak|бней[\s\-]?брак'),
    ("Bat Yam",     r'בת\s*ים|bat.?yam|бат[\s\-]?ям'),
    ("Holon",       r'חולון|holon|холон'),
    ("Herzliya",    r'הרצליה|herzliya|герцли'),
    ("Petah Tikva", r'פתח\s*תקווה|petah.?tikva|петах[\s\-]?тикв'),
    ("Rishon",      r'ראשון\s*לציון|rishon|ришон'),
    ("Netanya",     r'נתניה|netanya|нетани'),
    ("Jerusalem",   r'ירושלים|jerusalem|иерусалим'),
    ("Haifa",       r'חיפה|haifa|хайф'),
    ("Ashdod",      r'אשדוד|ashdod|ашдод'),
    ("Beer Sheva",  r'באר\s*שבע|beer.?sheva|беэр[\s\-]?шев'),
]


def _city(txt: str) -> Optional[str]:
    """Первый упомянутый город — по позиции в тексте, а не по порядку в списке.

    Порядок важен: в объявлении «דירה ברמת גן, 10 דקות מתל אביב» город — Рамат-Ган,
    а Тель-Авив упомянут как ориентир. Побеждает тот, кто встретился раньше.
    """
    best, best_pos = None, len(txt) + 1
    for name, pattern in _CITY_PATTERNS:
        m = re.search(pattern, txt, re.IGNORECASE)
        if m and m.start() < best_pos:
            best, best_pos = name, m.start()
    return best



# ── признаки: русские формулировки ───────────────────────────────────────────
# Портированный extract_features знает по-русски немного: «отремонт», «кладовка»,
# «меблирована». Реальные объявления пишут иначе — «после ремонта», «с мебелью»,
# «свободна с 1 июля», «от хозяина». Добавляем без изменения оригинала.

_RU_FEATURES = {
    "renovated":       r'после\s+ремонта|с\s+ремонтом|новый\s+ремонт|евроремонт|отремонтир\w*',
    "storage":         r'кладовк\w*|мачсан|склад\w*\s+в\s+доме',
    "immediate_entry": r'свободна\s+сейчас|заселение\s+сразу|въезд\s+сразу|можно\s+сразу|немедленн\w*\s+въезд',
    "no_broker":       r'без\s+посредник\w*|без\s+маклер\w*|от\s+хозяин\w*|от\s+собственник\w*|без\s+комисси\w*',
    "air_conditioning": r'кондиционер\w*|сплит[\s\-]?систем\w*',
    "balcony":         r'балкон\w*|лоджи\w*',
    "parking":         r'парковк\w*\s+(?:в\s+доме|подземн\w*|своя|частн\w*)|подземн\w*\s+парковк\w*|машиноместо',
    "elevator":        r'\bлифт\w*',
}

_RU_FURNISHED_FULL = re.compile(r'полностью\s+меблирован\w*|вся\s+мебель|с\s+мебелью\s+и\s+техник\w*', re.IGNORECASE)
_RU_FURNISHED_PART = re.compile(r'частично\s+меблирован\w*|част\w*\s+мебел\w*|с\s+мебелью', re.IGNORECASE)


def _apply_ru_features(txt: str, f) -> None:
    """Дополнить признаки русскими формулировками. Уже проставленное не трогаем,
    в том числе явные отрицания — они выставляются позже и перебивают всё."""
    for fieldname, pattern in _RU_FEATURES.items():
        if getattr(f, fieldname) is None and re.search(pattern, txt, re.IGNORECASE):
            setattr(f, fieldname, YES)
    if f.furnished is None:
        if _RU_FURNISHED_FULL.search(txt):
            f.furnished = "full"
        elif _RU_FURNISHED_PART.search(txt):
            f.furnished = "partial"


# ── главная функция ──────────────────────────────────────────────────────────

# Позитивные признаки берём из тегов, которые уже умеет ставить портированный
# extract_features (он сам вычищает отрицания перед проверкой).
_TAG_TO_FIELD = {
    "elevator": "elevator",
    "parking": "parking",
    "balcony": "balcony",
    "storage": "storage",
    "A/C": "air_conditioning",
    "garden/yard": "garden",
    "pets ok": "pets_allowed",
    "renovated": "renovated",
    "immediate entry": "immediate_entry",
    "no broker": "no_broker",
}


def extract(text: str) -> Facts:
    """Разобрать текст объявления в факты. Ничего не выдумывает: чего нет — None."""
    f = Facts()
    if not text or not text.strip():
        return f

    tags = set(t.strip() for t in (rules.extract_features(text) or "").split(";"))

    # значения
    f.price = _price(text)
    if f.price is not None and is_per_night(text):
        # цена за ночь месячной арендой не является — оставляем «нет данных»
        f.price = None
    f.rooms = _rooms(text)
    f.area_sqm = _area(text)
    f.floor, f.total_floors = _extract_floor(text)
    f.district = rules.extract_district(text)
    f.city = _city(text) or (rules.get_city(f.district) if f.district else None)
    f.deal_type = _deal_type(text)
    # extract_phones возвращает множество — приводим к списку, иначе не сериализуется
    f.phones = sorted(rules.extract_phones(text) or [])
    f.fingerprint = rules.text_fingerprint(text)

    if "furnished" in tags:
        f.furnished = "full"
    elif "partly furnished" in tags:
        f.furnished = "partial"

    # убежище: mamad и miklat — разные поля, не одно
    f.mamad, f.miklat, f.mamad_evidence = _safe_room(text)

    # позитивные признаки
    for tag, fieldname in _TAG_TO_FIELD.items():
        if tag in tags:
            setattr(f, fieldname, YES)

    _apply_ru_features(text, f)

    # явные отрицания перебивают отсутствие признака
    for fieldname, target in _NEG_PATTERNS.items():
        if _negated(text, target):
            setattr(f, fieldname, NO)

    return f
