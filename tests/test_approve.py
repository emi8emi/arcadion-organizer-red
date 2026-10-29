"""/draft approve: confirm a day's draft and post it (one thread per slot, one message per party)."""
from bot import handlers
from bot.posting import party_message, post_day_parties
from conftest import S2, add_party_players, confirm, draft_for_day, organizer, player, run_async
from bot.solver_io import import_draft

D2, D3 = "2026-11-02", "2026-11-03"
S2B = "2026-11-02T20:00"
PARTIES_CHANNEL = 4242


class FakeMessage:
    def __init__(self, id):
        self.id = id


class FakeThread:
    def __init__(self, id, name):
        self.id, self.name, self.sent, self.fail = id, name, [], 0

    async def send(self, content=None, **kwargs):
        if self.fail:
            self.fail -= 1
            raise RuntimeError("Missing Access")
        self.sent.append({"content": content, **kwargs})
        return FakeMessage(9000 + len(self.sent))


class FakeParties:
    """A channel that can make threads, plus the lookup the bot uses to find threads by id."""

    def __init__(self):
        self.id, self.threads, self.fail_create = PARTIES_CHANNEL, {}, 0

    async def create_thread(self, name, type=None, auto_archive_duration=None):
        if self.fail_create:
            self.fail_create -= 1
            raise RuntimeError("Missing Permissions")
        t = FakeThread(7000 + len(self.threads), name)
        self.threads[t.id] = t
        return t

    async def get(self, channel_id):
        return self if channel_id == PARTIES_CHANNEL else self.threads.get(channel_id)


def setup_day(ctx, make_run, slots=(S2,), first=1):
    """A run with an imported draft for D2: one party per given slot, 8 players each."""
    run = make_run(per_day=True)
    n = len(slots)
    ids = [add_party_players(ctx, run, [slot], first_id=first + 8 * i) for i, slot in enumerate(slots)]
    roles = ["tank"] * 2 + ["healer"] * 2 + ["dps"] * 4
    from bot.solver_io import build_signups, fingerprint
    data, _ = build_signups(ctx.db, run, ctx.cfg, D2, ctx.now())
    draft = {"version": 1, "input_fingerprint": fingerprint(data), "unfilled_slots": [], "unmatched_players": [],
             "parties": [{"slot": slot, "party": 0, "target": {"index": 0, "name": "P1 Fresh"},
                          "members": [{"player": i, "role": r, "helper": False, "placed_down": False, "distance": 0}
                                      for i, r in zip(ids[k], roles)]} for k, slot in enumerate(slots)]}
    assert n == len(draft["parties"])
    import_draft(ctx.db, run, draft, ctx.cfg, D2)
    return run, ids


def attach(ctx, channel):
    ctx.cfg["parties_channel_id"] = PARTIES_CHANNEL
    ctx.get_channel = channel.get


def approve(ctx, *args):
    it = organizer()
    run_async(handlers.draft_approve(ctx, it, *args))
    return it


def test_approve_confirms_and_posts_one_thread_per_slot_one_message_per_party(ctx, make_run, clock):
    run, ids = setup_day(ctx, make_run, (S2, S2B))
    chan = FakeParties()
    attach(ctx, chan)
    it = approve(ctx)
    assert "Approved 2 parties" in it.text and "Posted 2 parties" in it.text
    assert {p["status"] for p in ctx.db.parties(run["id"])} == {"confirmed"}
    assert len(chan.threads) == 2
    first = chan.threads[7000]
    assert first.name == "UWU week 1 · Mon 02 Nov 17:00" and len(first.sent) == 1
    msg = first.sent[0]
    assert msg["allowed_mentions"].users is True and msg["allowed_mentions"].everyone is False
    text = msg["content"]
    assert text.startswith("**Party 1** · <t:") and ":F>" in text and ":R>" in text
    assert "Target: 0: P1 Fresh" in text and "• <@1> tank" in text and "• <@8> dps" in text
    slots = {s["slot_key"]: s for s in ctx.db.run_slots(run["id"])}
    assert slots[S2]["thread_id"] == "7000" and slots[S2B]["thread_id"] == "7001"
    assert all(p["message_id"] for p in ctx.db.parties(run["id"]))


def test_party_message_marks_helpers_and_players_placed_down(ctx, make_run):
    run, ids = setup_day(ctx, make_run)
    with ctx.db.conn:
        ctx.db.conn.execute("UPDATE party_members SET is_helper = 1 WHERE discord_id = '3'")
        ctx.db.conn.execute("UPDATE party_members SET placed_down = 1 WHERE discord_id = '8'")
    confirm(ctx, run)
    slot, [(party, members)] = ctx.db.approved_slots(run["id"], D2)[0]
    text = party_message(run, slot, party, members, ctx.db.checkpoints(1))
    assert "• <@3> healer · helper" in text and "• <@8> dps · placed below their prog" in text
    assert len(text) < 2000


def test_approve_refuses_without_a_draft_and_when_already_approved(ctx, make_run):
    make_run(per_day=True)
    assert "no draft to approve" in approve(ctx).text
    assert "no draft to approve" in approve(ctx, None, D2).text


