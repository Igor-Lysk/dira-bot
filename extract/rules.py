"""Детерминированное извлечение фактов из текста объявления.

Портировано из FB_scrapper/create_final.py — код проверен на ~7100 постах
(иврит / русский / английский). Здесь только чистые функции над текстом:
никакого Excel, никаких файлов, никаких сетевых вызовов.

Правило: этот слой работает ДО обращения к LLM. Всё, что он извлёк уверенно,
LLM больше не спрашивают. Всё, что вернуло None — кандидат на добор моделью
или на честное «нет данных».
"""

import re
from datetime import datetime, timedelta
from pathlib import Path

# ── helpers ──────────────────────────────────────────────────────────────────

_MONTH_MAP = {
    'january':1,'jan':1,'январ':1,'янв':1,'ינואר':1,
    'february':2,'feb':2,'феврал':2,'фев':2,'פברואר':2,
    'march':3,'mar':3,'март':3,'מרץ':3,
    'april':4,'apr':4,'апрел':4,'апр':4,'אפריל':4,
    'may':5,'май':5,'מאי':5,
    'june':6,'jun':6,'июн':6,'יוני':6,
    'july':7,'jul':7,'июл':7,'יולי':7,
    'august':8,'aug':8,'август':8,'авг':8,'אוגוסט':8,
    'september':9,'sep':9,'сентябр':9,'сен':9,'ספטמבר':9,
    'october':10,'oct':10,'октябр':10,'окт':10,'אוקטובר':10,
    'november':11,'nov':11,'ноябр':11,'ноя':11,'נובמבר':11,
    'december':12,'dec':12,'декабр':12,'дек':12,'דצמבר':12,
}

def normalize_date(raw: str, reference: datetime = None) -> str:
    """Convert Facebook date string to YYYY-MM-DD.
    reference: use as 'now' when parsing relative dates (default: datetime.now()).
    Returns original string if parsing fails.
    """
    if not raw or raw in ('-', 'nan', 'None', ''):
        return '-'
    now = reference or datetime.now()
    s = raw.strip()

    # Already YYYY-MM-DD
    if re.match(r'^\d{4}-\d{2}-\d{2}', s):
        return s[:10]

    sl = s.lower()

    # ── Relative: N units ago ────────────────────────────────────────────────
    m = re.search(
        r'(\d+)\s*('
        r'мин|min|דקות?|'
        r'ч\b|h\b|שעות?|'
        r'дн|д\b|d\b|ימים?|'
        r'нед|w\b|שבועות?|'
        r'мес|month|חודשים?|'
        r'г\b|y\b|שנים?'
        r')',
        sl)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if   unit in ('мин','min') or unit.startswith('דק'):  dt = now - timedelta(minutes=n)
        elif unit in ('ч','h')     or unit.startswith('שע'):  dt = now - timedelta(hours=n)
        elif unit in ('дн','д','d') or unit.startswith('ימ') or unit == 'יום': dt = now - timedelta(days=n)
        elif unit in ('нед','w')   or unit.startswith('שב'):  dt = now - timedelta(weeks=n)
        elif unit in ('мес','month') or unit.startswith('חוד'): dt = now - timedelta(days=n*30)
        elif unit in ('г','y')     or unit.startswith('שנ'):  dt = now - timedelta(days=n*365)
        else: dt = now
        return dt.strftime('%Y-%m-%d')

    # ── Yesterday / today ────────────────────────────────────────────────────
    if any(w in sl for w in ('вчера','yesterday','אתמול')):
        return (now - timedelta(days=1)).strftime('%Y-%m-%d')
    if any(w in sl for w in ('сегодня','today','just now','היום')):
        return now.strftime('%Y-%m-%d')

    # ── Named month: "14 март", "בפברואר 27", "February 14" ─────────────────
    for month_str, month_num in _MONTH_MAP.items():
        if month_str in sl:
            day_m = re.search(r'\b(\d{1,2})\b(?![\d:])', sl)
            if day_m:
                day = int(day_m.group(1))
                if 1 <= day <= 31:
                    year = now.year
                    try:
                        dt = datetime(year, month_num, day)
                        if dt > now + timedelta(days=1):
                            dt = datetime(year - 1, month_num, day)
                    except ValueError:
                        dt = now
                    return dt.strftime('%Y-%m-%d')

    # ── Numeric date DD.MM or DD.MM.YYYY ─────────────────────────────────────
    m2 = re.search(r'(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?', sl)
    if m2:
        day, month = int(m2.group(1)), int(m2.group(2))
        year = int(m2.group(3)) if m2.group(3) else now.year
        if len(str(year)) == 2:
            year += 2000
        if 1 <= day <= 31 and 1 <= month <= 12:
            try:
                return datetime(year, month, day).strftime('%Y-%m-%d')
            except ValueError:
                pass

    return s  # could not parse — return as-is


