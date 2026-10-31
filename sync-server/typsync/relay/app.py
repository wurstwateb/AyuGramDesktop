"""HTTP API of the relay (see docs/sync/protocol.md)."""
from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from aiohttp import web

from typsync.relay.store import CATCHER, DEVICE, Device, Store

log = logging.getLogger("typsync.relay")

MB = 1024 * 1024
HEX64 = re.compile(r"^[0-9a-f]{64}$")
BLOB_ID = re.compile(r"^[0-9a-f]{32}$")
LOCAL_ADDRESSES = {"127.0.0.1", "::1"}

Clock = Callable[[], float]


@dataclass
class Config:
    db_path: str = "relay.db"
    blob_dir: str = "blobs"
    quota_bytes: int = 1024 * MB
    retention_seconds: int = 5 * 24 * 3600
    max_blob_bytes: int = 50 * MB
    max_event_bytes: int = 1 * MB
    max_events_per_request: int = 500
    max_request_bytes: int = 2 * MB
    pairing_ttl_seconds: int = 600
    registration_secret: str = ""
    cleanup_interval_seconds: int = 3600

    @classmethod
    def from_env(cls) -> "Config":
        env = os.environ.get
        c = cls()
        c.db_path = env("TYPSYNC_DB", c.db_path)
        c.blob_dir = env("TYPSYNC_BLOB_DIR", c.blob_dir)
        c.quota_bytes = int(env("TYPSYNC_QUOTA_BYTES", c.quota_bytes))
        c.retention_seconds = int(env("TYPSYNC_RETENTION_SECONDS", c.retention_seconds))
        c.registration_secret = env("TYPSYNC_REGISTRATION_SECRET", "")
        return c


class RateLimiter:
    """Token bucket per key."""

    def __init__(self, rate: float, burst: float, clock: Clock):
        self.rate, self.burst, self.clock = rate, burst, clock
        self.buckets: dict[str, tuple[float, float]] = {}

    def allow(self, key: str) -> bool:
        now = self.clock()
        tokens, last = self.buckets.get(key, (self.burst, now))
        tokens = min(self.burst, tokens + (now - last) * self.rate)
        if tokens < 1:
            self.buckets[key] = (tokens, now)
            return False
        self.buckets[key] = (tokens - 1, now)
        if len(self.buckets) > 10_000:
            self.buckets.clear()
        return True


def _error(status: int, code: str) -> web.HTTPException:
    classes = {400: web.HTTPBadRequest, 401: web.HTTPUnauthorized, 403: web.HTTPForbidden,
               404: web.HTTPNotFound, 413: web.HTTPRequestEntityTooLarge,
               429: web.HTTPTooManyRequests}
    kwargs: dict[str, Any] = {"text": f'{{"error":"{code}"}}', "content_type": "application/json"}
    if status == 413:
        return web.HTTPRequestEntityTooLarge(max_size=0, actual_size=0, **kwargs)
    return classes[status](**kwargs)


