"""The buttons on party threads: ended, edit composition, the "why was I swapped" notice, /party import."""
import json

from bot import handlers, parties, party_views
from bot.db import parse_iso
from bot.posting import party_message, post_rating_prompts
from bot.votes import time_reason
from conftest import FakeAttachment, add_player, at, organizer, player, run_async
from test_approve import D2, FakeParties, FakeThread, PARTIES_CHANNEL, S2, approve, attach, setup_day


class Thread(FakeThread):
    def __init__(self, id, name):
        super().__init__(id, name)
        self.edits, self.removed, self.added = [], [], []

    def get_partial_message(self, message_id):
        thread = self

        class Partial:
            async def edit(self, **kw):
                thread.edits.append({"id": message_id, **kw})
        return Partial()

    async def remove_user(self, user):
        self.removed.append(user.id)

    async def add_user(self, user):
        self.added.append(user.id)


class Channel(FakeParties):
    async def create_thread(self, name, type=None, auto_archive_duration=None):
        t = Thread(7000 + len(self.threads), name)
        self.threads[t.id] = t
        self.sent = getattr(self, "sent", [])
        return t

    async def send(self, content=None, **kw):
        self.sent = getattr(self, "sent", [])
        self.sent.append({"content": content, **kw})


def approved(ctx, make_run, slots=(S2,)):
    """An approved day with one party (players 1-8), threads faked. Returns (run, party id, thread, channel)."""
    run, ids = setup_day(ctx, make_run, slots)
    chan = Channel()
    attach(ctx, chan)
    approve(ctx, None, D2)
    party = ctx.db.parties(run["id"])[0]
    return run, party["id"], chan.threads[7000], chan


def press(ctx, party_id, who):
    run_async(party_views.press_end(ctx, who, party_id))
    return who


def end_it(ctx, party_id, who, override=False):
    it = player(who) if not isinstance(who, int) else player(who)
    run_async(party_views.confirm_end(ctx, it, party_id, override))
    return it


# ---------------------------------------------------------------- the original parties

def test_approving_stores_the_solvers_parties_and_edits_do_not_touch_them(ctx, make_run, clock):
    run, pid, thread, chan = approved(ctx, make_run)
    original = ctx.db.party_original(pid)
    assert {m["discord_id"] for m in original["members"]} == {str(i) for i in range(1, 9)}
    run_async(parties.apply_swap(ctx, 100, pid, "1", "20", "tank", 9, reason_code="no_show", reason_text=""))
    after = ctx.db.party_original(pid)
    assert after == original
    assert "20" in {m["discord_id"] for m in ctx.db.party_members(pid)}


# ---------------------------------------------------------------- party ended

def test_ended_is_refused_before_the_slot_starts(ctx, make_run, clock):
    run, pid, thread, chan = approved(ctx, make_run)
    it = press(ctx, pid, player(1))
    assert "has not started yet" in it.text and not it.response.sent[0].get("view")


def test_a_member_confirms_and_the_party_is_done_with_play_time_and_a_rate_button(ctx, make_run, clock):
    run, pid, thread, chan = approved(ctx, make_run)
    at(clock, "2026-11-02T22:40")                                      # 40 minutes into the slot
    it = press(ctx, pid, player(3))
    assert "Mark it as ended?" in it.text and it.response.sent[0]["view"] is not None
    assert ctx.db.party(pid)["status"] == "confirmed"                  # asking changes nothing
    done = end_it(ctx, pid, 3)
    assert "Marked as ended" in done.text
    p = ctx.db.party(pid)
    assert p["status"] == "done" and p["ended_by"] == "3"
    logs = {r["discord_id"]: r["minutes"] for r in ctx.db.conn.execute("SELECT * FROM play_log")}
    assert logs == {str(i): 40 for i in range(1, 9)}
    assert "✅ Ended" in thread.edits[-1]["content"]
    assert "Rate your party" in thread.sent[-1]["content"] and thread.sent[-1]["view"] is not None
    assert time_reason(p, ctx.cfg, clock["now"]) == ""                  # ratings open before the slot would end


def test_cancelling_the_confirmation_changes_nothing_and_non_members_are_refused(ctx, make_run, clock):
    run, pid, thread, chan = approved(ctx, make_run)
    at(clock, "2026-11-02T22:40")
    stranger = press(ctx, pid, player(99))
    assert "Only the players" in stranger.text
    view = party_views.EndConfirmView(ctx, pid, "3", False)
    cancel = player(3)
    run_async(view.cancel.callback(cancel))
    assert "Cancelled" in cancel.text and ctx.db.party(pid)["status"] == "confirmed"