_RENT_KW = r'(?:מחיר|שכ[\'\"״]{0,2}ד|שכד|שכירות|שכר\s+דירה|rent|price|Цена|аренда|стоимость)'
_CUR     = r'(?:ש[\'\"״]?ח\b|[\u20aa]|שקל\w*\b|לחודש\b|\u0448\u0435\u043a\w*\b|NIS\b|ILS\b)'

def extract_price(txt):
    """Return monthly rent in NIS, or None. Avoids vaad/arnona fees."""
    txt_clean = re.sub(
        r'(?:ועד\s+בית|ארנונה|דמי\s+ניהול|management\s+fee|vaad)[^\d\n]{0,80}\d[\d,]*',
        '', txt, flags=re.IGNORECASE)

    # Normalize dot-thousands: "3.500₪" → "3500₪" (3 digits after dot = thousands, not decimal)
    txt_clean = re.sub(r'(\d+)\.(\d{3})(?=\D|$)', lambda m: m.group(1) + m.group(2), txt_clean)
    # Normalize space-thousands before currency: "6 700 шек" → "6700 шек"
    txt_clean = re.sub(r'(\d{1,3})\s(\d{3})(?=\s*' + _CUR + r')', r'\1\2', txt_clean)

    # Pass 1: "N אלף [currency]" – Hebrew "N thousand", e.g. "24 אלף ש״ח" → 24000
    alef = re.search(
        r'(\d+(?:[.,]\d+)?)\s*אלף\s*(?:ש[\'\"״]?ח|[\u20aa]|shek\w*|NIS|ILS|\u0448\u0435\u043a)',
        txt_clean, re.IGNORECASE)
    if alef:
        v = int(float(alef.group(1).replace(',', '.')) * 1000)
        if 2500 <= v <= 200000:
            return v

    # Pass 2: keyword + number + currency (standard order), same line
    labeled = re.search(
        _RENT_KW + r'[^\n]{0,80}?(\d[\d,]+)\s*' + _CUR,
        txt_clean, re.IGNORECASE)
    if labeled:
        v = int(labeled.group(1).replace(',', ''))
        if 2500 <= v <= 200000:
            return v

    # Pass 2b: keyword + currency + number, e.g. "מחיר: ₪18,000"
    labeled_cur_first = re.search(
        _RENT_KW + r'[^\n]{0,40}?' + _CUR + r'\s*(\d[\d,]+)',
        txt_clean, re.IGNORECASE)
    if labeled_cur_first:
        v = int(labeled_cur_first.group(1).replace(',', ''))
        if 2500 <= v <= 200000:
            return v

    # Pass 2c: keyword + number, no currency (finditer skips small values)
    # e.g. "שכירות: 4900" / "Rent: 4900" / "שכ״ד 7000" / "מחיר מבוקש 8,200"
    for m in re.finditer(
            _RENT_KW + r'[^\n]{0,60}?(\d[\d,]+)\s*(?:$|\s)',
            txt_clean, re.IGNORECASE | re.MULTILINE):
        v = int(m.group(1).replace(',', ''))
        if 2500 <= v <= 200000:
            return v

    # Pass 2d: number then keyword (reversed order), e.g. "4700\nаренда"
    for m in re.finditer(r'(\d[\d,]+)\s*\n?\s*' + _RENT_KW, txt_clean, re.IGNORECASE):
        v = int(m.group(1).replace(',', ''))
        if 2500 <= v <= 200000:
            return v

    # Pass 3a: number + currency token, no keyword required
    for m in re.finditer(r'(\d[\d,]+)\s*' + _CUR, txt_clean, re.IGNORECASE):
        v = int(m.group(1).replace(',', ''))
        if 3000 <= v <= 200000:
            return v

    # Pass 3b: currency + number (e.g. standalone "₪18,000" without keyword context)
    for m in re.finditer(_CUR + r'\s*(\d[\d,]+)', txt_clean, re.IGNORECASE):
        v = int(m.group(1).replace(',', ''))
        if 3000 <= v <= 200000:
            return v

    return None


