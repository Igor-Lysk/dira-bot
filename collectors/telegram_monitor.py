"""Dira Bot — Telethon-based Telegram channel monitor.

Connects to the user's Telegram account via StringSession and listens
for new messages in configured rental channels. When a message looks like
a rental listing (keyword match), it is passed to the processing pipeline.

Uses the session string from telegram-mcp.
"""

import asyncio
import logging
import re
from typing import Callable, Awaitable

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import Channel, Chat
from telethon import utils as tl_utils

import config
from collectors.base import BaseCollector

log = logging.getLogger(__name__)

# Pre-compiled rent keyword pattern (Hebrew / Russian / English)
_RENT_RE = re.compile(
    "|".join(re.escape(kw) for kw in config.RENT_KEYWORDS),
    re.IGNORECASE,
)
_EXCLUDE_RE = re.compile(
    "|".join(re.escape(kw) for kw in config.EXCLUDE_KEYWORDS),
    re.IGNORECASE,
)


def _looks_like_listing(text: str) -> bool:
    """Quick check: does this message look like a rental listing?"""
    if not text or len(text) < 40:
        return False
    if _EXCLUDE_RE.search(text):
        return False
    return bool(_RENT_RE.search(text))


class TelegramMonitor(BaseCollector):
    """Real-time Telegram channel monitor using Telethon."""

    source_name = "telegram"

    def __init__(self):
        self._client: TelegramClient | None = None
        self._on_listing: Callable[[dict], Awaitable[None]] | None = None
        self._channel_ids: dict[int, str] = {}  # id → username

    async def start(self, on_listing: Callable[[dict], Awaitable[None]]):
        """Start monitoring. Calls on_listing(raw_dict) for each new post.

        Args:
            on_listing: async callback receiving a raw listing dict.
        """
        self._on_listing = on_listing

        self._client = TelegramClient(
            StringSession(config.TG_SESSION),
            config.TG_API_ID,
            config.TG_API_HASH,
        )
        await self._client.start()
        me = await self._client.get_me()
        log.info("Telethon connected as %s (id=%s)", me.first_name, me.id)

        # Resolve channel usernames → entities
        for username in config.TG_CHANNELS:
            try:
                entity = await self._client.get_entity(username)
                # Store both raw ID and marked peer ID for robust lookup
                self._channel_ids[entity.id] = username
                try:
                    marked = tl_utils.get_peer_id(entity)
                    if marked != entity.id:
                        self._channel_ids[marked] = username
                except Exception:
                    pass
                log.info("  Monitoring: @%s (id=%s)", username, entity.id)
            except Exception as e:
                log.warning("  Could not resolve @%s: %s", username, e)

        if not self._channel_ids:
            log.error("No channels resolved! Check TG_CHANNELS config.")
            return

        # Register event handler for new messages in monitored channels
        @self._client.on(events.NewMessage(chats=list(self._channel_ids.keys())))
        async def _handler(event):
            await self._handle_message(event)

        unique_channels = len(set(self._channel_ids.values()))
        log.info("Listening for new messages in %d channels...", unique_channels)

    async def _handle_message(self, event):
        """Process a new message from a monitored channel."""
        text = event.message.text or ""
        if not _looks_like_listing(text):
            return

        chat_id = event.chat_id
        msg_id = event.message.id
        username = self._channel_ids.get(chat_id, str(chat_id))

        # Build URL to the message
        url = f"https://t.me/{username}/{msg_id}"

        raw = {
            "source_id": f"tg_{chat_id}_{msg_id}",
            "raw_text": text,
            "url": url,
            "channel": username,
        }

        log.info("New listing from @%s (msg %s, %d chars)", username, msg_id, len(text))

        if self._on_listing:
            try:
                await self._on_listing(raw)
            except Exception as e:
                log.exception("Error in on_listing callback: %s", e)

    async def collect(self) -> list[dict]:
        """Not used for real-time monitoring — see start() instead.
        Kept for BaseCollector interface compliance.
        """
        return []

    async def stop(self):
        if self._client:
            await self._client.disconnect()
            log.info("Telethon disconnected.")

    def is_running(self) -> bool:
        return self._client is not None and self._client.is_connected()
