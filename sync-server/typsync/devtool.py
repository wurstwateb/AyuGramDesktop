"""Command-line stand-in for a TypendruckerGram device, for testing the relay and
catcher before the Desktop client is ready.

    python -m typsync.devtool --state dev.json init --relay URL --invite SECRET
    python -m typsync.devtool --state dev.json pair-catcher      # prints a code
    python -m typsync.devtool --state dev.json pull              # decrypt and print
    python -m typsync.devtool --state dev.json status
    python -m typsync.devtool --state dev.json settings --catcher off --media-limit 0

The state file holds the owner private key: keep it private.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
from typing import Any

from typsync import crypto
from typsync import events as ev
from typsync.client import RelayClient, b64


def load(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save(path: str, state: dict[str, Any]) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


async def cmd_init(args: argparse.Namespace) -> None:
    private, public = crypto.generate_keypair()
    async with RelayClient(args.relay) as relay:
        body = await relay.call("POST", "/v1/owners", auth=False, json={
            "publicKey": b64(public), "name": args.name, "invite": args.invite or ""})
    save(args.state, {"relay": args.relay, "token": body["token"], "ownerId": body["ownerId"],
                      "deviceId": body["deviceId"], "private": b64(private),
                      "public": b64(public)})
    print(f"owner {body['ownerId']} created, device {body['deviceId']}")


async def cmd_pair(args: argparse.Namespace, kind: str) -> None:
    state = load(args.state)
    code = crypto.new_pair_code()
    if kind == "catcher":
        payload = {"ownerPublic": state["public"], "relayUrl": state["relay"]}
    else:
        payload = {"ownerPrivate": state["private"], "relayUrl": state["relay"]}
    async with RelayClient(state["relay"], state["token"]) as relay:
        body = await relay.call("POST", "/v1/pair/start", json={
            "lookupHash": crypto.pair_lookup_hash(code), "kind": kind,
            "payload": b64(crypto.pair_encrypt(code, json.dumps(payload).encode()))})
    print(f"pair code ({kind}, valid 10 min): {code}  expires at {body['expiresAt']}")


async def cmd_pull(args: argparse.Namespace) -> None:
    state = load(args.state)
    private = base64.b64decode(state["private"])
    async with RelayClient(state["relay"], state["token"]) as relay:
        while True:
            body = await relay.call("GET", "/v1/events")
            if body["trimmed"]:
                print("! relay trimmed data because of the quota")
            for item in body["events"]:
                try:
                    event = ev.Event.decode(crypto.open_sealed(private, base64.b64decode(item["blob"])))
                except (crypto.CryptoError, ev.EventError) as e:
                    print(f"# seq {item['seq']}: cannot read ({e})")
                    continue
                print(json.dumps({"seq": item["seq"], "from": item["from"], **event.to_json()},
                                 ensure_ascii=False))
                if args.media:
                    for snap in [event.snapshot, event.previous, *event.snapshots]:
                        if snap is not None and snap.media is not None:
                            data = await relay.call("GET", f"/v1/blobs/{snap.media.blob_id}")
                            out = os.path.join(args.media, f"{snap.media.blob_id}-{snap.media.name or snap.media.kind}")
                            with open(out, "wb") as f:
                                f.write(crypto.open_sealed(private, data))
                            print(f"#   media saved to {out}")
            if not body["more"]:
                break


async def cmd_simple(args: argparse.Namespace) -> None:
    state = load(args.state)
    async with RelayClient(state["relay"], state["token"]) as relay:
        if args.command == "status":
            body = await relay.call("GET", "/v1/status")
        elif args.command == "heartbeat":
            body = await relay.call("POST", "/v1/heartbeat")
        else:
            changes: dict[str, Any] = {}
            if args.catcher:
                changes["catcherEnabled"] = args.catcher == "on"
            if args.media_limit is not None:
                changes["mediaLimit"] = args.media_limit
            body = await relay.call("PUT", "/v1/settings", json=changes)
    print(json.dumps(body, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="TypendruckerGram Sync test device")
    parser.add_argument("--state", default="typsync-dev.json")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--relay", default="https://unionofjackbox.tokenized.name/typsync")
    init.add_argument("--invite")
    init.add_argument("--name", default="devtool")
    sub.add_parser("pair-catcher")
    sub.add_parser("pair-device")
    pull = sub.add_parser("pull")
    pull.add_argument("--media", help="directory to save decrypted media")
    sub.add_parser("status")
    sub.add_parser("heartbeat")
    settings = sub.add_parser("settings")
    settings.add_argument("--catcher", choices=["on", "off"])
    settings.add_argument("--media-limit", type=int)
    args = parser.parse_args()

    if args.command == "init":
        coro = cmd_init(args)
    elif args.command == "pair-catcher":
        coro = cmd_pair(args, "catcher")
    elif args.command == "pair-device":
        coro = cmd_pair(args, "device")
    elif args.command == "pull":
        coro = cmd_pull(args)
    else:
        coro = cmd_simple(args)
    asyncio.run(coro)


if __name__ == "__main__":
    main()