def extract_rooms(txt):
    """Return room count as float, or None."""
    # חדר וחצי = 1.5 rooms (must check before digit patterns)
    if re.search(r'חדר\s+וחצי', txt):
        return 1.5

    # Hebrew written numbers: שני חדרים = 2, etc.
    _HNUM = [
        (r'חדר\s+(?:אחד|אחת)\b',     1.0),
        (r'(?:שני|שתי)\s+חדרים\b',    2.0),
        (r'שלושה?\s+חדרים\b',         3.0),
        (r'ארבעה?\s+חדרים\b',         4.0),
        (r'חמישה?\s+חדרים\b',         5.0),
        (r'שישה?\s+חדרים\b',          6.0),
    ]
    for pattern, val in _HNUM:
        if re.search(pattern, txt, re.IGNORECASE):
            return val

    for p in [
        r'(\d+(?:[.,]\d)?)\s*(?:חד[\'"]?\b|חדרים|חדר\b)',   # 2חד / 2 חד' / 2 חדרים
        r'(\d+(?:[.,]\d)?)\s*(?:ком|комн\w*)',
        r'(\d+(?:[.,]\d)?)\s*(?:rooms?|bedrooms?)',
    ]:
        m = re.search(p, txt, re.IGNORECASE)
        if m:
            v = float(m.group(1).replace(',', '.'))
            if 1 <= v <= 10:
                return v

    if re.search(r'(?:studio|סטודיו|студия|חד\s*א|דירת\s*חדר\b)', txt, re.IGNORECASE):
        return 1.0

    # Bare "חדר" with no number = 1 room (e.g. "חדר + מרפסת", "חדר בקינג ג'ורג'")
    # Only if no other room count found and text clearly mentions a single room
    if re.search(r'(?:^|\s|,)חדר(?:\s*\+|\s*,|\s*$|\s+(?:ו|עם|ב|מ|ל))', txt, re.MULTILINE):
        return 1.0

    return None


DISTRICT_PATTERNS = [
    ('HaSolelim (Bitzaron)',   r'(?:הסוללים|solelim|bitzaron|ביצרון)'),
    ('Neve Tzedek',            r'(?:נוה צדק|נווה צדק|neve.?tzedek)'),
    ('Kerem HaTeimanim',       r'(?:כרם התימנים|kerem.?ha?tei)'),
    ('Florentin',              r'(?:פלורנטין|florentin)'),
    ('Shapira',                r'(?:שפירא|shapira)'),
    # Jaffa — word boundary via Hebrew-letter negative lookaround (P3)
    ('Jaffa',                  r'(?:(?<![א-ת])יפו(?![א-ת])|jaffa|яффо)'),
    ('Bavli',                  r'(?:בבלי|bavli|יוסף פעמוני)'),
    ("Neve Sha'anan",          r'(?:נווה שאנן|neve.?shaanan|צנחנים)'),
    ('Lev HaIr (Center)',      r'(?:מרכז העיר|center.*city|כיכר רבין|rabin.?sq|רוטשילד|rothschild|'
                               r'שינקין|shenkin|allenby|אלנבי|כיכר המדינה|גן מאיר|שוק הכרמל|'
                               r'כרמל.*שוק|כולי עלמה|leonarda|לאונרדו|ניסים אלוני|'
                               r'דיזנגוף|dizengoff|בוגרשוב|bograshov|טרומפלדור|trumpeldor|'
                               r'הנביאים|hanevi|מונטיפיורי|montefiore|הבימה|habima|'
                               r'שלמה\s+המלך|king.?george|קינג\s+ג[\'\"]?ורג|מלכי\s+ישראל|'
                               r'בן\s+גוריון|ben.?gurion)'),
    ('Old North',              r'(?:צפון ישן|הצפון הישן|old.?north|ארלוזורוב|בזל|bazal|'
                               r'ירמיהו|זאן זורס|גורדון.*ים|הירקון|בן יהודה|ben.?yehuda|'
                               r'פרישמן|frishman|nordau|נורדאו)'),
    ('Nahalat Yitzhak',        r'(?:נחלת יצחק|nahalat.?yitzhak|ערבי נחל)'),
    ('North Tel Aviv',         r'(?:רמת אביב|ramat.?aviv|צפון ת[\'"]?א|north.?tlv|'
                               r'גלילות|galilot|הדר יוסף|hadar.?yosef|נווה שרת|neve.?sha?ret|'
                               r'כוכב הצפון|רביבים|revivim|נווה חן|neve.?chen)'),
    ('South Tel Aviv',         r'(?:דרום.*ת[\'"]?א|south.*tel.?aviv|גבעת הרצל|givat.?herzl|'
                               r'כפר\s+שלם|kfar.?shalem|יד\s+אליהו|yad.?eliyahu|'
                               r'תל\s+חיים|tel.?haim|צ[\'\"]לנוב|chelnov|דרך\s+שלמה)'),
    ('Givatayim',              r'(?:גבעתיים|givatayim)'),
    ('Ramat Gan',              r'(?:רמת\s*גן|ramat.?gan|ר[\"״]ג\b|'
                               r'\u0440\u0430\u043c\u0430\u0442.?\u0433\u0430\u043d)'),
    ('Bat Yam',                r'(?:בת\s*ים|bat.?yam|\u0431\u0430\u0442.?\u044f\u043c)'),
    ('Holon',                  r'(?:חולון|holon|\u0445\u043e\u043b\u043e\u043d|'
                               r'תל\s+גיבורים|tel.?giborim)'),
    ('Netanya',                r'(?:נתניה|netanya)'),
    ('Bnei Brak',              r'(?:בני ברק|bnei.?brak)'),
]

