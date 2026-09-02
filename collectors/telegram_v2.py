"""Мониторинг Telegram-каналов через Telethon.

Перенесено из v1 почти без изменений — эта часть работала. Сохранены две вещи,
которые дались дорого:

1. **Обработчик регистрируется на объекты сущностей, а не на id.** У каналов
   «помеченный» peer id (-1001…) отличается от `entity.id`, и при передаче id
   события просто не приходили.
2. **Автовступление в каналы.** Telethon шлёт real-time события только по
   диалогам, в которых участвует аккаунт. История читается и без членства,
   а живые обновления — нет.

И главное: `is_healthy()`. В v1 таск `run_until_disconnected` однажды тихо
завершился без исключения и без записи в лог, бот 36 часов выглядел живым и
ничего не присылал. Теперь состояние проверяется явно, а планировщик перестаёт
пинговать healthchecks.io, если монитор нездоров, — и приходит внешний алерт.
"""

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional, Tuple

from telethon import TelegramClient, events, utils as tl_utils
from telethon.sessions import StringSession
from telethon.tl.functions.channels import JoinChannelRequest

from core import settings

log = logging.getLogger(__name__)

RENT_WORDS = re.compile(
    r"להשכרה|שכירות|דירה|חדרים|חד'|аренда|сдам|сдаётся|сдается|снять|квартир|комнат|"
    r"for rent|apartment|flat|studio|rooms",
    re.IGNORECASE)
EXCLUDE_WORDS = re.compile(
    r"למכירה|for sale|продаётся|продается|продам|вакансия|ищу\s+работу",
    re.IGNORECASE)


def looks_like_listing(text: str) -> bool:
    if not text or len(text) < 40:
        return False
    if EXCLUDE_WORDS.search(text):
        return False
    return bool(RENT_WORDS.search(text))


class TelegramMonitor:
    # Каналы аренды затихают ночью, но не на восемь часов подряд: если событий
    # нет дольше, монитор считается мёртвым.
    MAX_IDLE_SEC = 8 * 3600

    def __init__(self, channels: list):
        self.channels = channels
        self._client: Optional[TelegramClient] = None
        self._on_listing: Optional[Callable[[dict], Awaitable[None]]] = None
        self._entities: dict = {}
        self._names: dict = {}
        self._task: Optional[asyncio.Task] = None
        self._started_at = 0.0
        self._last_event_at = 0.0

    async def start(self, on_listing):
        self._on_listing = on_listing
        self._started_at = self._last_event_at = time.monotonic()
        self._client = TelegramClient(
            StringSession(settings.TG_SESSION), settings.TG_API_ID, settings.TG_API_HASH)
        await self._client.start()
        me = await self._client.get_me()
        log.info("Telethon подключён как %s", me.first_name)

        for name in self.channels:
            try:
                entity = await self._client.get_entity(name)
                self._entities[name] = entity
                self._names[entity.id] = name
                try:
                    marked = tl_utils.get_peer_id(entity)
                    self._names[marked] = name
                except Exception:                     # noqa: BLE001
                    pass
                try:
                    await self._client(JoinChannelRequest(entity))
                except Exception:                     # noqa: BLE001
                    pass                              # уже участник либо нельзя — не страшно
            except Exception as e:                    # noqa: BLE001
                log.warning("канал @%s не резолвится: %s", name, e)

        if not self._entities:
            log.error("ни один канал не резолвится — мониторить нечего")
            return

        self._client.add_event_handler(
            self._on_message, events.NewMessage(chats=list(self._entities.values())))
        self._task = asyncio.create_task(self._client.run_until_disconnected())
        log.info("слушаю %d каналов", len(self._entities))

    async def _on_message(self, event):
        self._last_event_at = time.monotonic()        # любое событие = цикл жив
        text = event.message.text or ""
        if not looks_like_listing(text):
            return
        name = self._names.get(event.chat_id, str(event.chat_id))
        raw = {
            "source": "telegram",
            "source_id": f"tg_{event.chat_id}_{event.message.id}",
            "channel": name,
            "url": f"https://t.me/{name}/{event.message.id}",
            "raw_text": text,
            "posted_at": event.message.date.isoformat() if event.message.date else None,
        }
        try:
            await self._on_listing(raw)
        except Exception as e:                        # noqa: BLE001
            log.exception("ошибка обработки объявления: %s", e)

    async def backfill(self, days: int = 3, per_channel: int = 300) -> int:
        """История за последние дни. Без неё бот стартует с пустой лентой."""
        if not self._client or not self._on_listing:
            return 0
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)
        total = 0
        for name, entity in self._entities.items():
            count = 0
            try:
                async for msg in self._client.iter_messages(entity, limit=per_channel):
                    if msg.date < cutoff:
                        break
                    text = msg.text or ""
                    if not looks_like_listing(text):
                        continue
                    await self._on_listing({
                        "source": "telegram",
                        "source_id": f"tg_{entity.id}_{msg.id}",
                        "channel": name,
                        "url": f"https://t.me/{name}/{msg.id}",
                        "raw_text": text,
                        "posted_at": msg.date.isoformat(),
                    })
                    count += 1
            except Exception as e:                    # noqa: BLE001
                log.warning("backfill @%s: %s", name, e)
            log.info("backfill @%s: %d", name, count)
            total += count
        return total

    def is_healthy(self) -> Tuple[bool, str]:
        if self._client is None or self._task is None:
            return True, "ещё не запущен"
        if not self._client.is_connected():
            return False, "клиент отключён"
        if self._task.done():
            return False, f"цикл обновлений завершился ({self._task.exception()!r})"
        idle = time.monotonic() - self._last_event_at
        if idle > self.MAX_IDLE_SEC:
            return False, f"нет событий {idle / 3600:.1f} ч"
        return True, "ok"

    async def stop(self):
        if self._client:
            await self._client.disconnect()