def test_organizer_override_and_undo(ctx, make_run, clock):
    run, pid, thread, chan = approved(ctx, make_run)
    at(clock, "2026-11-02T22:40")
    it = press(ctx, pid, organizer(user_id=500))
    assert "organizer override" in it.text
    run_async(party_views.confirm_end(ctx, organizer(user_id=500), pid, True))
    assert [c["kind"] for c in ctx.db.party_changes(pid)] == ["end_override"]
    member = press(ctx, pid, player(3))                                # a member only learns it ended
    assert "already marked ended" in member.text and not member.response.sent[0].get("view")
    org = press(ctx, pid, organizer(user_id=500))
    assert "Undo it?" in org.text
    undo = organizer(user_id=500)
    run_async(org.response.sent[0]["view"].undo.callback(undo))
    p = ctx.db.party(pid)
    assert p["status"] == "confirmed" and p["ended_utc"] is None and p["rating_posted_utc"] is None
    assert ctx.db.conn.execute("SELECT COUNT(*) FROM play_log").fetchone()[0] == 0
    assert ctx.db.party_changes(pid)[-1]["kind"] == "end_undo"


def test_a_player_cannot_undo(ctx, make_run, clock):
    run, pid, thread, chan = approved(ctx, make_run)
    at(clock, "2026-11-02T22:40")
    end_it(ctx, pid, 3)
    view = party_views.UndoEndView(ctx, pid, "3")
    it = player(3)
    run_async(view.undo.callback(it))
    assert "Nothing to undo" in it.text and ctx.db.party(pid)["status"] == "done"


def test_parties_end_by_themselves_when_the_slot_runs_out(ctx, make_run, clock):
    run, pid, thread, chan = approved(ctx, make_run)
    at(clock, "2026-11-02T23:00")
    assert run_async(parties.auto_end_parties(ctx)) == []                # 60 of 90 minutes
    at(clock, "2026-11-02T23:31")
    assert run_async(parties.auto_end_parties(ctx)) == [pid]
    p = ctx.db.party(pid)
    assert p["status"] == "done" and p["ended_by"] == "auto" and p["ended_utc"].startswith("2026-11-02T23:30")
    assert {r["minutes"] for r in ctx.db.conn.execute("SELECT * FROM play_log")} == {90}
    assert run_async(parties.auto_end_parties(ctx)) == []                # only once


def test_the_slot_prompt_skips_parties_that_already_have_their_button(ctx, make_run, clock):
    run, pid, thread, chan = approved(ctx, make_run, (S2, "2026-11-02T20:00"))
    at(clock, "2026-11-02T22:40")
    end_it(ctx, pid, 3)
    sent = len(thread.sent)
    at(clock, "2026-11-03T01:00")
    assert run_async(post_rating_prompts(ctx)) == 0                      # nothing left to prompt in slot 1
    assert len(thread.sent) == sent
    assert {r["slot_key"]: r["rating_posted_utc"] for r in ctx.db.run_slots(run["id"])}[S2]


# ---------------------------------------------------------------- swapping

def test_the_standby_pool_is_ticked_but_unseated_players(ctx, make_run, clock):
    run, pid, thread, chan = approved(ctx, make_run)
    add_player(ctx, run, 30, ["dps"], 0, [S2])
    add_player(ctx, run, 31, ["dps"], 0, ["2026-11-03T17:00"])
    slot = ctx.db.party(pid)["slot_id"]
    assert ctx.db.standby_pool(slot) == ["30"]


def test_swap_updates_the_party_thread_standby_and_notifies_in_the_channel(ctx, make_run, clock):
    run, pid, thread, chan = approved(ctx, make_run)
    add_player(ctx, run, 30, ["tank"], 0, [S2])
    change, problems = run_async(parties.apply_swap(
        ctx, 100, pid, "1", "30", "tank", 500, reason_code="no_show", reason_text=""))
    assert problems == []
    ids = {m["discord_id"] for m in ctx.db.party_members(pid)}
    assert "30" in ids and "1" not in ids
    slot = ctx.db.party(pid)["slot_id"]
    assert ctx.db.standby_pool(slot) == ["1"]
    assert thread.removed == [1] and thread.added == [30]
    assert "<@30> joins party 1 as tank, replacing <@1>" in thread.sent[-1]["content"]
    assert thread.sent[-1]["allowed_mentions"].users[0].id == 30         # the new player is really pinged
    assert "<@30>" in thread.edits[-1]["content"] and "<@1>" not in thread.edits[-1]["content"]
    notice = chan.sent[-1]
    assert "<@1> you were swapped out of party 1" in notice["content"]
    assert "no_show" not in notice["content"] and "Did not show" not in notice["content"]   # the reason stays private
    c = ctx.db.change(change)
    assert (c["kind"], c["player_id"], c["other_player_id"], c["reason_code"]) == ("swap", "1", "30", "no_show")


