import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from bot.db import DraftConflict
from bot.solver_io import DraftError, build_signups, drop_units, fingerprint, import_draft, phase_positions
from conftest import NOW, SLOT, confirm, add_player, draft_for, fill_one_party

SOLVER_DIR = Path(__file__).resolve().parent.parent.parent / "raid-solver"


@pytest.fixture
def world(ctx, make_run):
    """A run with one full party's worth of players, all ticked on the same slot."""
    run = make_run()
    ids = fill_one_party(ctx, run, SLOT)
    return run, [str(i) for i in ids]


# ---------------------------------------------------------------- export shape

def test_export_has_the_shape_the_solver_reads(ctx, world):
    run, ids = world
    data, warnings = build_signups(ctx.db, run, ctx.cfg)
    assert warnings == []
    assert set(data) == {"version", "run", "fight", "slots", "players", "votes"} and data["votes"] == []
    assert data["run"] == {"name": "UWU week 1", "timezone": "America/New_York"}
    assert len(data["fight"]["checkpoints"]) == 19
    assert data["fight"]["checkpoints"][4] == {"index": 4, "phase": "P1", "name": "Enrage"}
    s = next(s for s in data["slots"] if s["id"] == SLOT)
    assert s == {"id": SLOT, "start": "2026-11-02T17:00:00-05:00", "minutes": 90}
    p = data["players"][0]
    assert set(p) == {"id", "name", "roles", "helper", "prog", "max_per_day", "max_per_run", "drop",
                      "unmatched_last_run", "available"}
    assert p["id"] == "1" and p["roles"] == ["tank"] and p["available"] == [SLOT]
    assert p["drop"] == {"opt_in": False, "max_drop": 0}


def test_export_is_deterministic_and_fingerprint_follows_the_data(ctx, world):
    run, ids = world
    a, _ = build_signups(ctx.db, run, ctx.cfg)
    b, _ = build_signups(ctx.db, run, ctx.cfg)
    assert fingerprint(a) == fingerprint(b)
    ctx.db.set_day_availability(run["id"], 1, "2026-11-02", [SLOT, "2026-11-02T18:30"])
    c, _ = build_signups(ctx.db, run, ctx.cfg)
    assert fingerprint(c) != fingerprint(a)
    ctx.db.upsert_player(1, "Player 1", prog_index=5)
    d, _ = build_signups(ctx.db, run, ctx.cfg)
    assert fingerprint(d) != fingerprint(c)


def test_helper_grant_shows_up_in_the_export(ctx, world):
    run, ids = world
    ctx.db.grant_helper(1, 3, 1, NOW)
    data, _ = build_signups(ctx.db, run, ctx.cfg)
    assert {p["id"]: p["helper"] for p in data["players"]}["3"] is True
    assert sum(p["helper"] for p in data["players"]) == 1


def test_players_without_roles_or_prog_are_left_out_with_a_warning(ctx, world):
    run, ids = world
    ctx.db.upsert_player(50, "No Profile")
    ctx.db.set_day_availability(run["id"], 50, "2026-11-02", [SLOT])
    data, warnings = build_signups(ctx.db, run, ctx.cfg)
    assert "50" not in {p["id"] for p in data["players"]}
    assert len(warnings) == 1 and "No Profile" in warnings[0]


def test_drop_distance_uses_phase_aware_units(ctx):
    cps = ctx.db.checkpoints(1)
    pos = phase_positions(cps, 3)
    assert pos[0] == 0 and pos[5] == 5 + 3 and pos[18] == 18 + 12       # +3 per phase crossed
    assert drop_units(pos, 11, 2) == 2          # P3 Gaols cleanup -> P3 Fresh: same phase
    assert drop_units(pos, 9, 1) == 4           # P3 Fresh -> P2 Enrage: crosses a phase boundary
    assert drop_units(pos, 1, 5) == 1           # cannot drop below checkpoint 0
    assert drop_units(pos, 7, 0) == 0


