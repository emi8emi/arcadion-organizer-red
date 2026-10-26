import sqlite3
import pytest

from bot.db import DB, DraftConflict, DuplicateRun
from conftest import NOW, add_player, fill_one_party


def test_schema_is_idempotent_and_seeds_the_fight_once(tmp_path):
    path = str(tmp_path / "bot.sqlite3")
    DB(path).close()
    db = DB(path)                                   # opening again re-runs schema.sql
    assert len(db.fights()) == 1
    assert len(db.checkpoints(1)) == 19


def test_ultima_weapon_checkpoints_are_the_agreed_19(ctx):
    cps = ctx.db.checkpoints(1)
    assert [c["idx"] for c in cps] == list(range(19))
    names = [f"{c['phase']} {c['name']}" for c in cps]
    assert names[0] == "P1 Fresh" and names[4] == "P1 Enrage" and names[11] == "P3 Gaols cleanup"
    assert names[14] == "Intermission Intermission" and names[18] == "P4 Enrage"
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
    assert ctx.db.get_limits(run["id"], 1, ctx.cfg) == {"max_per_day": 2, "max_per_run": 4,
                                                        "drop_checkpoints": 0, "is_default": True}
    ctx.db.set_limits(run["id"], 1, ctx.cfg, max_per_day=3)
    lim = ctx.db.set_limits(run["id"], 1, ctx.cfg, drop_checkpoints=2)
    assert (lim["max_per_day"], lim["max_per_run"], lim["drop_checkpoints"], lim["is_default"]) == (3, 4, 2, False)


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
    assert ctx.db.previous_unmatched(new) == {"99"}
    assert ctx.db.previous_unmatched(old) == set()                 # no earlier run