def test_why_was_i_swapped_reveals_the_reason_only_to_that_player_and_organizers(ctx, make_run, clock):
    run, pid, thread, chan = approved(ctx, make_run)
    add_player(ctx, run, 30, ["tank"], 0, [S2])
    change, _ = run_async(parties.apply_swap(ctx, 100, pid, "1", "30", "tank", 500, reason_code="other",
                                             reason_text="Please check your prog first"))
    swapped, other, org = player(1), player(2), organizer(user_id=500)
    for it in (swapped, other, org):
        run_async(party_views.show_reason(ctx, it, change))
    assert "Please check your prog first" in swapped.text and "<@500>" in swapped.text
    assert "not about you" in other.text and "prog" not in other.text
    assert "Please check" in org.text


def test_anyone_can_be_added_even_someone_who_never_signed_up(ctx, make_run, clock):
    run, pid, thread, chan = approved(ctx, make_run)
    run_async(parties.apply_swap(ctx, 100, pid, "8", "777", "dps", 500, reason_code="cant_make_it", reason_text="",
                                 in_name="Newcomer"))
    assert ctx.db.get_player("777")["display_name"] == "Newcomer"


def test_the_warnings_cover_ticks_limits_roles_and_double_booking(ctx, make_run, clock):
    run, pid, thread, chan = approved(ctx, make_run)
    party = ctx.db.party(pid)
    stranger = parties.swap_warnings(ctx, party, "777", "tank")
    assert any("not signed up" in w for w in stranger) and any("did not tick" in w for w in stranger)
    add_player(ctx, run, 31, ["dps"], 0, ["2026-11-03T17:00"])
    w = parties.swap_warnings(ctx, party, "31", "tank")
    assert any("do not list tank" in x for x in w) and any("did not tick" in x for x in w)
    seated = parties.swap_warnings(ctx, party, "2", "tank")              # player 2 is in this very party
    assert any("already in party 1 of this same slot" in x for x in seated)
    assert parties.comp_warnings(ctx, party, {"1": None, "31": "dps"}) == [
        "The roles would be 1 tank, 2 healer, 5 dps instead of 2 tank, 2 healer, 4 dps."]
    assert parties.comp_warnings(ctx, party, {"1": None, "31": "tank"}) == []


def test_the_limit_warnings(ctx, make_run, clock):
    run, pid, thread, chan = approved(ctx, make_run, (S2, "2026-11-02T20:00"))
    add_player(ctx, run, 40, ["tank"], 0, [S2, "2026-11-02T20:00"], per_day=1, per_run=1)
    second = ctx.db.parties(run["id"])[1]
    run_async(parties.apply_swap(ctx, 100, pid, "1", "40", "tank", 500, reason_code="no_show", reason_text=""))
    w = parties.swap_warnings(ctx, ctx.db.party(second["id"]), "40", "tank")
    assert any("their max is 1" in x and "today" in x for x in w) and any("of the run" in x for x in w)


def test_swapping_after_the_party_ended_keeps_play_logs_right_and_drops_the_old_votes(ctx, make_run, clock):
    run, pid, thread, chan = approved(ctx, make_run)
    at(clock, "2026-11-02T22:40")
    end_it(ctx, pid, 3)
    slot = ctx.db.party(pid)["slot_id"]
    ctx.db.add_vote(run["id"], slot, "1", "2", "like", clock["now"])
    ctx.db.add_vote(run["id"], slot, "2", "3", "like", clock["now"])
    add_player(ctx, run, 30, ["tank"], 0, [S2])
    run_async(parties.apply_swap(ctx, 100, pid, "1", "30", "tank", 500, reason_code="no_show", reason_text=""))
    logs = {r["discord_id"]: r["minutes"] for r in ctx.db.conn.execute("SELECT * FROM play_log")}
    assert "1" not in logs and logs["30"] == 40 and len(logs) == 8
    votes = ctx.db.conn.execute("SELECT voter_id, target_id FROM votes").fetchall()
    assert [(v[0], v[1]) for v in votes] == [("2", "3")]
    assert ctx.db.party(pid)["status"] == "done"


