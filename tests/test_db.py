import sqlite3
import pytest

from bot.db import DB, DraftConflict, DuplicateRun, SchemaNotReady
import bot.db
from conftest import NOW, add_player, confirm, fill_one_party


def test_opening_twice_is_fine_and_syncs_the_fight_once(tmp_path):
    path = str(tmp_path / "bot.sqlite3")
    DB(path).close()
    db = DB(path)                                   # opening again changes nothing
    assert len(db.fights()) == 1
    assert len(db.checkpoints(1)) == 20           # the 19 points of the test file plus the automatic Clear


def test_checkpoints_come_from_the_prog_point_file(ctx):
    cps = ctx.db.checkpoints(1)
    assert [c["idx"] for c in cps] == list(range(20))
    names = [f"{c['phase']} {c['name']}" for c in cps]
    assert names[0] == "P1 Fresh" and names[4] == "P1 Enrage" and names[11] == "P3 Gaols cleanup"
    assert names[14] == "Intermission Intermission" and names[18] == "P4 Enrage"
    assert names[19] == "P4 Clear"                                  # every fight ends with Clear
    phases = [c["phase"] for c in cps]
    assert [p for i, p in enumerate(phases) if i == 0 or p != phases[i - 1]] == ["P1", "P2", "P3", "Intermission", "P4"]


def test_find_fight_by_short_name_and_partial(ctx):
    assert ctx.db.find_fight("uwu")["id"] == 1
    assert ctx.db.find_fight("Ultima")["id"] == 1
    assert ctx.db.find_fight("nope") is None


def test_foreign_keys_are_enforced(ctx, make_run):
    run = make_run()
    with pytest.raises(sqlite3.IntegrityError):
        ctx.db.conn.execute("INSERT INTO availability (run_id, discord_id, slot_id) VALUES (?, 'ghost', 1)",
                            (run["id"],))


def test_create_run_stores_slots_and_utc_times(ctx, make_run):
    run = make_run()
    slots = ctx.db.run_slots(run["id"])
    assert len(slots) == 33
    assert slots[0]["slot_key"] == "2026-11-01T08:00" and slots[0]["start_utc"] == "2026-11-01T13:00:00+00:00"
    assert run["status"] == "open"


def test_run_names_are_unique_per_server_only(ctx, make_run):
    make_run("A")
    with pytest.raises(DuplicateRun):
        make_run("A")
    assert make_run("A", guild_id=999)["guild_id"] == "999"


def test_get_run_by_name_and_newest(ctx, make_run):
    a, b = make_run("First", status="closed"), make_run("Second")
    assert ctx.db.get_run(100)["id"] == b["id"]
    assert ctx.db.get_run(100, "first")["id"] == a["id"]
    assert ctx.db.get_run(100, "missing") is None
    assert ctx.db.open_run(100)["id"] == b["id"]


def test_upsert_player_updates_only_given_fields(ctx):
    ctx.db.upsert_player(5, "Ann", roles=["dps", "tank"], prog_index=3, now=NOW)
    p = ctx.db.upsert_player(5, "Ann the Tank", timezone="America/Chicago", now=NOW)
    assert p["display_name"] == "Ann the Tank" and p["roles"] == "tank,dps"       # fixed role order
    assert p["prog_index"] == 3 and p["timezone"] == "America/Chicago"
    assert ctx.db.upsert_player(5, "Ann the Tank", prog_index=0)["prog_index"] == 0   # 0 is a real value


def test_set_day_availability_replaces_only_that_day(ctx, make_run):
    run = make_run()
    ctx.db.upsert_player(1, "A")
    ctx.db.set_day_availability(run["id"], 1, "2026-11-01", ["2026-11-01T17:00", "2026-11-01T18:30"])
    ctx.db.set_day_availability(run["id"], 1, "2026-11-02", ["2026-11-02T17:00"])
    ctx.db.set_day_availability(run["id"], 1, "2026-11-01", ["2026-11-01T20:00", "2026-11-02T09:30", "bogus"])
    assert ctx.db.ticked_keys(run["id"], 1) == ["2026-11-01T20:00", "2026-11-02T17:00"]   # other-day and bad keys ignored


def test_limits_default_then_partial_update(ctx, make_run):
    run = make_run()
    ctx.db.upsert_player(1, "A")
    assert ctx.db.get_limits(run["id"], 1, ctx.cfg) == {"max_per_day": 2, "max_per_run": 99,
                                                        "drop_checkpoints": 0, "is_default": True}
    ctx.db.set_limits(run["id"], 1, ctx.cfg, max_per_day=3)
    lim = ctx.db.set_limits(run["id"], 1, ctx.cfg, drop_checkpoints=2)
    assert (lim["max_per_day"], lim["max_per_run"], lim["drop_checkpoints"], lim["is_default"]) == (3, 99, 2, False)


def test_helper_grant_and_revoke(ctx):
    ctx.db.grant_helper(1, 7, 1, NOW)
    ctx.db.grant_helper(1, 7, 1, NOW)                 # granting twice is harmless
    assert ctx.db.helper_ids(1) == {"7"}
    assert ctx.db.revoke_helper(1, 7) and not ctx.db.revoke_helper(1, 7)


