"""Minimal async client for the relay API, used by the catcher and the devtool."""
from __future__ import annotations

import base64
import secrets
from typing import Any

import aiohttp

from typsync import crypto
from typsync import events as ev


class RelayError(RuntimeError):
    def __init__(self, status: int, body: Any):
        super().__init__(f"relay returned {status}: {body}")
        self.status = status
        self.body = body


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


class RelayClient:
    def __init__(self, base_url: str, token: str = "",
                 session: aiohttp.ClientSession | None = None):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._session = session
        self._own_session = session is None

    async def __aenter__(self) -> "RelayClient":
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120))
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._own_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def call(self, method: str, path: str, *, auth: bool = True, **kwargs: Any) -> Any:
        assert self._session is not None, "use `async with RelayClient(...)`"
        headers = {"Authorization": f"Bearer {self.token}"} if auth and self.token else {}
        async with self._session.request(method, self.base_url + path,
                                         headers=headers, **kwargs) as resp:
            if resp.content_type == "application/json":
                body = await resp.json()
            else:
                body = await resp.read()
            if resp.status >= 400:
                raise RelayError(resp.status, body)
            return body

    # Typed helpers.

    async def presence(self) -> dict[str, Any]:
        return await self.call("GET", "/v1/presence")

    async def post_events(self, owner_public: bytes, events: list[ev.Event]) -> dict[str, Any]:
        items = [{"key": ev.relay_key(e), "blob": b64(crypto.seal(owner_public, e.encode()))}
                 for e in events]
        return await self.call("POST", "/v1/events", json={"events": items})

    async def put_blob(self, owner_public: bytes, data: bytes) -> str:
        blob_id = secrets.token_hex(16)
        await self.call("PUT", f"/v1/blobs/{blob_id}", data=crypto.seal(owner_public, data))
        return blob_id

    async def claim(self, code: str, name: str) -> dict[str, Any]:
        body = await self.call("POST", "/v1/pair/claim", auth=False, json={
            "lookupHash": crypto.pair_lookup_hash(code), "name": name})
        body["payload"] = crypto.pair_decrypt(code, base64.b64decode(body["payload"]))
        return body
