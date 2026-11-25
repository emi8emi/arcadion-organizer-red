"""Run with:  python -m bot"""
import logging
import os
import sys

from dotenv import load_dotenv

from .app import RaidBot, build_ctx
from .db import SchemaNotReady
from .config import ROOT, load_config


def main():
    load_dotenv(ROOT / ".env")
    token = os.environ.get("DISCORD_TOKEN", "")
    if not token or token.startswith("paste-"):
        sys.exit("Set DISCORD_TOKEN in .env (copy .env.example to .env first).")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    try:
        ctx = build_ctx(cfg)
    except SchemaNotReady as e:
        sys.exit(str(e))
    bot = RaidBot(ctx, os.environ.get("GUILD_ID") or None)
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
