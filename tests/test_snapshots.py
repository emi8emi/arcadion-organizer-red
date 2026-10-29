"""Day snapshots: a day's signups are frozen at its cutoff, so later profile edits cannot make its draft stale."""
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord

from bot import handlers
from bot.db import DB
from bot.services import snapshot_note
from bot.snapshots import LATE_WARNING, ensure_snapshot, take_due_snapshots
from bot.solver_io import build_signups, fingerprint, import_draft
from conftest import FakeAttachment, S2, confirm, add_party_players, at, draft_for_day, organizer, player, run_async

UTC = timezone.utc
D2, D3 = "2026-11-02", "2026-11-03"
S3_ = "2026-11-03T17:00"
CUTOFF2 = datetime(2026, 11, 2, 4, 59, tzinfo=UTC)        # Monday closes Sunday 23:59 EST
OLD = Path(__file__).resolve().parent / "fixtures" / "schema_before_snapshots.sql"
SOLVER_DIR = Path(__file__).resolve().parent.parent.parent / "raid-solver"


def do(ctx, handler, it, *args, **kwargs):
    run_async(handler(ctx, it, *args, **kwargs))
    return it


def sig(ctx, run, day):
    data, _ = build_signups(ctx.db, run, ctx.cfg, day)
    return data, fingerprint(data)


def world(ctx, make_run, clock, now="2026-11-02T05:00"):
    """Per-day run; eight players ticked Mon and Tue; clock just after Monday's cutoff, Tuesday still open."""
    run = make_run(per_day=True)
    ids = add_party_players(ctx, run, [S2, S3_])
    at(clock, now)
    return run, ids


# ---------------------------------------------------------------- when snapshots are taken

def test_the_loop_function_takes_the_snapshot_at_the_cutoff_and_is_idempotent(ctx, make_run, clock):
    run, _ = world(ctx, make_run, clock, "2026-11-02T04:58")
    assert all(t[1] != D2 for t in take_due_snapshots(ctx.db, clock["now"], ctx.cfg))       # before Monday's cutoff
    assert ctx.db.run_day(run["id"], D2)["snapshot_utc"] is None
    at(clock, "2026-11-02T05:00")
    taken = take_due_snapshots(ctx.db, clock["now"], ctx.cfg)
    assert (run["id"], D2, False) in taken and all(t[1] != D3 for t in taken)
    day = ctx.db.run_day(run["id"], D2)
    assert day["snapshot_utc"] == "2026-11-02T05:00:00+00:00" and not day["snapshot_late"]
    assert len(ctx.db.snapshot_rows(run["id"], D2)) == 8 and ctx.db.snapshot_rows(run["id"], D3) == {}
    assert take_due_snapshots(ctx.db, clock["now"] + timedelta(minutes=1), ctx.cfg) == []      # idempotent


def test_the_snapshot_holds_name_roles_prog_and_effective_limits(ctx, make_run, clock):
    run, _ = world(ctx, make_run, clock)
    ctx.db.upsert_player(9, "Defaulty", roles=["dps"], prog_index=4, now=clock["now"])
    ctx.db.set_day_availability(run["id"], 9, D2, [S2])                  # never set limits: defaults are stored
    take_due_snapshots(ctx.db, clock["now"], ctx.cfg)
    row = ctx.db.snapshot_rows(run["id"], D2)["9"]
    assert (row["display_name"], row["roles"], row["prog_index"]) == ("Defaulty", "dps", 4)
    assert (row["max_per_day"], row["max_per_run"], row["drop_checkpoints"]) == (
        ctx.cfg["default_max_per_day"], ctx.cfg["default_max_per_run"], 0)


def test_a_day_nobody_signed_up_for_still_gets_the_marker(ctx, make_run, clock):
    run = make_run(per_day=True)
    at(clock, "2026-11-02T05:00")
    assert any(t[:2] == (run["id"], D2) for t in take_due_snapshots(ctx.db, clock["now"], ctx.cfg))
    assert ctx.db.run_day(run["id"], D2)["snapshot_utc"] and ctx.db.snapshot_rows(run["id"], D2) == {}
    assert take_due_snapshots(ctx.db, clock["now"] + timedelta(minutes=1), ctx.cfg) == []


