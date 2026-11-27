"""Editing party compositions: the operations core, short parties, Party Finder seats and the staged slot editor."""
import json
from types import SimpleNamespace

from bot import confirmations, dashboard, party_edit, party_views, slot_editor
from bot.confirm_views import ConfirmView
from bot.solver_io import build_signups, fingerprint, import_draft
from conftest import add_party_players, organizer, player, run_async
from test_approve import D2, S2, approve, attach
from test_party_actions import Channel, press_apply

ROLES_8 = ["main_tank", "off_tank", "regen", "shield", "melee", "melee", "ranged", "caster"]


def two_parties(ctx, make_run):
    """An approved slot with two parties: players 1-8 and 9-16. Returns (run, slot id, [party ids], thread, channel)."""
    run = make_run(per_day=True)
    first = add_party_players(ctx, run, [S2], first_id=1)
    second = add_party_players(ctx, run, [S2], first_id=9)
    data, _ = build_signups(ctx.db, run, ctx.cfg, D2, ctx.now())
    draft = {"version": 1, "input_fingerprint": fingerprint(data), "unfilled_slots": [], "unmatched_players": [],
             "parties": [{"slot": S2, "party": n, "target": {"index": 0, "name": "P1 Fresh"},
                          "members": [{"player": i, "role": r, "helper": False, "placed_down": False, "distance": 0}
                                      for i, r in zip(ids, ROLES_8)]} for n, ids in enumerate((first, second))]}
    import_draft(ctx.db, run, draft, ctx.cfg, D2)
    chan = Channel()
    attach(ctx, chan)
    approve(ctx, None, D2)
    ps = ctx.db.parties(run["id"])
    return run, ps[0]["slot_id"], [p["id"] for p in ps], chan.threads[7000], chan


def members(ctx, pid):
    return {m["discord_id"]: m["role"] for m in ctx.db.party_members(pid)}


def swap_op(pid, out, into, code="no_show", text=""):
    return {"op": "swap", "party": pid, "out": out, "in": into, "reason": {"code": code, "text": text}}


def apply(ctx, slot_id, ops, **kw):
    return run_async(party_edit.apply(ctx, 100, slot_id, ops, 500, **kw))


# ---------------------------------------------------------------- planning (nothing is written)

def test_a_plan_changes_nothing_and_reports_every_error(ctx, make_run, clock):
    run, slot, (a, b), thread, chan = two_parties(ctx, make_run)
    plan = party_edit.plan(ctx, slot, [
        {"op": "move", "player": "1", "to": b},                         # party 2 is full
        {"op": "remove", "party": a, "player": "2"},                    # no reason
        {"op": "swap", "party": a, "out": "99", "in": "3", "reason": {"code": "no_show"}},   # 99 is not in it
        {"op": "bogus"}])
    text = " ".join(plan.errors)
    assert "is full. Use trade" in text and "pick why they are leaving" in text and "is not in party 1" in text \
        and "unknown operation" in text
    assert members(ctx, a)["1"] == "main_tank" and len(members(ctx, a)) == 8


def test_trade_keeps_roles_and_move_needs_room(ctx, make_run, clock):
    run, slot, (a, b), thread, chan = two_parties(ctx, make_run)
    plan = party_edit.plan(ctx, slot, [{"op": "trade", "a": "1", "b": "10"}])
    assert not plan.errors and plan.after[a]["members"]["10"]["role"] == "off_tank"
    assert plan.after[b]["members"]["1"]["role"] == "main_tank" and "1" not in plan.after[a]["members"]
    same = party_edit.plan(ctx, slot, [{"op": "trade", "a": "1", "b": "2"}])
    assert "two different parties" in same.errors[0]
    freed = party_edit.plan(ctx, slot, [{"op": "remove", "party": b, "player": "9", "reason": {"code": "no_show"}},
                                        {"op": "move", "player": "1", "to": b}])
    assert not freed.errors and "1" in freed.after[b]["members"] and not freed.warnings[:0]
    assert all("already in party" not in w for w in freed.warnings)        # moving inside a slot is not double booking


