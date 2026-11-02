"""Prog scorer glue: the two cleanup modes, checkpoint sync and the /debug commands (scorer stubbed)."""
from types import SimpleNamespace

from bot import handlers, progpoints, prog_scorer, progscore
from bot.fflogs import FFLogsError
from bot.config import ROOT
from conftest import NOW, organizer, run_async


def fight(ctx):
    return ctx.db.fight(1)


def doc_of(ctx):
    return progscore.load_doc(ctx.cfg, fight(ctx))


def make_state(doc, pulls):
    """pulls: list of reached-idx lists, newest first."""
    state = prog_scorer.new_state(doc, "Ann Tank", doc["encounter_id"])
    for i, reached in enumerate(pulls):
        hi = max(reached)
        pt = next(p for p in doc["points"] if p["idx"] == hi)
        state["pulls"][f"R#{i}"] = {"highest": hi, "phase": pt["phase"], "name": pt["name"], "t": 1.0, "kill": False,
                                    "at": 1000 - i, "reached": reached}
    prog_scorer.refresh_highest(state)
    state["tags"] = progpoints.tag_report(doc, state["pulls"].values())
    return state


UP_TO_GAOLS = list(range(10))          # Garuda, Ifrit, Titan fresh and the Gaols cleanup point (idx 9)
UP_TO_TITAN = list(range(9))


def test_bundled_prog_points_are_valid(ctx):
    doc = doc_of(ctx)
    assert [p["idx"] for p in doc["points"]] == list(range(len(doc["points"])))
    assert doc["points"][-1]["name"] == "Clear" and doc["points"][-1]["idx"] == 20


def test_normal_mode_ignores_the_cleanup_tag(ctx):
    doc = doc_of(ctx)
    state = make_state(doc, [UP_TO_GAOLS] + [UP_TO_TITAN] * 4)         # Gaols reached once in five pulls
    assert progscore.failed_cleanup_points(doc, state) == {9}
    assert progscore.effective_highest(doc, state, "normal")["idx"] == 9


def test_consistent_mode_skips_failed_cleanup_points(ctx):
    doc = doc_of(ctx)
    state = make_state(doc, [UP_TO_GAOLS] + [UP_TO_TITAN] * 4)
    best = progscore.effective_highest(doc, state, "consistent")
    assert (best["idx"], best["name"]) == (8, "Fresh")


def test_consistent_mode_counts_cleanup_points_that_pass(ctx):
    doc = doc_of(ctx)
    state = make_state(doc, [UP_TO_GAOLS] * 5)
    assert progscore.effective_highest(doc, state, "consistent")["idx"] == 9


def test_points_past_a_failed_cleanup_point_still_count(ctx):
    doc = doc_of(ctx)
    past = [i for i in range(11) if i != 9]                            # reached Healer gaol without ever clearing Gaols
    state = make_state(doc, [past] * 5)
    assert progscore.effective_highest(doc, state, "consistent")["idx"] == 10


def test_no_pulls_means_nothing_reached(ctx):
    doc = doc_of(ctx)
    state = prog_scorer.new_state(doc, "Ann Tank", doc["encounter_id"])
    assert progscore.effective_highest(doc, state, "normal") is None
    assert progscore.effective_highest(doc, state, "consistent") is None


def test_remap_matches_by_phase_and_name_then_by_phase_position():
    old = [(0, "P1", "Fresh"), (1, "P1", "Plumes"), (2, "P2", "Fresh"), (3, "P2", "Enrage")]
    new = [(0, "Garuda", "Fresh"), (1, "Ifrit", "Fresh"), (2, "Ifrit", "Eruption"), (3, "Ifrit", "Enrage")]
    assert progscore.checkpoint_remap(old, old) == {0: 0, 1: 1, 2: 2, 3: 3}
    # renamed phases (same count): matched by position; Plumes has no counterpart so it falls back to Fresh
    assert progscore.checkpoint_remap(old, new) == {0: 0, 1: 0, 2: 1, 3: 3}
    inserted = [(0, "P1", "Fresh"), (1, "P1", "New point"), (2, "P1", "Plumes"), (3, "P2", "Fresh"), (4, "P2", "Enrage")]
    assert progscore.checkpoint_remap(old, inserted) == {0: 0, 1: 2, 2: 3, 3: 4}          # inserting shifts, nothing is lost


