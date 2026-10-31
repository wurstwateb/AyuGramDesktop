"""python -m typsync.relay --host 127.0.0.1 --port 8090"""
import argparse
import logging

from aiohttp import web

from typsync.relay.app import Config, create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="TypendruckerGram Sync relay")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    web.run_app(create_app(Config.from_env()), host=args.host, port=args.port,
                access_log=None)


if __name__ == "__main__":
    main()