def extract_district(txt):
    for name, pattern in DISTRICT_PATTERNS:
        if re.search(pattern, txt, re.IGNORECASE):
            return name
    return None


# ── Negation handling ───────────────────────────────────────────────────────
# Targets that, when preceded by a negation word, should NOT yield a positive tag.
_NEG_TARGETS = (
    # Hebrew
    r'מעלית\w*|'
    r'חנ[יי]ה\w*|חניון|'
    r'ממ[\"\'״]?ד\b|מרחב\s+מוגן|מקלט\w*|'
    r'בעלי\s+חיים|בע[\"\'״]?ח\b|חיות\s+מחמד|'
    r'מזגן\w*|מיזוג\w*|ממוזג\w*|'
    r'מרפסת\w*|'
    r'גינה\w*|חצר\w*|'
    # English
    r'elevator|lift|parking|a/?c|air.?cond|pets?|balcony|garden|yard|shelter|safe.?room|'
    # Russian
    r'\u043b\u0438\u0444\u0442\w*|'                      # лифт
    r'\u043f\u0430\u0440\u043a\u043e\u0432\w*|'          # парков
    r'\u043a\u043e\u043d\u0434\u0438\u0446\w*|'          # кондиц
    r'\u0431\u0430\u043b\u043a\u043e\u043d\w*|'          # балкон
    r'\u0436\u0438\u0432\u043e\u0442\w*|'                # животн
    r'\u043f\u0438\u0442\u043e\u043c\u0446\w*|'          # питомц
    r'\u0443\u043a\u0440\u044b\u0442\w*|'                # укрыт
    r'\u0441\u0430\u0434\b|\u0434\u0432\u043e\u0440\b'   # сад / двор
)
_NEG_WORDS = (
    r'ללא|אין|בלי|ל[אֹ]|'
    r'no\b|without|not\b|'
    r'\u0431\u0435\u0437\b|\u043d\u0435\u0442\s+'  # без / нет
)
_NEG_STRIP_RE = re.compile(
    r'(?:' + _NEG_WORDS + r')\s+(?:дмi\s+|דמי\s+)?(?:' + _NEG_TARGETS + r')',
    re.IGNORECASE
)

def strip_negations(txt: str) -> str:
    """Blank out negated phrases like 'ללא מעלית' so they don't match positive patterns."""
    return _NEG_STRIP_RE.sub(' ', txt)


