"""Run with:  python -m bot"""
import logging
import os
import sys

from dotenv import load_dotenv

from .app import RaidBot, build_ctx
from .config import ROOT, load_config


def main():
    load_dotenv(ROOT / ".env")
    token = os.environ.get("DISCORD_TOKEN", "")
    if not token or token.startswith("paste-"):
        sys.exit("Set DISCORD_TOKEN in .env (copy .env.example to .env first).")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    bot = RaidBot(build_ctx(cfg), os.environ.get("GUILD_ID") or None)
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
