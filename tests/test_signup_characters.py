"""Characters on signups, against the real db.py and the patched schema (stand-in config only)."""
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot import characters as ch  # noqa: E402
from bot.db import DB  # noqa: E402

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
CFG = {"default_max_per_day": 2, "default_max_per_run": 4}
D1, D2 = "2026-11-02", "2026-11-03"


def make_db(path=":memory:"):
    db = DB(path)
    with db.conn:
        db.conn.execute("""INSERT INTO runs (id, guild_id, name, fight_id, start_date, end_date, signup_deadline_utc,
                           status, created_utc) VALUES (1, 'g', 'r', 1, ?, ?, '2026-11-01T00:00:00+00:00', 'open', 'x')""",
                        (D1, D2))
        for i, day in enumerate((D1, D2), start=1):
            db.conn.execute("INSERT INTO slots (id, run_id, slot_key, start_utc) VALUES (?, 1, ?, ?)",
                            (i, f"{day}T17:00", f"{day}T20:00:00+00:00"))
            db.conn.execute("INSERT INTO run_days (run_id, day, cutoff_utc) VALUES (1, ?, ?)",
                            (day, "2026-11-01T00:00:00+00:00"))
    return db


def verified(db, uid, name, lid):
    db.upsert_player(uid, f"P{uid}", now=NOW)
    db.conn.execute("INSERT INTO characters (discord_id, lodestone_id, name, world, verified_utc) VALUES (?, ?, ?, 'Exodus', 'x')",
                    (str(uid), lid, name))
    db.conn.commit()
    return db.conn.execute("SELECT id FROM characters WHERE lodestone_id = ?", (lid,)).fetchone()["id"]


def test_fresh_database_has_the_character_tables_and_no_old_block():
    db = make_db()
    names = {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"characters", "character_claims", "signup_characters"} <= names
    assert "character_id" in {r[1] for r in db.conn.execute("PRAGMA table_info(day_snapshots)")}


def test_the_only_character_is_used_without_picking():
    db = make_db()
    assert db.chosen_character(1, D1, 5) is None                       # nothing verified
    a = verified(db, 5, "Ann Tank", "111")
    assert db.chosen_character(1, D1, 5)["id"] == a
    verified(db, 5, "Ann Alt", "112")                                  # two characters: has to pick
    assert db.chosen_character(1, D1, 5) is None


def test_picking_per_day_and_overriding():
    db = make_db()
    a, b = verified(db, 5, "Ann Tank", "111"), verified(db, 5, "Ann Alt", "112")
    db.set_signup_character(1, [D1, D2], 5, a)
    db.set_signup_character(1, [D2], 5, b)
    assert db.chosen_character(1, D1, 5)["name"] == "Ann Tank"
    assert db.chosen_character(1, D2, 5)["name"] == "Ann Alt"


def test_the_snapshot_freezes_the_choice():
    db = make_db()
    a = verified(db, 5, "Ann Tank", "111")
    db.conn.execute("INSERT INTO availability (run_id, discord_id, slot_id) VALUES (1, '5', 1)")
    db.take_snapshot(1, D1, CFG, NOW)
    verified(db, 5, "Ann Alt", "112")                                  # an alt added after the cutoff
    assert db.chosen_character(1, D1, 5)["id"] == a                    # day 1 is frozen
    assert db.chosen_character(1, D2, 5) is None                       # day 2 is still open: two characters now


def test_removing_a_character_leaves_signups_without_one_instead_of_breaking():
    db = make_db()
    a = verified(db, 5, "Ann Tank", "111")
    db.conn.execute("INSERT INTO availability (run_id, discord_id, slot_id) VALUES (1, '5', 1)")
    db.set_signup_character(1, [D1, D2], 5, a)
    db.take_snapshot(1, D1, CFG, NOW)
    assert ch.delete_character(db, 5, a)
    assert db.chosen_character(1, D1, 5) is None and db.chosen_character(1, D2, 5) is None


def test_takeover_clears_the_old_holders_signups():
    db = make_db()
    a = verified(db, 1, "Ann Tank", "111")
    db.set_signup_character(1, [D1], 1, a)
    db.upsert_player(2, "P2", now=NOW)
    profile = SimpleNamespace(lodestone_id="111", name="Ann Tank", world="Exodus")
    row, displaced = ch.complete_claim(db, 2, profile, NOW)
    assert displaced == "1" and row["discord_id"] == "2"
    assert db.chosen_character(1, D1, 1) is None


def test_party_members_show_the_character_they_brought():
    db = make_db()
    a = verified(db, 1, "Ann Tank", "111")
    db.upsert_player(2, "P2", now=NOW)                                 # P2 has no character
    with db.conn:
        db.conn.execute("INSERT INTO parties (id, run_id, slot_id, party_index, status) VALUES (1, 1, 1, 0, 'confirmed')")
        db.conn.executemany("INSERT INTO party_members (party_id, discord_id, role) VALUES (1, ?, ?)",
                            [("1", "tank"), ("2", "healer")])
    got = db.party_member_characters(1)
    assert got["1"]["id"] == a and got["2"] is None
