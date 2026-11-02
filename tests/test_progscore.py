"""Prog scorer glue: the two cleanup modes, checkpoint sync and the /debug commands (scorer stubbed)."""
from types import SimpleNamespace

from bot import handlers, progpoints, prog_scorer, progscore
from bot.fflogs import FFLogsError
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


def test_normal_mode_ignores_the_cleanup_tag(ctx):
    doc = doc_of(ctx)
    state = make_state(doc, [UP_TO_GAOLS] + [UP_TO_TITAN] * 4)         # Gaols reached once in five pulls
    assert progscore.failed_cleanup_points(state) == {9}
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


def test_sync_checkpoints_replaces_them_and_clears_prog(ctx):
    doc = doc_of(ctx)
    ctx.db.upsert_player(5, "P5", prog_index=3, now=NOW)
    assert not progscore.checkpoints_match(ctx.db, 1, doc)            # the 19 seeded rows differ from the file
    assert progscore.sync_checkpoints(ctx.db, 1, doc) == 1
    assert progscore.checkpoints_match(ctx.db, 1, doc)
    assert ctx.db.get_player(5)["prog_index"] is None


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
    progscore.sync_checkpoints(ctx.db, 1, doc_of(ctx))


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
    assert "from 0 to 19" in it.text and ctx.db.get_player(7)["prog_index"] == 12


def test_sync_needs_confirmation(ctx):
    ctx.db.upsert_player(7, "Ann", prog_index=3, now=NOW)
    it = organizer()
    run_async(handlers.debug_sync_checkpoints(ctx, it))
    assert "confirm" in it.text and ctx.db.get_player(7)["prog_index"] == 3
    it = organizer()
    run_async(handlers.debug_sync_checkpoints(ctx, it, confirm=True))
    assert ctx.db.get_player(7)["prog_index"] is None and len(ctx.db.checkpoints(1)) == 20


def stub_scorer(monkeypatch, reached_lists):
    async def fake(async_query, doc, state, name, encounter, **kw):
        fresh = make_state(doc, reached_lists)
        state.update(pulls=fresh["pulls"], highest=fresh["highest"], tags=fresh["tags"])
        return state
    monkeypatch.setattr(prog_scorer, "score_player_async", fake)


def test_score_refuses_until_checkpoints_match(ctx, monkeypatch):
    ctx.fflogs = object()
    add_char(ctx)
    it = organizer()
    run_async(handlers.debug_score(ctx, it, member()))
    assert "sync_checkpoints" in it.text


def test_score_sets_prog_in_each_mode(ctx, monkeypatch):
    synced(ctx)
    ctx.fflogs = SimpleNamespace(query=None)
    add_char(ctx)
    stub_scorer(monkeypatch, [UP_TO_GAOLS] + [UP_TO_TITAN] * 4)
    it = organizer()
    run_async(handlers.debug_score(ctx, it, member(), mode="normal"))
    assert ctx.db.get_player(7)["prog_index"] == 9 and "Gaols" in it.text
    it = organizer()
    run_async(handlers.debug_score(ctx, it, member(), mode="consistent"))
    assert ctx.db.get_player(7)["prog_index"] == 8
    assert "❌" in it.text                                             # the failed cleanup point is shown


def test_score_with_no_pulls_leaves_prog_alone(ctx, monkeypatch):
    synced(ctx)
    ctx.fflogs = SimpleNamespace(query=None)
    add_char(ctx)
    ctx.db.upsert_player(7, "Ann", prog_index=4, now=NOW)
    stub_scorer(monkeypatch, [])
    it = organizer()
    run_async(handlers.debug_score(ctx, it, member()))
    assert ctx.db.get_player(7)["prog_index"] == 4 and "left as it was" in it.text


def test_score_reports_fflogs_errors(ctx, monkeypatch):
    synced(ctx)
    ctx.fflogs = SimpleNamespace(query=None)
    add_char(ctx)

    async def boom(*a, **k):
        raise FFLogsError("HTTP 429 from FFLogs")
    monkeypatch.setattr(prog_scorer, "score_player_async", boom)
    it = organizer()
    run_async(handlers.debug_score(ctx, it, member()))
    assert "429" in it.text


def test_score_restarts_when_stored_state_is_stale(ctx, monkeypatch):
    synced(ctx)
    ctx.fflogs = SimpleNamespace(query=None)
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
    ctx.fflogs = SimpleNamespace(query=None)
    it = organizer()
    run_async(handlers.debug_score(ctx, it, member(99)))
    assert "no matching verified character" in it.text
