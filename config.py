"""Dira Bot — configuration loaded from environment / .env file."""

import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=True)
DB_PATH = os.getenv("DB_PATH", str(BASE_DIR / "data" / "dira.db"))

# ── Telegram Bot ──────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ── Telethon (user account for monitoring groups) ─────────────────────────────
TG_API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
TG_API_HASH = os.getenv("TELEGRAM_API_HASH", "")
TG_SESSION = os.getenv("TELEGRAM_SESSION_STRING", "")

# ── Anthropic ─────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

# ── Apify (Phase 3) ──────────────────────────────────────────────────────────
APIFY_TOKEN = os.getenv("APIFY_TOKEN", "")

# ── ScraperAPI (Yad2 + Madlan proxy to bypass server IP block) ────────────────
SCRAPERAPI_KEY = os.getenv("SCRAPERAPI_KEY", "")

# ── Healthchecks.io (external watchdog — alerts to Telegram if bot goes silent) ─
HEALTHCHECK_URL = os.getenv("HEALTHCHECK_URL", "")
HEALTHCHECK_INTERVAL_MIN = 10

# ── Search criteria ───────────────────────────────────────────────────────────
MIN_ROOMS = 2.0          # 2+ rooms (Israeli count)
MAX_PRICE = 7000          # NIS/month
MOVE_IN = "начало мая 2026"
MIN_LEASE_MONTHS = 6

SEND_THRESHOLD = 7        # score >= 7 → instant alert
MAYBE_THRESHOLD = 5       # score 5-6 → alert with "?"

# ── Yad2 (Phase 2) ───────────────────────────────────────────────────────────
YAD2_API_URL = "https://gw.yad2.co.il/feed-search-legacy/realestate/rent"
YAD2_CITIES = {
    "tel_aviv":  "5000",
    "ramat_gan": "8600",
    "givatayim": "6900",
    "bat_yam":   "6600",
    "holon":     "6400",
    "bnei_brak": "7900",
}
YAD2_CHECK_INTERVAL = 30 * 60  # seconds

# ── Telegram channels to monitor ──────────────────────────────────────────────
TG_CHANNELS = [
    # ── Active ────────────────────────────────────────────────────────────────
    "Israel_arenda",              # Аренда в Израиле (рус) ✅
    "flamingorent",               # Аренда ✅ (бывает Хайфа, Claude фильтрует)
    "snyat_kvartiruy",            # Снять квартиру (рус) ✅
    "aptfornew",                  # Квартиры репатриантам ✅
    "ambery_longrent_telaviv",    # Долгосрочная аренда ТА ✅
    "jeremy_public",              # Agent Jeremy — Тель-Авив ✅
    "jeremy_public_ramat_gan",    # Agent Jeremy — Рамат-Ган ✅
    "isra_home_arenda",           # Аренда Israel
    # ── Removed (dead / wrong) ────────────────────────────────────────────────
    # "BROOTTO"           — переименован в канал про работу (последнее сообщение 2023)
    # "fishyTLV"          — заброшен (последнее сообщение 2022)
    # "sapirrent"         — заброшен (вакансии, последнее сообщение 2025)
    # "marianadlanru"     — заброшен (последнее сообщение 2024)
    # "olerealty"         — заброшен (последнее сообщение 2020)
    # "israel_sublet"     — только саблеты, всё фильтруется
    # "Sublet_Israel"     — только саблеты
]

# ── Facebook groups (Phase 3, for Apify) ──────────────────────────────────────
# IMPORTANT: apify~facebook-groups-scraper charges per event (post fetched).
# Keep this list small to control costs (~$3 per 1000 events on free tier).
# Budget math: 5 posts × 7 groups × 2 runs/day × 30 days = 2100 events/month ≈ $0.63
FB_GROUPS = [
    # Tel Aviv — word of mouth (highest quality, most active)
    "https://www.facebook.com/groups/35819517694",       # דירות מפה לאוזן בת"א (~20k members)
    "https://www.facebook.com/groups/101875683484689",   # דירות מפה לאוזן בתל אביב
    # Tel Aviv — general rent
    "https://www.facebook.com/groups/912651298791978",   # Secret — Apartments for rent in TLV
    "https://www.facebook.com/groups/305724686290054",   # General Israel rent
    # Ramat Gan / Givatayim
    "https://www.facebook.com/groups/1092766584127776",  # דירות להשכרה בלבד גבעתיים רמת גן
    "https://www.facebook.com/groups/402682483445663",   # דירות להשכרה רמת גן/גבעתיים
    "https://www.facebook.com/groups/186810449287215",   # דירות להשכרה גבעתיים–רמת גן–תל אביב
    # Removed to save costs:
    # 457465901082882, 253957624766723, 2495267894112896,
    # 458499457501175, 509654872819955, 1432828703704444,
    # 188430365379, 1386194455009158
]

# ── Rent keyword filters (reused from facebook_apartments.py) ─────────────────
RENT_KEYWORDS = [
    "להשכרה", "לשכירה", "דירה", "חדרים", "חד'", "שכירות", "להשכיר",
    "מטר", "קומה", "מרפסת", "חניה", "מחסן",
    "for rent", "apartment", "flat", "studio", "rooms", "available",
    "аренда", "сдаётся", "сдается", "снять", "квартира", "комнат",
]

EXCLUDE_KEYWORDS = [
    "for sale", "למכירה", "продается", "продам",
    "מחפש שותף", "looking for roommate",
    "ищу соседа", "ищу замену",
]

# ── Daily digest time (Israel timezone) ───────────────────────────────────────
DIGEST_HOUR = 20  # 20:00 Israel time
TIMEZONE = "Asia/Jerusalem"

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
