import json

from typsync import crypto
from typsync import events as ev

from tests.vectors import VECTORS_PATH, build


def test_committed_vectors_are_up_to_date():
    committed = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))
    assert committed == build(), "run: python -m tests.vectors"


def test_vectors_decrypt():
    v = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))
    priv = bytes.fromhex(v["ownerPrivateHex"])
    for item in v["seal"]:
        plaintext = crypto.open_sealed(priv, bytes.fromhex(item["blobHex"]))
        assert plaintext.hex() == item["plaintextHex"]
        event = ev.Event.decode(plaintext)
        assert event.to_json() == item["event"]
        assert ev.dedup_keys(event) == item["dedupKeys"]
    pair = v["pair"]
    assert crypto.pair_decrypt(
        pair["codeAsTyped"], bytes.fromhex(pair["encryptedHex"])).hex() == pair["payloadHex"]
