"""bot/seed.py: fake test players so one person can run the whole flow alone."""
import json
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bot import seed
from bot.solver_io import build_signups, import_draft
from conftest import NOW, SLOT, add_player

SOLVER_DIR = Path(__file__).resolve().parent.parent.parent / "raid-solver"
SOLO_RUN = dict(start=datetime(2026, 11, 2).date(), end=datetime(2026, 11, 2).date())


@pytest.fixture
def solo(make_run):
    """One day, one slot (17:00), like the live-test checklist."""
    return make_run(name="test1", earliest="17:00", latest="17:00", **SOLO_RUN)


def roles_of(ctx, ids):
    return Counter(ctx.db.get_player(i)["roles"] for i in ids)


def test_default_seeds_seven_fakes_that_leave_a_dps_seat_for_you(ctx, solo):
    report = seed.seed(ctx.db, ctx.cfg, solo, now=NOW)
    ids = [f["id"] for f in report["fakes"]]
    assert len(ids) == 7 and all(seed.is_fake(i) for i in ids)
    assert roles_of(ctx, ids) == {"tank": 2, "healer": 2, "dps": 3}
    assert [f["name"] for f in report["fakes"]][:3] == ["Test Tank 1", "Test Tank 2", "Test Healer 1"]
    assert all(ctx.db.get_player(i)["prog_index"] == 0 for i in ids)
    assert all(ctx.db.ticked_keys(solo["id"], i) == [SLOT] for i in ids)
    assert report["slots_ticked"] == 1 and report["you"] == "dps" and report["warnings"] == []


def test_the_seat_you_take_changes_the_fakes(ctx, solo):
    report = seed.seed(ctx.db, ctx.cfg, solo, you="tank", now=NOW)
    assert roles_of(ctx, [f["id"] for f in report["fakes"]]) == {"tank": 1, "healer": 2, "dps": 4}


def test_larger_counts_add_whole_parties(ctx, solo):
    report = seed.seed(ctx.db, ctx.cfg, solo, count=15, now=NOW)
    assert Counter(f["role"] for f in report["fakes"]) == {"tank": 4, "healer": 4, "dps": 7}


def test_one_helper_by_default_and_the_count_follows_the_option(ctx, solo):
    seed.seed(ctx.db, ctx.cfg, solo, now=NOW)
    assert len(ctx.db.helper_ids(solo["fight_id"])) == 1
    seed.seed(ctx.db, ctx.cfg, solo, helpers=2, now=NOW)
    assert len(ctx.db.helper_ids(solo["fight_id"])) == 2
    seed.seed(ctx.db, ctx.cfg, solo, helpers=0, now=NOW)            # running again revokes the extras
    assert ctx.db.helper_ids(solo["fight_id"]) == set()


def test_running_twice_does_not_duplicate_anything(ctx, solo):
    seed.seed(ctx.db, ctx.cfg, solo, now=NOW)
    seed.seed(ctx.db, ctx.cfg, solo, now=NOW)
    assert ctx.db.conn.execute("SELECT COUNT(*) FROM players").fetchone()[0] == 7
    assert ctx.db.conn.execute("SELECT COUNT(*) FROM availability").fetchone()[0] == 7


def test_slot_option_ticks_only_that_slot(ctx, make_run):
    run = make_run(name="week", start=datetime(2026, 11, 2).date(), end=datetime(2026, 11, 2).date())
    report = seed.seed(ctx.db, ctx.cfg, run, slots=[SLOT, "2026-11-02T18:30"], now=NOW)
    assert report["slots_ticked"] == 2
    assert ctx.db.ticked_keys(run["id"], report["fakes"][0]["id"]) == [SLOT, "2026-11-02T18:30"]


def test_unknown_slot_prog_role_and_count_are_refused(ctx, solo):
    for kwargs, text in [({"slots": ["2030-01-01T17:00"]}, "Not slots"), ({"prog": 99}, "--prog"),
                         ({"you": "bard"}, "--you"), ({"count": 0}, "--count")]:
        with pytest.raises(seed.SeedError, match=text):
            seed.seed(ctx.db, ctx.cfg, solo, now=NOW, **kwargs)


def test_a_day_with_a_snapshot_is_skipped_with_a_warning(ctx, solo):
    ctx.db.take_snapshot(solo["id"], "2026-11-02", ctx.cfg, NOW)
    report = seed.seed(ctx.db, ctx.cfg, solo, now=NOW)
    assert report["skipped_days"] == ["2026-11-02"] and report["slots_ticked"] == 0
    assert any("snapshot" in w for w in report["warnings"])
    assert ctx.db.ticked_keys(solo["id"], report["fakes"][0]["id"]) == []


def test_a_passed_cutoff_without_a_snapshot_warns_that_it_will_be_late(ctx, solo):
    later = datetime(2026, 11, 1, tzinfo=timezone.utc)               # after the shared 2026-10-30 22:00 UTC cutoff
    report = seed.seed(ctx.db, ctx.cfg, solo, now=later)
    assert any("taken late" in w for w in report["warnings"])


def test_a_draft_run_gets_a_warning(ctx, make_run):
    run = make_run(name="d", status="draft", **SOLO_RUN)
    assert any("still a draft" in w for w in seed.seed(ctx.db, ctx.cfg, run, now=NOW)["warnings"])