# Street-parking phrases that should NOT count as 'parking' feature.
_STREET_PARKING_RE = re.compile(
    r'אין\s+בעיות\s+חני\w+|הרבה\s+חני\w+\s+באזור|תו\s+חני\w+|'
    r'חני[יה]\s+(?:ברחוב|באזור|בסביבה)|חנ[יי]ת\s+רחוב',
    re.IGNORECASE
)


# Shared-apartment markers (room in shared flat, not whole apt)
_SHARED_APT_RE = re.compile(
    r'דירת\s+שותפ\w*|דירת\s+שותפות|חדר\s+בדירת\s+שותפ|'
    r'shared\s+apart|room\s+in\s+a\s+(?:\d[-\s])?(?:bed|\w+\s+)?apart|'
    r'חדר\s+בשותפות|מתפנה\s+חדר|'
    r'\u043a\u043e\u043c\u043d\u0430\u0442\u0430\s+\u0432\s+\u043a\u0432\u0430\u0440\u0442\u0438\u0440\u0435',
    re.IGNORECASE
)

def is_shared_apartment(txt: str) -> bool:
    return bool(_SHARED_APT_RE.search(txt))


# Explicit non-Tel-Aviv city detection in text (overrides group-based defaults)
_CITY_TEXT_PATTERNS = [
    ('Ramat Gan',  r'ב?רמת\s*גן|ramat.?gan|\bר[\"״]ג\b|\u0440\u0430\u043c\u0430\u0442.?\u0433\u0430\u043d'),
    ('Bat Yam',    r'ב?בת\s*ים|bat.?yam|\u0431\u0430\u0442.?\u044f\u043c'),
    ('Holon',      r'ב?חולון|holon|\u0445\u043e\u043b\u043e\u043d'),
    ('Givatayim',  r'ב?גבעתיים|givatayim'),
    ('Bnei Brak',  r'ב?בני\s*ברק|bnei.?brak'),
    ('Netanya',    r'ב?נתניה|netanya'),
]

def extract_city_from_text(txt: str):
    """Return city name if explicitly mentioned in text; else None (→ default Tel Aviv)."""
    for city, pattern in _CITY_TEXT_PATTERNS:
        if re.search(pattern, txt, re.IGNORECASE):
            return city
    return None


def classify_safe_room(txt: str):
    """Return safe-room tag: 'mamad', 'miklat', 'safe room', or None.

    mamad  = ממ"ד — fortified room inside the apartment.
    miklat = מקלט — communal shelter in the building.
    safe room = מרחב מוגן without clear scope — needs manual review.
    None   = negated, public-only, or no mention.
    """
    # ── מקלט תקני — explicitly a building/code-standard shelter (NOT mamad) ──
    # ── ממ"ד — unambiguously in-apartment ────────────────────────────────────
    # ממ"ד — no strict \b, Hebrew letters around it checked via lookahead/behind
    if re.search(r'ממ["\'\u05f4״]?ד(?!["\'\u05f4״])', txt, re.IGNORECASE):
        if not re.search(r'(?:ללא|אין|בלי|no|without|без)\s+(?:\S+/)*\s*ממ["\'\u05f4״]?ד', txt, re.IGNORECASE):
            return 'mamad'

    # Check FIRST so "מקלט תקני בבניין" is never misclassified as mamad.
    if re.search(r'מקלט\s+תקני', txt, re.IGNORECASE):
        if not re.search(r'(?:ללא|אין|בלי|no|without|без)\s+(?:\S+/)*\s*מקלט', txt, re.IGNORECASE):
            return 'miklat'

    # ── "safe room" (English) — typically in-apartment in listings ───────────
    if re.search(r'safe[\s\-]?room', txt, re.IGNORECASE):
        if not re.search(r'(?:no|without)\s+safe', txt, re.IGNORECASE):
            return 'mamad'

    # ── מרחב מוגן — context-dependent ────────────────────────────────────────
    if re.search(r'מרחב\s+מוגן', txt, re.IGNORECASE):
        if re.search(r'(?:ללא|אין|בלי)\s+מרחב\s+מוגן', txt, re.IGNORECASE):
            pass  # negated
        else:
            # extract up to 50 chars after the phrase to check context
            m_ctx = re.search(r'מרחב\s+מוגן(.{0,50})', txt, re.IGNORECASE)
            ctx = m_ctx.group(1) if m_ctx else ''
            if re.search(r'בדירה|דירתי|פרטי(?!\s+מ)', ctx, re.IGNORECASE):
                return 'mamad'   # clearly in-apartment
            elif re.search(
                r'קרוב|ליד|סמוך|מתחת|בקומה|בקומת|קומתי|בבניין|ממול|הסמוך|חניון|'
                r'במרחק|מ["\']ר מ|צעדים|שניות|דקות',
                ctx, re.IGNORECASE):
                return 'miklat'  # clearly external / building / floor-level
            else:
                return 'safe room'  # truly ambiguous — flag for manual review

    # ── מקלט — building / public shelter ─────────────────────────────────────
    if re.search(r'מקלט', txt, re.IGNORECASE):
        if re.search(r'(?:ללא|אין|בלי|no|without|без)\s+(?:\S+/)*\s*מקלט', txt, re.IGNORECASE):
            return None
        if re.search(r'מקלט\s+(?:ציבורי|קהילתי|שכונתי)', txt, re.IGNORECASE):
            return None          # public/neighborhood shelter only — skip
        # Shelter clearly far from building → skip
        if re.search(r'מקלט\s+(?:במרחק\s+(?:[5-9]\d|\d{3})|בבניין\s+(?:ממול|הסמוך|אחר))', txt, re.IGNORECASE):
            return None
        # Shelter at short walking distance → borderline but keep as miklat
        return 'miklat'

    # ── Russian: укрытие / бомбоубежище ──────────────────────────────────────
    if re.search(r'укрыти\w+|убежищ\w+|бомбоубежищ\w+', txt, re.IGNORECASE):
        if not re.search(r'(?:без|нет)\s+(?:укрытия|убежища)', txt, re.IGNORECASE):
            return 'miklat'

    return None


