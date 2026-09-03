"""Страница объявления: то, чего нет в строке таблицы.

Из выдачи доски мы берём цену, комнаты, этаж и адрес. На самой странице есть
описание от хозяина, ваад-байт, арнона, точная дата въезда — и, что важнее
всего, упоминание комиссии: «ללא תיווך» или «דמי תיווך חודש». Комиссия это
реальные деньги, но структурного поля под неё нет ни на одной доске, она живёт
в тексте описания.

Ходим не за всеми подряд: 300 объявлений в час превратились бы в 300 лишних
запросов к доске, и нас ограничат. Забираем страницы только тех объявлений,
которые кому-то подошли — это единицы в день.

Телефона на страницах нет: обе доски прячут его за кнопкой «показать номер».
"""

import logging
import re
from typing import Optional

import httpx

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/123.0 Safari/537.36")

_STRIP_RE = re.compile(r"<script.*?</script>|<style.*?</style>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _plain(html: str) -> str:
    text = _STRIP_RE.sub(" ", html)
    text = _TAG_RE.sub(" ", text).replace("&nbsp;", " ").replace("&#8362;", "₪")
    return re.sub(r"\s+", " ", text).strip()


# Подписи с числами: «קומה: 2 מתוך 4», «מ"ר: 65», «ארנונה לחודשיים: 0»
def _labelled(text: str, label: str) -> Optional[str]:
    m = re.search(re.escape(label) + r"\s*:?\s*([^:]{1,40}?)(?=\s{2,}|\s+[֐-׿]{2,}\s*:|$)", text)
    return m.group(1).strip() if m else None


def _number(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    m = re.search(r"\d[\d,]*", value)
    return int(m.group().replace(",", "")) if m else None


# ── комиссия ─────────────────────────────────────────────────────────────────
# «ללא תיווך» / «בלי תיווך» — без комиссии; «דמי תיווך» с суммой или процентом —
# с комиссией. Формулировок много, поэтому сначала явное отрицание, потом
# упоминание суммы, и только затем голое слово.
_NO_FEE_RE = re.compile(
    r"ללא\s*(?:דמי\s*)?תיוו?ך|בלי\s*תיוו?ך|ללא\s*עמלה|no\s*(?:broker|agent)|"
    r"без\s*(?:комисси|посредник|маклер)", re.IGNORECASE)
_FEE_AMOUNT_RE = re.compile(
    r"(?:דמי\s*תיוו?ך|עמלת\s*תיוו?ך)[^\d%]{0,20}(\d{1,3}\s*%|\d[\d,]{2,6}\s*₪|חודש)",
    re.IGNORECASE)
_FEE_WORD_RE = re.compile(r"דמי\s*תיוו?ך|עמלת\s*תיוו?ך|תיווך\s*מלא", re.IGNORECASE)


def commission(text: str) -> Optional[str]:
    """'none' — без комиссии; строка с суммой; None — в тексте про это не сказано."""
    if _NO_FEE_RE.search(text):
        return "none"
    m = _FEE_AMOUNT_RE.search(text)
    if m:
        return m.group(1).strip()
    if _FEE_WORD_RE.search(text):
        return "есть"
    return None


def _homeless(text: str) -> dict:
    return {
        "area_sqm": _number(_labelled(text, 'מ"ר')),
        "arnona": _number(_labelled(text, "ארנונה לחודשיים")),
        "entry_raw": _labelled(text, "כניסה"),
    }


def _komo(text: str) -> dict:
    vaad = re.search(r"ועד\s*בית\s*(\d[\d,]*)", text)
    area = re.search(r"(\d{2,3})\s*מ['״\"]?ר", text)
    entry = re.search(r"(\d{2}/\d{2}/\d{4})\s*תאריך\s*כניסה", text)
    return {
        "area_sqm": int(area.group(1)) if area else None,
        "vaad": int(vaad.group(1).replace(",", "")) if vaad else None,
        "entry_raw": entry.group(1) if entry else None,
    }


PARSERS = {"homeless": _homeless, "komo": _komo}


async def fetch(client: httpx.AsyncClient, url: str, source: str) -> Optional[dict]:
    """Скачать и разобрать страницу объявления. None — если не получилось."""
    try:
        response = await client.get(url)
        response.raise_for_status()
    except Exception as e:                            # noqa: BLE001
        log.warning("страница %s: %s", url, type(e).__name__)
        return None

    text = _plain(response.text)
    facts = {"commission": commission(text)}
    parser = PARSERS.get(source)
    if parser:
        extra = parser(text)
        if extra.get("area_sqm"):
            facts["area_sqm"] = extra["area_sqm"]
        entry = extra.get("entry_raw")
        if entry:
            if "מיידי" in entry:
                facts["entry_date"] = "now"
            else:
                m = re.match(r"(\d{2})/(\d{2})/(\d{4})", entry)
                if m:
                    facts["entry_date"] = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"

    # описание пригодится LLM-слою: там встречается всё, чего нет в подписях
    description = ""
    m = re.search(r"תיאור\s*הנכס\s*:?\s*(.{40,900}?)(?:קרא עוד|מודעות דומות|$)", text)
    if m:
        description = m.group(1).strip()
    return {"facts": {k: v for k, v in facts.items() if v is not None},
            "description": description, "text_len": len(text)}


async def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=40, follow_redirects=True,
                             headers={"User-Agent": UA, "Accept-Language": "he-IL,he;q=0.9"})