def test_nobody_can_be_seated_twice_and_pf_seats_need_room(ctx, make_run, clock):
    run, slot, (a, b), thread, chan = two_parties(ctx, make_run)
    assert "already in a party of this slot" in party_edit.plan(ctx, slot, [swap_op(a, "1", "10")]).errors[0]
    assert "is full" in party_edit.plan(ctx, slot, [{"op": "pf_add", "party": a, "role": "melee"}]).errors[0]
    ok = party_edit.plan(ctx, slot, [{"op": "remove", "party": a, "player": "5", "reason": {"code": "no_show"}},
                                     {"op": "pf_add", "party": a, "role": "melee", "name": " Some Body "}])
    assert not ok.errors and ok.after[a]["pf"] == [{"id": None, "role": "melee", "name": "Some Body"}]
    assert party_edit.short_by(ctx, ok.after[a]) == 0


# ---------------------------------------------------------------- applying

def test_removing_a_player_leaves_the_party_short_and_flagged(ctx, make_run, clock):
    run, slot, (a, b), thread, chan = two_parties(ctx, make_run)
    sent_before = len(chan.sent) if getattr(chan, "sent", None) else 0
    result, ids, problems = apply(ctx, slot, [{"op": "remove", "party": a, "player": "8",
                                               "reason": {"code": "cant_make_it", "text": ""}}])
    assert not result.errors and not problems
    assert "8" not in members(ctx, a) and len(members(ctx, a)) == 7
    assert "8" in ctx.db.standby_pool(slot)
    change = ctx.db.change(ids[0])
    assert change["kind"] == "remove" and change["player_id"] == "8" and change["reason_code"] == "cant_make_it"
    notice = chan.sent[sent_before:][-1]
    assert "<@8> you were taken out of party 1" in notice["content"] and notice["view"] is not None
    assert any("A Caster seat is open" in (m["content"] or "") for m in thread.sent)
    text = confirmations.prompt_text(ctx, ctx.db.run(run["id"]), ctx.db.conn.execute(
        "SELECT * FROM slots WHERE id = ?", (slot,)).fetchone())
    assert "⚠ Short by 1" in text
    rows = run_async(dashboard.collect(ctx, ctx.db.run(run["id"]), D2))
    issues = [t for r in rows for _, t in r["issues"]]
    assert any("7 of 8 seats filled" in t and "Edit composition" in t for t in issues)


def test_the_removed_player_can_read_why(ctx, make_run, clock):
    run, slot, (a, b), thread, chan = two_parties(ctx, make_run)
    _, ids, _ = apply(ctx, slot, [{"op": "remove", "party": a, "player": "8",
                                   "reason": {"code": "other", "text": "Needed the seat"}}])
    it = player(8)
    run_async(party_views.show_reason(ctx, it, ids[0]))
    assert "taken out of the party" in it.text and "Needed the seat" in it.text
    other = player(3)
    run_async(party_views.show_reason(ctx, other, ids[0]))
    assert "not about you" in other.text


def test_party_finder_seats_are_tracked(ctx, make_run, clock):
    run, slot, (a, b), thread, chan = two_parties(ctx, make_run)
    apply(ctx, slot, [{"op": "remove", "party": a, "player": "8", "reason": {"code": "no_show"}}])
    result, ids, problems = apply(ctx, slot, [{"op": "pf_add", "party": a, "role": "caster", "name": "Void Walker"}])
    assert not result.errors
    [seat] = ctx.db.party_pf(a)
    assert seat["role"] == "caster" and seat["name"] == "Void Walker" and seat["added_by"] == "500"
    assert ctx.db.change(ids[0])["kind"] == "pf_add"
    assert len(members(ctx, a)) == 7 and party_edit.short_by(ctx, party_edit.load(ctx, slot)[a]) == 0
    row = run_async(dashboard.collect(ctx, ctx.db.run(run["id"]), D2))[0]
    assert not any("seats filled" in t for _, t in row["issues"])                    # nothing short
    assert "Party Finder · Caster · Void Walker" in dashboard.detail_text(ctx.db.run(run["id"]), row)
    slot_row = ctx.db.conn.execute("SELECT * FROM slots WHERE id = ?", (slot,)).fetchone()
    assert "Party Finder · Caster · Void Walker" in confirmations.prompt_text(ctx, ctx.db.run(run["id"]), slot_row)
    again = apply(ctx, slot, [{"op": "pf_remove", "party": a, "pf": 0}])[0]
    assert not again.errors and ctx.db.party_pf(a) == []
    assert [c["kind"] for c in ctx.db.party_changes(a)][-3:] == ["remove", "pf_add", "pf_remove"]