def extract_features(txt):
    """Extract feature tags from post text, honoring negations."""
    tags = []

    # ── Negation-sensitive tags detected FIRST on original text ──────────────
    # 'no broker' depends on the negation word itself being present.
    if re.search(
        r'ל?לא\s+(?:דמי\s+)?תיוו?ך|בלי\s+תיוו?ך|'
        r'ישיר\s+מ?בעל|בעל\s+הדירה|מ?פרטי\b|'
        r'no.?brok|without\s+brok|direct\s+from\s+owner|'
        r'\u0431\u0435\u0437.?\u043c\u0430\u043a\u043b\u0435\u0440|\u043e\u0442\s+\u0441\u043e\u0431\u0441\u0442\u0432\u0435\u043d\u043d\u0438\u043a',
        txt, re.IGNORECASE):
        tags.append('no broker')

    # safe room classification (mamad / miklat / unidentified) — before strip_negations
    sr = classify_safe_room(txt)
    if sr:
        tags.append(sr)

    # pets ok: positive mention AND no negation
    pets_neg = re.search(
        r'(?:ללא|אין|בלי|לא)\s+(?:בעלי\s+חיים|בע[\"\'״]?ח\b|חיות)|no\s+pets',
        txt, re.IGNORECASE)
    pets_pos = re.search(
        r'(?:מאשר\w*|מותר|אפשר\w*|ok(?:ay)?)\s+(?:עם\s+)?(?:בעלי\s+חיים|בע[\"\'״]?ח|חיות|כלב|חתול)|'
        r'pets?\s+(?:ok|welcome|allowed|friendly)|'
        r'\u043c\u043e\u0436\u043d\u043e\s+(?:\u0441\s+)?\u0436\u0438\u0432\u043e\u0442\u043d|\u043f\u0438\u0442\u043e\u043c\u0446\u044b\s+\u0440\u0430\u0437\u0440',
        txt, re.IGNORECASE)
    if pets_pos and not pets_neg:
        tags.append('pets ok')

    # ── Strip negations so positive patterns don't match them ────────────────
    tn = strip_negations(txt)
    # Strip street-parking phrases so they don't trigger 'parking' tag
    tp = _STREET_PARKING_RE.sub(' ', tn)

    checks = [
        ('renovated',        tn, r'(?:משופצת|משופץ|שופץ|שיפוץ|מחודש\w*|renovated|\u043e\u0442\u0440\u0435\u043c\u043e\u043d\u0442)'),
        ('new building',     tn, r'(?:חדש.*מקבלן|בניין חדש|חדשה.*קבלן|מהניילונים|חדש לגמרי|new.*developer|\u043d\u043e\u0432\u044b\u0439\s+\u0434\u043e\u043c)'),
        ('furnished',        tn, r'(?:מרוהטת קומפלט|מרוהט קומפלט|ריהוט מלא|ריהוט קומפלט|מרוהטת במלואה|מרוהט במלואו|מרוהטת מלא|מרוהט מלא|fully furnished|\u043c\u0435\u0431\u043b\u0438\u0440\u043e\u0432\u0430\u043d\u0430)'),
        ('partly furnished', tn, r'(?:מרוהטת חלקית|מרוהט חלקית|ריהוט חלקי|partial.*furn|partly furn|מרוהט\w*)'),
        ('elevator',         tn, r'(?:מעלית|elevator|\u043b\u0438\u0444\u0442)'),
        ('parking',          tp, r'(?:חני[יה]\s+(?:פרטית|בטאבו|צמודה|בבניין|כפולה|מקורה|תת[\s\-]?קרקעית)|חניון|private\s+parking|\u043f\u0430\u0440\u043a\u043e\u0432)'),
        ('balcony',          tn, r'(?:מרפסת|גזוזטרה|balcony|\u0431\u0430\u043b\u043a\u043e\u043d)'),
        # garden/yard — ТОЛЬКО явные слова (без голого גן\b, P2/P16)
        ('garden/yard',      tn, r'(?:גינה|גינת\s+\w+|חצר|דירת\s+גן|גן\s+פרטי|garden|yard|\u0434\u0432\u043e\u0440\b|\u0441\u0430\u0434\b)'),
        ('storage',          tn, r'(?:מחסן|storage|\u043a\u043b\u0430\u0434\u043e\u0432\u043a\u0430)'),
        # safe room handled separately by classify_safe_room above
        # A/C — expanded forms (P4/P18)
        ('A/C',              tn, r'(?:מזגן\w*|מזגנים|מיזוג\w*|ממוזג\w*|A/C|AC\b|air.?cond|\u043a\u043e\u043d\u0434\u0438\u0446)'),
        ('gym',              tn, r'(?:חדר כושר|gym|fitness|\u0442\u0440\u0435\u043d\u0430\u0436\u0435\u0440)'),
        ('pool',             tn, r'(?:בריכה|pool|\u0431\u0430\u0441\u0441\u0435\u0439\u043d)'),
        ('sea view',         tn, r'(?:נוף.*ים|נוף לים|כיוון ים|view.*sea|\u043c\u043e\u0440\u0435|\u0432\u0438\u0434.*\u043c\u043e\u0440\u0435)'),
        ('quiet',            tn, r'(?:שקט\w*|quiet|\u0442\u0438\u0445\u0430\u044f)'),
        # immediate entry — allow separators (P7)
        ('immediate entry',  tn, r'(?:כניסה[\s:,\-]+מי?ידי?[תה]?|פנוי\s*(?:מיד|מיידית)|זמין\s+מיד|immediate\s*(?:entry|move.?in)|\u043d\u0435\u043c\u0435\u0434\u043b\u0435\u043d)'),
        ('guard 24/7',       tn, r'(?:שומר.*24|אבטחה.*24|guard.*24|\u043e\u0445\u0440\u0430\u043d\u0430.*24)'),
        ('penthouse',        tn, r'(?:פנטהאוז|penthouse|\u043f\u0435\u043d\u0442\u0445\u0430\u0443\u0441)'),
        ('duplex',           tn, r'(?:דופלקס|דו.?קומתי|duplex|\u0434\u0443\u043f\u043b\u0435\u043a\u0441)'),
    ]
    for tag, text_ctx, pattern in checks:
        if re.search(pattern, text_ctx, re.IGNORECASE):
            if tag == 'furnished' and 'partly furnished' in tags:
                continue
            if tag == 'partly furnished' and 'furnished' in tags:
                continue
            tags.append(tag)

    sqm = re.search(r'(\d+)\s*מ[\'"״]?ר\b|(\d+)\s*sqm\b|(\d+)\s*מטר(?:\s+רבוע)?\b', txt)
    if sqm:
        v = int(next(g for g in sqm.groups() if g))
        if 15 <= v <= 500:
            tags.insert(0, f'{v} sqm')
    return '; '.join(tags) if tags else '-'


