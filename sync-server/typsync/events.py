"""Plaintext event schema of TypendruckerGram Sync (see docs/sync/protocol.md).

An event is serialized to canonical JSON, then sealed with typsync.crypto.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

MESSAGE = "message"
EDIT = "edit"
DELETE = "delete"
TYPES = (MESSAGE, EDIT, DELETE)

MEDIA_KINDS = ("photo", "video", "voice", "round", "audio", "sticker", "animation", "file")


class EventError(ValueError):
    """Event JSON is malformed or has an unknown type."""


@dataclass
class Media:
    blob_id: str
    mime: str
    size: int
    kind: str
    name: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"blobId": self.blob_id, "mime": self.mime, "size": self.size,
                "kind": self.kind, "name": self.name}

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Media":
        return cls(str(d["blobId"]), str(d["mime"]), int(d["size"]),
                   str(d["kind"]), str(d.get("name", "")))


@dataclass
class Snapshot:
    """One version of one message."""
    dialog: int
    msg_id: int
    from_id: int
    date: int
    text: str
    edit_date: int = 0
    topic: int = 0
    entities: list[dict[str, Any]] = field(default_factory=list)
    reply_to: int = 0
    grouped_id: int = 0
    media: Media | None = None

    def to_json(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "dialog": self.dialog, "msgId": self.msg_id, "fromId": self.from_id,
            "date": self.date, "editDate": self.edit_date, "topic": self.topic,
            "text": self.text, "entities": self.entities,
            "replyTo": self.reply_to, "groupedId": self.grouped_id,
        }
        if self.media is not None:
            d["media"] = self.media.to_json()
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Snapshot":
        return cls(
            dialog=int(d["dialog"]), msg_id=int(d["msgId"]),
            from_id=int(d.get("fromId", 0)), date=int(d.get("date", 0)),
            text=str(d.get("text", "")), edit_date=int(d.get("editDate", 0)),
            topic=int(d.get("topic", 0)), entities=list(d.get("entities", [])),
            reply_to=int(d.get("replyTo", 0)), grouped_id=int(d.get("groupedId", 0)),
            media=Media.from_json(d["media"]) if d.get("media") else None,
        )


@dataclass
class Event:
    """`message`: snapshot. `edit`: previous (replaced version) + new_edit_date.
    `delete`: msg_ids (+ dialog for channels, 0 otherwise) + known snapshots."""
    type: str
    account: int
    snapshot: Snapshot | None = None
    previous: Snapshot | None = None
    new_edit_date: int = 0
    dialog: int = 0
    msg_ids: list[int] = field(default_factory=list)
    snapshots: list[Snapshot] = field(default_factory=list)
    date: int = 0

    def to_json(self) -> dict[str, Any]:
        d: dict[str, Any] = {"v": 1, "type": self.type, "account": self.account}
        if self.type == MESSAGE:
            d["snapshot"] = _need(self.snapshot, "snapshot").to_json()
        elif self.type == EDIT:
            d["previous"] = _need(self.previous, "previous").to_json()
            d["newEditDate"] = self.new_edit_date
        elif self.type == DELETE:
            d.update(dialog=self.dialog, msgIds=list(self.msg_ids),
                     snapshots=[s.to_json() for s in self.snapshots], date=self.date)
        else:
            raise EventError(f"unknown event type {self.type!r}")
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Event":
        try:
            kind = d["type"]
            account = int(d["account"])
            if kind == MESSAGE:
                return cls(MESSAGE, account, snapshot=Snapshot.from_json(d["snapshot"]))
            if kind == EDIT:
                return cls(EDIT, account, previous=Snapshot.from_json(d["previous"]),
                           new_edit_date=int(d["newEditDate"]))
            if kind == DELETE:
                return cls(DELETE, account, dialog=int(d.get("dialog", 0)),
                           msg_ids=[int(x) for x in d["msgIds"]],
                           snapshots=[Snapshot.from_json(s) for s in d.get("snapshots", [])],
                           date=int(d.get("date", 0)))
        except (KeyError, TypeError, ValueError) as e:
            raise EventError(f"malformed event: {e}") from e
        raise EventError(f"unknown event type {kind!r}")

    def encode(self) -> bytes:
        return canonical_json(self.to_json())

    @classmethod
    def decode(cls, data: bytes) -> "Event":
        try:
            return cls.from_json(json.loads(data.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise EventError("event is not valid JSON") from e


def _need(value: Snapshot | None, name: str) -> Snapshot:
    if value is None:
        raise EventError(f"{name} is required")
    return value


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def dedup_key(account: int, dialog: int, msg_id: int, edit_date: int, kind: str) -> str:
    return hashlib.sha256(
        f"{account}|{dialog}|{msg_id}|{edit_date}|{kind}".encode()).hexdigest()


def dedup_keys(event: Event) -> list[str]:
    """Per-message keys; devices use them to merge idempotently."""
    if event.type == MESSAGE:
        s = _need(event.snapshot, "snapshot")
        return [dedup_key(event.account, s.dialog, s.msg_id, s.edit_date, MESSAGE)]
    if event.type == EDIT:
        p = _need(event.previous, "previous")
        return [dedup_key(event.account, p.dialog, p.msg_id, event.new_edit_date, EDIT)]
    if event.type == DELETE:
        return [dedup_key(event.account, event.dialog, i, 0, DELETE)
                for i in sorted(set(event.msg_ids))]
    raise EventError(f"unknown event type {event.type!r}")


def relay_key(event: Event) -> str:
    """One key per event for relay-side deduplication."""
    keys = dedup_keys(event)
    if len(keys) == 1:
        return keys[0]
    return hashlib.sha256("|".join(keys).encode()).hexdigest()
