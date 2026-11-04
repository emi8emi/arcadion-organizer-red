import os
from dotenv import load_dotenv

load_dotenv()


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

# The PostgreSQL database the bot and the dashboard share; give both the same URL. The dashboard owns its
# schema and migrates it on startup: see migrate.py and migrations/.
DATABASE_URL = _required("DATABASE_URL")
