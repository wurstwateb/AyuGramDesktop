import base64
import os

import pytest

from typsync import crypto
from typsync.relay.app import RELAY_KEY, Config, create_app


class Clock:
    def __init__(self):
        self.t = 1_761_900_000.0

    def __call__(self):
        return self.t


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def key(n: int) -> str:
    return f"{n:064x}"


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def make_client(aiohttp_client, tmp_path, clock):
    async def make(**overrides):
        config = Config(db_path=str(tmp_path / "relay.db"), blob_dir=str(tmp_path / "blobs"),
                        **overrides)
        return await aiohttp_client(create_app(config, clock, run_cleanup=False))
    return make


@pytest.fixture
async def client(make_client):
    return await make_client()


def auth(token):
    return {"Authorization": f"Bearer {token}"}


async def create_owner(client, name="pc"):
    _, pub = crypto.generate_keypair()
    resp = await client.post("/v1/owners", json={"publicKey": b64(pub), "name": name})
    assert resp.status == 200
    return await resp.json()


async def pair(client, owner_token, kind="device", name="phone"):
    code = crypto.new_pair_code()
    resp = await client.post("/v1/pair/start", headers=auth(owner_token), json={
        "lookupHash": crypto.pair_lookup_hash(code), "kind": kind,
        "payload": b64(crypto.pair_encrypt(code, b"secret"))})
    assert resp.status == 200
    resp = await client.post("/v1/pair/claim", json={
        "lookupHash": crypto.pair_lookup_hash(code), "name": name})
    assert resp.status == 200
    body = await resp.json()
    assert crypto.pair_decrypt(code, base64.b64decode(body["payload"])) == b"secret"
    return body


async def post(client, token, *items):
    resp = await client.post("/v1/events", headers=auth(token), json={
        "events": [{"key": k, "blob": b64(blob)} for k, blob in items]})
    return resp.status, await resp.json()


async def fetch(client, token, after=None):
    url = "/v1/events" if after is None else f"/v1/events?after={after}"
    resp = await client.get(url, headers=auth(token))
    assert resp.status == 200
    return await resp.json()


def relay(client):
    return client.server.app[RELAY_KEY]


async def test_create_owner_validates_key(client):
    resp = await client.post("/v1/owners", json={"publicKey": b64(b"short")})
    assert resp.status == 400
    resp = await client.post("/v1/owners", json={"publicKey": "@@"})
    assert resp.status == 400


async def test_registration_secret(make_client):
    client = await make_client(registration_secret="letmein")
    _, pub = crypto.generate_keypair()
    resp = await client.post("/v1/owners", json={"publicKey": b64(pub)})
    assert resp.status == 403
    resp = await client.post("/v1/owners", json={"publicKey": b64(pub), "invite": "letmein"})
    assert resp.status == 200


async def test_unauthorized(client):
    assert (await client.get("/v1/status")).status == 401
    assert (await client.get("/v1/status", headers=auth("nope"))).status == 401


async def test_events_flow_between_devices(client):
    pc = await create_owner(client)
    phone = await pair(client, pc["token"])
    assert phone["ownerId"] == pc["ownerId"]

    status, body = await post(client, pc["token"], (key(1), b"e1"), (key(2), b"e2"))
    assert status == 200 and body == {"accepted": 2, "duplicates": 0}
    status, body = await post(client, phone["token"], (key(1), b"same key"), (key(3), b"e3"))
    assert body == {"accepted": 1, "duplicates": 1}

    got = await fetch(client, phone["token"])
    assert [base64.b64decode(e["blob"]) for e in got["events"]] == [b"e1", b"e2"]
    assert all(e["from"] == pc["deviceId"] for e in got["events"])
    # Cursor is remembered by the server.
    assert (await fetch(client, phone["token"]))["events"] == []

    got = await fetch(client, pc["token"])
    assert [base64.b64decode(e["blob"]) for e in got["events"]] == [b"e3"]


