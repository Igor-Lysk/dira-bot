"""Настройки из окружения.

Того, что было в v1, здесь заметно меньше: критерии поиска, список городов и
пороги переехали в базу, к профилям пользователей. В окружении остаются только
ключи и параметры среды — всё, что нельзя или не нужно менять из чата.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_env_file(path: Path) -> None:
    """Простой разбор .env. Файл писался под Windows, поэтому терпим CRLF и BOM."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_file(BASE_DIR / ".env")

DB_PATH = os.getenv("DB_PATH", str(BASE_DIR / "data" / "dira.db"))

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_API_ID = int(os.getenv("TELEGRAM_API_ID", "0") or 0)
TG_API_HASH = os.getenv("TELEGRAM_API_HASH", "")
TG_SESSION = os.getenv("TELEGRAM_SESSION_STRING", "")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

HEALTHCHECK_URL = os.getenv("HEALTHCHECK_URL", "")
HEALTHCHECK_INTERVAL_MIN = 10

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
TIMEZONE = "Asia/Jerusalem"

# Как часто дозаполнять факты моделью и подбирать сорвавшееся.
ENRICH_INTERVAL_MIN = 10
ENRICH_BATCH = 25
RETRY_INTERVAL_MIN = 60
DELIVERY_INTERVAL_MIN = 5
BACKFILL_DAYS = 3
