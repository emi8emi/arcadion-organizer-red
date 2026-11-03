"""The migration runner: the dashboard owns the schema, so this is what keeps the shared database honest."""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import migrate  # noqa: E402
from app.migrate import MigrationError  # noqa: E402

REAL = migrate.MIGRATIONS_DIR


def folder(tmp_path, **files):
    d = tmp_path / "migrations"
    d.mkdir(parents=True, exist_ok=True)
    for name, sql in files.items():
        (d / f"{name}.sql").write_text(sql)
    return d


def tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def test_a_fresh_database_gets_every_migration_and_records_them(tmp_path):
    path = str(tmp_path / "raid.sqlite3")
    n = len(migrate.load_migrations())
    assert migrate.upgrade(path) == list(range(1, n + 1))
    conn = sqlite3.connect(path)
    assert {"runs", "slots", "parties", "players", "characters", "prog_scores", "guild_settings",
            "schema_migrations"} <= tables(conn)
    assert migrate.current_version(conn) == n
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"       # bot and dashboard share the file
    assert conn.execute("SELECT name FROM schema_migrations WHERE version = 1").fetchone()[0] == "baseline"


def test_running_again_changes_nothing(tmp_path):
    path = str(tmp_path / "raid.sqlite3")
    migrate.upgrade(path)
    assert migrate.upgrade(path) == []


def test_a_database_the_bot_already_built_is_stamped_and_keeps_its_rows(tmp_path):
    path = str(tmp_path / "raid.sqlite3")
    conn = sqlite3.connect(path)
    conn.executescript((REAL / "0001_baseline.sql").read_text())              # what the bot used to do on every start
    conn.execute("INSERT INTO players (discord_id, display_name) VALUES ('1', 'Ann')")
    conn.commit()
    conn.close()
    assert migrate.upgrade(path)[0] == 1
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT display_name FROM players").fetchall() == [("Ann",)]


def test_an_applied_migration_that_was_edited_is_refused(tmp_path):
    d = folder(tmp_path, **{"0001_a": "CREATE TABLE a (x INTEGER);"})
    conn = sqlite3.connect(":memory:")
    migrate.apply_migrations(conn, d)
    (d / "0001_a.sql").write_text("CREATE TABLE a (x INTEGER, y INTEGER);")
    with pytest.raises(MigrationError, match="edited after it was applied"):
        migrate.apply_migrations(conn, d)


def test_line_endings_do_not_count_as_an_edit(tmp_path):
    d = folder(tmp_path, **{"0001_a": "CREATE TABLE a (\n x INTEGER\n);\n"})
    conn = sqlite3.connect(":memory:")
    migrate.apply_migrations(conn, d)
    (d / "0001_a.sql").write_bytes(b"CREATE TABLE a (\r\n x INTEGER\r\n);\r\n")     # git on Windows
    assert migrate.apply_migrations(conn, d) == []


def test_a_database_newer_than_the_dashboard_is_refused(tmp_path):
    d = folder(tmp_path, **{"0001_a": "CREATE TABLE a (x INTEGER);", "0002_b": "CREATE TABLE b (x INTEGER);"})
    conn = sqlite3.connect(":memory:")
    migrate.apply_migrations(conn, d)
    (d / "0002_b.sql").unlink()
    with pytest.raises(MigrationError, match="only knows 1"):
        migrate.apply_migrations(conn, d)


def test_a_failing_migration_is_rolled_back_whole_and_later_ones_do_not_run(tmp_path):
    d = folder(tmp_path, **{"0001_a": "CREATE TABLE a (x INTEGER);",
                            "0002_bad": "CREATE TABLE half (x INTEGER); INSERT INTO nope VALUES (1);",
                            "0003_c": "CREATE TABLE c (x INTEGER);"})
    conn = sqlite3.connect(":memory:")
    with pytest.raises(MigrationError, match="0002_bad.sql failed and was rolled back"):
        migrate.apply_migrations(conn, d)
    assert migrate.current_version(conn) == 1
    assert "half" not in tables(conn) and "c" not in tables(conn) and "a" in tables(conn)


def test_a_migration_that_leaves_broken_foreign_keys_is_rolled_back(tmp_path):
    d = folder(tmp_path, **{"0001_a": "CREATE TABLE a (id INTEGER PRIMARY KEY);",
                            "0002_orphan": "CREATE TABLE b (a_id INTEGER REFERENCES a(id)); INSERT INTO b VALUES (5);"})
    conn = sqlite3.connect(":memory:")
    with pytest.raises(MigrationError, match="foreign key violation"):
        migrate.apply_migrations(conn, d)
    assert "b" not in tables(conn) and migrate.current_version(conn) == 1


def test_foreign_keys_are_back_on_afterwards(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    migrate.apply_migrations(conn)
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_numbering_gaps_and_stray_names_are_refused(tmp_path):
    with pytest.raises(MigrationError, match="without gaps"):
        migrate.load_migrations(folder(tmp_path, **{"0001_a": "SELECT 1;", "0003_c": "SELECT 1;"}))
    with pytest.raises(MigrationError, match="named 0001_short_name.sql"):
        migrate.load_migrations(folder(tmp_path / "x", **{"fix": "SELECT 1;"}))


def test_a_relative_db_path_means_relative_to_the_repo_root_like_the_bot():
    assert migrate.resolve_db_path("raidbot.sqlite3") == str(migrate.REPO_ROOT / "raidbot.sqlite3")
    assert migrate.resolve_db_path(":memory:") == ":memory:"
    assert (migrate.REPO_ROOT / "bot").is_dir()                      # REPO_ROOT really is the folder with the bot in it


def test_the_command_line_reports_what_it_did(tmp_path, capsys):
    path = str(tmp_path / "raid.sqlite3")
    assert migrate.main([path]) == 0
    assert "applied 0001" in capsys.readouterr().out
    assert migrate.main([path]) == 0
    assert "already up to date" in capsys.readouterr().out
