"""Dira Bot — Claude-powered listing analyzer.

Uses Anthropic API (claude-haiku-4-5) to evaluate each listing against
the user's search criteria and return a structured JSON analysis.
"""

import json
import logging
import re
from typing import Optional

import anthropic

import config

log = logging.getLogger(__name__)

_client: Optional[anthropic.AsyncAnthropic] = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


# ── Prompts ───────────────────────────────────────────────────────────────────

SYSTEM = (
    "Ты — умный помощник по поиску квартиры в аренду в Израиле. "
    "Анализируешь объявления и определяешь, подходят ли они под критерии. "
    "Отвечаешь строго в JSON."
)

ANALYZE_TEMPLATE = """\
Проанализируй объявление об аренде квартиры в Израиле.

КРИТЕРИИ:
- Город: Тель-Авив или рядом (Рамат-Ган, Гиватаим, Бат-Ям, Холон, Бней-Брак)
- Рядом с ж/д станцией или электричкой (~15 мин пешком) — желательно
- Комнаты: 2+ (в израильском счёте)
- Цена: до 7,000 шек/месяц (идеально до 6,500)
- ממ"ד (мамад) или бомбоубежище рядом — ОБЯЗАТЕЛЬНО
- Не полностью пустая (хотя бы базовая мебель)
- Въезд: начало мая 2026
- Срок аренды: от 6 месяцев

{preferences_section}

ОБЪЯВЛЕНИЕ (источник: {source}):
{text}

Верни ТОЛЬКО JSON:
{{
  "score": <0-10>,
  "recommendation": "<SEND|MAYBE|SKIP>",
  "price_found": <число или null>,
  "rooms_found": <число или null>,
  "location": "<адрес/район>",
  "near_station": <true|false|null>,
  "has_mamad": <true|false|null>,
  "furnished": "<full|partial|empty|unknown>",
  "available_may": <true|false|null>,
  "lease_ok": <true|false|null>,
  "issues": ["..."],
  "pros": ["..."],
  "summary": "<1-2 предложения на русском>"
}}

ПРАВИЛА:
- SEND если score >= 7
- MAYBE если score 5-6
- SKIP если score <= 4
- Продажа / поиск соседей / субаренда < 6 мес → SKIP (score 0)
- Цена > 7000 → максимум score 3
- ממ"ד не упомянут → has_mamad: null (не снижай сильно, может быть не указан)
- Явно "без ממ"ד" → has_mamad: false, score -3
"""

DIGEST_TEMPLATE = """\
Вот топ-{count} квартир за последние {days} дней. Составь краткий дайджест на русском.

{listings_text}

Формат ответа — JSON:
{{
  "digest": "<текст дайджеста с emoji, 5-15 строк>",
  "trends": ["тренд 1", "тренд 2"],
  "recommendation": "<общая рекомендация>"
}}
"""

PREFERENCES_TEMPLATE = """\
Вот история фидбека пользователя по квартирам:

{feedback_text}

Проанализируй паттерны и выдели предпочтения. JSON:
{{
  "preferences": [
    {{"key": "название", "value": "описание", "confidence": 0.0-1.0}}
  ],
  "summary": "<краткий вывод на русском>"
}}
"""


# ── Analysis functions ────────────────────────────────────────────────────────

async def analyze_listing(
    text: str,
    source: str = "telegram",
    preferences: list[dict] | None = None,
) -> dict:
    """Analyze a single listing with Claude. Returns parsed dict."""

    # Build preferences section if available
    pref_section = ""
    if preferences:
        lines = [f"- {p['key']}: {p['value']} (уверенность: {p['confidence']:.0%})"
                 for p in preferences]
        pref_section = "ВЫУЧЕННЫЕ ПРЕДПОЧТЕНИЯ:\n" + "\n".join(lines)

    prompt = ANALYZE_TEMPLATE.format(
        source=source,
        text=text[:3000],  # truncate very long posts
        preferences_section=pref_section,
    )

    try:
        msg = await _get_client().messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=1024,
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        return _parse_json(raw)

    except anthropic.APIError as e:
        log.error("Anthropic API error: %s", e)
        return _error_result(f"API error: {e}")
    except Exception as e:
        log.exception("Analyzer error: %s", e)
        return _error_result(str(e))


