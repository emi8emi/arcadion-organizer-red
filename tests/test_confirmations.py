"""Player confirmations (draft and approved), the decline flow, reminders and the status dashboard."""
from types import SimpleNamespace

from bot import confirmations as cf
from bot import confirm_views, dashboard, handlers
from bot.confirm_views import ConfirmView, DeclineView
from conftest import FakeInteraction, at, organizer, player, run_async
from test_approve import D2, PARTIES_CHANNEL, S2, FakeParties, FakeThread, approve, attach, setup_day

START = "2026-11-02T22:00"             # S2 (17:00 New York) in UTC


class Msg:
    def __init__(self, store, id):
        self.id, self.store = id, store

    async def edit(self, **kw):
        self.store.edits.append({"id": self.id, **kw})

    async def delete(self):
        self.store.deleted.append(self.id)


class Thread(FakeThread):
    def __init__(self, id, name):
        super().__init__(id, name)
        self.edits, self.deleted = [], []

    def get_partial_message(self, message_id):
        return Msg(self, message_id)

    async def remove_user(self, user): ...

    async def add_user(self, user): ...


class Channel(FakeParties):
    def __init__(self):
        super().__init__()
        self.sent, self.edits, self.deleted = [], [], []

    async def create_thread(self, name, type=None, auto_archive_duration=None):
        t = Thread(7000 + len(self.threads), name)
        self.threads[t.id] = t
        return t

    async def send(self, content=None, **kw):
        self.sent.append({"content": content, **kw})
        return SimpleNamespace(id=5000 + len(self.sent))

    def get_partial_message(self, message_id):
        return Msg(self, message_id)


def draft_day(ctx, make_run, slots=(S2,)):
    run, ids = setup_day(ctx, make_run, slots)
    chan = Channel()
    attach(ctx, chan)
    return run, ids, chan


def ask(ctx, *args, who=None):
    it = who or organizer()
    run_async(handlers.draft_confirm(ctx, it, *args))
    return it


def slot_id(ctx, run, key=S2):
    return next(s["id"] for s in ctx.db.run_slots(run["id"]) if s["slot_key"] == key)


def status(ctx, sid, uid):
    row = cf.statuses(ctx.db, sid).get(str(uid))
    return row["status"] if row else None


def pressed(ctx, fn, sid, uid, *args):
    it = player(uid)
    it.channel = Channel()
    run_async(fn(ctx, it, sid, *args))
    return it


# ---------------------------------------------------------------- before approval: /draft confirm

def test_draft_confirm_posts_one_message_per_slot_pinging_everyone(ctx, make_run):
    run, ids, chan = draft_day(ctx, make_run, (S2, "2026-11-02T20:00"))
    it = ask(ctx)
    assert "2 slot(s), 16 player(s) pinged" in it.text
    assert len(chan.sent) == 2
    first = chan.sent[0]
    assert isinstance(first["view"], ConfirmView) and {u.id for u in first["allowed_mentions"].users} == set(range(1, 9))
    assert [c.item.custom_id for c in first["view"].children] == [f"cfm:{slot_id(ctx, run)}", f"cnc:{slot_id(ctx, run)}"]
    assert "⏳ <@1> Main tank" in first["content"] and "Draft parties are ready" in first["content"]
    assert status(ctx, slot_id(ctx, run), 1) == "pending"
    assert cf.prompt_rows(ctx.db, slot_id(ctx, run))["draft"]["channel_id"] == str(PARTIES_CHANNEL)


def test_draft_confirm_is_for_organizers_and_needs_a_draft_and_a_channel(ctx, make_run):
    run, ids, chan = draft_day(ctx, make_run)
    it = ask(ctx, who=player(2))
    assert "Only organizers" in it.text and not chan.sent
    ctx.get_channel = None
    assert "parties channel" in ask(ctx).text
    attach(ctx, chan)
    assert "No run called" in ask(ctx, "nope").text


def test_asking_again_pings_only_those_who_have_not_confirmed(ctx, make_run):
    run, ids, chan = draft_day(ctx, make_run)
    ask(ctx)
    sid = slot_id(ctx, run)
    pressed(ctx, confirm_views.press_confirm, sid, 3)
    ask(ctx)
    pinged = {u.id for u in chan.sent[-1]["allowed_mentions"].users}
    assert pinged == {1, 2, 4, 5, 6, 7, 8}
    assert chan.deleted == [5001]                                      # the old message is replaced, not duplicated