def test_opted_in_drop_is_exported_in_solver_units(ctx, make_run):
    run = make_run()
    add_player(ctx, run, 1, ["dps"], 9, [SLOT], drop=1)
    data, _ = build_signups(ctx.db, run, ctx.cfg)
    assert data["players"][0]["drop"] == {"opt_in": True, "max_drop": 4}


def test_unmatched_last_run_flag_comes_from_the_previous_run(ctx, make_run):
    old = make_run("Old", status="closed")
    ids = fill_one_party(ctx, old, SLOT)
    add_player(ctx, old, 99, ["dps"], 0, ["2026-11-02T20:00"])
    draft = draft_for(ctx, old, [str(i) for i in ids])
    import_draft(ctx.db, old, draft, ctx.cfg)
    confirm(ctx, old)
    new = make_run("New")
    add_player(ctx, new, 99, ["dps"], 0, [SLOT])
    add_player(ctx, new, 1, ["tank"], 0, [SLOT])
    flags = {p["id"]: p["unmatched_last_run"] for p in build_signups(ctx.db, new, ctx.cfg)[0]["players"]}
    assert flags == {"1": False, "99": True}


# ---------------------------------------------------------------- fingerprint

def test_fingerprint_ignores_key_order_and_whitespace():
    a = {"b": [1, 2], "a": {"y": 1, "x": 2}}
    b = json.loads(json.dumps({"a": {"x": 2, "y": 1}, "b": [1, 2]}, indent=4))
    assert fingerprint(a) == fingerprint(b)


@pytest.mark.skipif(not (SOLVER_DIR / "solve.py").exists(), reason="raid-solver not next to raid-bot")
def test_fingerprint_matches_the_solvers(ctx, world):
    sys.path.insert(0, str(SOLVER_DIR))
    try:
        import solve
    finally:
        sys.path.remove(str(SOLVER_DIR))
    data, _ = build_signups(ctx.db, world[0], ctx.cfg)
    assert fingerprint(data) == solve.fingerprint(data)
    assert fingerprint(data) == solve.fingerprint(json.loads(json.dumps(data, indent=1)))   # via the file


# ---------------------------------------------------------------- import

def test_import_accepts_a_matching_draft(ctx, world):
    run, ids = world
    s = import_draft(ctx.db, run, draft_for(ctx, run, ids), ctx.cfg)
    assert (s["parties"], s["slots"], s["seated"], s["no_party"]) == (1, 1, 8, 0)
    parties = ctx.db.parties(run["id"])
    assert len(parties) == 1 and parties[0]["status"] == "draft" and parties[0]["slot_key"] == SLOT
    assert len(ctx.db.party_members(parties[0]["id"])) == 8
    assert [(n["kind"], n["ref"]) for n in ctx.db.draft_notes(run["id"])] == [("slot", "2026-11-02T18:30")]


def test_import_twice_replaces_the_draft_instead_of_duplicating(ctx, world):
    run, ids = world
    draft = draft_for(ctx, run, ids)
    import_draft(ctx.db, run, draft, ctx.cfg)
    import_draft(ctx.db, run, draft, ctx.cfg)
    assert len(ctx.db.parties(run["id"])) == 1


def test_import_refuses_a_draft_when_signups_changed_after_export(ctx, world):
    run, ids = world
    draft = draft_for(ctx, run, ids)
    ctx.db.set_day_availability(run["id"], 8, "2026-11-02", [])            # someone untick their slot
    with pytest.raises(DraftError, match="different signups"):
        import_draft(ctx.db, run, draft, ctx.cfg)
    assert ctx.db.parties(run["id"]) == []


def test_import_refuses_when_a_profile_changed(ctx, world):
    run, ids = world
    draft = draft_for(ctx, run, ids)
    ctx.db.upsert_player(4, "Player 4", prog_index=9)
    with pytest.raises(DraftError, match="different signups"):
        import_draft(ctx.db, run, draft, ctx.cfg)