def test_a_failing_thread_step_is_reported_but_the_swap_stands(ctx, make_run, clock):
    run, pid, thread, chan = approved(ctx, make_run)
    add_player(ctx, run, 30, ["tank"], 0, [S2])

    async def deny(user):
        raise RuntimeError("Missing Permissions")
    thread.remove_user = deny
    _, problems = run_async(parties.apply_swap(ctx, 100, pid, "1", "30", "tank", 500, reason_code="no_show",
                                               reason_text=""))
    assert any("Manage Threads" in p for p in problems)
    assert "30" in {m["discord_id"] for m in ctx.db.party_members(pid)}


# ---------------------------------------------------------------- the panel

def test_the_panel_is_for_organizers_only(ctx, make_run, clock):
    run, pid, thread, chan = approved(ctx, make_run)
    it = player(1)
    run_async(party_views.open_edit_panel(ctx, it, pid))
    assert "Only organizers" in it.text and not it.response.sent[0].get("view")
    org = organizer(user_id=500)
    run_async(party_views.open_edit_panel(ctx, org, pid))
    view = org.response.sent[0]["view"]
    assert "can read the reason" in org.text and len(view.out_select.options) == 8


def test_a_swap_with_warnings_needs_a_second_confirmation(ctx, make_run, clock):
    run, pid, thread, chan = approved(ctx, make_run)
    add_player(ctx, run, 31, ["dps"], 0, ["2026-11-03T17:00"])           # did not tick this slot
    panel = party_views.EditPartyView(ctx, pid, "500")
    panel.out_id, panel.in_id, panel.in_name, panel.reason_code = "8", "31", "P31", "cant_make_it"
    modal = party_views.SwapModal(panel)
    modal.role._value, modal.text._value = "", ""
    sub = organizer(user_id=500)
    run_async(modal.on_submit(sub))
    assert "did not tick this slot" in sub.text
    assert "31" not in {m["discord_id"] for m in ctx.db.party_members(pid)}          # not yet
    force = sub.response.sent[0]["view"]
    go = organizer(user_id=500)
    run_async(force.force.callback(go))
    assert "Swapped" in go.text and "31" in {m["discord_id"] for m in ctx.db.party_members(pid)}
    change = ctx.db.party_changes(pid)[-1]
    assert "did not tick" in change["warnings_json"] and change["reason_code"] == "cant_make_it"


def test_a_clean_swap_needs_no_confirmation_and_other_needs_text(ctx, make_run, clock):
    run, pid, thread, chan = approved(ctx, make_run)
    add_player(ctx, run, 30, ["dps"], 0, [S2])
    panel = party_views.EditPartyView(ctx, pid, "500")
    panel.out_id, panel.in_id, panel.in_name, panel.reason_code = "8", "30", "P30", "other"
    modal = party_views.SwapModal(panel)
    modal.role._value, modal.text._value = "", "  "
    refused = organizer(user_id=500)
    run_async(modal.on_submit(refused))
    assert "needs a written reason" in refused.text and "30" not in {m["discord_id"] for m in ctx.db.party_members(pid)}
    panel.out_id, panel.in_id = "8", "30"
    modal.text._value = "Prog too far apart"
    ok = organizer(user_id=500)
    run_async(modal.on_submit(ok))
    assert "Swapped" in ok.text and "30" in {m["discord_id"] for m in ctx.db.party_members(pid)}
    assert ctx.db.party_changes(pid)[-1]["reason_text"] == "Prog too far apart"


def test_the_panel_texts_and_the_ended_message_fit_discords_limits(ctx, make_run, clock):
    run, pid, thread, chan = approved(ctx, make_run)
    at(clock, "2026-11-02T22:40")
    end_it(ctx, pid, 3)
    slot, [(party, members)] = ctx.db.approved_slots(run["id"], D2)[0]
    assert len(party_message(run, slot, party, members, ctx.db.checkpoints(1))) < 2000
    panel = party_views.EditPartyView(ctx, pid, "500")
    assert len(panel.render()) < 2000
    modal = party_views.SwapModal(panel)
    assert len(modal.text.label) <= 45 and len(modal.role.label) <= 45
    for item in panel.children:
        for o in getattr(item, "options", []):
            assert len(o.label) <= 100


# ---------------------------------------------------------------- JSON round trip

def edited(ctx, pid, **changes):
    data = parties.party_json(ctx, pid)
    data.update(changes)
    return FakeAttachment(json.dumps(data).encode())


def do_import(ctx, attachment):
    it = organizer(user_id=500)
    run_async(handlers.party_import(ctx, it, attachment))
    return it


def test_export_has_the_members_and_the_solvers_original(ctx, make_run, clock):
    run, pid, thread, chan = approved(ctx, make_run)
    data = parties.party_json(ctx, pid)
    assert data["party_id"] == pid and data["slot"] == S2 and len(data["members"]) == 8
    assert data["original"]["members"] and data["target"]["index"] == 0