def test_a_snapshot_is_never_overwritten(ctx, make_run, clock):
    run, _ = world(ctx, make_run, clock)
    take_due_snapshots(ctx.db, clock["now"], ctx.cfg)
    ctx.db.upsert_player(1, "Renamed", prog_index=9)
    assert ctx.db.take_snapshot(run["id"], D2, ctx.cfg, clock["now"] + timedelta(hours=1)) is False
    assert ctx.db.snapshot_rows(run["id"], D2)["1"]["prog_index"] == 0


def test_a_loop_that_was_down_flags_the_snapshot_as_late(ctx, make_run, clock):
    run, _ = world(ctx, make_run, clock, "2026-11-02T09:00")             # four hours after the cutoff
    take_due_snapshots(ctx.db, clock["now"], ctx.cfg)
    assert ctx.db.run_day(run["id"], D2)["snapshot_late"] == 1


def test_export_takes_a_missing_snapshot_lazily_and_warns(ctx, make_run, clock):
    run, _ = world(ctx, make_run, clock)
    it = do(ctx, handlers.draft_export, organizer(), None, D2)
    assert LATE_WARNING in it.text and "Left out" not in it.text
    row = ctx.db.run_day(run["id"], D2)
    assert row["snapshot_utc"] and row["snapshot_late"] == 1 and row["exported_utc"]


def test_import_and_status_also_take_a_missing_snapshot(ctx, make_run, clock):
    run, _ = world(ctx, make_run, clock)
    do(ctx, handlers.run_status, organizer(), None, D2)
    assert ctx.db.run_day(run["id"], D2)["snapshot_utc"]
    assert ctx.db.run_day(run["id"], D3)["snapshot_utc"] is None          # still open: nothing to freeze
    other = make_run("Other", per_day=True)
    do(ctx, handlers.draft_import, organizer(), FakeAttachment(b"{}"), "Other", D2)
    assert ctx.db.run_day(other["id"], D2)["snapshot_utc"]


def test_nothing_is_taken_for_a_draft_run_or_an_open_day(ctx, make_run, clock):
    run = make_run(per_day=True, status="draft")
    at(clock, "2026-11-02T05:00")
    assert not ensure_snapshot(ctx, run, D2) and take_due_snapshots(ctx.db, clock["now"], ctx.cfg) == []
    ctx.db.set_run_status(run["id"], "open")
    assert not ensure_snapshot(ctx, ctx.db.run(run["id"]), D3)


def test_run_close_takes_the_snapshot_of_each_day_it_closes(ctx, make_run, clock):
    run, _ = world(ctx, make_run, clock)
    it = do(ctx, handlers.run_close, organizer(), None, D3)
    assert "saved" in it.text
    row = ctx.db.run_day(run["id"], D3)
    assert row["snapshot_utc"] == "2026-11-02T05:00:00+00:00" and not row["snapshot_late"]
    assert len(ctx.db.snapshot_rows(run["id"], D3)) == 8
    do(ctx, handlers.run_close, organizer())                              # everything left
    assert all(d["snapshot_utc"] for d in ctx.db.run_days(run["id"]))


def test_extend_clears_the_snapshot_and_a_new_one_is_taken_at_the_new_cutoff(ctx, make_run, clock):
    run, ids = world(ctx, make_run, clock)
    take_due_snapshots(ctx.db, clock["now"], ctx.cfg)
    before = sig(ctx, run, D2)[1]
    it = do(ctx, handlers.run_extend, organizer(), D2, "2026-11-02 07:00")
    assert "dropped" in it.text
    assert ctx.db.run_day(run["id"], D2)["snapshot_utc"] is None and ctx.db.snapshot_rows(run["id"], D2) == {}
    ctx.db.upsert_player(1, "Player 1", prog_index=6)                     # day is open again: live data
    assert sig(ctx, run, D2)[1] != before
    at(clock, "2026-11-02T12:30")                                         # new cutoff 12:00Z passed
    assert take_due_snapshots(ctx.db, clock["now"], ctx.cfg)[0][:2] == (run["id"], D2)
    assert ctx.db.snapshot_rows(run["id"], D2)["1"]["prog_index"] == 6


# ---------------------------------------------------------------- edits after the cutoff