def _party(slot, idx, ids):
    roles = ["tank"] * 2 + ["healer"] * 2 + ["dps"] * 4
    return {"slot_key": slot, "party_index": idx, "target_idx": 0,
            "members": [{"discord_id": str(i), "role": r, "helper": False, "placed_down": False, "distance": 0}
                        for i, r in zip(ids, roles)]}


def test_replace_draft_swaps_the_previous_draft(ctx, make_run):
    run = make_run()
    ids = fill_one_party(ctx, run)
    ctx.db.replace_draft(run["id"], [_party("2026-11-02T17:00", 0, ids)], [("slot", "2026-11-02T18:30", "too few")])
    ctx.db.replace_draft(run["id"], [_party("2026-11-02T17:00", 0, ids)], [])
    assert len(ctx.db.parties(run["id"])) == 1 and len(ctx.db.party_members(ctx.db.parties(run["id"])[0]["id"])) == 8
    assert ctx.db.draft_notes(run["id"]) == []


def test_replace_draft_refuses_to_overwrite_confirmed_parties(ctx, make_run):
    run = make_run()
    ids = fill_one_party(ctx, run)
    ctx.db.replace_draft(run["id"], [_party("2026-11-02T17:00", 0, ids)], [])
    with ctx.db.conn:
        ctx.db.conn.execute("UPDATE parties SET status = 'confirmed'")
    with pytest.raises(DraftConflict):
        ctx.db.replace_draft(run["id"], [], [])
    assert len(ctx.db.parties(run["id"])) == 1       # nothing was lost


def test_previous_unmatched_lists_ticked_players_without_a_party(ctx, make_run):
    old = make_run("Old", status="closed")
    ids = fill_one_party(ctx, old)
    add_player(ctx, old, 99, ["dps"], 0, ["2026-11-02T20:00"])     # ticked, never seated
    ctx.db.replace_draft(old["id"], [_party("2026-11-02T17:00", 0, ids)], [])
    new = make_run("New")
    assert ctx.db.previous_unmatched(new) == set()                 # an unapproved draft does not count
    confirm(ctx, old)
    assert ctx.db.previous_unmatched(new) == {"99"}
    assert ctx.db.previous_unmatched(old) == set()                 # no earlier run


def test_replace_draft_with_a_day_keeps_other_days_and_their_notes(ctx, make_run):
    run = make_run()
    ids = fill_one_party(ctx, run, "2026-11-01T17:00")
    ctx.db.replace_draft(run["id"], [_party("2026-11-01T17:00", 0, ids), _party("2026-11-02T17:00", 0, ids)],
                         [("slot", "2026-11-01T18:30", "d1 note"), ("slot", "2026-11-02T18:30", "d2 note")])
    ctx.db.replace_draft(run["id"], [], [("slot", "2026-11-02T20:00", "new d2 note")], day="2026-11-02")
    assert [p["slot_key"] for p in ctx.db.parties(run["id"])] == ["2026-11-01T17:00"]
    assert sorted(n["ref"] for n in ctx.db.draft_notes(run["id"])) == ["2026-11-01T18:30", "2026-11-02T20:00"]


def test_replace_draft_with_a_day_ignores_confirmed_parties_on_other_days(ctx, make_run):
    run = make_run()
    ids = fill_one_party(ctx, run, "2026-11-01T17:00")
    ctx.db.replace_draft(run["id"], [_party("2026-11-01T17:00", 0, ids)], [])
    with ctx.db.conn:
        ctx.db.conn.execute("UPDATE parties SET status = 'confirmed'")
    ctx.db.replace_draft(run["id"], [_party("2026-11-02T17:00", 0, ids)], [], day="2026-11-02")
    with pytest.raises(DraftConflict):
        ctx.db.replace_draft(run["id"], [], [], day="2026-11-01")


# ---------------------------------------------------------------- the dashboard owns the schema

def test_the_bot_refuses_a_database_nobody_migrated(tmp_path, monkeypatch):
    monkeypatch.setattr(DB, "bootstrap", None)              # no test help: this is what a real start sees
    path = str(tmp_path / "empty.sqlite3")
    with pytest.raises(SchemaNotReady, match=r"schema version 0 .*python -m app\.migrate"):
        DB(path)
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == 0     # and created nothing


def test_the_bot_refuses_a_database_behind_the_version_it_needs(tmp_path, monkeypatch):
    path = str(tmp_path / "bot.sqlite3")
    DB(path).close()
    monkeypatch.setattr(DB, "bootstrap", None)
    monkeypatch.setattr(bot.db, "REQUIRED_SCHEMA_VERSION", bot.db.REQUIRED_SCHEMA_VERSION + 1)
    with pytest.raises(SchemaNotReady, match="needs 2"):
        DB(path)


def test_opening_a_migrated_database_changes_no_table(tmp_path, monkeypatch):
    path = str(tmp_path / "bot.sqlite3")
    DB(path).close()
    snapshot = lambda: sqlite3.connect(path).execute(          # noqa: E731
        "SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
    before = snapshot()
    monkeypatch.setattr(DB, "bootstrap", None)
    DB(path).close()
    assert snapshot() == before


def test_a_database_ahead_of_the_bot_is_fine(tmp_path, monkeypatch):
    path = str(tmp_path / "bot.sqlite3")
    DB(path).close()
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO schema_migrations (version, name, checksum, applied_utc) VALUES (99, 'later', 'x', 'x')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(DB, "bootstrap", None)              # the bot must not need the dashboard's runner for this
    DB(path).close()