def test_startup_sync_moves_stored_numbers_instead_of_clearing_them(ctx):
    doc = doc_of(ctx)
    assert not progscore.checkpoints_match(ctx.db, 1, doc)            # the test DB has the small 19-point file
    ctx.db.upsert_player(5, "P5", prog_index=18, now=NOW)             # legacy "P4 Enrage"
    ctx.db.upsert_player(6, "P6", prog_index=11, now=NOW)             # legacy "P3 Gaols cleanup"
    ctx.db.upsert_player(7, "P7", now=NOW)                            # no prog stays no prog
    assert progscore.sync_fights(ctx.db, ROOT / "prog_points")[0]["changed"]
    assert progscore.checkpoints_match(ctx.db, 1, doc)
    assert ctx.db.get_player(5)["prog_index"] == 19                   # Ultima Enrage
    assert ctx.db.get_player(6)["prog_index"] == 9                    # Titan Gaols
    assert ctx.db.get_player(7)["prog_index"] is None
    assert progscore.sync_fights(ctx.db, ROOT / "prog_points")[0]["changed"] is False        # second start: nothing


def test_sync_moves_party_targets_snapshots_and_scorer_results(ctx, make_run):
    from conftest import fill_one_party
    run = make_run()
    fill_one_party(ctx, run, prog=18)
    ctx.db.conn.execute("INSERT INTO characters (discord_id, lodestone_id, name, world, verified_utc) VALUES ('1','9','A B','X','x')")
    ctx.db.conn.execute("INSERT INTO prog_scores (character_id, encounter, state, updated_utc, confirmed_idx, latest_idx) "
                        "VALUES (1, 1074, '{}', 'x', 18, 11)")
    ctx.db.conn.commit()
    progscore.sync_fights(ctx.db, ROOT / "prog_points")
    row = ctx.db.conn.execute("SELECT confirmed_idx, latest_idx FROM prog_scores").fetchone()
    assert (row[0], row[1]) == (19, 9)
    assert {r[0] for r in ctx.db.conn.execute("SELECT prog_index FROM players")} == {19}


def test_a_new_prog_point_file_creates_its_fight(ctx, tmp_path):
    import json
    doc = {"encounter": "TEA", "display_name": "The Epic of Alexander (Ultimate)", "encounter_id": 1062,
           "points": [{"phase": "P1", "name": "Fresh", "conditions": [{"type": "time", "gte": 1}]},
                      {"phase": "P2", "name": "Fresh", "conditions": [{"type": "time", "gte": 2}]}]}
    (tmp_path / "TEA.json").write_text(json.dumps(doc))
    (tmp_path / "broken.json").write_text("{ nope")                    # skipped, does not stop the others
    reports = progscore.sync_fights(ctx.db, tmp_path)
    assert len(reports) == 1 and reports[0]["created"]
    fight = ctx.db.find_fight("TEA")
    assert fight["name"] == "The Epic of Alexander (Ultimate)"
    assert [c["name"] for c in ctx.db.checkpoints(fight["id"])] == ["Fresh", "Fresh", "Clear"]
    assert len(ctx.db.checkpoints(1)) == 20                           # the other fight is untouched


def test_state_round_trip(ctx):
    doc = doc_of(ctx)
    ctx.db.upsert_player(1, "A", now=NOW)
    ctx.db.conn.execute("INSERT INTO characters (discord_id, lodestone_id, name, world, verified_utc) VALUES ('1','9','Ann Tank','Exodus','x')")
    cid = ctx.db.conn.execute("SELECT id FROM characters").fetchone()["id"]
    state = make_state(doc, [UP_TO_TITAN])
    progscore.save_state(ctx.db, cid, doc["encounter_id"], state, NOW)
    progscore.save_state(ctx.db, cid, doc["encounter_id"], state, NOW)       # upsert
    assert progscore.load_state(ctx.db, cid, doc["encounter_id"]) == state
    assert progscore.load_state(ctx.db, cid, 1) is None


# ---------------------------------------------------------------- commands

def member(uid=7, name="Ann"):
    return SimpleNamespace(id=uid, display_name=name)


def add_char(ctx, uid=7, name="Ann Tank", fflogs_id=900):
    ctx.db.upsert_player(uid, "Ann", now=NOW)
    ctx.db.conn.execute("INSERT INTO characters (discord_id, lodestone_id, name, world, fflogs_id, verified_utc) "
                        "VALUES (?, ?, ?, 'Exodus', ?, 'x')", (str(uid), f"L{uid}{name}", name, fflogs_id))
    ctx.db.conn.commit()


def synced(ctx):
    """Bring the test DB (which has the small 19-point test file) onto the real prog_points/UWU.json."""
    progscore.sync_fights(ctx.db, ROOT / "prog_points")


def test_debug_commands_are_for_organizers(ctx):
    from conftest import player
    it = player(2)
    run_async(handlers.debug_setprog(ctx, it, member(), 3))
    assert "Only organizers" in it.text


