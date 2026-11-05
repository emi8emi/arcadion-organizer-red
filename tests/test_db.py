import psycopg
import pytest

from bot.db import DB, DraftConflict, DuplicateRun, SchemaNotReady
import bot.db
from conftest import NOW, add_player, confirm, fill_one_party


def test_opening_twice_is_fine_and_syncs_the_fight_once(db_url):
    DB(db_url).close()
    db = DB(db_url)                                   # opening again changes nothing
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
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
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
    ctx.db.upsert_player(5, "Ann", roles=["melee", "main_tank"], prog_index=3, now=NOW)
    p = ctx.db.upsert_player(5, "Ann the Tank", timezone="America/Chicago", now=NOW)
    assert p["display_name"] == "Ann the Tank" and p["roles"] == "main_tank,melee"       # fixed role order
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
    roles = ["main_tank", "off_tank", "regen", "shield", "melee", "melee", "ranged", "caster"]
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
    add_player(ctx, old, 99, ["melee"], 0, ["2026-11-02T20:00"])     # ticked, never seated
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

def tables(url):
    with psycopg.connect(url, autocommit=True) as conn:
        return conn.execute("SELECT table_name FROM information_schema.tables "
                            "WHERE table_schema = current_schema() ORDER BY 1").fetchall()


def test_the_bot_refuses_a_database_nobody_migrated(unmigrated_url):
    with pytest.raises(SchemaNotReady, match=r"schema version 0 .*python -m app\.migrate"):
        DB(unmigrated_url)
    assert tables(unmigrated_url) == []                     # and created nothing


def test_the_bot_refuses_a_database_behind_the_version_it_needs(db_url, monkeypatch):
    monkeypatch.setattr(bot.db, "REQUIRED_SCHEMA_VERSION", bot.db.REQUIRED_SCHEMA_VERSION + 1)
    with pytest.raises(SchemaNotReady, match="needs 3"):
        DB(db_url)


def test_opening_a_migrated_database_changes_no_table(db_url):
    DB(db_url).close()
    before = tables(db_url)
    DB(db_url).close()
    assert tables(db_url) == before and ("schema_migrations",) in [tuple(r) for r in before]


def test_a_database_ahead_of_the_bot_is_fine(db_url):
    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute("INSERT INTO schema_migrations (version, name, checksum, applied_utc) VALUES (99, 'later', 'x', 'x')")
    DB(db_url).close()


def test_a_lost_connection_is_reopened_for_the_next_statement(ctx):
    ctx.db.conn._c.close()                                    # what a database restart or idle timeout looks like
    assert ctx.db.fights()[0]["short_name"] == "UWU"


def test_parse_role_accepts_the_usual_spellings():
    from bot.config import parse_role
    assert parse_role("Main tank") == "main_tank" and parse_role(" off-tank ") == "off_tank"
    assert parse_role("CASTER") == "caster" and parse_role("dps") is None and parse_role("") is None


def test_migration_0002_refuses_old_roles_and_then_accepts_the_new_ones(unmigrated_url, tmp_path):
    """A database with tank/healer/dps parties is not converted behind your back: it stops with a message."""
    import shutil
    from conftest import dashboard_migrate as mig
    folder = tmp_path / "only_0001"
    folder.mkdir()
    shutil.copy(mig.MIGRATIONS_DIR / "0001_baseline.sql", folder)
    with psycopg.connect(unmigrated_url, autocommit=True) as conn:
        assert mig.apply_migrations(conn, folder) == [1]
        conn.execute("INSERT INTO fights (id, name, short_name) VALUES (1, 'F', 'F')")
        conn.execute("INSERT INTO runs (guild_id, name, fight_id, start_date, end_date, signup_deadline_utc, status, "
                     "created_by, created_utc) VALUES ('1', 'r', 1, '2026-11-01', '2026-11-01', 'x', 'open', '1', 'x')")
        conn.execute("INSERT INTO slots (run_id, slot_key, start_utc, minutes) VALUES (1, 'k', 'x', 90)")
        conn.execute("INSERT INTO players (discord_id, display_name) VALUES ('9', 'Old')")
        conn.execute("INSERT INTO parties (run_id, slot_id, party_index, status, target_idx) VALUES (1, 1, 0, 'draft', 0)")
        conn.execute("INSERT INTO party_members (party_id, discord_id, role) VALUES (1, '9', 'dps')")
        with pytest.raises(mig.MigrationError, match="old tank/healer/dps roles"):
            mig.apply_migrations(conn)
        assert mig.current_version(conn) == 1                       # rolled back, nothing half-converted
        conn.execute("DELETE FROM parties")
        assert mig.apply_migrations(conn) == [2]
        conn.execute("INSERT INTO parties (run_id, slot_id, party_index, status, target_idx) VALUES (1, 1, 0, 'draft', 0)")
        conn.execute("INSERT INTO party_members (party_id, discord_id, role) VALUES (2, '9', 'caster')")
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("INSERT INTO party_members (party_id, discord_id, role) VALUES (2, '9', 'dps')")