def test_remove_deletes_fakes_everywhere_and_keeps_real_players(ctx, solo):
    add_player(ctx, solo, 42, ["dps"], 0, [SLOT])                    # a real player
    report = seed.seed(ctx.db, ctx.cfg, solo, now=NOW)
    fake = report["fakes"][0]["id"]
    ctx.db.set_limits(solo["id"], fake, ctx.cfg, max_per_day=1)
    ctx.db.conn.execute("INSERT INTO votes (run_id, slot_id, voter_id, target_id, vote, created_utc) "
                        "SELECT ?, id, '42', ?, 'like', ? FROM slots WHERE run_id = ? LIMIT 1",
                        (solo["id"], fake, NOW.isoformat(), solo["id"]))
    ctx.db.conn.commit()
    deleted = seed.remove(ctx.db)
    assert deleted["players"] == 7 and deleted["availability"] == 7 and deleted["votes"] == 1
    assert deleted["limits"] == 1 and deleted["helper_grants"] == 1
    left = [r["discord_id"] for r in ctx.db.conn.execute("SELECT discord_id FROM players")]
    assert left == ["42"] and ctx.db.ticked_keys(solo["id"], 42) == [SLOT]
    assert seed.remove(ctx.db) == {}                                  # nothing left to remove
    assert ctx.db.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_is_fake_never_matches_real_looking_ids():
    assert seed.is_fake(seed.fake_id(1)) and seed.is_fake(seed.fake_id(9999))
    assert not any(seed.is_fake(x) for x in (42, "42", 1234567890123456789, 900000000000000001, "abc", seed.FAKE_MAX + 1))


def test_find_run_errors_are_readable(ctx, solo):
    assert seed.find_run(ctx.db, "TEST1")["id"] == solo["id"]         # names are case-insensitive
    with pytest.raises(seed.SeedError, match="No run called"):
        seed.find_run(ctx.db, "nope")


def test_you_plus_the_fakes_make_one_full_party_in_the_export(ctx, solo):
    seed.seed(ctx.db, ctx.cfg, solo, now=NOW)
    add_player(ctx, solo, 42, ["dps"], 0, [SLOT], name="You")
    data, warnings = build_signups(ctx.db, solo, ctx.cfg, day="2026-11-02")
    assert warnings == []
    assert Counter(r for p in data["players"] for r in p["roles"]) == {"tank": 2, "healer": 2, "dps": 4}
    assert sum(1 for p in data["players"] if p["helper"]) == 1


@pytest.mark.skipif(not (SOLVER_DIR / "solve.py").exists(), reason="raid-solver not next to raid-bot")
def test_the_real_solver_builds_a_party_from_the_fakes_and_one_real_player(ctx, solo, tmp_path):
    seed.seed(ctx.db, ctx.cfg, solo, now=NOW)
    add_player(ctx, solo, 42, ["dps"], 0, [SLOT], name="You")
    data, _ = build_signups(ctx.db, solo, ctx.cfg, day="2026-11-02")
    signups = tmp_path / "signups.json"
    signups.write_text(json.dumps(data))
    solver_cfg = json.loads((SOLVER_DIR / "config.json").read_text())
    solver_cfg["pass1"]["time_limit_s"], solver_cfg["pass2"]["time_limit_s"] = 20, 10
    cfg_path = tmp_path / "solver_config.json"
    cfg_path.write_text(json.dumps(solver_cfg))
    out = tmp_path / "draft.json"
    done = subprocess.run([sys.executable, str(SOLVER_DIR / "solve.py"), str(signups), "-o", str(out),
                           "--config", str(cfg_path)], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    summary = import_draft(ctx.db, solo, json.loads(out.read_text()), ctx.cfg, day="2026-11-02")
    assert summary["parties"] == 1 and summary["seated"] == 8


def test_cli_seeds_and_removes_in_a_real_database_file(tmp_path, capsys, monkeypatch):
    """The command line path, against a file database (the one the bot would use)."""
    from bot.db import DB
    from bot.grid import GridConfig, run_slots
    from bot.config import load_config
    cfg = load_config(env={})
    path = str(tmp_path / "cli.sqlite3")
    db = DB(path)
    slots = run_slots(datetime(2026, 11, 2).date(), datetime(2026, 11, 2).date(), GridConfig.from_config(cfg), "17:00", "17:00")
    db.create_run(guild_id=1, name="cli", fight_id=1, start_date=datetime(2026, 11, 2).date(),
                  end_date=datetime(2026, 11, 2).date(),
                  cutoffs={"2026-11-02": datetime(2030, 1, 1, tzinfo=timezone.utc)}, slots=slots, created_by=1, now=NOW)
    db.close()
    assert seed.main(["--run", "cli", "--db", path]) == 0
    out = capsys.readouterr().out
    assert "Seeded 7 fake players" in out and "pick dps" in out
    assert seed.main(["--run", "missing", "--db", path]) == 1
    assert "No run called" in capsys.readouterr().err
    assert seed.main(["--remove", "--db", path]) == 0
    assert "Removed fake players" in capsys.readouterr().out
    assert seed.main(["--db", path]) == 1                              # needs --run or --remove
