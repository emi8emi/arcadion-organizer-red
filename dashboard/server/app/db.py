from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import config
from .migrate import upgrade

# Opened in main.py's lifespan, after the migrations ran. Rows come back as dicts.
pool = ConnectionPool(
    config.DATABASE_URL,
    min_size=1,
    max_size=5,
    open=False,
    kwargs={"row_factory": dict_row, "connect_timeout": 10},
    check=ConnectionPool.check_connection,        # a connection the server dropped is replaced, not handed out
)


def get_conn():
    """FastAPI dependency: a pooled connection for one request. Commits when the request succeeds."""
    with pool.connection() as conn:
        yield conn


def init_db():
    """Bring the shared database up to the schema this dashboard ships. The bot refuses to start on an older one."""
    return upgrade(config.DATABASE_URL)


def close_db():
    pool.close()