def test_edits_after_the_cutoff_change_neither_that_days_export_nor_its_fingerprint(ctx, make_run, clock):
    run, ids = world(ctx, make_run, clock)
    take_due_snapshots(ctx.db, clock["now"], ctx.cfg)
    mon_data, mon_fp = sig(ctx, run, D2)
    _, tue_fp = sig(ctx, run, D3)
    mon = {p["id"]: p for p in mon_data["players"]}
    ctx.db.upsert_player(1, "Renamed Tank", roles=["healer"], prog_index=11, now=clock["now"])     # name, roles, prog
    ctx.db.set_limits(run["id"], 3, ctx.cfg, max_per_day=1, max_per_run=1, drop_checkpoints=2)     # limits
    assert sig(ctx, run, D2) == (mon_data, mon_fp)
    tue = {p["id"]: p for p in sig(ctx, run, D3)[0]["players"]}
    assert sig(ctx, run, D3)[1] != tue_fp                                  # the open day follows the live profile
    assert tue["1"]["name"] == "Renamed Tank" and tue["1"]["roles"] == ["healer"] and tue["1"]["prog"] == 11
    assert tue["3"]["max_per_run"] == 1 and tue["3"]["max_per_day"] == 1
    assert mon["3"]["max_per_run"] == 4 and mon["3"]["max_per_day"] == 2
    assert mon["1"]["name"] == "Player 1" and mon["1"]["roles"] == ["tank"] and mon["1"]["prog"] == 0


def test_a_draft_built_from_the_export_still_imports_after_profile_edits(ctx, make_run, clock):
    run, ids = world(ctx, make_run, clock)
    take_due_snapshots(ctx.db, clock["now"], ctx.cfg)
    draft = draft_for_day(ctx, run, D2, S2, ids)
    ctx.db.upsert_player(1, "Renamed Tank", roles=["healer"], prog_index=11, now=clock["now"])
    ctx.db.upsert_player(5, "Player 5", roles=["tank"])
    ctx.db.set_limits(run["id"], 2, ctx.cfg, max_per_day=1, max_per_run=1)
    summary = import_draft(ctx.db, run, draft, ctx.cfg, D2)
    assert summary["seated"] == 8
    # the same through the handler, without naming the day: it is found by the fingerprint
    ctx.db.mark_exported(run["id"], D2, clock["now"])
    it = do(ctx, handlers.draft_import, organizer(), FakeAttachment(json.dumps(draft).encode()))
    assert "Draft stored" in it.text and "Draft refused" not in it.text


def test_a_helper_grant_after_the_cutoff_still_makes_the_export_stale(ctx, make_run, clock):
    run, ids = world(ctx, make_run, clock)
    take_due_snapshots(ctx.db, clock["now"], ctx.cfg)
    draft = draft_for_day(ctx, run, D2, S2, ids)
    ctx.db.grant_helper(1, 3, 1, clock["now"])
    assert {p["id"]: p["helper"] for p in sig(ctx, run, D2)[0]["players"]}["3"] is True
    try:
        import_draft(ctx.db, run, draft, ctx.cfg, D2)
        raise AssertionError("a helper grant must make the draft stale")
    except Exception as e:
        assert "different signups" in str(e)


def test_per_run_cap_is_the_snapshots_minus_parties_on_earlier_days(ctx, make_run, clock):
    run = make_run(per_day=True)
    ids = add_party_players(ctx, run, ["2026-11-01T17:00", S2])
    at(clock, "2026-11-02T05:00")
    take_due_snapshots(ctx.db, clock["now"], ctx.cfg)
    import_draft(ctx.db, run, draft_for_day(ctx, run, "2026-11-01", "2026-11-01T17:00", ids), ctx.cfg, "2026-11-01")
    confirm(ctx, run, "2026-11-01")
    ctx.db.set_limits(run["id"], 1, ctx.cfg, max_per_run=9)                # edited after Monday's cutoff
    assert {p["id"]: p["max_per_run"] for p in sig(ctx, run, D2)[0]["players"]}["1"] == 3     # snapshot 4 - 1 played