EXCLUDE_RE = re.compile(
    r'(?:'
    r'מחפש(?:ים|ת|)?\s+(?:דירת?|חדר|שותפ)|'              # heb: looking for apt/room/roommate
    r'מחפש(?:ים|ת)?\s+לגור|'
    r'\u0438\u0449\u0443\s+(?:\u043a\u0432\u0430\u0440\u0442\u0438\u0440\u0443|\u0437\u0430\u043c\u0435\u043d\u0443|\u043a\u043e\u043c\u043d\u0430\u0442\u0443)|\u0438\u0449\u0435\u043c\s+|'
    r'\u043e\u0447\u0438\u0449\u0430\u044e\s+\u0441\u0432\u043e\u044e|'   # Russian: clearing my room
    r'\u0438\u0449\u0443\s+\u0441\u043e\u0441\u0435\u0434\u0430|'         # Russian: looking for roommate
    r'מפנ[האה]\s+(?:את\s+)?(?:ה)?חדר|'
    r'מחליפ|החלפה\s+בחוזה|\u0438\u0449\u0443\s+\u0437\u0430\u043c\u0435\u043d\u0443|'
    r'סאבלט|sublet|\u0441\u0430\u0431\u043b|\u0441\u0443\u0431\u043b|'
    r'מכירה\b|מכירת|\u043f\u0440\u043e\u0434\u0430\u0436|לרכישה\b|'
    r'seenker\.com|'
    r'מחפש[ת]?\s+שותפ|\u0438\u0449\u0443\s+\u0441\u043e\u0441\u0435\u0434\u0430|looking\s+for\s+roomm|'
    r'(?:לתקופ|לטווח)\s+קצר|short.?term|\u043a\u0440\u0430\u0442\u043a\u043e\u0441\u0440\u043e\u0447\u043d|'
    r'\u0432\u0438\u043b\u043e\u043d\u043e\u0442|שולחן\s+עבודה|'
    r'approved.*building.*permit|'
    r'toronto.*tel.?aviv|nairobi|wangige|kenya|ksh\b|'
    r'home\s+exchange|'
    r'executive\s+\d+\s+bedroom\s+to\s+let\s+regen'
    r')',
    re.IGNORECASE
)