def test_move_and_trade_keep_everyone_in_the_slot(ctx, make_run, clock):
    run, slot, (a, b), thread, chan = two_parties(ctx, make_run)
    thread_before = len(thread.sent)
    result, ids, problems = apply(ctx, slot, [{"op": "trade", "a": "1", "b": "9"}])
    assert not result.errors and not problems
    assert members(ctx, a)["9"] == "main_tank" and members(ctx, b)["1"] == "main_tank"
    assert ctx.db.standby_pool(slot) == []                                  # nobody left the slot
    assert [c["kind"] for c in ctx.db.party_changes(b)][-1] == "move"
    said = [m["content"] for m in thread.sent[thread_before:] if m["content"]]
    assert any("<@1> moves from party 1 to party 2" in t and "<@9> moves from party 2 to party 1" in t for t in said)


def test_one_apply_sends_one_notice_per_affected_player(ctx, make_run, clock):
    from conftest import add_player
    run, slot, (a, b), thread, chan = two_parties(ctx, make_run)
    add_player(ctx, run, 30, ["caster"], 0, [S2])
    add_player(ctx, run, 31, ["melee"], 0, [S2])
    chan.sent = getattr(chan, "sent", [])
    n = len(chan.sent)
    result, ids, problems = apply(ctx, slot, [swap_op(a, "8", "30"), swap_op(b, "13", "31"),
                                              {"op": "role", "party": a, "player": "7", "role": "caster"}])
    assert not result.errors and len(ids) == 3
    notices = chan.sent[n:]
    assert len(notices) == 2 and "<@8>" in notices[0]["content"] and "<@13>" in notices[1]["content"]
    joined = [m for m in thread.sent if m["content"] and "joins party" in m["content"]]
    assert len(joined) == 1 and "<@30> joins party 1" in joined[0]["content"] and "<@31> joins party 2" in joined[0]["content"]
    assert members(ctx, a)["7"] == "caster" and "30" in members(ctx, a)


def test_a_stale_edit_is_refused_and_nothing_is_written(ctx, make_run, clock):
    run, slot, (a, b), thread, chan = two_parties(ctx, make_run)
    result, ids, problems = apply(ctx, slot, [swap_op(a, "1", "30"), swap_op(a, "1", "31")])
    assert result.errors and ids == [] and "1" in members(ctx, a)


# ---------------------------------------------------------------- the slot editor

def editor(ctx, slot):
    return slot_editor.SlotEditView(ctx, slot, 500, 100)


def pick(view, select, value):
    select._values = [value]
    it = organizer(user_id=500)
    run_async(select.callback(it))
    return it


def click(view, kind):
    it = organizer(user_id=500)
    run_async(next(c for c in view.children if isinstance(c, kind)).callback(it))
    return it


def test_the_editor_stages_previews_undoes_and_applies(ctx, make_run, clock):
    run, slot, (a, b), thread, chan = two_parties(ctx, make_run)
    view = editor(ctx, slot)
    assert "Party 1" in view.render() and "Party 2" in view.render() and "Standby: nobody" in view.render()
    pick(view, view.action_select, "remove")
    pick(view, view.subject_select, "8")
    modal = slot_editor.ReasonModal(view)
    modal.text._value = ""
    it = organizer(user_id=500)
    run_async(modal.on_submit(it))
    assert len(view.ops) == 1 and "⚠ Short by 1: 1 Caster" in view.render() and "nothing applied yet" in view.render()
    assert len(members(ctx, a)) == 8                                           # still only a preview
    click(view, slot_editor.UndoButton)
    assert view.ops == [] and "Short by" not in view.render()
    pick(view, view.action_select, "remove")
    pick(view, view.subject_select, "8")
    modal = slot_editor.ReasonModal(view)
    modal.text._value = ""
    run_async(modal.on_submit(organizer(user_id=500)))
    assert len(view.ops) == 1
    done = press_apply(view)
    assert "was taken out of party 1" in done.text and view.ops == []
    assert len(members(ctx, a)) == 7 and "⚠ Short by 1" in view.render()


