"""Служебные сообщения администратору — в одном месте и с явной пометкой.

Пометка нужна не для красоты. Служебное сообщение и обычное приходят в один и
тот же чат, выглядят одинаково, и при разборе очередной странности первым делом
приходится вспоминать, видел ли это пользователь или только администратор. Одна
строка сверху снимает вопрос.

Заодно здесь собран сам обход администраторов: раньше он был скопирован в пяти
местах, и в каждом свой try/except. Когда таких мест пять, шестое обязательно
окажется без обработки ошибки — и одна недоставленная строка уронит фоновую
задачу целиком.
"""

import logging

from core.store import Store

log = logging.getLogger(__name__)

PREFIX = "🔧 <i>admin only</i>"


def admin_text(text: str) -> str:
    """Пометить сообщение как служебное."""
    return f"{PREFIX}\n\n{text}"


async def notify_admins(bot, store: Store, text: str, exclude: int = None) -> int:
    """Разослать служебное сообщение всем администраторам. Возвращает, скольким ушло.

    Ошибка доставки одному не мешает остальным и не выходит наружу: это
    уведомление, а не работа.
    """
    sent = 0
    for admin in await store.admins():
        if exclude is not None and admin == exclude:
            continue
        try:
            await bot.send_message(admin, admin_text(text), parse_mode="HTML",
                                   disable_web_page_preview=True)
            sent += 1
        except Exception as e:                      # noqa: BLE001
            log.warning("служебное сообщение не ушло администратору %s: %s", admin, e)
    return sent
