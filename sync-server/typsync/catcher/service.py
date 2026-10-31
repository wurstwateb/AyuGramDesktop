"""The catcher process: polls presence, connects the userbot at night, ships events."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable

from typsync import events as ev
from typsync.catcher.capture import Capture
from typsync.catcher.presence import Action, Presence, PresenceMachine, State
from typsync.client import RelayClient

log = logging.getLogger("typsync.catcher")

POLL_SECONDS = 30
FLUSH_SECONDS = 5
BATCH = 500
MAX_OUTBOX = 50_000


@dataclass
class CatcherConfig:
    relay_url: str
    token: str
    owner_public: str  # base64
    api_id: int
    api_hash: str
    session_path: str

    @property
    def owner_public_bytes(self) -> bytes:
        return base64.b64decode(self.owner_public)

    @classmethod
    def load(cls, path: str) -> "CatcherConfig":
        with open(path, encoding="utf-8") as f:
            return cls(**json.load(f))

    def save(self, path: str) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2)


def make_telegram_client(config: CatcherConfig) -> Any:
    from telethon import TelegramClient
    return TelegramClient(
        config.session_path, config.api_id, config.api_hash,
        device_model="TypendruckerGram Catcher", system_version="server",
        app_version="1.0", catch_up=True)


class Catcher:
    def __init__(self, config: CatcherConfig, relay: RelayClient,
                 make_client: Callable[[], Any], clock: Callable[[], float] = time.time):
        self.config = config
        self.relay = relay
        self.make_client = make_client
        self.clock = clock
        self.machine = PresenceMachine()
        self.media_limit = 0
        self.outbox: list[ev.Event] = []
        self.client: Any = None
        self.capture: Capture | None = None

    async def run(self) -> None:
        flusher = asyncio.create_task(self._flush_loop())
        try:
            while True:
                await self.tick()
                await asyncio.sleep(POLL_SECONDS)
        finally:
            flusher.cancel()
            await self.disconnect()

    async def tick(self) -> None:
        try:
            p = await self.relay.presence()
        except Exception as e:
            log.warning("presence unavailable: %s", e)
            return
        self.media_limit = int(p.get("mediaLimit", 0))
        action = self.machine.step(
            self.clock(), Presence(int(p["allOfflineForSeconds"]), bool(p["catcherEnabled"])))
        if action is Action.CONNECT:
            try:
                await self.connect()
                log.info("all devices offline: userbot connected")
            except Exception:
                log.exception("userbot could not connect")
                self.machine.state = State.IDLE
                await self.disconnect()
        elif action is Action.DISCONNECT:
            await self.disconnect()
            log.info("a device is back: userbot disconnected")

    async def connect(self) -> None:
        from telethon import events
        client = self.make_client()
        self.client = client
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram session is not authorized; run typsync.catcher.setup")
        me = await client.get_me()
        self.capture = Capture(me.id, self._emit, self._save_media, lambda: self.media_limit)
        client.add_event_handler(self._on_new, events.NewMessage())
        client.add_event_handler(self._on_edit, events.MessageEdited())
        client.add_event_handler(self._on_delete, events.MessageDeleted())

    async def disconnect(self) -> None:
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:
                log.exception("disconnect failed")
        self.client = None
        self.capture = None  # drop in-memory plaintext snapshots
        await self.flush()

    def _emit(self, event: ev.Event) -> None:
        self.outbox.append(event)
        if len(self.outbox) > MAX_OUTBOX:
            dropped = len(self.outbox) - MAX_OUTBOX
            del self.outbox[:dropped]
            log.warning("relay unreachable, dropped %d oldest events", dropped)

    async def _on_new(self, event: Any) -> None:
        if self.capture is not None:
            await self.capture.on_new(event.message)

    async def _on_edit(self, event: Any) -> None:
        if self.capture is not None:
            await self.capture.on_edit(event.message)

    async def _on_delete(self, event: Any) -> None:
        if self.capture is not None:
            self.capture.on_delete(list(event.deleted_ids), event.chat_id, int(self.clock()))

    async def _save_media(self, msg: Any, kind: str) -> ev.Media | None:
        data = await self.client.download_media(msg, file=bytes)
        if not data:
            return None
        blob_id = await self.relay.put_blob(self.config.owner_public_bytes, data)
        file = msg.file
        return ev.Media(blob_id, getattr(file, "mime_type", None) or "application/octet-stream",
                        len(data), kind, getattr(file, "name", None) or "")

    async def flush(self) -> None:
        while self.outbox:
            batch = self.outbox[:BATCH]
            try:
                await self.relay.post_events(self.config.owner_public_bytes, batch)
            except Exception as e:
                log.warning("events not sent yet (%d queued): %s", len(self.outbox), e)
                return
            del self.outbox[:len(batch)]

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(FLUSH_SECONDS)
            await self.flush()