def test_setprog_sets_and_validates(ctx):
    synced(ctx)
    it = organizer()
    run_async(handlers.debug_setprog(ctx, it, member(), 12))
    assert ctx.db.get_player(7)["prog_index"] == 12 and "#12" in it.text
    it = organizer()
    run_async(handlers.debug_setprog(ctx, it, member(), 99))
    assert "from 0 to 20" in it.text and ctx.db.get_player(7)["prog_index"] == 12


def test_sync_command_reports_what_it_moved(ctx):
    ctx.db.upsert_player(7, "Ann", prog_index=18, now=NOW)
    it = organizer()
    run_async(handlers.debug_sync_checkpoints(ctx, it))
    assert "1 stored prog number" in it.text and ctx.db.get_player(7)["prog_index"] == 19
    assert len(ctx.db.checkpoints(1)) == 21
    it = organizer()
    run_async(handlers.debug_sync_checkpoints(ctx, it))
    assert "already match" in it.text


class FakeLogs:
    """Answers the recentReports query: one report per entry of `ages_h` (hours before NOW), newest first."""
    def __init__(self, ages_h=(1,)):
        from datetime import timedelta
        self.reports = [{"code": f"CODE{i}", "startTime": (NOW - timedelta(hours=h)).timestamp() * 1000}
                        for i, h in enumerate(ages_h)]

    async def query(self, graphql, variables):
        return {"characterData": {"character": {"recentReports": {"data": self.reports[:variables["limit"]],
                                                                  "has_more_pages": False}}}}


def stub_scorer(monkeypatch, reached_lists):
    async def fake(async_query, doc, state, name, encounter, **kw):
        fresh = make_state(doc, reached_lists)
        state.update(pulls=fresh["pulls"], highest=fresh["highest"], tags=fresh["tags"])
        return state
    monkeypatch.setattr(prog_scorer, "score_player_async", fake)


def test_score_refuses_until_checkpoints_match(ctx, monkeypatch):
    ctx.fflogs = FakeLogs()
    add_char(ctx)
    it = organizer()
    run_async(handlers.debug_score(ctx, it, member()))
    assert "sync_checkpoints" in it.text


def test_score_sets_prog_in_each_mode(ctx, monkeypatch):
    synced(ctx)
    ctx.fflogs = FakeLogs()
    add_char(ctx)
    stub_scorer(monkeypatch, [UP_TO_GAOLS] + [UP_TO_TITAN] * 4)
    it = organizer()
    run_async(handlers.debug_score(ctx, it, member(), mode="normal"))
    assert ctx.db.get_player(7)["prog_index"] == 9 and "Gaols" in it.text
    it = organizer()
    run_async(handlers.debug_score(ctx, it, member(), mode="consistent"))
    assert ctx.db.get_player(7)["prog_index"] == 9                      # never goes down
    assert "❌" in it.text                                             # the failed cleanup point is shown


def test_consistent_mode_alone_sets_the_lower_prog(ctx, monkeypatch):
    synced(ctx)
    ctx.fflogs = FakeLogs()
    add_char(ctx)
    stub_scorer(monkeypatch, [UP_TO_GAOLS] + [UP_TO_TITAN] * 4)
    run_async(handlers.debug_score(ctx, organizer(), member(), mode="consistent"))
    assert ctx.db.get_player(7)["prog_index"] == 8


def test_confirmed_prog_is_kept_and_latest_is_tracked(ctx, monkeypatch):
    synced(ctx)
    ctx.fflogs = FakeLogs()
    add_char(ctx)
    stub_scorer(monkeypatch, [UP_TO_GAOLS] * 5)
    run_async(handlers.debug_score(ctx, organizer(), member()))
    stub_scorer(monkeypatch, [list(range(4))])                          # a later, worse run
    it = organizer()
    run_async(handlers.debug_score(ctx, it, member(), reset=True))
    row = progscore.get_scores(ctx.db, 1, 1074)
    assert (row["confirmed_idx"], row["latest_idx"]) == (9, 3)
    assert row["confirmed_utc"] and row["latest_utc"]
    assert ctx.db.get_player(7)["prog_index"] == 9 and "stays at" in it.text


def test_manual_prog_above_the_scored_one_is_not_lowered(ctx, monkeypatch):
    synced(ctx)
    ctx.fflogs = FakeLogs()
    add_char(ctx)
    ctx.db.upsert_player(7, "Ann", prog_index=15, now=NOW)
    stub_scorer(monkeypatch, [UP_TO_TITAN])
    run_async(handlers.debug_score(ctx, organizer(), member()))
    assert ctx.db.get_player(7)["prog_index"] == 15