def test_approve_twice_is_refused_once_everything_is_posted(ctx, make_run):
    run, _ = setup_day(ctx, make_run)
    chan = FakeParties()
    attach(ctx, chan)
    approve(ctx, None, D2)
    again = approve(ctx, None, D2)
    assert "already approved" in again.text and len(chan.threads) == 1 and len(chan.threads[7000].sent) == 1


def test_without_a_configured_channel_the_day_is_still_confirmed(ctx, make_run):
    run, _ = setup_day(ctx, make_run)
    it = approve(ctx)
    assert "parties_channel_id" in it.text and "/myschedule" in it.text
    assert {p["status"] for p in ctx.db.parties(run["id"])} == {"confirmed"}
    assert "already approved" in approve(ctx, None, D2).text                 # nothing to retry without a channel


def test_a_channel_that_cannot_be_found_still_confirms(ctx, make_run):
    run, _ = setup_day(ctx, make_run)
    ctx.cfg["parties_channel_id"] = 999

    async def nobody(_):
        return None
    ctx.get_channel = nobody
    it = approve(ctx)
    assert "could not find channel 999" in it.text
    assert {p["status"] for p in ctx.db.parties(run["id"])} == {"confirmed"}


def test_a_failed_post_is_retried_by_approving_again_without_duplicates(ctx, make_run):
    run, _ = setup_day(ctx, make_run, (S2, S2B))
    chan = FakeParties()
    attach(ctx, chan)
    chan.fail_create = 1                                                    # the first slot's thread fails
    it = approve(ctx)
    assert "Some posts failed" in it.text and "Posted 1 parties" in it.text
    assert {p["status"] for p in ctx.db.parties(run["id"])} == {"confirmed"}
    again = approve(ctx, None, D2)
    assert "posting the parties that are missing" in again.text and "Posted 1 parties" in again.text
    assert sum(len(t.sent) for t in chan.threads.values()) == 2 and len(chan.threads) == 2


def test_a_party_message_that_failed_is_the_only_one_reposted(ctx, make_run):
    run, _ = setup_day(ctx, make_run)
    chan = FakeParties()
    attach(ctx, chan)
    orig = chan.create_thread

    async def flaky(*a, **k):
        t = await orig(*a, **k)
        t.fail = 1
        return t
    chan.create_thread = flaky
    assert "Some posts failed" in approve(ctx).text
    assert "Posted 1 parties" in approve(ctx, None, D2).text
    assert len(chan.threads[7000].sent) == 1


def test_default_day_is_the_earliest_with_a_draft(ctx, make_run):
    run, ids = setup_day(ctx, make_run)
    add_party_players(ctx, run, ["2026-11-03T17:00"], first_id=30)
    import_draft(ctx.db, run, draft_for_day(ctx, run, D3, "2026-11-03T17:00", [str(30 + i) for i in range(8)]), ctx.cfg, D3)
    it = approve(ctx)
    assert "Mon 02 Nov" in it.text
    assert ctx.db.day_party_counts(run["id"], D2) == (0, 1) and ctx.db.day_party_counts(run["id"], D3) == (1, 0)


def test_approving_needs_an_organizer(ctx, make_run):
    setup_day(ctx, make_run)
    it = player(5)
    run_async(handlers.draft_approve(ctx, it))
    assert "Only organizers" in it.text


def test_myschedule_shows_the_party_once_it_is_confirmed(ctx, make_run, clock):
    run, ids = setup_day(ctx, make_run)
    it = player(1)
    run_async(handlers.myschedule(ctx, it))
    assert "Your confirmed parties" not in it.text and "draft is with the organizers" in it.text
    approve(ctx)
    it = player(1)
    run_async(handlers.myschedule(ctx, it))
    assert "Your confirmed parties:" in it.text and "as tank" in it.text


def test_an_imported_draft_cannot_replace_an_approved_day(ctx, make_run):
    run, ids = setup_day(ctx, make_run)
    approve(ctx)
    import pytest
    from bot.db import DraftConflict
    with pytest.raises(DraftConflict):
        import_draft(ctx.db, run, draft_for_day(ctx, run, D2, S2, [str(i) for i in range(1, 9)]), ctx.cfg, D2)


def test_post_day_parties_without_threads_to_make_is_a_noop(ctx, make_run):
    run, _ = setup_day(ctx, make_run)
    confirm(ctx, run)
    chan = FakeParties()
    attach(ctx, chan)
    posted, errors = run_async(post_day_parties(ctx, run, D2, chan))
    assert (posted, errors) == (1, [])
    assert run_async(post_day_parties(ctx, run, D2, chan)) == (0, [])


def test_thread_names_and_messages_fit_discords_limits(ctx, make_run):
    run = make_run("N" * 80, per_day=True)
    ids = add_party_players(ctx, run, [S2])
    import_draft(ctx.db, run, draft_for_day(ctx, run, D2, S2, ids), ctx.cfg, D2)
    chan = FakeParties()
    attach(ctx, chan)
    approve(ctx, run["name"], D2)
    thread = next(iter(chan.threads.values()))
    assert len(thread.name) <= 100 and all(len(m["content"]) <= 2000 for m in thread.sent)
