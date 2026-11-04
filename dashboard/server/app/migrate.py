"""Schema migrations for the shared raid database (PostgreSQL).

The dashboard owns the schema: every table change is a numbered file in `migrations/` (0001_baseline.sql,
0002_whatever.sql, ...) that this module applies in order, once. The dashboard runs it on startup, and
`python -m app.migrate [database url]` runs it by hand. The bot never changes the schema; it only checks that
`schema_migrations` reaches the version it needs (bot/db.py REQUIRED_SCHEMA_VERSION).

Does not import `app.config` (which demands a full .env), because the bot's tests load this file by path to
build their databases.

Rules for migration files:
  * Plain PostgreSQL. Each file runs in its own transaction (DDL is transactional here), so a failed
    migration leaves nothing half-applied.
  * Never edit a file that was applied somewhere (its checksum is stored and a changed file is refused).
    Add the next number instead.
  * Keep changes additive while the bot may be older than the dashboard (new tables, new nullable or
    defaulted columns). To drop or rename something, first ship a bot that no longer uses it.
  * Two dashboards starting at once is safe: migrations take an advisory lock.
"""
import argparse
import hashlib
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
LOCK_KEY = 7_201_926                                 # arbitrary, only has to be the same for every migrator
_FILE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")

TABLE = """CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    checksum    TEXT NOT NULL,
    applied_utc TEXT NOT NULL
)"""


class MigrationError(RuntimeError):
    """The migrations cannot be applied (bad files, a changed applied file, or a failed statement)."""


def database_url(value=None):
    """The database both programs share: the argument, else DATABASE_URL, else an error that says what to set."""
    value = value or os.environ.get("DATABASE_URL")
    if not value:
        raise MigrationError("DATABASE_URL is not set. Put it in .env, like postgresql://user:password@host:5432/raidbot")
    return value


def load_migrations(directory=MIGRATIONS_DIR):
    """[(version, name, sql, checksum)] in order. Raises MigrationError for a gap, a duplicate or a stray file."""
    found = []
    for path in sorted(Path(directory).glob("*.sql")):
        m = _FILE.match(path.name)
        if not m:
            raise MigrationError(f"{path.name}: migration files are named 0001_short_name.sql")
        raw = path.read_bytes().replace(b"\r\n", b"\n")      # git on Windows may change line endings
        found.append((int(m.group(1)), m.group(2), raw.decode("utf-8"), hashlib.sha256(raw).hexdigest()))
    versions = [v for v, *_ in found]
    if versions != list(range(1, len(versions) + 1)):
        raise MigrationError(f"migration numbers must run 1, 2, 3 ... without gaps or repeats, found {versions}")
    return found


def current_version(conn):
    """Highest applied migration, 0 when none (or when the table does not exist yet)."""
    if conn.execute("SELECT to_regclass('schema_migrations')").fetchone()[0] is None:
        return 0
    return conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]


def _applied(conn):
    return {r[0]: (r[1], r[2]) for r in conn.execute("SELECT version, name, checksum FROM schema_migrations")}


def apply_migrations(conn, directory=MIGRATIONS_DIR):
    """Bring the database behind `conn` (a psycopg connection) up to date. Returns the versions applied.

    Refuses a database that has a migration this code does not know about (the dashboard is older than the
    database) and one whose applied files were edited afterwards.
    """
    migrations = load_migrations(directory)
    known = {v: (name, checksum) for v, name, _, checksum in migrations}
    conn.commit()                                          # start from a clean state whatever the caller did
    done = []
    try:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
            conn.execute(TABLE)
            for version, (name, checksum) in sorted(_applied(conn).items()):
                if version not in known:
                    raise MigrationError(f"the database has migration {version} ({name}) but this dashboard only "
                                         f"knows {len(known)}: update the dashboard instead of running an older one")
                if known[version][1] != checksum:
                    raise MigrationError(f"migration {version:04d}_{name}.sql was edited after it was applied; "
                                         f"put the change in a new migration instead")
        for version, name, sql, checksum in migrations:
            try:
                with conn.transaction():
                    conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
                    if version in _applied(conn):          # another process got there while we waited for the lock
                        continue
                    conn.execute(sql)
                    conn.execute("INSERT INTO schema_migrations (version, name, checksum, applied_utc) "
                                 "VALUES (%s, %s, %s, %s)",
                                 (version, name, checksum, datetime.now(timezone.utc).isoformat(timespec="seconds")))
                    done.append(version)
            except psycopg.Error as e:
                raise MigrationError(f"migration {version:04d}_{name}.sql failed and was rolled back: {e}") from e
    except psycopg.Error as e:
        raise MigrationError(f"could not use the database: {e}") from e
    return done


def upgrade(url=None, directory=MIGRATIONS_DIR):
    """Connect, apply what is missing, return the versions applied."""
    try:
        conn = psycopg.connect(database_url(url), autocommit=True, connect_timeout=10)
    except psycopg.Error as e:
        raise MigrationError(f"could not connect to the database: {e}") from e
    with conn:
        return apply_migrations(conn, directory)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m app.migrate", description=__doc__.split("\n\n")[0])
    parser.add_argument("url", nargs="?", help="database URL (default: DATABASE_URL from .env)")
    args = parser.parse_args(argv)
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    try:
        done = upgrade(args.url)
    except MigrationError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"applied {', '.join(f'{v:04d}' for v in done)}" if done else "database already up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
