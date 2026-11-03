import os
from dotenv import load_dotenv

load_dotenv()

from .migrate import resolve_db_path  # noqa: E402  (after load_dotenv, so DB_PATH from .env is seen)


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing setting {name} in your .env file")
    return value


DISCORD_CLIENT_ID = _required("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = _required("DISCORD_CLIENT_SECRET")
DISCORD_BOT_TOKEN = _required("DISCORD_BOT_TOKEN")
DISCORD_GUILD_ID = _required("DISCORD_GUILD_ID")
ORGANIZER_ROLE_ID = _required("ORGANIZER_ROLE_ID")
SESSION_SECRET = _required("SESSION_SECRET")

BASE_URL = os.environ.get("BASE_URL", "http://localhost:5173").rstrip("/")
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "false").lower() == "true"

# The one database file the bot and the dashboard share. A relative path is relative to the repo root,
# which is where the bot runs from, so leaving DB_PATH out (or giving the same value to both) means the
# same file. The dashboard owns its schema: see migrate.py and migrations/.
DB_PATH = resolve_db_path(os.environ.get("DB_PATH"))