def test_import_refuses_a_draft_for_another_run(ctx, world, make_run):
    run, ids = world
    draft = draft_for(ctx, run, ids)
    other = make_run("Other")
    with pytest.raises(DraftError, match="different signups"):
        import_draft(ctx.db, other, draft, ctx.cfg)


@pytest.mark.parametrize("mutate, message", [
    (lambda d: d.update(version=2), "Unsupported draft version"),
    (lambda d: d.pop("input_fingerprint"), "does not look like"),
    (lambda d: d["parties"][0].update(slot="2026-11-02T03:00"), "unknown slot"),
    (lambda d: d["parties"][0]["members"].pop(), "roles do not match"),
    (lambda d: d["parties"][0]["members"][0].update(role="dps"), "roles do not match"),
    (lambda d: d["parties"][0]["members"][0].update(player="12345"), "not in the current signups"),
    (lambda d: d["parties"][0]["members"][2].update(role="tank"), "roles do not match"),
    (lambda d: d["parties"][0]["target"].update(index=99), "bad target"),
    (lambda d: d["parties"].append(copy.deepcopy(d["parties"][0])), "appears twice"),
])
def test_import_refuses_drafts_that_break_the_signups(ctx, world, mutate, message):
    run, ids = world
    draft = draft_for(ctx, run, ids)
    mutate(draft)
    with pytest.raises(DraftError, match=message):
        import_draft(ctx.db, run, draft, ctx.cfg)
    assert ctx.db.parties(run["id"]) == []


def test_import_refuses_a_player_in_a_slot_they_did_not_tick(ctx, world):
    run, ids = world
    add_player(ctx, run, 60, ["tank"], 0, ["2026-11-02T20:00"])           # ticked another slot only
    draft = draft_for(ctx, run, ids)
    draft["input_fingerprint"] = fingerprint(build_signups(ctx.db, run, ctx.cfg)[0])
    draft["parties"][0]["members"][0]["player"] = "60"
    with pytest.raises(DraftError, match="did not tick"):
        import_draft(ctx.db, run, draft, ctx.cfg)


def test_import_will_not_overwrite_confirmed_parties(ctx, world):
    run, ids = world
    draft = draft_for(ctx, run, ids)
    import_draft(ctx.db, run, draft, ctx.cfg)
    with ctx.db.conn:
        ctx.db.conn.execute("UPDATE parties SET status = 'confirmed'")
    with pytest.raises(DraftConflict):
        import_draft(ctx.db, run, draft, ctx.cfg)


# ---------------------------------------------------------------- the real solver

@pytest.mark.skipif(not (SOLVER_DIR / "solve.py").exists(), reason="raid-solver not next to raid-bot")
def test_end_to_end_with_the_real_solver(ctx, world, tmp_path):
    """Export -> run raid-solver/solve.py as a subprocess -> import its draft."""
    run, ids = world
    data, _ = build_signups(ctx.db, run, ctx.cfg)
    signups = tmp_path / "signups.json"
    signups.write_text(json.dumps(data, indent=1))
    solver_cfg = json.loads((SOLVER_DIR / "config.json").read_text())
    solver_cfg["pass1"]["time_limit_s"] = 30
    solver_cfg["pass2"]["time_limit_s"] = 15
    cfg_path = tmp_path / "solver_config.json"
    cfg_path.write_text(json.dumps(solver_cfg))
    assert solver_cfg["phase_cross_cost"] == ctx.cfg["solver_phase_cross_cost"]   # the two configs must agree

    out = tmp_path / "draft.json"
    done = subprocess.run([sys.executable, str(SOLVER_DIR / "solve.py"), str(signups), "-o", str(out),
                           "--config", str(cfg_path)], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    draft = json.loads(out.read_text())
    assert draft["input_fingerprint"] == fingerprint(data)

    summary = import_draft(ctx.db, run, draft, ctx.cfg)
    assert summary["parties"] == 1 and summary["seated"] == 8
    assert len(ctx.db.parties(run["id"])) == 1
