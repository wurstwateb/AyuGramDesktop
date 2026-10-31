"""Turns Telegram updates into sync events while the catcher is active.

Works on Telethon `Message` objects but only reads plain attributes, so tests
can pass simple stand-ins. Snapshots live in memory for the active session
only (needed to fill `previous` for edits and snapshots for deletions) and
are dropped when the catcher disconnects.
"""
from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Any, Awaitable, Callable

from typsync import events as ev

log = logging.getLogger("typsync.catcher")

MediaSaver = Callable[[Any, str], Awaitable["ev.Media | None"]]
Emit = Callable[[ev.Event], None]

ENTITY_TYPES = {
    "MessageEntityBold": "bold",
    "MessageEntityItalic": "italic",
    "MessageEntityUnderline": "underline",
    "MessageEntityStrike": "strike",
    "MessageEntitySpoiler": "spoiler",
    "MessageEntityCode": "code",
    "MessageEntityPre": "pre",
    "MessageEntityUrl": "url",
    "MessageEntityTextUrl": "textUrl",
    "MessageEntityMention": "mention",
    "MessageEntityMentionName": "mentionName",
    "MessageEntityHashtag": "hashtag",
    "MessageEntityCashtag": "cashtag",
    "MessageEntityBotCommand": "botCommand",
    "MessageEntityEmail": "email",
    "MessageEntityPhone": "phone",
    "MessageEntityCustomEmoji": "customEmoji",
    "MessageEntityBlockquote": "blockquote",
}

# Marked ids as used by Telethon / Bot API.
CHANNEL_MARK = -1_000_000_000_000


def resolve_dialog(marked: int) -> tuple[int, bool]:
    """Telethon chat id -> (AyuGram dialog id, is_channel).

    AyuGram: users positive, groups and channels negative bare id (no -100).
    """
    if marked <= CHANNEL_MARK:
        return -bare_id(marked), True
    return marked, False


def bare_id(marked: int | None) -> int:
    """Marked id -> positive bare id (what AyuGram stores as fromId)."""
    if not marked:
        return 0
    if marked >= 0:
        return marked
    if marked <= CHANNEL_MARK:
        return -marked + CHANNEL_MARK
    return -marked


def convert_entities(entities: list[Any] | None) -> list[dict[str, Any]]:
    out = []
    for e in entities or []:
        kind = ENTITY_TYPES.get(type(e).__name__)
        if kind is None:
            continue
        item: dict[str, Any] = {"type": kind, "offset": e.offset, "length": e.length}
        if kind == "textUrl":
            item["url"] = e.url
        elif kind == "mentionName":
            item["userId"] = e.user_id
        elif kind == "customEmoji":
            item["documentId"] = e.document_id
        elif kind == "pre" and getattr(e, "language", ""):
            item["language"] = e.language
        out.append(item)
    return out


def media_kind(msg: Any) -> str | None:
    if getattr(msg, "photo", None):
        return "photo"
    if getattr(msg, "voice", None):
        return "voice"
    if getattr(msg, "video_note", None):
        return "round"
    if getattr(msg, "gif", None):
        return "animation"
    if getattr(msg, "sticker", None):
        return "sticker"
    if getattr(msg, "video", None):
        return "video"
    if getattr(msg, "audio", None):
        return "audio"
    if getattr(msg, "document", None):
        return "file"
    return None


def _timestamp(value: Any) -> int:
    if value is None:
        return 0
    return int(value.timestamp()) if hasattr(value, "timestamp") else int(value)