_ZWS_RE = re.compile(r'[\u200b-\u200f\u00a0\ufeff]')

def is_excluded(txt):
    return bool(EXCLUDE_RE.search(_ZWS_RE.sub(' ', txt)))


def text_fingerprint(txt):
    """Create a fingerprint for deduplication (same post in multiple groups)."""
    # Remove whitespace, punctuation, URLs; keep first 400 meaningful chars
    cleaned = re.sub(r'https?://\S+', '', txt)
    cleaned = re.sub(r'[\s\W]+', '', cleaned)
    return cleaned[:400].lower()


def extract_phones(txt):
    """Return set of normalized Israeli phone numbers (9 digits, without leading 0).
    Handles: 054-123-4567 / 054 123 4567 / 0541234567 / +972-54-123-4567
    """
    phones = set()
    # +972 prefix
    for m in re.finditer(r'\+972[\s\-.]?(\d[\d\s\-.]{7,11})', txt):
        d = re.sub(r'\D', '', m.group(1))
        if len(d) == 9:
            phones.add(d)
    # Local 0XX format (mobile 05X or landline 0X)
    for m in re.finditer(r'\b0(\d[\d\s\-.]{7,10})\b', txt):
        d = re.sub(r'\D', '', m.group(1))
        if len(d) == 9:
            phones.add(d)
    return phones


DISTRICT_TO_CITY = {
    'Givatayim': 'Givatayim',
    'Ramat Gan': 'Ramat Gan',
    'Bat Yam': 'Bat Yam',
    'Holon': 'Holon',
    'Netanya': 'Netanya',
    'Bnei Brak': 'Bnei Brak',
}

def get_city(district):
    return DISTRICT_TO_CITY.get(district, 'Tel Aviv')


# ── main ─────────────────────────────────────────────────────────────────────

def _norm_url(raw_url):
    raw_url = str(raw_url).strip()
    return '' if raw_url.lower() in ('', 'nan', 'none') else raw_url.split('?')[0]