def test_a_forced_export_of_an_open_day_uses_live_data(ctx, make_run, clock):
    run, _ = world(ctx, make_run, clock)
    ctx.db.upsert_player(1, "Live Name", prog_index=7)
    it = do(ctx, handlers.draft_export, organizer(), None, D3, True)
    assert "still open" in it.text and ctx.db.run_day(run["id"], D3)["snapshot_utc"] is None
    assert {p["id"]: p["prog"] for p in sig(ctx, run, D3)[0]["players"]}["1"] == 7


def test_the_whole_run_export_stays_live(ctx, make_run, clock):
    run, _ = world(ctx, make_run, clock)
    take_due_snapshots(ctx.db, clock["now"], ctx.cfg)
    ctx.db.upsert_player(1, "Player 1", prog_index=7)
    assert {p["id"]: p["prog"] for p in build_signups(ctx.db, run, ctx.cfg)[0]["players"]}["1"] == 7


# ---------------------------------------------------------------- what players see

def test_menu_note_after_an_edit_names_the_frozen_days_and_the_first_open_day(ctx, make_run, clock):
    run, _ = world(ctx, make_run, clock)
    take_due_snapshots(ctx.db, clock["now"], ctx.cfg)

    async def scenario():
        it = player(1, "Player 1")
        await handlers.availability(ctx, it)
        view = it.response.sent[0]["view"]
        view.prog_select._values = ["5"]
        edit = player(1)
        await view.prog_select.callback(edit)
        text = edit.response.edits[-1]["content"]
        assert ("Your roles, prog and limits for Mon 02 Nov were saved at their cutoffs. "
                "This change applies from Tue 03 Nov.") in text and len(text) <= 2000
        slot = player(1)                                                   # a plain tick change shows no note
        view.slot_select._values = [S3_]
        await view.slot_select.callback(slot)
        assert "were saved at their cutoffs" not in slot.response.edits[-1]["content"]
    run_async(scenario())


def test_menu_shows_no_note_when_no_signed_up_day_is_frozen(ctx, make_run, clock):
    run, _ = world(ctx, make_run, clock)
    ctx.db.upsert_player(40, "New", roles=["dps"], prog_index=1)
    ctx.db.set_day_availability(run["id"], 40, D3, [S3_])                  # only the open day
    take_due_snapshots(ctx.db, clock["now"], ctx.cfg)
    assert snapshot_note(ctx, run, 40) == "" and snapshot_note(ctx, run, 999) == ""


def test_limits_command_shows_the_note_too(ctx, make_run, clock):
    world(ctx, make_run, clock)
    take_due_snapshots(ctx.db, clock["now"], ctx.cfg)
    it = do(ctx, handlers.limits, player(1), 1)
    assert "were saved at their cutoffs" in it.text
    assert "were saved" not in do(ctx, handlers.limits, player(1)).text      # just looking: no note


def test_myschedule_shows_the_prog_locked_in_for_each_closed_day(ctx, make_run, clock):
    run, _ = world(ctx, make_run, clock)
    take_due_snapshots(ctx.db, clock["now"], ctx.cfg)
    ctx.db.upsert_player(1, "Player 1", prog_index=11)
    text = do(ctx, handlers.myschedule, player(1)).text
    assert "Prog locked in at each closed day's cutoff:" in text
    assert "• Mon 02 Nov: 0: P1 Fresh" in text and "Tue 03 Nov: " not in text
    assert "Prog: 11:" in text                                              # the live profile line is still there


def test_note_and_menu_strings_stay_within_discords_limits(ctx, make_run, clock):
    from datetime import date
    run = make_run("Long", start=date(2026, 11, 1), end=date(2026, 11, 14), per_day=True)
    keys = [s["slot_key"] for s in ctx.db.run_slots(run["id"])]
    ctx.db.upsert_player(5, "Ann", roles=["tank"], prog_index=18)
    for day in sorted({k[:10] for k in keys}):
        ctx.db.set_day_availability(run["id"], 5, day, [k for k in keys if k[:10] == day])
    at(clock, "2026-11-10T12:00")
    take_due_snapshots(ctx.db, clock["now"], ctx.cfg)
    note = snapshot_note(ctx, run, 5)
    assert note.startswith("Your roles, prog and limits for ") and "and " in note and len(note) < 300

    async def scenario():
        it = player(5, "Ann")
        await handlers.availability(ctx, it)
        view = it.response.sent[0]["view"]
        view.prog_select._values = ["3"]
        edit = player(5)
        await view.prog_select.callback(edit)
        assert len(edit.response.edits[-1]["content"]) <= 2000 and "saved at their cutoffs" in edit.response.edits[-1]["content"]
        done = player(5)
        for child in view.children:
            if isinstance(child, discord.ui.Button) and child.label == "Done":
                await child.callback(done)
        assert len(done.response.edits[-1]["content"]) <= 2000
    run_async(scenario())
    text = do(ctx, handlers.myschedule, player(5)).text
    assert len(text) <= 2000