def snapshot_of(msg: Any, media: ev.Media | None = None) -> ev.Snapshot:
    dialog, _ = resolve_dialog(msg.chat_id)
    reply = getattr(msg, "reply_to", None)
    topic = 0
    reply_to = 0
    if reply is not None:
        reply_to = getattr(reply, "reply_to_msg_id", 0) or 0
        if getattr(reply, "forum_topic", False):
            topic = getattr(reply, "reply_to_top_id", 0) or reply_to
    return ev.Snapshot(
        dialog=dialog,
        msg_id=msg.id,
        from_id=bare_id(getattr(msg, "sender_id", None)) or abs(dialog),
        date=_timestamp(msg.date),
        edit_date=_timestamp(getattr(msg, "edit_date", None)),
        topic=topic,
        text=getattr(msg, "message", None) or "",
        entities=convert_entities(getattr(msg, "entities", None)),
        reply_to=reply_to,
        grouped_id=getattr(msg, "grouped_id", None) or 0,
        media=media,
    )


class Capture:
    def __init__(self, account: int, emit: Emit, save_media: MediaSaver | None,
                 media_limit: Callable[[], int], *, max_cache: int = 20_000):
        self.account = account
        self.emit = emit
        self.save_media = save_media
        self.media_limit = media_limit
        self.max_cache = max_cache
        self.cache: OrderedDict[tuple[int, int], ev.Snapshot] = OrderedDict()
        # Ids in private chats and basic groups are unique per account, and
        # Telegram does not say which chat such a deletion belongs to.
        self.by_id: dict[int, tuple[int, int]] = {}

    def _remember(self, snap: ev.Snapshot, is_channel: bool) -> None:
        key = (snap.dialog, snap.msg_id)
        self.cache[key] = snap
        self.cache.move_to_end(key)
        if not is_channel:
            self.by_id[snap.msg_id] = key
        while len(self.cache) > self.max_cache:
            (dialog, msg_id), _ = self.cache.popitem(last=False)
            if self.by_id.get(msg_id) == (dialog, msg_id):
                del self.by_id[msg_id]

    def _forget(self, key: tuple[int, int]) -> ev.Snapshot | None:
        snap = self.cache.pop(key, None)
        if snap is not None and self.by_id.get(snap.msg_id) == key:
            del self.by_id[snap.msg_id]
        return snap

    async def _media(self, msg: Any) -> ev.Media | None:
        kind = media_kind(msg)
        file = getattr(msg, "file", None)
        size = getattr(file, "size", None) or 0
        if kind is None or self.save_media is None or not 0 < size <= self.media_limit():
            return None
        try:
            return await self.save_media(msg, kind)
        except Exception:
            log.exception("media of message %s not saved", msg.id)
            return None

    async def on_new(self, msg: Any) -> None:
        _, is_channel = resolve_dialog(msg.chat_id)
        snap = snapshot_of(msg, await self._media(msg))
        self._remember(snap, is_channel)
        self.emit(ev.Event(ev.MESSAGE, self.account, snapshot=snap))

    async def on_edit(self, msg: Any) -> None:
        dialog, is_channel = resolve_dialog(msg.chat_id)
        new_edit_date = _timestamp(getattr(msg, "edit_date", None))
        previous = self.cache.get((dialog, msg.id))
        if getattr(msg, "out", False) or not new_edit_date or previous is None:
            # Own edits are not tracked (as in AyuGram); older versions of
            # messages received before the catcher woke up are on the devices.
            return
        current = snapshot_of(msg, previous.media)
        if current.text == previous.text and current.entities == previous.entities:
            return  # reactions, views and other non-content changes
        self._remember(current, is_channel)
        self.emit(ev.Event(ev.EDIT, self.account, previous=previous,
                           new_edit_date=new_edit_date))

    def on_delete(self, msg_ids: list[int], chat_id: int | None, now: int) -> None:
        if not msg_ids:
            return
        dialog = resolve_dialog(chat_id)[0] if chat_id else 0
        snapshots = []
        for msg_id in msg_ids:
            key = (dialog, msg_id) if dialog else self.by_id.get(msg_id)
            snap = self._forget(key) if key else None
            if snap is not None:
                snapshots.append(snap)
        self.emit(ev.Event(ev.DELETE, self.account, dialog=dialog, msg_ids=list(msg_ids),
                           snapshots=snapshots, date=now))
