"""Извлечение фактов из текста объявления.

    from extract import extract
    facts = extract(post_text)

Слои:
    rules.py          — портированные из FB_scrapper регулярки (цена, комнаты, район, убежище)
    deterministic.py  — фасад: собирает Facts, добавляет отрицания, этаж, тип сделки
    schema.py         — схема фактов, три состояния у каждого признака
"""

from .deterministic import extract
from .schema import Facts, YES, NO, UNKNOWN, SCHEMA_VERSION

__all__ = ["extract", "Facts", "YES", "NO", "UNKNOWN", "SCHEMA_VERSION"]