def _b64decode(value: Any, field: str) -> bytes:
    if not isinstance(value, str):
        raise _error(400, f"bad_{field}")
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise _error(400, f"bad_{field}") from None


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _client_ip(request: web.Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    return forwarded.split(",")[0].strip() or (request.remote or "")


class Relay:
    def __init__(self, config: Config, clock: Clock = time.time):
        self.config = config
        self.clock = clock
        self.store = Store(config.db_path, config.blob_dir,
                           quota_bytes=config.quota_bytes,
                           retention_seconds=config.retention_seconds)
        self.token_limiter = RateLimiter(rate=20, burst=120, clock=clock)
        # Owner creation and pair claims: 10 attempts per 10 minutes per IP.
        self.ip_limiter = RateLimiter(rate=10 / 600, burst=10, clock=clock)

    def now(self) -> int:
        return int(self.clock())

    async def _json(self, request: web.Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except ValueError:
            raise _error(400, "bad_json") from None
        if not isinstance(body, dict):
            raise _error(400, "bad_json")
        return body

    def _limit_ip(self, request: web.Request) -> None:
        if not self.ip_limiter.allow(_client_ip(request)):
            raise _error(429, "rate_limited")

    def _auth(self, request: web.Request, *kinds: str) -> Device:
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            raise _error(401, "unauthorized")
        device = self.store.device_by_token(header[7:].strip())
        if device is None:
            raise _error(401, "unauthorized")
        if not self.token_limiter.allow(device.id):
            raise _error(429, "rate_limited")
        if kinds and device.kind not in kinds:
            raise _error(403, "forbidden")
        if device.kind == DEVICE:
            self.store.touch(device, self.now())
        return device

    # Handlers.

    async def create_owner(self, request: web.Request) -> web.Response:
        self._limit_ip(request)
        body = await self._json(request)
        secret = self.config.registration_secret
        if secret and not hmac.compare_digest(str(body.get("invite", "")), secret):
            raise _error(403, "invite_required")
        public_key = _b64decode(body.get("publicKey"), "public_key")
        if len(public_key) != 32:
            raise _error(400, "bad_public_key")
        creds = self.store.create_owner(public_key, _name(body), self.now())
        return web.json_response({"ownerId": creds.owner_id, "deviceId": creds.device_id,
                                  "token": creds.token, "kind": creds.kind})

    async def pair_start(self, request: web.Request) -> web.Response:
        device = self._auth(request, DEVICE)
        body = await self._json(request)
        lookup = body.get("lookupHash")
        kind = body.get("kind", DEVICE)
        if not isinstance(lookup, str) or not HEX64.match(lookup):
            raise _error(400, "bad_lookup_hash")
        if kind not in (DEVICE, CATCHER):
            raise _error(400, "bad_kind")
        payload = _b64decode(body.get("payload"), "payload")
        if len(payload) > 4096:
            raise _error(400, "bad_payload")
        expires = self.now() + self.config.pairing_ttl_seconds
        self.store.start_pairing(device.owner_id, lookup, kind, payload, expires)
        return web.json_response({"expiresAt": expires})

    async def pair_claim(self, request: web.Request) -> web.Response:
        self._limit_ip(request)
        body = await self._json(request)
        lookup = body.get("lookupHash")
        if not isinstance(lookup, str) or not HEX64.match(lookup):
            raise _error(400, "bad_lookup_hash")
        claimed = self.store.claim_pairing(lookup, _name(body), self.now())
        if claimed is None:
            raise _error(404, "pairing_not_found")
        creds, payload = claimed
        return web.json_response({
            "ownerId": creds.owner_id, "deviceId": creds.device_id, "token": creds.token,
            "kind": creds.kind, "payload": _b64(payload),
            "publicKey": _b64(self.store.owner_public_key(creds.owner_id)),
        })

    async def post_events(self, request: web.Request) -> web.Response:
        device = self._auth(request, DEVICE, CATCHER)
        body = await self._json(request)
        raw = body.get("events")
        if not isinstance(raw, list) or len(raw) > self.config.max_events_per_request:
            raise _error(400, "bad_events")
        items = []
        for item in raw:
            if not isinstance(item, dict):
                raise _error(400, "bad_events")
            key = item.get("key")
            if not isinstance(key, str) or not HEX64.match(key):
                raise _error(400, "bad_key")
            blob = _b64decode(item.get("blob"), "blob")
            if not blob or len(blob) > self.config.max_event_bytes:
                raise _error(400, "bad_blob")
            items.append((key, blob))
        accepted, duplicates = self.store.add_events(device, items, self.now())
        return web.json_response({"accepted": accepted, "duplicates": duplicates})

    async def get_events(self, request: web.Request) -> web.Response:
        device = self._auth(request, DEVICE)
        try:
            after = int(request.query.get("after", device.cursor))
            limit = min(max(int(request.query.get("limit", 200)), 1), 500)
        except ValueError:
            raise _error(400, "bad_query") from None
        rows, cursor, trimmed = self.store.fetch_events(device, after, limit)
        return web.json_response({
            "events": [{"seq": r["seq"], "key": r["relay_key"], "blob": _b64(r["blob"]),
                        "from": r["from_device"]} for r in rows],
            "cursor": cursor,
            "more": len(rows) == limit,
            "trimmed": trimmed,
        })

    async def put_blob(self, request: web.Request) -> web.Response:
        device = self._auth(request, DEVICE, CATCHER)
        blob_id = request.match_info["blob_id"]
        if not BLOB_ID.match(blob_id):
            raise _error(400, "bad_blob_id")
        if self.store.has_blob(device.owner_id, blob_id):
            return web.json_response({"stored": False})
        path = self.store.blob_path(device.owner_id, blob_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        size = 0
        try:
            with open(tmp, "wb") as f:
                async for chunk in request.content.iter_chunked(64 * 1024):
                    size += len(chunk)
                    if size > self.config.max_blob_bytes:
                        raise _error(413, "blob_too_large")
                    f.write(chunk)
            if size == 0:
                raise _error(400, "empty_blob")
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
        self.store.add_blob(device.owner_id, blob_id, size, self.now())
        return web.json_response({"stored": True, "size": size})

    async def get_blob(self, request: web.Request) -> web.StreamResponse:
        device = self._auth(request, DEVICE)
        blob_id = request.match_info["blob_id"]
        if not BLOB_ID.match(blob_id) or not self.store.has_blob(device.owner_id, blob_id):
            raise _error(404, "blob_not_found")
        return web.FileResponse(self.store.blob_path(device.owner_id, blob_id),
                                headers={"Content-Type": "application/octet-stream"})

    async def heartbeat(self, request: web.Request) -> web.Response:
        self._auth(request, DEVICE)
        return web.json_response({"ok": True, "now": self.now()})

    async def status(self, request: web.Request) -> web.Response:
        device = self._auth(request, DEVICE)
        owner = device.owner_id
        return web.json_response({
            "ownerId": owner,
            "deviceId": device.id,
            "devices": [{"id": d.id, "name": d.name, "kind": d.kind, "lastSeen": d.last_seen}
                        for d in self.store.devices(owner)],
            "usedBytes": self.store.used_bytes(owner),
            "quotaBytes": self.config.quota_bytes,
            "retentionSeconds": self.config.retention_seconds,
            "settings": self.store.settings(owner),
            "now": self.now(),
        })

    async def put_settings(self, request: web.Request) -> web.Response:
        device = self._auth(request, DEVICE)
        body = await self._json(request)
        changes: dict[str, Any] = {}
        if "catcherEnabled" in body:
            if not isinstance(body["catcherEnabled"], bool):
                raise _error(400, "bad_settings")
            changes["catcherEnabled"] = body["catcherEnabled"]
        if "mediaLimit" in body:
            limit = body["mediaLimit"]
            if not isinstance(limit, int) or isinstance(limit, bool) \
                    or not 0 <= limit <= self.config.max_blob_bytes:
                raise _error(400, "bad_settings")
            changes["mediaLimit"] = limit
        return web.json_response({"settings": self.store.update_settings(device.owner_id, changes)})

    async def delete_device(self, request: web.Request) -> web.Response:
        device = self._auth(request, DEVICE)
        if not self.store.remove_device(device.owner_id, request.match_info["device_id"]):
            raise _error(404, "device_not_found")
        return web.json_response({"ok": True})

    async def presence(self, request: web.Request) -> web.Response:
        if request.remote not in LOCAL_ADDRESSES or "X-Forwarded-For" in request.headers:
            raise _error(403, "local_only")
        device = self._auth(request, CATCHER)
        settings = self.store.settings(device.owner_id)
        return web.json_response({
            "allOfflineForSeconds": self.store.all_offline_for(device.owner_id, self.now()),
            "catcherEnabled": settings["catcherEnabled"],
            "mediaLimit": settings["mediaLimit"],
            "publicKey": _b64(self.store.owner_public_key(device.owner_id)),
        })

    async def cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.cleanup_interval_seconds)
            try:
                log.info("cleanup: %s", self.store.cleanup(self.now()))
            except Exception:
                log.exception("cleanup failed")


def _name(body: dict[str, Any]) -> str:
    name = body.get("name", "")
    return name[:64] if isinstance(name, str) and name else "device"


RELAY_KEY = web.AppKey("relay", Relay)
CLEANUP_KEY = web.AppKey("cleanup", asyncio.Task)


def create_app(config: Config, clock: Clock = time.time,
               *, run_cleanup: bool = True) -> web.Application:
    relay = Relay(config, clock)
    app = web.Application(client_max_size=config.max_request_bytes)
    app[RELAY_KEY] = relay
    r = app.router
    r.add_post("/v1/owners", relay.create_owner)
    r.add_post("/v1/pair/start", relay.pair_start)
    r.add_post("/v1/pair/claim", relay.pair_claim)
    r.add_post("/v1/events", relay.post_events)
    r.add_get("/v1/events", relay.get_events)
    r.add_put("/v1/blobs/{blob_id}", relay.put_blob)
    r.add_get("/v1/blobs/{blob_id}", relay.get_blob)
    r.add_post("/v1/heartbeat", relay.heartbeat)
    r.add_get("/v1/status", relay.status)
    r.add_put("/v1/settings", relay.put_settings)
    r.add_delete("/v1/devices/{device_id}", relay.delete_device)
    r.add_get("/v1/presence", relay.presence)

    async def start_cleanup(app: web.Application) -> None:
        if run_cleanup:
            app[CLEANUP_KEY] = asyncio.create_task(relay.cleanup_loop())

    async def stop(app: web.Application) -> None:
        task = app.get(CLEANUP_KEY)
        if task:
            task.cancel()
        relay.store.close()

    app.on_startup.append(start_cleanup)
    app.on_cleanup.append(stop)
    return app
