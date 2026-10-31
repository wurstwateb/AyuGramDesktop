"""One-time interactive setup of the catcher. Run on the server as the typsync user:

    sudo -u typsync /opt/typsync/venv/bin/python -m typsync.catcher.setup

1. Pairs with the owner using a code generated on a device ("Connect server
   userbot" in TypendruckerGram, or `typsync.devtool pair-catcher`).
2. Logs into Telegram (phone, code, 2FA password) and stores the session.
"""
import argparse
import asyncio
import base64
import getpass
import json
import os

from typsync.catcher.service import CatcherConfig, make_telegram_client
from typsync.client import RelayClient


async def setup(args: argparse.Namespace) -> None:
    print("API ID и API hash берутся на https://my.telegram.org → API development tools.")
    api_id = int(args.api_id or input("API ID: ").strip())
    api_hash = args.api_hash or getpass.getpass("API hash (не отображается): ").strip()
    code = input("Код сопряжения с устройства (XXXX-XXXX-XXXX): ").strip()

    async with RelayClient(args.relay) as relay:
        claimed = await relay.claim(code, "catcher")
    if claimed["kind"] != "catcher":
        raise SystemExit("Этот код создан для обычного устройства, нужен код для юзербота.")
    payload = json.loads(claimed["payload"])
    if base64.b64decode(payload["ownerPublic"]) != base64.b64decode(claimed["publicKey"]):
        raise SystemExit("Ключ из кода не совпадает с ключом на сервере, сопряжение отменено.")

    config = CatcherConfig(relay_url=args.relay, token=claimed["token"],
                           owner_public=payload["ownerPublic"], api_id=api_id,
                           api_hash=api_hash, session_path=args.session)
    config.save(args.config)
    print(f"Сопряжение выполнено, настройки сохранены в {args.config}.")

    print("Теперь вход в Telegram для юзербота.")
    client = make_telegram_client(config)
    await client.start()
    me = await client.get_me()
    await client.disconnect()
    os.chmod(args.session + ".session", 0o600)
    print(f"Готово: юзербот вошёл как {me.first_name} (id {me.id}).")
    print("Запустите сервис: sudo systemctl enable --now typsync-catcher")


def main() -> None:
    parser = argparse.ArgumentParser(description="Set up the TypendruckerGram Sync catcher")
    parser.add_argument("--config", default="/var/lib/typsync/catcher.json")
    parser.add_argument("--session", default="/var/lib/typsync/catcher")
    parser.add_argument("--relay", default="http://127.0.0.1:8090")
    parser.add_argument("--api-id")
    parser.add_argument("--api-hash")
    asyncio.run(setup(parser.parse_args()))


if __name__ == "__main__":
    main()