# ---------------------------------------------------------------- the buttons

def test_confirming_marks_the_player_and_edits_the_message(ctx, make_run):
    run, ids, chan = draft_day(ctx, make_run)
    ask(ctx)
    sid = slot_id(ctx, run)
    it = pressed(ctx, confirm_views.press_confirm, sid, 3)
    assert "Confirmed" in it.text and status(ctx, sid, 3) == "confirmed"
    assert "✅ <@3>" in chan.edits[-1]["content"]


def test_only_seated_players_can_answer_and_not_after_the_slot(ctx, make_run, clock):
    run, ids, chan = draft_day(ctx, make_run)
    ask(ctx)
    sid = slot_id(ctx, run)
    assert "not in a party" in pressed(ctx, confirm_views.press_confirm, sid, 99).text
    at(clock, "2026-11-03T05:00")
    assert "already over" in pressed(ctx, confirm_views.press_confirm, sid, 1).text
    assert status(ctx, sid, 1) == "pending"


def test_cancel_defaults_to_unavailable_at_this_slot(ctx, make_run):
    run, ids, chan = draft_day(ctx, make_run, (S2, "2026-11-02T20:00"))
    ask(ctx)
    sid = slot_id(ctx, run)
    it = pressed(ctx, confirm_views.press_cancel, sid, 1)
    view = it.response.sent[0]["view"]
    assert isinstance(view, DeclineView) and view.code == "slot"
    select = next(c for c in view.children if isinstance(c, confirm_views.ReasonSelect))
    assert [o.value for o in select.options if o.default] == ["slot"]


def test_declining_the_slot_unticks_only_that_slot_and_tells_the_organizers(ctx, make_run):
    run, ids, chan = draft_day(ctx, make_run)
    ctx.db.set_day_availability(run["id"], 1, D2, [S2, "2026-11-02T20:00"])
    ask(ctx)
    sid = slot_id(ctx, run)
    it = pressed(ctx, confirm_views.submit_decline, sid, 1, "slot", None)
    assert "removed this slot" in it.text
    assert status(ctx, sid, 1) == "declined"
    assert ctx.db.ticked_keys(run["id"], 1) == ["2026-11-02T20:00"]
    assert "❌ <@1>" in chan.edits[-1]["content"] and "Unavailable at this slot" in chan.edits[-1]["content"]
    note = it.channel.sent[0]["content"]
    assert "<@1> can't make it" in note and "/draft import" in note


def test_the_other_reasons(ctx, make_run):
    run, ids, chan = draft_day(ctx, make_run)
    for uid in (1, 2, 3):
        ctx.db.set_day_availability(run["id"], uid, D2, [S2, "2026-11-02T20:00"])
    ask(ctx)
    sid = slot_id(ctx, run)
    pressed(ctx, confirm_views.submit_decline, sid, 1, "day", None)
    assert ctx.db.ticked_keys(run["id"], 1) == []                       # the whole day
    pressed(ctx, confirm_views.submit_decline, sid, 2, "role", None)
    assert ctx.db.ticked_keys(run["id"], 2) == [S2, "2026-11-02T20:00"]  # ticks kept, organizers told
    blank = pressed(ctx, confirm_views.submit_decline, sid, 3, "other", "  ")
    assert "write a reason" in blank.text and status(ctx, sid, 3) == "pending"
    pressed(ctx, confirm_views.submit_decline, sid, 3, "other", "family dinner")
    row = cf.statuses(ctx.db, sid)["3"]
    assert (row["reason_code"], row["reason_text"]) == ("other", "family dinner")
    assert ctx.db.ticked_keys(run["id"], 3) == ["2026-11-02T20:00"]


def test_a_declined_player_drops_out_of_the_next_export(ctx, make_run):
    from bot.solver_io import build_signups
    run, ids, chan = draft_day(ctx, make_run)
    ask(ctx)
    pressed(ctx, confirm_views.submit_decline, slot_id(ctx, run), 1, "slot", None)
    data, _ = build_signups(ctx.db, run, ctx.cfg, D2, ctx.now())
    assert not [p for p in data["players"] if p["id"] == "1" and S2 in p["available"]]
    assert [p for p in data["players"] if p["id"] == "2" and S2 in p["available"]]          # the others are unchanged