def test_default_fetch_reads_only_the_last_24_hours(ctx):
    ctx.fflogs = FakeLogs([1, 23, 30, 50])
    char = {"fflogs_id": 900, "name": "Ann Tank", "world": "Exodus"}
    assert run_async(progscore.recent_codes(ctx, char, 10, 24)) == ["CODE0", "CODE1"]
    assert len(run_async(progscore.recent_codes(ctx, char, 10, 0))) == 4        # no time limit
    assert run_async(progscore.recent_codes(ctx, char, 3, 0)) == ["CODE0", "CODE1", "CODE2"]


def test_no_reports_in_the_window_skips_the_scorer(ctx, monkeypatch):
    synced(ctx)
    ctx.fflogs = FakeLogs([60])
    add_char(ctx)

    async def never(*a, **k):
        raise AssertionError("scorer should not run")
    monkeypatch.setattr(prog_scorer, "score_player_async", never)
    it = organizer()
    run_async(handlers.debug_score(ctx, it, member()))
    assert "nothing in this window" in it.text


def test_cleanup_point_counts_when_the_next_point_was_reached_consistently(ctx):
    doc = doc_of(ctx)
    skipping = [i for i in range(11) if i != 9]                         # Healer gaol (10) in every pull, Gaols (9) never seen
    state = make_state(doc, [skipping] * 5)
    assert progscore.failed_cleanup_points(doc, state) == set()
    assert progscore.effective_highest(doc, state, "consistent")["idx"] == 10


def test_passing_on_once_or_twice_does_not_count(ctx):
    doc = doc_of(ctx)
    lucky = [i for i in range(11) if i != 9]                            # went past Gaols twice, never cleaned it
    state = make_state(doc, [lucky] * 2 + [UP_TO_TITAN] * 3)
    assert progscore.failed_cleanup_points(doc, state) == {9}
    best = progscore.effective_highest(doc, state, "consistent")
    assert best["idx"] == 8                                             # not Healer gaol (10)
    assert progscore.effective_highest(doc, state, "normal")["idx"] == 10


def test_failed_cleanup_only_blocks_characters_below_it(ctx):
    doc = doc_of(ctx)
    lucky = [i for i in range(11) if i != 9]
    state = make_state(doc, [lucky] * 2 + [UP_TO_TITAN] * 3)
    assert progscore.effective_highest(doc, state, "consistent", current=8)["idx"] == 8
    assert progscore.effective_highest(doc, state, "consistent", current=9)["idx"] == 10   # already past Gaols
    assert progscore.effective_highest(doc, state, "consistent", current=None)["idx"] == 8


def test_the_clear_point_checks_for_a_kill(ctx):
    doc = doc_of(ctx)
    clear = doc["points"][-1]
    assert [c["type"] for c in clear["conditions"]] == ["kill"]
    assert progpoints.point_time(clear, {"kill": True, "duration": 812.5}) == 812.5
    assert progpoints.point_time(clear, {"kill": False, "duration": 700}) is None
    assert progscore.with_clear(doc) is doc                              # added once only


def test_score_with_no_pulls_leaves_prog_alone(ctx, monkeypatch):
    synced(ctx)
    ctx.fflogs = FakeLogs()
    add_char(ctx)
    ctx.db.upsert_player(7, "Ann", prog_index=4, now=NOW)
    stub_scorer(monkeypatch, [])
    it = organizer()
    run_async(handlers.debug_score(ctx, it, member()))
    assert ctx.db.get_player(7)["prog_index"] == 4 and "stays at" in it.text


def test_score_reports_fflogs_errors(ctx, monkeypatch):
    synced(ctx)
    ctx.fflogs = FakeLogs()
    add_char(ctx)

    async def boom(*a, **k):
        raise FFLogsError("HTTP 429 from FFLogs")
    monkeypatch.setattr(prog_scorer, "score_player_async", boom)
    it = organizer()
    run_async(handlers.debug_score(ctx, it, member()))
    assert "429" in it.text


def test_score_restarts_when_stored_state_is_stale(ctx, monkeypatch):
    synced(ctx)
    ctx.fflogs = FakeLogs()
    add_char(ctx)
    doc = doc_of(ctx)
    old = make_state(doc, [UP_TO_TITAN])
    old["points_hash"] = "stale"
    cid = ctx.db.conn.execute("SELECT id FROM characters").fetchone()["id"]
    progscore.save_state(ctx.db, cid, doc["encounter_id"], old, NOW)
    stub_scorer(monkeypatch, [UP_TO_TITAN])
    it = organizer()
    run_async(handlers.debug_score(ctx, it, member()))
    assert "Started over" in it.text


def test_score_needs_a_character_and_fflogs(ctx):
    synced(ctx)
    it = organizer()
    run_async(handlers.debug_score(ctx, it, member()))
    assert "not configured" in it.text
    ctx.fflogs = FakeLogs()
    it = organizer()
    run_async(handlers.debug_score(ctx, it, member(99)))
    assert "no matching verified character" in it.text