def test_import_swaps_with_reasons_and_records_the_source(ctx, make_run, clock):
    run, pid, thread, chan = approved(ctx, make_run)
    add_player(ctx, run, 30, ["dps"], 0, [S2])
    data = parties.party_json(ctx, pid)
    for m in data["members"]:
        if m["player"] == "8":
            m["player"] = "30"
    data["reasons"] = {"8": {"code": "no_show", "text": ""}}
    it = do_import(ctx, FakeAttachment(json.dumps(data).encode()))
    assert "✅ <@8> → <@30> as dps" in it.text
    c = ctx.db.party_changes(pid)[-1]
    assert c["reason_code"] == "no_show" and json.loads(c["detail_json"])["source"] == "import"
    assert "<@8> you were swapped out" in chan.sent[-1]["content"]


def test_import_refuses_removals_without_a_reason_and_unforced_warnings(ctx, make_run, clock):
    run, pid, thread, chan = approved(ctx, make_run)
    add_player(ctx, run, 31, ["dps"], 0, ["2026-11-03T17:00"])
    data = parties.party_json(ctx, pid)
    for m in data["members"]:
        if m["player"] == "8":
            m["player"] = "31"
    no_reason = do_import(ctx, FakeAttachment(json.dumps(data).encode()))
    assert "refused" in no_reason.text and 'reasons["8"]' in no_reason.text
    data["reasons"] = {"8": {"code": "no_show"}}
    unforced = do_import(ctx, FakeAttachment(json.dumps(data).encode()))
    assert "refused" in unforced.text and "did not tick" in unforced.text and '"force": true' in unforced.text
    assert "8" in {m["discord_id"] for m in ctx.db.party_members(pid)}
    data["force"] = True
    forced = do_import(ctx, FakeAttachment(json.dumps(data).encode()))
    assert "✅" in forced.text and "31" in {m["discord_id"] for m in ctx.db.party_members(pid)}


def test_import_handles_role_changes_bad_files_and_non_organizers(ctx, make_run, clock):
    run, pid, thread, chan = approved(ctx, make_run)
    data = parties.party_json(ctx, pid)
    data["members"][0]["role"] = "healer"
    data["force"] = True
    it = do_import(ctx, FakeAttachment(json.dumps(data).encode()))
    assert "is now healer" in it.text and ctx.db.party_changes(pid)[-1]["kind"] == "role"
    assert "not valid JSON" in do_import(ctx, FakeAttachment(b"nope")).text
    assert "not a party export" in do_import(ctx, FakeAttachment(b"[1]")).text
    assert "Nothing differs" in do_import(ctx, edited(ctx, pid)).text
    bad = parties.party_json(ctx, pid)
    bad["members"].pop()
    assert "party size must stay" in do_import(ctx, FakeAttachment(json.dumps(bad).encode())).text
    plain = player(5)
    run_async(handlers.party_import(ctx, plain, edited(ctx, pid)))
    assert "Only organizers" in plain.text


def test_a_slow_swap_answers_discord_first_so_the_interaction_cannot_expire(ctx, make_run, clock):
    run, pid, thread, chan = approved(ctx, make_run)
    add_player(ctx, run, 30, ["dps"], 0, [S2])
    panel = party_views.EditPartyView(ctx, pid, "500")
    panel.out_id, panel.in_id, panel.in_name = "8", "30", "P30"
    modal = party_views.SwapModal(panel)
    modal.role._value, modal.text._value = "", ""
    it = organizer(user_id=500)
    order = []
    orig_defer, orig_edit = it.response.defer, it.edit_original_response

    async def defer(**kw):
        order.append("defer")
        await orig_defer(**kw)

    async def edit(**kw):
        order.append("edit")
        await orig_edit(**kw)
    it.response.defer, it.edit_original_response = defer, edit
    run_async(modal.on_submit(it))
    assert order == ["defer", "edit"] and "Swapped" in it.text


def test_a_user_discord_does_not_know_is_not_reported_as_a_problem(ctx, make_run, clock):
    import discord
    run, pid, thread, chan = approved(ctx, make_run)
    add_player(ctx, run, 30, ["tank"], 0, [S2])

    async def unknown(user):
        raise discord.NotFound(type("R", (), {"status": 404, "reason": "Not Found"})(), "Unknown User")
    thread.remove_user = unknown
    _, problems = run_async(parties.apply_swap(ctx, 100, pid, "1", "30", "tank", 500, reason_code="no_show",
                                               reason_text=""))
    assert problems == []