def test_a_new_draft_keeps_confirmations_and_forgets_declines(ctx, make_run):
    run, ids, chan = draft_day(ctx, make_run)
    ask(ctx)
    sid = slot_id(ctx, run)
    pressed(ctx, confirm_views.press_confirm, sid, 2)
    pressed(ctx, confirm_views.submit_decline, sid, 1, "role", None)
    ctx.db.conn.execute("DELETE FROM party_members WHERE discord_id = '3'")     # the solver left player 3 out
    ctx.db.conn.commit()
    cf.after_import(ctx.db, run["id"], D2)
    assert status(ctx, sid, 2) == "confirmed"           # still seated in the same slot
    assert status(ctx, sid, 1) is None                  # declined: asked again next time
    assert status(ctx, sid, 3) is None                  # no longer seated


# ---------------------------------------------------------------- after approval

def test_approval_asks_unconfirmed_players_in_the_thread(ctx, make_run):
    run, ids, chan = draft_day(ctx, make_run)
    ask(ctx)
    sid = slot_id(ctx, run)
    for uid in range(1, 5):
        pressed(ctx, confirm_views.press_confirm, sid, uid)
    approve(ctx, None, D2)
    thread = chan.threads[7000]
    prompt = [m for m in thread.sent if isinstance(m.get("view"), ConfirmView)]
    assert len(prompt) == 1
    assert {u.id for u in prompt[0]["allowed_mentions"].users} == {5, 6, 7, 8}     # confirmed ones are not pinged
    assert "Your party is set" in prompt[0]["content"]
    assert cf.prompt_rows(ctx.db, sid)["thread"]["channel_id"] == "7000"


def test_no_thread_prompt_when_everyone_confirmed(ctx, make_run):
    run, ids, chan = draft_day(ctx, make_run)
    ask(ctx)
    sid = slot_id(ctx, run)
    for uid in range(1, 9):
        pressed(ctx, confirm_views.press_confirm, sid, uid)
    approve(ctx, None, D2)
    assert not [m for m in chan.threads[7000].sent if isinstance(m.get("view"), ConfirmView)]


def test_a_decline_after_approval_tells_the_organizers_to_swap(ctx, make_run):
    run, ids, chan = draft_day(ctx, make_run)
    approve(ctx, None, D2)
    sid = slot_id(ctx, run)
    it = pressed(ctx, confirm_views.submit_decline, sid, 4, "slot", None)
    assert status(ctx, sid, 4) == "declined"
    assert "Edit composition" in it.channel.sent[0]["content"]


def test_a_reminder_goes_out_once_shortly_before_the_slot(ctx, make_run, clock):
    run, ids, chan = draft_day(ctx, make_run)
    approve(ctx, None, D2)
    sid = slot_id(ctx, run)
    pressed(ctx, confirm_views.press_confirm, sid, 1)
    at(clock, "2026-11-02T10:00")
    assert run_async(cf.send_confirmation_reminders(ctx)) == 0          # too early
    at(clock, "2026-11-02T20:00")                                        # 2h before the start
    assert run_async(cf.send_confirmation_reminders(ctx)) == 1
    thread = chan.threads[7000]
    reminder = [m for m in thread.sent if isinstance(m.get("view"), ConfirmView)][-1]
    assert {u.id for u in reminder["allowed_mentions"].users} == set(range(2, 9))
    assert "starts soon" in reminder["content"]
    assert run_async(cf.send_confirmation_reminders(ctx)) == 0          # only once


def test_a_swapped_in_player_is_asked_to_confirm(ctx, make_run):
    from bot.parties import apply_swap
    run, ids, chan = draft_day(ctx, make_run)
    approve(ctx, None, D2)
    sid = slot_id(ctx, run)
    pid = ctx.db.parties(run["id"])[0]["id"]
    run_async(apply_swap(ctx, 100, pid, "1", "30", "main_tank", 999, reason_code="no_show", reason_text=""))
    assert status(ctx, sid, 30) == "pending"
    last = [m for m in chan.threads[7000].sent if isinstance(m.get("view"), ConfirmView)][-1]
    assert [u.id for u in last["allowed_mentions"].users] == [30]


# ---------------------------------------------------------------- the dashboard

def issues(ctx, run, day=D2):
    rows = run_async(dashboard.collect(ctx, run, day))
    return rows, [(lvl, text) for r in rows for lvl, text in r["issues"]]


