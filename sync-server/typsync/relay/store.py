"""SQLite storage of the relay. Every method takes `now` so tests control time.

The relay only ever sees opaque encrypted blobs. It keeps them until every
paired device has fetched them, the retention period ends, or the owner's
quota forces the oldest data out.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEVICE = "device"
CATCHER = "catcher"

DEFAULT_SETTINGS = {"catcherEnabled": True, "mediaLimit": 20 * 1024 * 1024}

SCHEMA = """
CREATE TABLE IF NOT EXISTS owners (
    id TEXT PRIMARY KEY,
    public_key BLOB NOT NULL,
    settings TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES owners(id),
    token_hash TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    last_seen INTEGER NOT NULL,
    cursor INTEGER NOT NULL DEFAULT 0,
    trimmed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id TEXT NOT NULL,
    from_device TEXT NOT NULL,
    relay_key TEXT NOT NULL,
    size INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    blob BLOB NOT NULL,
    UNIQUE (owner_id, relay_key)
);
CREATE INDEX IF NOT EXISTS events_owner_seq ON events (owner_id, seq);
CREATE TABLE IF NOT EXISTS deliveries (
    seq INTEGER NOT NULL,
    device_id TEXT NOT NULL,
    PRIMARY KEY (seq, device_id)
);
CREATE TABLE IF NOT EXISTS blobs (
    owner_id TEXT NOT NULL,
    id TEXT NOT NULL,
    size INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (owner_id, id)
);
CREATE TABLE IF NOT EXISTS pairings (
    lookup_hash TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload BLOB NOT NULL,
    expires_at INTEGER NOT NULL
);
"""


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _new_id() -> str:
    return secrets.token_hex(8)


def _new_token() -> str:
    return secrets.token_urlsafe(32)


@dataclass
class Device:
    id: str
    owner_id: str
    name: str
    kind: str
    last_seen: int
    cursor: int


@dataclass
class Credentials:
    owner_id: str
    device_id: str
    token: str
    kind: str


class Store:
    def __init__(self, db_path: str | os.PathLike, blob_dir: str | os.PathLike,
                 *, quota_bytes: int, retention_seconds: int):
        self.blob_dir = Path(blob_dir)
        self.blob_dir.mkdir(parents=True, exist_ok=True)
        self.quota_bytes = quota_bytes
        self.retention_seconds = retention_seconds
        self.db = sqlite3.connect(str(db_path), check_same_thread=False,
                                  isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # Owners and devices.

    def create_owner(self, public_key: bytes, device_name: str, now: int) -> Credentials:
        owner_id = _new_id()
        with self.db:
            self.db.execute("BEGIN")
            self.db.execute(
                "INSERT INTO owners (id, public_key, settings, created_at) VALUES (?, ?, ?, ?)",
                (owner_id, public_key, json.dumps(DEFAULT_SETTINGS), now))
            return self._add_device(owner_id, device_name, DEVICE, now)

    def _add_device(self, owner_id: str, name: str, kind: str, now: int) -> Credentials:
        device_id, token = _new_id(), _new_token()
        # Cursor starts at 0: the owner key is shared, so a newly paired
        # device can decrypt and should receive everything still kept here.
        self.db.execute(
            "INSERT INTO devices (id, owner_id, token_hash, name, kind, created_at, last_seen)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (device_id, owner_id, hash_token(token), name, kind, now, now))
        return Credentials(owner_id, device_id, token, kind)

    def device_by_token(self, token: str) -> Device | None:
        row = self.db.execute(
            "SELECT id, owner_id, name, kind, last_seen, cursor FROM devices WHERE token_hash = ?",
            (hash_token(token),)).fetchone()
        return Device(**dict(row)) if row else None

    def touch(self, device: Device, now: int) -> None:
        self.db.execute("UPDATE devices SET last_seen = ? WHERE id = ?", (now, device.id))

    def devices(self, owner_id: str) -> list[Device]:
        rows = self.db.execute(
            "SELECT id, owner_id, name, kind, last_seen, cursor FROM devices"
            " WHERE owner_id = ? ORDER BY created_at", (owner_id,)).fetchall()
        return [Device(**dict(r)) for r in rows]

    def remove_device(self, owner_id: str, device_id: str) -> bool:
        with self.db:
            self.db.execute("BEGIN")
            cur = self.db.execute(
                "DELETE FROM devices WHERE id = ? AND owner_id = ?", (device_id, owner_id))
            self.db.execute("DELETE FROM deliveries WHERE device_id = ?", (device_id,))
        return cur.rowcount > 0

    def owner_public_key(self, owner_id: str) -> bytes:
        return self.db.execute(
            "SELECT public_key FROM owners WHERE id = ?", (owner_id,)).fetchone()[0]

    def settings(self, owner_id: str) -> dict[str, Any]:
        row = self.db.execute("SELECT settings FROM owners WHERE id = ?", (owner_id,)).fetchone()
        return {**DEFAULT_SETTINGS, **json.loads(row[0])}

    def update_settings(self, owner_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        merged = {**self.settings(owner_id), **changes}
        self.db.execute("UPDATE owners SET settings = ? WHERE id = ?",
                        (json.dumps(merged), owner_id))
        return merged

    def all_offline_for(self, owner_id: str, now: int) -> int:
        row = self.db.execute(
            "SELECT MAX(last_seen) FROM devices WHERE owner_id = ? AND kind = ?",
            (owner_id, DEVICE)).fetchone()
        if row[0] is None:
            return 10 ** 9
        return max(0, now - row[0])

    # Pairing.

    def start_pairing(self, owner_id: str, lookup_hash: str, kind: str,
                      payload: bytes, expires_at: int) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO pairings (lookup_hash, owner_id, kind, payload, expires_at)"
            " VALUES (?, ?, ?, ?, ?)", (lookup_hash, owner_id, kind, payload, expires_at))

    def claim_pairing(self, lookup_hash: str, name: str,
                      now: int) -> tuple[Credentials, bytes] | None:
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT owner_id, kind, payload, expires_at FROM pairings WHERE lookup_hash = ?",
                (lookup_hash,)).fetchone()
            if row is None:
                return None
            self.db.execute("DELETE FROM pairings WHERE lookup_hash = ?", (lookup_hash,))
            if row["expires_at"] < now:
                return None
            creds = self._add_device(row["owner_id"], name, row["kind"], now)
            return creds, row["payload"]

    # Events.

    def add_events(self, device: Device, items: list[tuple[str, bytes]],
                   now: int) -> tuple[int, int]:
        accepted = 0
        with self.db:
            self.db.execute("BEGIN")
            for key, blob in items:
                cur = self.db.execute(
                    "INSERT OR IGNORE INTO events"
                    " (owner_id, from_device, relay_key, size, created_at, blob)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (device.owner_id, device.id, key, len(blob), now, blob))
                accepted += cur.rowcount
        self.enforce_quota(device.owner_id)
        return accepted, len(items) - accepted

    def fetch_events(self, device: Device, after: int,
                     limit: int) -> tuple[list[sqlite3.Row], int, bool]:
        rows = self.db.execute(
            "SELECT seq, relay_key, blob, from_device FROM events"
            " WHERE owner_id = ? AND seq > ? AND from_device != ? ORDER BY seq LIMIT ?",
            (device.owner_id, after, device.id, limit)).fetchall()
        cursor = rows[-1]["seq"] if rows else after
        with self.db:
            self.db.execute("BEGIN")
            self.db.executemany(
                "INSERT OR IGNORE INTO deliveries (seq, device_id) VALUES (?, ?)",
                [(r["seq"], device.id) for r in rows])
            trimmed = self.db.execute(
                "SELECT trimmed FROM devices WHERE id = ?", (device.id,)).fetchone()[0]
            self.db.execute("UPDATE devices SET cursor = MAX(cursor, ?), trimmed = 0"
                            " WHERE id = ?", (cursor, device.id))
        return rows, cursor, bool(trimmed)

    # Blobs.

    def blob_path(self, owner_id: str, blob_id: str) -> Path:
        return self.blob_dir / owner_id / blob_id

    def has_blob(self, owner_id: str, blob_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM blobs WHERE owner_id = ? AND id = ?",
                               (owner_id, blob_id)).fetchone() is not None

    def add_blob(self, owner_id: str, blob_id: str, size: int, now: int) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO blobs (owner_id, id, size, created_at) VALUES (?, ?, ?, ?)",
            (owner_id, blob_id, size, now))
        self.enforce_quota(owner_id)

    def _delete_blob(self, owner_id: str, blob_id: str) -> None:
        self.db.execute("DELETE FROM blobs WHERE owner_id = ? AND id = ?", (owner_id, blob_id))
        self.blob_path(owner_id, blob_id).unlink(missing_ok=True)

    # Space management.

    def used_bytes(self, owner_id: str) -> int:
        events = self.db.execute(
            "SELECT COALESCE(SUM(size), 0) FROM events WHERE owner_id = ?",
            (owner_id,)).fetchone()[0]
        blobs = self.db.execute(
            "SELECT COALESCE(SUM(size), 0) FROM blobs WHERE owner_id = ?",
            (owner_id,)).fetchone()[0]
        return events + blobs

    def enforce_quota(self, owner_id: str) -> bool:
        """Drops the oldest media first, then the oldest events. Returns True if trimmed."""
        trimmed = False
        while self.used_bytes(owner_id) > self.quota_bytes:
            blob = self.db.execute(
                "SELECT id FROM blobs WHERE owner_id = ? ORDER BY created_at, rowid LIMIT 1",
                (owner_id,)).fetchone()
            if blob is not None:
                self._delete_blob(owner_id, blob[0])
            else:
                event = self.db.execute(
                    "SELECT seq FROM events WHERE owner_id = ? ORDER BY seq LIMIT 1",
                    (owner_id,)).fetchone()
                if event is None:
                    break
                self._delete_events([event[0]])
            trimmed = True
        if trimmed:
            self.db.execute("UPDATE devices SET trimmed = 1 WHERE owner_id = ?", (owner_id,))
        return trimmed

    def _delete_events(self, seqs: list[int]) -> None:
        if not seqs:
            return
        marks = ",".join("?" * len(seqs))
        self.db.execute(f"DELETE FROM events WHERE seq IN ({marks})", seqs)
        self.db.execute(f"DELETE FROM deliveries WHERE seq IN ({marks})", seqs)

    def cleanup(self, now: int) -> dict[str, int]:
        """Hourly job: expired data, fully delivered events, stale pairings."""
        cutoff = now - self.retention_seconds
        expired = [r[0] for r in self.db.execute(
            "SELECT seq FROM events WHERE created_at < ?", (cutoff,))]
        # Delivered to every paired device except the sender; events nobody
        # could receive yet (single device) wait for a future pairing.
        delivered = [r[0] for r in self.db.execute(
            "SELECT e.seq FROM events e"
            " WHERE EXISTS (SELECT 1 FROM devices d WHERE d.owner_id = e.owner_id"
            "               AND d.kind = ? AND d.id != e.from_device)"
            " AND NOT EXISTS (SELECT 1 FROM devices d WHERE d.owner_id = e.owner_id"
            "               AND d.kind = ? AND d.id != e.from_device"
            "               AND NOT EXISTS (SELECT 1 FROM deliveries x"
            "                               WHERE x.seq = e.seq AND x.device_id = d.id))",
            (DEVICE, DEVICE))]
        old_blobs = self.db.execute(
            "SELECT owner_id, id FROM blobs WHERE created_at < ?", (cutoff,)).fetchall()
        with self.db:
            self.db.execute("BEGIN")
            self._delete_events(sorted(set(expired) | set(delivered)))
            for owner_id, blob_id in old_blobs:
                self._delete_blob(owner_id, blob_id)
            pairings = self.db.execute(
                "DELETE FROM pairings WHERE expires_at < ?", (now,)).rowcount
        return {"expired": len(expired), "delivered": len(set(delivered) - set(expired)),
                "blobs": len(old_blobs), "pairings": pairings}