def test_the_editor_fills_the_gap_with_a_party_finder_player(ctx, make_run, clock):
    run, slot, (a, b), thread, chan = two_parties(ctx, make_run)
    view = editor(ctx, slot)
    view.action, view.subject, view.reason_code = "remove", "8", "no_show"
    run_async(slot_editor.ReasonModal(view).on_submit(organizer(user_id=500)))
    pick(view, view.action_select, "pf_add")
    assert [o.label for o in view.subject_select.options] == ["Party 1 · short by 1"]
    pick(view, view.subject_select, str(a))
    pick(view, view.object_select, "caster")
    modal = slot_editor.PfModal(view)
    modal.name._value = "Void Walker"
    run_async(modal.on_submit(organizer(user_id=500)))
    text = view.render()
    assert "Party Finder · Caster · Void Walker" in text and "Short by" not in text
    press_apply(view)
    assert ctx.db.party_pf(a)[0]["name"] == "Void Walker"


def test_the_editor_adds_anyone_from_the_server_and_warns_before_applying(ctx, make_run, clock):
    run, slot, (a, b), thread, chan = two_parties(ctx, make_run)
    view = editor(ctx, slot)
    view.action, view.subject, view.reason_code = "remove", "8", "no_show"
    run_async(slot_editor.ReasonModal(view).on_submit(organizer(user_id=500)))
    pick(view, view.action_select, "add")
    pick(view, view.subject_select, str(a))
    assert slot_editor.ANYONE in [o.value for o in view.object_select.options]
    modal = slot_editor.AnyoneModal(view)
    modal.user._values = [SimpleNamespace(id=77, display_name="Guest", bot=False)]
    run_async(modal.on_submit(organizer(user_id=500)))
    assert view.object == "77" and ctx.db.get_player("77")["display_name"] == "Guest"
    click(view, slot_editor.StageButton)
    assert [o["op"] for o in view.ops] == ["remove", "add"] and view.ops[1]["role"] == "caster"
    asked = click(view, slot_editor.ApplyButton)
    assert "They have not signed up" in asked.response.sent[0]["content"]       # warnings ask first
    assert "77" not in members(ctx, a)
    go = organizer(user_id=500)
    run_async(asked.response.sent[0]["view"].force.callback(go))
    assert members(ctx, a)["77"] == "caster" and "8" not in members(ctx, a)


def test_the_editor_moves_a_player_with_the_dropdowns(ctx, make_run, clock):
    run, slot, (a, b), thread, chan = two_parties(ctx, make_run)
    view = editor(ctx, slot)
    view.action, view.subject, view.reason_code = "remove", "9", "no_show"
    run_async(slot_editor.ReasonModal(view).on_submit(organizer(user_id=500)))
    pick(view, view.action_select, "move")
    pick(view, view.subject_select, "1")
    assert [o.value for o in view.object_select.options] == [str(b)]            # only the other party
    pick(view, view.object_select, str(b))
    click(view, slot_editor.StageButton)
    assert view.ops[-1] == {"op": "move", "player": "1", "to": b}
    press_apply(view)
    assert members(ctx, b)["1"] == "main_tank" and "1" not in members(ctx, a)


def test_incomplete_picks_and_bad_stages_are_reported_not_staged(ctx, make_run, clock):
    run, slot, (a, b), thread, chan = two_parties(ctx, make_run)
    view = editor(ctx, slot)
    it = click(view, slot_editor.StageButton) if False else None
    view.action = "move"
    it = organizer(user_id=500)
    run_async(view.stage(it))
    assert "Pick the player and the party they move to" in it.text and view.ops == []
    view.subject, view.object = "1", str(b)                                         # party 2 is full
    it = organizer(user_id=500)
    run_async(view.stage(it))
    assert "Not staged" in it.text and "is full" in it.text and view.ops == []
    assert "Nothing staged yet" in click(view, slot_editor.ApplyButton).response.edits[-1]["content"]


def test_the_editor_is_for_organizers_and_its_owner(ctx, make_run, clock):
    run, slot, (a, b), thread, chan = two_parties(ctx, make_run)
    view = editor(ctx, slot)
    assert run_async(view.interaction_check(organizer(user_id=500))) is True
    assert run_async(view.interaction_check(organizer(user_id=501))) is False
    assert run_async(view.interaction_check(player(500))) is False


def test_the_editor_fits_discords_limits_with_a_full_slot(ctx, make_run, clock):
    run, slot, (a, b), thread, chan = two_parties(ctx, make_run)
    view = editor(ctx, slot)
    for action in slot_editor.ACTIONS:
        view.action = action
        view.refresh()
        assert len(view.render()) <= 1990
        for item in view.children:
            assert len(getattr(item, "options", [])) <= 25
    assert len(view.children) == 9 and {c.row for c in view.children} == {0, 1, 2, 3, 4}