async def test_event_validation(client):
    pc = await create_owner(client)
    assert (await post(client, pc["token"], ("zz", b"x")))[0] == 400
    assert (await post(client, pc["token"], (key(1), b"")))[0] == 400
    too_many = [(key(i), b"x") for i in range(501)]
    assert (await post(client, pc["token"], *too_many))[0] == 400


async def test_catcher_permissions_and_presence(client, clock):
    pc = await create_owner(client)
    catcher = await pair(client, pc["token"], kind="catcher", name="catcher")
    assert catcher["kind"] == "catcher"

    status, _ = await post(client, catcher["token"], (key(9), b"night"))
    assert status == 200
    assert (await client.get("/v1/events", headers=auth(catcher["token"]))).status == 403
    assert (await client.get("/v1/presence", headers=auth(pc["token"]))).status == 403
    proxied = {**auth(catcher["token"]), "X-Forwarded-For": "1.2.3.4"}
    assert (await client.get("/v1/presence", headers=proxied)).status == 403

    clock.t += 500
    resp = await client.get("/v1/presence", headers=auth(catcher["token"]))
    body = await resp.json()
    assert body["allOfflineForSeconds"] == 500
    assert body["catcherEnabled"] is True and body["mediaLimit"] == 20 * 1024 * 1024
    assert base64.b64decode(body["publicKey"])

    await client.post("/v1/heartbeat", headers=auth(pc["token"]))
    body = await (await client.get("/v1/presence", headers=auth(catcher["token"]))).json()
    assert body["allOfflineForSeconds"] == 0

    got = await fetch(client, pc["token"])
    assert [base64.b64decode(e["blob"]) for e in got["events"]] == [b"night"]


async def test_pairing_expiry_and_single_use(client, clock):
    pc = await create_owner(client)
    code = crypto.new_pair_code()
    lookup = crypto.pair_lookup_hash(code)
    await client.post("/v1/pair/start", headers=auth(pc["token"]),
                      json={"lookupHash": lookup, "payload": b64(b"x")})
    clock.t += 601
    resp = await client.post("/v1/pair/claim", json={"lookupHash": lookup})
    assert resp.status == 404

    await client.post("/v1/pair/start", headers=auth(pc["token"]),
                      json={"lookupHash": lookup, "payload": b64(b"x")})
    assert (await client.post("/v1/pair/claim", json={"lookupHash": lookup})).status == 200
    assert (await client.post("/v1/pair/claim", json={"lookupHash": lookup})).status == 404


async def test_claim_rate_limit(client):
    statuses = []
    for i in range(12):
        resp = await client.post("/v1/pair/claim", json={"lookupHash": key(i)})
        statuses.append(resp.status)
    assert statuses[:10] == [404] * 10
    assert statuses[10:] == [429, 429]


async def test_blobs(make_client):
    client = await make_client(max_blob_bytes=1000)
    pc = await create_owner(client)
    phone = await pair(client, pc["token"])
    catcher = await pair(client, pc["token"], kind="catcher")
    blob_id = "ab" * 16

    resp = await client.put(f"/v1/blobs/{blob_id}", headers=auth(catcher["token"]), data=b"z" * 900)
    assert resp.status == 200 and (await resp.json())["size"] == 900
    resp = await client.put(f"/v1/blobs/{blob_id}", headers=auth(pc["token"]), data=b"other")
    assert (await resp.json())["stored"] is False

    resp = await client.get(f"/v1/blobs/{blob_id}", headers=auth(phone["token"]))
    assert resp.status == 200 and await resp.read() == b"z" * 900
    assert (await client.get(f"/v1/blobs/{blob_id}", headers=auth(catcher["token"]))).status == 403
    assert (await client.get(f"/v1/blobs/{'cd' * 16}", headers=auth(phone["token"]))).status == 404
    assert (await client.put("/v1/blobs/bad", headers=auth(pc["token"]), data=b"x")).status == 400

    resp = await client.put(f"/v1/blobs/{'ef' * 16}", headers=auth(pc["token"]), data=b"y" * 1001)
    assert resp.status == 413
    leftovers = [p for p in os.listdir(relay(client).store.blob_dir / pc["ownerId"])
                 if not p == blob_id]
    assert leftovers == []


