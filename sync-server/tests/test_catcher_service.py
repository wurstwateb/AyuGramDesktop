import base64
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from typsync import crypto
from typsync import events as ev
from typsync.catcher.service import Catcher, CatcherConfig
from typsync.client import RelayClient, b64
from typsync.relay.app import Config, create_app

from tests.test_relay import Clock


class FakeTelegram:
    def __init__(self):
        self.handlers = {}
        self.connected = False

    async def connect(self):
        self.connected = True

    async def is_user_authorized(self):
        return True

    async def get_me(self):
        return SimpleNamespace(id=42)

    def add_event_handler(self, callback, builder):
        self.handlers[type(builder).__name__] = callback

    async def download_media(self, msg, file=bytes):
        return b"photo bytes"

    async def disconnect(self):
        self.connected = False

    async def fire(self, kind, **event):
        await self.handlers[kind](SimpleNamespace(**event))


def message(id, text, edit_date=None, **extra):
    return SimpleNamespace(
        id=id, chat_id=555, sender_id=555, message=text, out=False, entities=None,
        reply_to=None, grouped_id=None, file=extra.pop("file", None),
        date=datetime.fromtimestamp(100, timezone.utc),
        edit_date=datetime.fromtimestamp(edit_date, timezone.utc) if edit_date else None,
        **extra)


@pytest.fixture
async def setup(aiohttp_client, tmp_path):
    clock = Clock()
    server = await aiohttp_client(create_app(
        Config(db_path=str(tmp_path / "r.db"), blob_dir=str(tmp_path / "b")), clock,
        run_cleanup=False))
    base = str(server.make_url("")).rstrip("/")
    private, public = crypto.generate_keypair()
    device = RelayClient(base, session=server.session)
    owner = await device.call("POST", "/v1/owners", auth=False,
                              json={"publicKey": b64(public), "name": "pc"})
    device.token = owner["token"]

    code = crypto.new_pair_code()
    await device.call("POST", "/v1/pair/start", json={
        "lookupHash": crypto.pair_lookup_hash(code), "kind": "catcher",
        "payload": b64(crypto.pair_encrypt(code, json.dumps(
            {"ownerPublic": b64(public), "relayUrl": base}).encode()))})
    claimed = await RelayClient(base, session=server.session).claim(code, "catcher")
    config = CatcherConfig(relay_url=base, token=claimed["token"],
                           owner_public=json.loads(claimed["payload"])["ownerPublic"],
                           api_id=1, api_hash="x", session_path="unused")
    telegram = FakeTelegram()
    catcher = Catcher(config, RelayClient(base, config.token, session=server.session),
                      lambda: telegram, clock)
    return SimpleNamespace(clock=clock, device=device, private=private,
                           catcher=catcher, telegram=telegram)


async def pull(s):
    body = await s.device.call("GET", "/v1/events")
    return [ev.Event.decode(crypto.open_sealed(s.private, base64.b64decode(e["blob"])))
            for e in body["events"]]


async def test_night_shift(setup):
    s = setup
    await s.catcher.tick()
    assert not s.telegram.connected, "device was just online"

    s.clock.t += 200  # everyone went to sleep
    await s.catcher.tick()
    assert s.telegram.connected

    await s.telegram.fire("NewMessage", message=message(
        1, "ночное", photo=1, file=SimpleNamespace(size=11, mime_type="image/jpeg", name="")))
    await s.telegram.fire("MessageEdited", message=message(1, "ночное (изм.)", edit_date=300))
    await s.telegram.fire("MessageDeleted", deleted_ids=[1, 99], chat_id=None)
    await s.catcher.flush()

    # Morning: the device comes back, pulls the night's events.
    await s.device.call("POST", "/v1/heartbeat")
    got = await pull(s)
    assert [e.type for e in got] == ["message", "edit", "delete"]
    assert got[0].snapshot.text == "ночное" and got[0].snapshot.media.kind == "photo"
    blob = await s.device.call("GET", f"/v1/blobs/{got[0].snapshot.media.blob_id}")
    assert crypto.open_sealed(s.private, blob) == b"photo bytes"
    assert got[1].previous.text == "ночное" and got[1].new_edit_date == 300
    assert got[2].msg_ids == [1, 99] and got[2].snapshots[0].text == "ночное (изм.)"

    await s.catcher.tick()
    assert s.telegram.connected, "drains for two minutes"
    s.clock.t += 121
    await s.device.call("POST", "/v1/heartbeat")
    await s.catcher.tick()
    assert not s.telegram.connected and s.catcher.capture is None


async def test_catcher_disabled_from_settings(setup):
    s = setup
    await s.device.call("PUT", "/v1/settings", json={"catcherEnabled": False})
    s.clock.t += 1000
    await s.catcher.tick()
    assert not s.telegram.connected


async def test_events_wait_while_relay_is_down(setup):
    s = setup
    s.clock.t += 200
    await s.catcher.tick()
    await s.telegram.fire("NewMessage", message=message(5, "x"))
    real = s.catcher.relay.post_events

    async def down(*a, **kw):
        raise ConnectionError("relay down")
    s.catcher.relay.post_events = down
    await s.catcher.flush()
    assert len(s.catcher.outbox) == 1
    s.catcher.relay.post_events = real
    await s.catcher.flush()
    assert s.catcher.outbox == []
    assert [e.snapshot.text for e in await pull(s)] == ["x"]
