"""Dira Bot — Telethon-based Telegram channel monitor.

Connects to the user's Telegram account via StringSession and listens
for new messages in configured rental channels. When a message looks like
a rental listing (keyword match), it is passed to the processing pipeline.

Uses the session string from telegram-mcp.
"""

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Callable, Awaitable

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import Channel, Chat
from telethon.tl.functions.channels import JoinChannelRequest
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
        self._channel_ids: dict[int, str] = {}   # peer_id → username
        self._entities: dict[str, object] = {}   # username → entity

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
                self._entities[username] = entity
            except Exception as e:
                log.warning("  Could not resolve @%s: %s", username, e)

        if not self._channel_ids:
            log.error("No channels resolved! Check TG_CHANNELS config.")
            return

        # Telethon fires real-time NewMessage updates only for dialogs the
        # user participates in. Backfill via iter_messages works on public
        # channels without membership, but live events don't arrive.
        # So: auto-join every monitored channel. Idempotent — joining a
        # channel you're already in is a no-op on Telegram's side.
        for username, entity in self._entities.items():
            try:
                await self._client(JoinChannelRequest(entity))
                log.info("  Joined @%s", username)
            except Exception as e:
                # Already a participant / banned / rate-limited — non-fatal.
                log.debug("  Join @%s skipped: %s", username, e)

        # Register event handler using entity objects (not IDs).
        # Passing IDs is unreliable for channels: Telegram channels use a
        # "marked" peer ID (-1001xxxxxxxxx) that differs from entity.id.
        # Telethon resolves entities correctly when given the objects directly.
        self._client.add_event_handler(
            self._handle_message,
            events.NewMessage(chats=list(self._entities.values())),
        )

        # Keep Telethon's update-receiving loop alive in the background.
        # Without this task, incoming channel updates may never fire handlers.
        asyncio.create_task(self._client.run_until_disconnected())

        unique_channels = len(set(self._channel_ids.values()))
        log.info("Listening for new messages in %d channels...", unique_channels)

    async def _handle_message(self, event):
        """Process a new message from a monitored channel."""
        text = event.message.text or ""
        log.debug("TG event: chat_id=%s len=%d", event.chat_id, len(text))
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

    async def backfill(self, days: int = 7):
        """Fetch recent history from all monitored channels.

        Iterates up to `days` days back in each channel and passes
        matching messages to the on_listing callback. Safe to call
        repeatedly — dedup in the DB prevents double-processing.
        """
        if not self._client or not self._on_listing or not self._entities:
            return

        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)
        total = 0

        for username, entity in self._entities.items():
            count = 0
            try:
                async for msg in self._client.iter_messages(entity, limit=500):
                    if msg.date < cutoff:
                        break
                    text = msg.text or ""
                    if not _looks_like_listing(text):
                        continue
                    url = f"https://t.me/{username}/{msg.id}"
                    raw = {
                        "source_id": f"tg_{entity.id}_{msg.id}",
                        "raw_text": text,
                        "url": url,
                        "channel": username,
                    }
                    await self._on_listing(raw)
                    count += 1
            except Exception as e:
                log.warning("Backfill @%s error: %s", username, e)
            log.info("Backfill @%s: %d messages", username, count)
            total += count

        log.info("Telegram backfill complete: %d listings processed", total)

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