async def test_quota_drops_oldest_media_then_events(make_client):
    client = await make_client(quota_bytes=1000)
    pc = await create_owner(client)
    phone = await pair(client, pc["token"])
    await client.put(f"/v1/blobs/{'01' * 16}", headers=auth(pc["token"]), data=b"m" * 600)
    await post(client, pc["token"], (key(1), b"a" * 300))
    await post(client, pc["token"], (key(2), b"b" * 300))  # 1200 > 1000: media goes
    store = relay(client).store
    assert not store.has_blob(pc["ownerId"], "01" * 16)
    await post(client, pc["token"], (key(3), b"c" * 300), (key(4), b"d" * 300))  # oldest event goes

    got = await fetch(client, phone["token"])
    assert got["trimmed"] is True
    assert [base64.b64decode(e["blob"])[:1] for e in got["events"]] == [b"b", b"c", b"d"]
    assert (await fetch(client, phone["token"]))["trimmed"] is False


async def test_cleanup_delivered_and_expired(client, clock):
    pc = await create_owner(client)
    store = relay(client).store
    await post(client, pc["token"], (key(1), b"solo"))
    store.cleanup(int(clock.t))
    assert store.used_bytes(pc["ownerId"]) == 4, "kept until another device can receive it"

    phone = await pair(client, pc["token"])
    await fetch(client, phone["token"])
    stats = store.cleanup(int(clock.t))
    assert stats["delivered"] == 1 and store.used_bytes(pc["ownerId"]) == 0

    await post(client, pc["token"], (key(2), b"old"))
    await client.put(f"/v1/blobs/{'02' * 16}", headers=auth(pc["token"]), data=b"media")
    clock.t += 5 * 24 * 3600 + 1
    stats = store.cleanup(int(clock.t))
    assert stats["expired"] == 1 and stats["blobs"] == 1
    assert store.used_bytes(pc["ownerId"]) == 0
    assert not (store.blob_dir / pc["ownerId"] / ("02" * 16)).exists()


async def test_settings_status_and_device_removal(client):
    pc = await create_owner(client)
    phone = await pair(client, pc["token"])
    resp = await client.put("/v1/settings", headers=auth(pc["token"]),
                            json={"catcherEnabled": False, "mediaLimit": 5_000_000})
    assert (await resp.json())["settings"] == {"catcherEnabled": False, "mediaLimit": 5_000_000}
    for bad in ({"mediaLimit": -1}, {"mediaLimit": 10 ** 12}, {"catcherEnabled": "yes"},
                {"mediaLimit": True}):
        resp = await client.put("/v1/settings", headers=auth(pc["token"]), json=bad)
        assert resp.status == 400

    status = await (await client.get("/v1/status", headers=auth(pc["token"]))).json()
    assert [d["name"] for d in status["devices"]] == ["pc", "phone"]
    assert status["quotaBytes"] == 1024 * 1024 * 1024

    resp = await client.delete(f"/v1/devices/{phone['deviceId']}", headers=auth(pc["token"]))
    assert resp.status == 200
    assert (await client.get("/v1/status", headers=auth(phone["token"]))).status == 401
    other = await create_owner(client, "stranger")
    resp = await client.delete(f"/v1/devices/{pc['deviceId']}", headers=auth(other["token"]))
    assert resp.status == 404


async def test_ten_thousand_events(client):
    pc = await create_owner(client)
    phone = await pair(client, pc["token"])
    for batch in range(20):
        status, body = await post(client, pc["token"],
                                  *[(key(batch * 500 + i), b"x" * 200) for i in range(500)])
        assert body["accepted"] == 500
    received = 0
    while True:
        got = await fetch(client, phone["token"])
        received += len(got["events"])
        if not got["more"]:
            break
    assert received == 10_000
    assert relay(client).store.cleanup(0)["delivered"] == 10_000
