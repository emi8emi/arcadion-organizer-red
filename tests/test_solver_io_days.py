"""Per-day export and import: scope, carry-over inside a run, stale drafts, and the real solver."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from bot.db import DraftConflict
from bot.solver_io import DraftError, build_signups, fingerprint, import_draft
from conftest import S2, add_party_players, add_player, draft_for_day

SOLVER_DIR = Path(__file__).resolve().parent.parent.parent / "raid-solver"
D1, D2, D3 = "2026-11-01", "2026-11-02", "2026-11-03"
S1_ = "2026-11-01T17:00"
S3_ = "2026-11-03T17:00"


@pytest.fixture
def week(ctx, make_run):
    """Run over Nov 1-3. The same eight players ticked a slot on every day."""
    run = make_run()
    ids = add_party_players(ctx, run, [S1_, S2, S3_])
    return run, ids


def seat(ctx, run, day, slot, ids):
    import_draft(ctx.db, run, draft_for_day(ctx, run, day, slot, ids), ctx.cfg, day)


def test_export_contains_only_that_days_slots_and_players(ctx, make_run):
    run = make_run()
    add_player(ctx, run, 1, ["tank"], 0, [S1_])
    add_player(ctx, run, 2, ["dps"], 0, [S2])
    data, _ = build_signups(ctx.db, run, ctx.cfg, D2)
    assert {s["id"][:10] for s in data["slots"]} == {D2} and len(data["slots"]) == 11
    assert [p["id"] for p in data["players"]] == ["2"] and data["players"][0]["available"] == [S2]


def test_fingerprint_is_deterministic_and_differs_by_day(ctx, week):
    run, _ = week
    a, _ = build_signups(ctx.db, run, ctx.cfg, D2)
    b, _ = build_signups(ctx.db, run, ctx.cfg, D2)
    c, _ = build_signups(ctx.db, run, ctx.cfg, D3)
    assert fingerprint(a) == fingerprint(b) != fingerprint(c)


def test_changing_another_days_ticks_does_not_change_this_days_export(ctx, week):
    run, _ = week
    before = fingerprint(build_signups(ctx.db, run, ctx.cfg, D2)[0])
    ctx.db.set_day_availability(run["id"], 1, D3, [])
    assert fingerprint(build_signups(ctx.db, run, ctx.cfg, D2)[0]) == before


def test_prog_is_read_at_export_time(ctx, week):
    run, _ = week
    assert {p["prog"] for p in build_signups(ctx.db, run, ctx.cfg, D2)[0]["players"]} == {0}
    ctx.db.upsert_player(1, "Player 1", prog_index=7)
    assert build_signups(ctx.db, run, ctx.cfg, D2)[0]["players"][0]["prog"] == 7


def test_per_run_cap_is_reduced_by_parties_on_earlier_days(ctx, week):
    run, ids = week
    ctx.db.set_limits(run["id"], 1, ctx.cfg, max_per_run=2)
    seat(ctx, run, D1, S1_, ids)
    p1 = {p["id"]: p for p in build_signups(ctx.db, run, ctx.cfg, D2)[0]["players"]}
    assert p1["1"]["max_per_run"] == 1 and p1["2"]["max_per_run"] == 4 - 1
    seat(ctx, run, D2, S2, ids)
    data, warnings = build_signups(ctx.db, run, ctx.cfg, D3)
    assert "1" not in {p["id"] for p in data["players"]}
    assert len(warnings) == 1 and "Player 1" in warnings[0] and "max is 2" in warnings[0]


def test_parties_on_the_same_or_later_days_do_not_reduce_the_cap(ctx, week):
    run, ids = week
    seat(ctx, run, D3, S3_, ids)
    assert {p["max_per_run"] for p in build_signups(ctx.db, run, ctx.cfg, D2)[0]["players"]} == {4}
    assert {p["max_per_run"] for p in build_signups(ctx.db, run, ctx.cfg, D3)[0]["players"]} == {4}


def test_unmatched_flag_comes_from_the_previous_day_with_a_draft(ctx, week):
    run, ids = week
    add_player(ctx, run, 99, ["dps"], 0, [S1_, S2])          # ticked day 1, never seated
    seat(ctx, run, D1, S1_, ids)
    flags = {p["id"]: p["unmatched_last_run"] for p in build_signups(ctx.db, run, ctx.cfg, D2)[0]["players"]}
    assert flags["99"] is True and flags["1"] is False


def test_unmatched_flag_falls_back_to_the_previous_run(ctx, make_run):
    old = make_run("Old", status="closed")
    ids = add_party_players(ctx, old, [S2])
    add_player(ctx, old, 99, ["dps"], 0, ["2026-11-02T20:00"])
    seat(ctx, old, D2, S2, ids)
    new = make_run("New")
    add_player(ctx, new, 99, ["dps"], 0, [S2])
    flags = {p["id"]: p["unmatched_last_run"] for p in build_signups(ctx.db, new, ctx.cfg, D2)[0]["players"]}
    assert flags == {"99": True}


# ---------------------------------------------------------------- import

def test_import_replaces_only_that_days_draft(ctx, week):
    run, ids = week
    seat(ctx, run, D1, S1_, ids)
    seat(ctx, run, D2, S2, ids)
    seat(ctx, run, D2, S2, ids)                                  # again: replaced, not duplicated
    keys = sorted(p["slot_key"] for p in ctx.db.parties(run["id"]))
    assert keys == [S1_, S2]


def test_import_refuses_slots_from_another_day(ctx, week):
    run, ids = week
    draft = draft_for_day(ctx, run, D2, S2, ids)
    draft["parties"][0]["slot"] = S3_
    with pytest.raises(DraftError, match="different day"):
        import_draft(ctx.db, run, draft, ctx.cfg, D2)
    assert ctx.db.parties(run["id"]) == []


def test_import_refuses_a_stale_day_draft(ctx, week):
    run, ids = week
    draft = draft_for_day(ctx, run, D2, S2, ids)
    ctx.db.set_day_availability(run["id"], 8, D2, [])
    with pytest.raises(DraftError, match="different signups"):
        import_draft(ctx.db, run, draft, ctx.cfg, D2)


def test_a_draft_for_one_day_is_refused_for_another(ctx, week):
    run, ids = week
    draft = draft_for_day(ctx, run, D2, S2, ids)
    with pytest.raises(DraftError):
        import_draft(ctx.db, run, draft, ctx.cfg, D3)


def test_conflict_is_scoped_to_the_day(ctx, week):
    run, ids = week
    seat(ctx, run, D1, S1_, ids)
    with ctx.db.conn:
        ctx.db.conn.execute("UPDATE parties SET status = 'confirmed'")
    seat(ctx, run, D2, S2, ids)                                  # another day is still importable
    with pytest.raises(DraftConflict):
        seat(ctx, run, D1, S1_, ids)
    assert len(ctx.db.parties(run["id"])) == 2


def test_import_summary_names_the_day(ctx, week):
    run, ids = week
    s = import_draft(ctx.db, run, draft_for_day(ctx, run, D2, S2, ids), ctx.cfg, D2)
    assert s["day"] == D2 and s["parties"] == 1 and s["seated"] == 8


# ---------------------------------------------------------------- the real solver, one day

@pytest.mark.skipif(not (SOLVER_DIR / "solve.py").exists(), reason="raid-solver not next to raid-bot")
def test_end_to_end_one_day_with_the_real_solver(ctx, week, tmp_path):
    run, ids = week
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
    summary = import_draft(ctx.db, run, json.loads(out.read_text()), ctx.cfg, D2)
    assert summary["parties"] == 1 and summary["seated"] == 8
    assert [p["slot_key"] for p in ctx.db.parties(run["id"])] == [S2]