def test_dashboard_flags_a_draft_that_was_never_asked(ctx, make_run):
    run, ids, chan = draft_day(ctx, make_run)
    rows, found = issues(ctx, run)
    assert rows[0]["level"] == dashboard.INFO and "not requested" in found[0][1]
    ask(ctx)
    rows, found = issues(ctx, run)
    assert "Waiting for 8" in found[0][1]


def test_dashboard_flags_declines_missing_threads_and_late_confirmations(ctx, make_run, clock):
    run, ids, chan = draft_day(ctx, make_run)
    sid = slot_id(ctx, run)
    pressed(ctx, confirm_views.submit_decline, sid, 1, "slot", None)
    rows, found = issues(ctx, run)
    assert rows[0]["level"] == dashboard.WARN and "Re-export" in found[0][1]        # draft: rerun the solver
    approve(ctx, None, D2)
    rows, found = issues(ctx, run)
    assert rows[0]["level"] == dashboard.URGENT and any("Swap them out" in t for _, t in found)
    at(clock, "2026-11-02T20:00")
    assert any("not confirmed, starts" in t for _, t in issues(ctx, run)[1])
    ctx.db.conn.execute("UPDATE slots SET thread_id = NULL")
    ctx.db.conn.commit()
    assert any("Not posted in a thread" in t for _, t in issues(ctx, run)[1])


def test_dashboard_notices_a_thread_that_cannot_be_opened(ctx, make_run):
    run, ids, chan = draft_day(ctx, make_run)
    approve(ctx, None, D2)
    chan.threads.clear()
    assert any("cannot be found" in t for _, t in issues(ctx, run)[1])


def test_dashboard_view_lists_filters_and_nudges(ctx, make_run, clock):
    run, ids, chan = draft_day(ctx, make_run, (S2, "2026-11-02T20:00"))
    approve(ctx, None, D2)
    sid = slot_id(ctx, run)
    pressed(ctx, confirm_views.submit_decline, sid, 4, "slot", None)
    it = organizer()
    run_async(handlers.party_dashboard(ctx, it))
    sent = it.response.sent[0]
    view = sent["view"]
    assert sent["ephemeral"] is True and "2 parties, 1 needing attention" in sent["content"]
    assert view.day == D2                                                      # opened on the day that needs something
    parties = view.children[1]
    assert parties.options[0].label.startswith("🔴") and parties.options[0].default is False

    click = FakeInteraction(user_id=it.user.id)
    view.children[2].callback                                                  # toggle button exists
    run_async(view.children[2].callback(click))
    assert "(showing only those)" in click.response.edits[-1]["content"]

    pick = FakeInteraction(user_id=it.user.id)
    parties = view.children[1]                                                  # the view was rebuilt by the toggle
    parties._values = [str(view.rows[0]["party"]["id"])] if view.rows[0]["level"] else [str(view.rows[1]["party"]["id"])]
    run_async(parties.callback(pick))
    detail = pick.response.edits[-1]["content"]
    assert "❌ <@4>" in detail and "Swap them out" in detail
    assert any(getattr(c, "url", None) for c in view.children)                 # "Open thread" link button

    before = len(chan.threads[7000].sent)
    nudge = FakeInteraction(user_id=it.user.id)
    nudger = next(c for c in view.children if getattr(c, "label", "").startswith("Nudge"))
    run_async(nudger.callback(nudge))
    assert "thread" in nudge.followup.sent[0]["content"]
    assert len(chan.threads[7000].sent) + len(chan.threads.get(7001, SimpleNamespace(sent=[])).sent) > before


def test_dashboard_is_private_to_the_organizer_who_opened_it(ctx, make_run):
    run, ids, chan = draft_day(ctx, make_run)
    it = organizer()
    run_async(handlers.party_dashboard(ctx, it))
    view = it.response.sent[0]["view"]
    other = FakeInteraction(user_id=777)
    assert run_async(view.interaction_check(other)) is False
    assert "belongs to the organizer" in other.response.sent[0]["content"]
    assert "Only organizers" in (lambda i: (run_async(handlers.party_dashboard(ctx, i)), i.text)[1])(player(3))


def test_dashboard_without_parties(ctx, make_run):
    make_run()
    it = organizer()
    run_async(handlers.party_dashboard(ctx, it))
    assert "no parties yet" in it.text