# ---------------------------------------------------------------- migration from the first commit's schema

def test_migration_adds_the_snapshot_columns_and_keeps_the_data(tmp_path, cfg):
    path = str(tmp_path / "old.sqlite3")
    conn = sqlite3.connect(path)
    conn.executescript(OLD.read_text())            # schema exactly as the first commit had it
    conn.execute("INSERT INTO runs (guild_id, name, fight_id, start_date, end_date, signup_deadline_utc, status, "
                 "created_by, created_utc) VALUES ('1', 'Old', 1, '2026-11-02', '2026-11-02', "
                 "'2026-11-01T04:59:00+00:00', 'open', '1', '2026-10-25T12:00:00+00:00')")
    conn.execute("INSERT INTO run_days (run_id, day, cutoff_utc, exported_utc) "
                 "VALUES (1, '2026-11-02', '2026-11-02T04:59:00+00:00', '2026-11-02T05:10:00+00:00')")
    conn.execute("INSERT INTO slots (run_id, slot_key, start_utc, minutes) VALUES (1, '2026-11-02T17:00', "
                 "'2026-11-02T22:00:00+00:00', 90)")
    conn.execute("INSERT INTO players (discord_id, display_name, roles, prog_index) VALUES ('7', 'Ann', 'dps', 3)")
    conn.execute("INSERT INTO availability (run_id, discord_id, slot_id) VALUES (1, '7', 1)")
    conn.commit()
    assert "snapshot_utc" not in {r[1] for r in conn.execute("PRAGMA table_info(run_days)")}
    conn.close()

    db = DB(path)
    cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(run_days)")}
    assert {"snapshot_utc", "snapshot_late"} <= cols
    day = db.run_day(1, "2026-11-02")
    assert day["exported_utc"] == "2026-11-02T05:10:00+00:00" and day["snapshot_utc"] is None
    assert db.snapshot_candidates(datetime(2026, 11, 3, tzinfo=UTC)) == []        # exported earlier: left live
    DB(path).close()                                                              # a second start changes nothing
    assert db.take_snapshot(1, "2026-11-02", cfg, datetime(2026, 11, 3, tzinfo=UTC))
    assert db.snapshot_rows(1, "2026-11-02")["7"]["prog_index"] == 3


# ---------------------------------------------------------------- the real solver

def test_real_solver_round_trip_survives_a_profile_edit_after_the_cutoff(ctx, make_run, clock, tmp_path):
    import pytest
    if not (SOLVER_DIR / "solve.py").exists():
        pytest.skip("raid-solver not next to raid-bot")
    run, ids = world(ctx, make_run, clock)
    take_due_snapshots(ctx.db, clock["now"], ctx.cfg)
    data, _ = build_signups(ctx.db, run, ctx.cfg, D2)
    signups = tmp_path / "signups.json"
    signups.write_text(json.dumps(data, indent=1))
    solver_cfg = json.loads((SOLVER_DIR / "config.json").read_text())
    solver_cfg["pass1"]["time_limit_s"] = 30
    solver_cfg["pass2"]["time_limit_s"] = 15
    cfg_path = tmp_path / "solver_config.json"
    cfg_path.write_text(json.dumps(solver_cfg))
    out = tmp_path / "draft.json"
    done = subprocess.run([sys.executable, str(SOLVER_DIR / "solve.py"), str(signups), "-o", str(out),
                           "--config", str(cfg_path)], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    ctx.db.upsert_player(1, "Renamed", roles=["dps"], prog_index=12)         # edited while the solver ran
    summary = import_draft(ctx.db, run, json.loads(out.read_text()), ctx.cfg, D2)
    assert summary["parties"] == 1 and summary["seated"] == 8
