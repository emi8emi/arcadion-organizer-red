"""Schema migrations for the shared raid database.

The dashboard owns the schema: every table change is a numbered file in `migrations/` (0001_baseline.sql,
0002_whatever.sql, ...) that this module applies in order, once. The dashboard runs it on startup, and
`python -m app.migrate [db path]` runs it by hand. The bot never changes the schema; it only checks that
`schema_migrations` reaches the version it needs (bot/db.py REQUIRED_SCHEMA_VERSION).

Standard library only and no import of `app.config` (which demands a full .env), because the bot's tests
load this file by path to build their databases.

Rules for migration files:
  * Plain SQL. Each file runs in one transaction, so a failed migration leaves nothing half-applied.
  * Never edit a file that was applied somewhere (its checksum is stored and a changed file is refused).
    Add the next number instead.
  * Keep changes additive while the bot may be older than the dashboard (new tables, new nullable or
    defaulted columns). To drop or rename something, first ship a bot that no longer uses it.
  * Rebuilding a table (the SQLite "12 steps" recipe) is fine: foreign keys are off while a migration
    runs and `PRAGMA foreign_key_check` must come back clean before it commits.
"""
import argparse
import hashlib
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
REPO_ROOT = Path(__file__).resolve().parents[3]      # <repo>/dashboard/server/app/migrate.py
DEFAULT_DB = "raidbot.sqlite3"                       # same default as the bot's config.json
_FILE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")

TABLE = """CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    checksum    TEXT NOT NULL,
    applied_utc TEXT NOT NULL
)"""


class MigrationError(RuntimeError):
    """The migrations cannot be applied (bad files, a changed applied file, or a failed statement)."""


def resolve_db_path(value=None):
    """The database file both programs must share. A relative path means 'relative to the repo root',
    which is where the bot runs from, so the same DB_PATH value points at the same file for both."""
    value = value or os.environ.get("DB_PATH") or DEFAULT_DB
    if value == ":memory:" or Path(value).is_absolute():
        return value
    return str(REPO_ROOT / value)


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
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'").fetchone():
        return 0
    return conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]


def apply_migrations(conn, directory=MIGRATIONS_DIR):
    """Bring the database behind `conn` up to date. Returns the versions applied, e.g. [] or [1, 2].

    Works on any sqlite3 connection (autocommit or not). Refuses a database that has a migration this
    code does not know about (the dashboard is older than the database) and one whose applied
    files were edited afterwards.
    """
    migrations = load_migrations(directory)
    known = {v: (name, checksum) for v, name, _, checksum in migrations}
    if conn.in_transaction:
        conn.commit()
    conn.execute(TABLE)
    conn.commit()
    applied = {r[0]: r[1:] for r in conn.execute("SELECT version, name, checksum FROM schema_migrations")}
    for version, (name, checksum) in sorted(applied.items()):
        if version not in known:
            raise MigrationError(f"the database has migration {version} ({name}) but this dashboard only knows "
                                 f"{len(known)}: update the dashboard instead of running an older one")
        if known[version][1] != checksum:
            raise MigrationError(f"migration {version:04d}_{name}.sql was edited after it was applied; "
                                 f"put the change in a new migration instead")
    done = []
    fk_was_on = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys = OFF")             # cannot be changed inside a transaction
    try:
        for version, name, sql, checksum in migrations:
            if version in applied:
                continue
            stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            try:
                conn.executescript("BEGIN IMMEDIATE;\n" + sql)
                conn.execute("INSERT INTO schema_migrations (version, name, checksum, applied_utc) VALUES (?, ?, ?, ?)",
                             (version, name, checksum, stamp))
                broken = conn.execute("PRAGMA foreign_key_check").fetchall()
                if broken:
                    raise MigrationError(f"{len(broken)} foreign key violation(s), first in table {broken[0][0]}")
                conn.execute("COMMIT")
            except Exception as e:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise MigrationError(f"migration {version:04d}_{name}.sql failed and was rolled back: {e}") from e
            done.append(version)
    finally:
        conn.execute(f"PRAGMA foreign_keys = {'ON' if fk_was_on else 'OFF'}")
    return done


def upgrade(db_path, directory=MIGRATIONS_DIR):
    """Open the database file (creating it and its folder), apply what is missing, return the versions applied."""
    db_path = resolve_db_path(db_path)
    if db_path != ":memory:":
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA journal_mode = WAL")         # the bot and the dashboard write to this file at once
        return apply_migrations(conn, directory)
    finally:
        conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m app.migrate", description=__doc__.split("\n\n")[0])
    parser.add_argument("db", nargs="?", help="database file (default: DB_PATH from .env, else raidbot.sqlite3 in the repo root)")
    args = parser.parse_args(argv)
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    path = resolve_db_path(args.db)
    try:
        done = upgrade(path)
    except MigrationError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"{path}: applied {', '.join(f'{v:04d}' for v in done)}" if done else f"{path}: already up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