async def generate_digest(listings: list[dict], count: int = 5, days: int = 7) -> dict:
    """Generate a daily/weekly digest comparing top listings."""
    listings_lines = []
    for i, l in enumerate(listings[:count], 1):
        line = (
            f"{i}. [{l.get('score', '?')}/10] {l.get('summary', 'N/A')}\n"
            f"   Цена: {l.get('price', '?')} | Комнат: {l.get('rooms', '?')} | "
            f"Район: {l.get('address', '?')}\n"
            f"   Мамад: {l.get('has_mamad', '?')} | Мебель: {l.get('furnished', '?')}\n"
            f"   URL: {l.get('url', '')}"
        )
        listings_lines.append(line)

    prompt = DIGEST_TEMPLATE.format(
        count=count,
        days=days,
        listings_text="\n\n".join(listings_lines),
    )

    try:
        msg = await _get_client().messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=1024,
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        return _parse_json(msg.content[0].text.strip())
    except Exception as e:
        log.exception("Digest error: %s", e)
        return {"digest": f"Error generating digest: {e}", "trends": [], "recommendation": ""}


async def learn_preferences(feedback_list: list[dict]) -> dict:
    """Analyze user feedback to learn preferences."""
    lines = []
    for f in feedback_list[:30]:  # last 30 feedbacks
        lines.append(
            f"- [{f['rating']}] score={f.get('score', '?')}: "
            f"{f.get('summary', f.get('raw_text', '')[:100])}"
            f"{' | Комментарий: ' + f['comment'] if f.get('comment') else ''}"
        )

    prompt = PREFERENCES_TEMPLATE.format(feedback_text="\n".join(lines))

    try:
        msg = await _get_client().messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=1024,
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        return _parse_json(msg.content[0].text.strip())
    except Exception as e:
        log.exception("Preferences error: %s", e)
        return {"preferences": [], "summary": f"Error: {e}"}


# ── helpers ───────────────────────────────────────────────────────────────────

def _sanitize_hebrew_json(text: str) -> str:
    """Fix common Hebrew abbreviation issue: ממ"ד contains a literal
    double-quote (gershayim) that breaks JSON. Replace with the
    Hebrew gershayim character (U+05F4) inside string values."""
    # Common Hebrew abbreviations with gershayim that break JSON:
    # ממ"ד (mamad), צה"ל, ת"א, etc.
    _HEBREW_ABBREVS = [
        ('ממ"ד', 'ממ״ד'),
        ('צה"ל', 'צה״ל'),
        ('ת"א', 'ת״א'),
        ('ר"ג', 'ר״ג'),
        ('ב"ב', 'ב״ב'),
        ('חד"ש', 'חד״ש'),
    ]
    for bad, good in _HEBREW_ABBREVS:
        text = text.replace(bad, good)
    return text


def _try_parse(text: str) -> dict | None:
    """Try json.loads; on failure try after Hebrew sanitization."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    try:
        return json.loads(_sanitize_hebrew_json(text))
    except (json.JSONDecodeError, ValueError):
        return None


def _parse_json(raw: str) -> dict:
    """Extract JSON from Claude response. Tries multiple strategies."""
    text = raw.strip()

    # Strategy 1: direct parse (with Hebrew fix fallback)
    result = _try_parse(text)
    if result is not None:
        return result

    # Strategy 2: strip markdown fences and parse content between them
    if "```" in text:
        parts = text.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("{"):
                result = _try_parse(part)
                if result is not None:
                    return result

    # Strategy 3: find JSON with balanced braces (handles surrounding text)
    start = text.find("{")
    if start >= 0:
        depth = 0
        end = start
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end > start:
            result = _try_parse(text[start:end])
            if result is not None:
                return result

    log.warning("Could not parse JSON (len=%d, first 80 repr: %r)", len(text), text[:80])
    return _error_result("JSON parse error")


def _error_result(msg: str) -> dict:
    return {
        "score": 0,
        "recommendation": "SKIP",
        "summary": msg,
        "pros": [],
        "issues": [msg],
    }
