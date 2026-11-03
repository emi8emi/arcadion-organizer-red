import sqlite3

from . import config
from .migrate import upgrade


def get_conn():
    """FastAPI dependency: one connection per request, always closed."""
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")          # the bot writes to this file too
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    """Bring the shared database up to the schema this dashboard ships. The bot refuses to start on an older one."""
    return upgrade(config.DB_PATH)
