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
    "BROOTTO",                    # was BROOTTO_Rent, renamed
    "Israel_arenda",
    "fishyTLV",
    "israel_sublet",              # replaced subletforrussiansinisrael (gone)
    "flamingorent",
    "snyat_kvartiruy",
    "aptfornew",
    "ambery_longrent_telaviv",
    "sapirrent",
    "Sublet_Israel",
    "marianadlanru",
]

# ── Facebook groups (Phase 3, for Apify) ──────────────────────────────────────
FB_GROUPS = [
    "https://www.facebook.com/groups/305724686290054",
    "https://www.facebook.com/groups/457465901082882",
    "https://www.facebook.com/groups/253957624766723",
    "https://www.facebook.com/groups/2495267894112896",
    "https://www.facebook.com/groups/458499457501175",
    "https://www.facebook.com/groups/509654872819955",
    "https://www.facebook.com/groups/1432828703704444",
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
