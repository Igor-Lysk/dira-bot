"""Список команд и описание бота в меню Telegram.

В BotFather у бота не было зарегистрировано ни одной команды: меню-подсказка
пустовало, и узнать про /feed или /settings можно было только из текста
приветствия. Ставится через API, руками в BotFather ходить не нужно.

    python3 scripts/set_bot_menu.py
"""

import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import settings                            # noqa: E402

COMMANDS = [
    ("feed", "что нашлось"),
    ("settings", "критерии поиска"),
    ("stats", "статистика"),
    ("pause", "приостановить"),
    ("resume", "продолжить"),
    ("setup", "настроить заново"),
]

SHORT = "Ищу квартиры в аренду и присылаю подходящие."
FULL = ("Читаю Telegram-каналы и доски объявлений, разбираю каждое объявление "
        "на факты и присылаю то, что подходит под твои критерии.\n\n"
        "Нажми «Начать» — десять коротких вопросов, и всё.")


def call(method: str, payload: dict):
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{settings.BOT_TOKEN}/{method}",
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response).get("ok")


if __name__ == "__main__":
    print("команды:", call("setMyCommands", {
        "commands": [{"command": c, "description": d} for c, d in COMMANDS]}))
    print("короткое описание:", call("setMyShortDescription", {"short_description": SHORT}))
    print("описание:", call("setMyDescription", {"description": FULL}))
