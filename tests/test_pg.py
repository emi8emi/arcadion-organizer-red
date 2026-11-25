"""bot/pg.py: the placeholder translation, rows, and the transaction behaviour the rest of the bot relies on."""
import pytest

from bot.pg import Conn, translate


def test_question_marks_become_placeholders_outside_quotes_only():
    assert translate("SELECT * FROM t WHERE a = ? AND b = ?") == "SELECT * FROM t WHERE a = %s AND b = %s"
    assert translate("SELECT '?' || ?") == "SELECT '?' || %s"
    assert translate("SELECT 'it''s ?', ?") == "SELECT 'it''s ?', %s"
    assert translate("SELECT \"odd?name\" FROM t WHERE x = ?") == "SELECT \"odd?name\" FROM t WHERE x = %s"


def test_a_literal_percent_is_kept_literal():
    assert translate("SELECT 100 % ?") == "SELECT 100 %% %s"
    assert translate("SELECT 'a%' LIKE ?") == "SELECT 'a%%' LIKE %s"


def test_rows_read_by_name_position_and_dict(db_url):
    conn = Conn(db_url)
    row = conn.execute("SELECT 5 AS a, 'x' AS b").fetchone()
    assert (row["a"], row["b"], row[0], row[1]) == (5, "x", 5, "x")
    assert dict(row) == {"a": 5, "b": "x"} and tuple(row) == (5, "x") and len(row) == 2
    assert row == (5, "x") and list(row.keys()) == ["a", "b"]
    with pytest.raises(KeyError):
        row["nope"]
    conn.close()


def test_a_with_block_commits_on_success_and_rolls_back_on_error(db_url):
    conn, other = Conn(db_url), Conn(db_url)
    with conn:
        conn.execute("INSERT INTO players (discord_id, display_name) VALUES ('1', 'A')")
    assert other.execute("SELECT COUNT(*) FROM players").fetchone()[0] == 1
    with pytest.raises(RuntimeError):
        with conn:
            conn.execute("INSERT INTO players (discord_id, display_name) VALUES ('2', 'B')")
            assert other.execute("SELECT COUNT(*) FROM players").fetchone()[0] == 1     # not visible before commit
            raise RuntimeError("boom")
    assert other.execute("SELECT COUNT(*) FROM players").fetchone()[0] == 1             # rolled back
    conn.close(), other.close()


def test_blocks_nest_and_an_inner_failure_can_be_caught_without_losing_the_outer_work(db_url):
    conn = Conn(db_url)
    with conn:
        conn.execute("INSERT INTO players (discord_id, display_name) VALUES ('1', 'A')")
        with pytest.raises(Exception):
            with conn:
                conn.execute("INSERT INTO players (discord_id, display_name) VALUES ('1', 'duplicate')")
        conn.execute("INSERT INTO players (discord_id, display_name) VALUES ('2', 'B')")
    assert [r[0] for r in conn.execute("SELECT discord_id FROM players ORDER BY 1")] == ["1", "2"]
    conn.close()


def test_a_statement_outside_a_block_commits_by_itself(db_url):
    conn, other = Conn(db_url), Conn(db_url)
    conn.execute("INSERT INTO players (discord_id, display_name) VALUES ('1', 'A')")
    assert other.execute("SELECT COUNT(*) FROM players").fetchone()[0] == 1
    assert not conn.in_transaction
    conn.close(), other.close()
