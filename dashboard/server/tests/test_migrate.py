"""The migration runner: the dashboard owns the schema, so this is what keeps the shared database honest.

Needs a PostgreSQL server: `docker compose up -d` in the repo root starts one that matches the default URL,
or set TEST_DATABASE_URL. Every test works in its own throwaway schema.
"""
import itertools
import os
import sys
import threading
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import migrate  # noqa: E402
from app.migrate import MigrationError  # noqa: E402

REAL = migrate.MIGRATIONS_DIR
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/raidtest")
_ids = itertools.count(1)


@pytest.fixture
def url():
    """A connection string whose search_path is a new empty schema of the test database."""
    try:
        admin = psycopg.connect(TEST_DATABASE_URL, autocommit=True, connect_timeout=5)
    except psycopg.Error as e:
        pytest.exit(f"Cannot reach the test database ({e}). Start one with `docker compose up -d` "
                    f"or set TEST_DATABASE_URL.", returncode=2)
    name = f"m{os.getpid()}_{next(_ids)}"
    admin.execute(f'CREATE SCHEMA "{name}"')
    yield make_conninfo(TEST_DATABASE_URL, options=f"-c search_path={name}")
    admin.execute(f'DROP SCHEMA "{name}" CASCADE')
    admin.close()


@pytest.fixture
def conn(url):
    with psycopg.connect(url, autocommit=True) as c:
        yield c


def folder(tmp_path, **files):
    d = tmp_path / "migrations"
    d.mkdir(parents=True, exist_ok=True)
    for name, sql in files.items():
        (d / f"{name}.sql").write_text(sql)
    return d


def tables(conn):
    return {r[0] for r in conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()")}


def test_a_fresh_database_gets_every_migration_and_records_them(url, conn):
    n = len(migrate.load_migrations())
    assert migrate.upgrade(url) == list(range(1, n + 1))
    assert {"runs", "slots", "parties", "players", "characters", "prog_scores", "guild_settings",
            "schema_migrations"} <= tables(conn)
    assert migrate.current_version(conn) == n
    assert conn.execute("SELECT name FROM schema_migrations WHERE version = 1").fetchone()[0] == "baseline"


def test_running_again_changes_nothing(url):
    migrate.upgrade(url)
    assert migrate.upgrade(url) == []


def test_run_names_are_unique_per_server_ignoring_case(url, conn):
    migrate.upgrade(url)
    conn.execute("INSERT INTO fights (name) VALUES ('f')")
    insert = ("INSERT INTO runs (guild_id, name, fight_id, start_date, end_date, signup_deadline_utc, created_utc) "
              "VALUES (%s, %s, 1, 'a', 'b', 'c', 'd')")
    conn.execute(insert, ("1", "UWU week 1"))
    conn.execute(insert, ("2", "uwu week 1"))                        # another server: fine
    with pytest.raises(psycopg.errors.UniqueViolation) as caught:
        conn.execute(insert, ("1", "uwu WEEK 1"))
    assert caught.value.diag.constraint_name == "runs_guild_name_key"   # the bot tells duplicate runs apart by this


def test_an_applied_migration_that_was_edited_is_refused(tmp_path, conn):
    d = folder(tmp_path, **{"0001_a": "CREATE TABLE a (x INTEGER);"})
    migrate.apply_migrations(conn, d)
    (d / "0001_a.sql").write_text("CREATE TABLE a (x INTEGER, y INTEGER);")
    with pytest.raises(MigrationError, match="edited after it was applied"):
        migrate.apply_migrations(conn, d)


def test_line_endings_do_not_count_as_an_edit(tmp_path, conn):
    d = folder(tmp_path, **{"0001_a": "CREATE TABLE a (\n x INTEGER\n);\n"})
    migrate.apply_migrations(conn, d)
    (d / "0001_a.sql").write_bytes(b"CREATE TABLE a (\r\n x INTEGER\r\n);\r\n")     # git on Windows
    assert migrate.apply_migrations(conn, d) == []


def test_a_database_newer_than_the_dashboard_is_refused(tmp_path, conn):
    d = folder(tmp_path, **{"0001_a": "CREATE TABLE a (x INTEGER);", "0002_b": "CREATE TABLE b (x INTEGER);"})
    migrate.apply_migrations(conn, d)
    (d / "0002_b.sql").unlink()
    with pytest.raises(MigrationError, match="only knows 1"):
        migrate.apply_migrations(conn, d)


def test_a_failing_migration_is_rolled_back_whole_and_later_ones_do_not_run(tmp_path, conn):
    d = folder(tmp_path, **{"0001_a": "CREATE TABLE a (x INTEGER);",
                            "0002_bad": "CREATE TABLE half (x INTEGER); INSERT INTO nope VALUES (1);",
                            "0003_c": "CREATE TABLE c (x INTEGER);"})
    with pytest.raises(MigrationError, match="0002_bad.sql failed and was rolled back"):
        migrate.apply_migrations(conn, d)
    assert migrate.current_version(conn) == 1                  # 0001 stays: each file is its own transaction
    assert "half" not in tables(conn) and "c" not in tables(conn) and "a" in tables(conn)


def test_a_migration_that_breaks_a_foreign_key_is_rolled_back(tmp_path, conn):
    d = folder(tmp_path, **{"0001_a": "CREATE TABLE a (id INTEGER PRIMARY KEY);",
                            "0002_orphan": "CREATE TABLE b (a_id INTEGER REFERENCES a(id)); INSERT INTO b VALUES (5);"})
    with pytest.raises(MigrationError, match="violates foreign key"):
        migrate.apply_migrations(conn, d)
    assert "b" not in tables(conn) and migrate.current_version(conn) == 1


def test_two_migrators_starting_at_once_apply_each_migration_once(url, conn):
    n = len(migrate.load_migrations())
    results, errors = [], []

    def run():
        try:
            results.append(migrate.upgrade(url))
        except Exception as e:                                   # noqa: BLE001
            errors.append(e)
    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert sorted(v for r in results for v in r) == list(range(1, n + 1))     # nobody applied one twice
    assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == n


def test_numbering_gaps_and_stray_names_are_refused(tmp_path):
    with pytest.raises(MigrationError, match="without gaps"):
        migrate.load_migrations(folder(tmp_path, **{"0001_a": "SELECT 1;", "0003_c": "SELECT 1;"}))
    with pytest.raises(MigrationError, match="named 0001_short_name.sql"):
        migrate.load_migrations(folder(tmp_path / "x", **{"fix": "SELECT 1;"}))


def test_the_database_url_must_be_given(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(MigrationError, match="DATABASE_URL is not set"):
        migrate.database_url()
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    assert migrate.database_url() == "postgresql://x/y" and migrate.database_url("postgresql://z/w") == "postgresql://z/w"


def test_an_unreachable_database_is_a_readable_error():
    with pytest.raises(MigrationError, match="could not connect"):
        migrate.upgrade("postgresql://nobody@127.0.0.1:1/none?connect_timeout=2")


def test_the_command_line_reports_what_it_did(url, capsys):
    assert migrate.main([url]) == 0
    assert "applied 0001" in capsys.readouterr().out
    assert migrate.main([url]) == 0
    assert "already up to date" in capsys.readouterr().out
