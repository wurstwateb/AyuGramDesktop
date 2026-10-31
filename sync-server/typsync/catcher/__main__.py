"""python -m typsync.catcher --config /var/lib/typsync/catcher.json"""
import argparse
import asyncio
import logging

from typsync.catcher.service import Catcher, CatcherConfig, make_telegram_client
from typsync.client import RelayClient


async def run(config_path: str) -> None:
    config = CatcherConfig.load(config_path)
    async with RelayClient(config.relay_url, config.token) as relay:
        await Catcher(config, relay, lambda: make_telegram_client(config)).run()


def main() -> None:
    parser = argparse.ArgumentParser(description="TypendruckerGram Sync catcher")
    parser.add_argument("--config", default="/var/lib/typsync/catcher.json")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("telethon").setLevel(logging.WARNING)
    asyncio.run(run(args.config))


if __name__ == "__main__":
    main()
