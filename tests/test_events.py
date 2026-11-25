"""Event specs: the validator, the stored spec, announcing and opening on schedule, and the guided form."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import discord
import pytest

from bot import event_form, events, handlers, openings
from conftest import NOW, FakeAttachment, organizer, player, run_async

UTC = timezone.utc
FIGHT = "Ultima Weapon (Ultimate)"


def spec(**kw):
    base = {"name": "UWU week 1", "content": FIGHT, "days": ["2026-11-02", "2026-11-04"]}
    base.update(kw)
    return base


def resolve(ctx, s, now=NOW):
    return events.resolve(s, ctx.cfg, ctx.grid, ctx.db, now)


# ---------------------------------------------------------------- the validator

def test_defaults_come_from_config_and_the_plan_is_canonical(ctx):
    plan, errors = resolve(ctx, spec(days=["2026-11-04", {"date": "2026-11-02"}]))
    assert errors == [] and [d.day.isoformat() for d in plan.days] == ["2026-11-02", "2026-11-04"]   # sorted
    assert len(plan.days[0].slots) == 11 and plan.days[0].slots[0].key == "2026-11-02T08:00"
    assert plan.spec["version"] == 1 and plan.spec["content"] == FIGHT
    assert [d["date"] for d in plan.spec["days"]] == ["2026-11-02", "2026-11-04"]
    assert plan.cutoffs["2026-11-02"] == datetime(2026, 11, 2, 4, 59, tzinfo=UTC)          # 23:59 the evening before, EST


def test_a_range_expands_and_strings_are_shorthand(ctx):
    plan, _ = resolve(ctx, spec(days={"from": "2026-11-02", "to": "2026-11-05"}))
    assert [d.day.day for d in plan.days] == [2, 3, 4, 5]
    plan, _ = resolve(ctx, spec(days=["2026-11-02", {"date": "2026-11-03"}]))
    assert len(plan.days) == 2


def test_a_day_can_have_its_own_window_slot_length_and_cutoff(ctx):
    plan, errors = resolve(ctx, spec(first_start="17:00", last_start="20:00", days=[
        "2026-11-02",
        {"date": "2026-11-04", "first_start": "19:00", "last_start": "21:00", "slot_minutes": 120,
         "cutoff_at": "2026-11-03 18:00"}]))
    assert errors == []
    mon, wed = plan.days
    assert [s.key[11:] for s in mon.slots] == ["17:00", "18:30", "20:00"] and mon.slots[0].minutes == 90
    assert [s.key[11:] for s in wed.slots] == ["19:00", "21:00"] and wed.slots[0].minutes == 120
    assert wed.cutoff_utc == datetime(2026, 11, 3, 23, 0, tzinfo=UTC)                     # 18:00 EST
    assert len(plan.slots) == 5 and set(plan.cutoffs) == {"2026-11-02", "2026-11-04"}


def test_slots_do_not_have_to_sit_on_the_old_grid(ctx):
    plan, _ = resolve(ctx, spec(first_start="19:00", last_start="21:00", slot_minutes=60, days=["2026-11-02"]))
    assert [s.key[11:] for s in plan.slots] == ["19:00", "20:00", "21:00"]


def test_cutoff_timezone_and_explicit_cutoff_are_read_in_that_timezone(ctx):
    plan, errors = resolve(ctx, spec(cutoff_timezone="America/Sao_Paulo", days=[
        {"date": "2026-11-04", "cutoff_at": "2026-11-03T12:00"}]))
    assert errors == [] and plan.days[0].cutoff_utc == datetime(2026, 11, 3, 15, 0, tzinfo=UTC)   # BRT is UTC-3
    assert plan.spec["cutoff_timezone"] == "America/Sao_Paulo"
    assert "not a timezone" in resolve(ctx, spec(cutoff_timezone="Mars/Base"))[1][0]


def test_every_problem_is_reported(ctx):
    _, errors = resolve(ctx, spec(name="", content="Nope", first_start="x", slot_minutes=3, colour="red",
                                  cutoff={"time": "99:99", "days_before": 12}))
    joined = " ".join(errors)
    for part in ("name of 1 to 60", "know the content", "first_start must look like", "between 15 and 480",
                 "Unknown field", "cutoff.time must look like", "days_before"):
        assert part in joined, part
    assert resolve(ctx, "not an object")[1] == ["The event must be a JSON object."]
    assert "must be a date" in " ".join(resolve(ctx, spec(days=["Nov 2"]))[1])
    assert "Day 1 must be" in " ".join(resolve(ctx, spec(days=[5]))[1])
    assert "cutoff_at must look like" in " ".join(resolve(ctx, spec(days=[{"date": "2026-11-02", "cutoff_at": "tomorrow"}]))[1])
    assert "Unknown field(s) in day 1: when" in " ".join(resolve(ctx, spec(days=[{"date": "2026-11-02", "when": 1}]))[1])


def test_a_cutoff_after_the_first_slot_is_refused(ctx):
    _, errors = resolve(ctx, spec(days=[{"date": "2026-11-02", "cutoff_at": "2026-11-02 12:00"}]))
    assert "before that day's first slot" in errors[0]


def test_announce_and_open_at_rules(ctx):
    plan, errors = resolve(ctx, spec(announce={"channel_id": "123", "ping_role_id": 456, "message": "Hi",
                                                "open_at": "2026-10-28 12:00"}))
    assert errors == [] and plan.open_at_utc == datetime(2026, 10, 28, 16, 0, tzinfo=UTC)   # EDT
    assert plan.announce == {"channel_id": "123", "ping_role_id": 456, "message": "Hi", "open_at": "2026-10-28 12:00"}
    bad = lambda **a: " ".join(resolve(ctx, spec(announce=a))[1])                              # noqa: E731
    assert "already in the past" in bad(open_at="2026-10-20 12:00")
    assert "before the first day's cutoff" in bad(open_at="2026-11-05 12:00")
    assert "open_at must look like" in bad(open_at="soon")
    assert "Discord id" in bad(channel_id="general") and "Discord id" in bad(ping_role_id="@everyone")
    assert "at most 500" in bad(message="x" * 501)
    assert "Unknown field(s) in announce" in bad(colour="red")


def test_describe_lists_every_day(ctx):
    plan, _ = resolve(ctx, spec(announce={"channel_id": "123", "ping_role_id": "456"}))
    text = events.describe(plan)
    assert "UWU week 1" in text and "Mon 02 Nov" in text and "Wed 04 Nov" in text and "11 × 90 min" in text
    assert "<#123>" in text and "<@&456>" in text and "/run open" in text
    with_open, _ = resolve(ctx, spec(announce={"open_at": "2026-10-28 12:00"}))
    assert "Opens by itself" in events.describe(with_open)


# ---------------------------------------------------------------- the stored spec and /run spec

def test_a_run_made_before_specs_gets_a_rebuilt_spec(ctx, make_run):
    run = make_run(per_day=True)
    assert run["spec"] is None
    rebuilt = events.spec_from_run(ctx.db, run, ctx.cfg, ctx.grid)
    assert rebuilt["name"] == "UWU week 1" and rebuilt["content"] == FIGHT
    assert [d["date"] for d in rebuilt["days"]] == ["2026-11-01", "2026-11-02", "2026-11-03"]
    first = rebuilt["days"][1]
    assert (first["first_start"], first["last_start"], first["slot_minutes"]) == ("08:00", "23:00", 90)
    assert first["cutoff_at"] == "2026-11-01 23:59"
    plan, errors = events.resolve(dict(rebuilt, name="Copy"), ctx.cfg, ctx.grid, ctx.db, NOW)
    assert errors == [] and len(plan.slots) == len(ctx.db.run_slots(run["id"]))             # the rebuilt spec is valid


def test_run_spec_sends_a_file_that_creates_the_same_event(ctx):
    run_async(handlers.submit_event(ctx, organizer(), spec(first_start="17:00", last_start="20:00")))
    it = organizer()
    run_async(handlers.run_spec(ctx, it, "uwu week 1"))
    [sent] = it.response.sent
    assert "uwu-week-1.event.json" == sent["file"].filename
    stored = json.loads(sent["file"].fp.read().decode())
    assert stored["name"] == "UWU week 1" and stored["first_start"] == "17:00"
    stored["name"] = "Copy"
    again = organizer()
    run_async(handlers.run_create(ctx, again, FakeAttachment(json.dumps(stored).encode())))
    assert "Created" in again.text
    a, b = (ctx.db.get_run(100, n) for n in ("uwu week 1", "copy"))
    assert [s["slot_key"] for s in ctx.db.run_slots(a["id"])] == [s["slot_key"] for s in ctx.db.run_slots(b["id"])]
    nope = organizer()
    run_async(handlers.run_spec(ctx, nope, "missing"))
    assert "No run called" in nope.text


# ---------------------------------------------------------------- announcing and opening

class FakeChannel:
    def __init__(self, id, fail=False):
        self.id, self.fail, self.sent = id, fail, []

    async def send(self, content=None, **kwargs):
        if self.fail:
            raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "no")
        self.sent.append({"content": content, **kwargs})
        return SimpleNamespace(id=9000 + len(self.sent))


@pytest.fixture
def channels(ctx):
    chans = {11: FakeChannel(11), 22: FakeChannel(22)}

    async def get_channel(cid):
        return chans[cid]
    ctx.get_channel = get_channel
    return chans


def created(ctx, **kw):
    run_async(handlers.submit_event(ctx, organizer(), spec(**kw)))
    return ctx.db.get_run(100, "uwu week 1")


def test_opening_posts_the_announcement_with_the_role_ping_once(ctx, channels):
    run = created(ctx, announce={"channel_id": "11", "ping_role_id": "456", "message": "Bring snacks"})
    it = organizer()
    run_async(handlers.run_open(ctx, it, "uwu week 1"))
    assert "is open" in it.text and "Announced in <#11>" in it.text
    [post] = channels[11].sent
    assert post["content"].startswith("<@&456> **UWU week 1** is open for signups!") and "Bring snacks" in post["content"]
    assert "Mon 02 Nov" in post["content"] and "/availability" in post["content"]
    assert post["allowed_mentions"].roles is True and post["allowed_mentions"].everyone is False
    assert ctx.db.run(run["id"])["announce_message_id"] == "9001"
    ctx.db.set_run_status(run["id"], "draft")                                    # reopening does not post again
    run_async(handlers.run_open(ctx, organizer(), "uwu week 1"))
    assert len(channels[11].sent) == 1


def test_no_channel_means_no_announcement_and_a_failed_post_warns(ctx, channels):
    created(ctx)
    it = organizer()
    run_async(handlers.run_open(ctx, it, "uwu week 1"))
    assert "is open" in it.text and "Announced" not in it.text and not channels[11].sent
    channels[22].fail = True
    run_async(handlers.submit_event(ctx, organizer(), spec(name="Second", announce={"channel_id": "22"})))
    ctx.db.set_run_status(ctx.db.get_run(100, "uwu week 1")["id"], "closed")
    it = organizer()
    run_async(handlers.run_open(ctx, it, "second"))
    assert "is open" in it.text and "could not post the announcement" in it.text
    assert ctx.db.run(ctx.db.get_run(100, "second")["id"])["status"] == "open"             # opening still worked


def test_an_event_opens_by_itself_at_open_at(ctx, channels, clock):
    run = created(ctx, announce={"channel_id": "11", "open_at": "2026-10-28 12:00"})        # 16:00 UTC
    assert run["open_at_utc"] == "2026-10-28T16:00:00+00:00" and run["status"] == "draft"
    assert run_async(openings.open_scheduled(ctx)) == 0                                    # too early
    clock["now"] = datetime(2026, 10, 28, 16, 0, tzinfo=UTC)
    assert run_async(openings.open_scheduled(ctx)) == 1
    done = ctx.db.run(run["id"])
    assert done["status"] == "open" and done["open_at_utc"] is None and len(channels[11].sent) == 1
    assert run_async(openings.open_scheduled(ctx)) == 0                                    # once only


def test_a_scheduled_opening_that_cannot_happen_tells_the_reminder_channel_once(ctx, channels, clock, make_run):
    ctx.cfg["reminder_channel_id"] = 22
    make_run("Busy", status="open", deadline=datetime(2026, 11, 9, tzinfo=UTC))             # still taking signups
    run = created(ctx, announce={"open_at": "2026-10-28 12:00"})
    clock["now"] = datetime(2026, 10, 29, tzinfo=UTC)
    assert run_async(openings.open_scheduled(ctx)) == 0
    assert ctx.db.run(run["id"])["status"] == "draft" and ctx.db.run(run["id"])["open_at_utc"] is None
    [note] = channels[22].sent
    assert "could not open **UWU week 1**" in note["content"] and "Busy" in note["content"]
    assert run_async(openings.open_scheduled(ctx)) == 0 and len(channels[22].sent) == 1    # not retried


# ---------------------------------------------------------------- the guided form

def fill(modal, **values):
    for key, value in values.items():
        item = getattr(modal, key)
        if isinstance(item, discord.ui.TextInput):
            item._value = value
        else:
            item._values = value if isinstance(value, list) else [value]


def open_form(ctx):
    it = organizer()
    run_async(handlers.run_create(ctx, it))
    sent = it.response.sent[0]
    return sent["view"], sent["content"]


async def press(view, item, user=None):
    it = user or organizer()
    await item.callback(it)
    return it


def test_the_form_builds_the_event_step_by_step(ctx, channels):
    view, text = open_form(ctx)
    assert "Press **Basics**" in text and view.create_button.disabled and view.days_select.disabled

    async def scenario():
        basics = await press(view, next(c for c in view.children if getattr(c, "label", "") == "Basics"))
        modal = basics.response.modals[0]
        fill(modal, name="  UWU week 1 ", content=FIGHT, dates="2026-11-02 to 2026-11-05")
        sub = organizer()
        await modal.on_submit(sub)
        assert "4 days" in sub.response.edits[-1]["content"] and not view.create_button.disabled
        assert [o.value for o in view.days_select.options if o.default] == ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05"]
        view.days_select._values = ["2026-11-02", "2026-11-04"]                      # untick two days
        await press(view, view.days_select)
        assert view.draft.days == ["2026-11-02", "2026-11-04"]

        times = (await press(view, next(c for c in view.children if getattr(c, "label", "") == "Times"))).response.modals[0]
        fill(times, first="17:00", last="20:00", minutes="")
        await times.on_submit(organizer())
        cutoff = (await press(view, next(c for c in view.children if getattr(c, "label", "") == "Cutoff"))).response.modals[0]
        fill(cutoff, time="20:00", before="2", tz="America/Sao_Paulo")
        await cutoff.on_submit(organizer())

        view.day_edit._values = ["2026-11-04"]                                       # one day differs
        day = (await press(view, view.day_edit)).response.modals[0]
        day.items["first_start"]._value, day.items["slot_minutes"]._value = "19:00", "120"
        await day.on_submit(organizer())
        assert "✎" in [o.label for o in view.day_edit.options][1]

        ann = (await press(view, next(c for c in view.children if getattr(c, "label", "") == "Announce"))).response.modals[0]
        ann.channel._values = [SimpleNamespace(id=11)]
        ann.role._values = [SimpleNamespace(id=456)]
        ann.message._value, ann.open_at._value = "Let's go", "2026-10-28 12:00"
        await ann.on_submit(organizer())
        assert "<#11>" in view.render() and "<@&456>" in view.render() and "Opens by itself" in view.render()

        done = organizer()
        await press(view, view.create_button, done)
        return done
    done = run_async(scenario())
    assert "Created the event" in done.text and done.response.edits[-1]["view"] is None
    run = ctx.db.get_run(100, "uwu week 1")
    assert [d["day"] for d in ctx.db.run_days(run["id"])] == ["2026-11-02", "2026-11-04"]
    s = run["spec"]
    assert s["first_start"] == "17:00" and s["cutoff"] == {"time": "20:00", "days_before": 2}
    assert s["cutoff_timezone"] == "America/Sao_Paulo" and s["days"][1] == {"first_start": "19:00", "slot_minutes": 120,
                                                                            "date": "2026-11-04"}
    assert s["announce"] == {"channel_id": "11", "ping_role_id": "456", "message": "Let's go", "open_at": "2026-10-28 12:00"}
    assert run["open_at_utc"] == "2026-10-28T15:00:00+00:00"                          # 12:00 in Sao Paulo (UTC-3)


def test_the_form_shows_problems_and_blocks_create(ctx):
    view, _ = open_form(ctx)

    async def scenario():
        modal = (await press(view, next(c for c in view.children if getattr(c, "label", "") == "Basics"))).response.modals[0]
        fill(modal, name="X", content=FIGHT, dates="2026-11-02")
        await modal.on_submit(organizer())
        assert not view.create_button.disabled
        times = (await press(view, next(c for c in view.children if getattr(c, "label", "") == "Times"))).response.modals[0]
        fill(times, first="25:00", last="", minutes="")
        sub = organizer()
        await times.on_submit(sub)
        assert view.create_button.disabled and "Fix these" in sub.response.edits[-1]["content"]
        assert "first_start must look like" in sub.response.edits[-1]["content"]
        bad = (await press(view, next(c for c in view.children if getattr(c, "label", "") == "Basics"))).response.modals[0]
        fill(bad, name="X", content=FIGHT, dates="next week")
        sub = organizer()
        await bad.on_submit(sub)
        assert "Dates:" in sub.response.edits[-1]["content"] and view.draft.days == ["2026-11-02"]   # unchanged
        far = (await press(view, next(c for c in view.children if getattr(c, "label", "") == "Basics"))).response.modals[0]
        fill(far, name="X", content=FIGHT, dates="2026-11-02 to 2026-12-30")
        sub = organizer()
        await far.on_submit(sub)
        assert "at most 14 days" in sub.response.edits[-1]["content"]
    run_async(scenario())
    assert ctx.db.get_run(100) is None


def test_only_the_organizer_who_opened_the_form_can_use_it_and_cancel_creates_nothing(ctx):
    view, _ = open_form(ctx)
    assert run_async(view.interaction_check(organizer())) is True
    stranger = organizer()
    stranger.user.id = 77
    assert run_async(view.interaction_check(stranger)) is False
    it = organizer()
    run_async(view.cancel(it))
    assert "Cancelled" in it.response.edits[-1]["content"] and ctx.db.get_run(100) is None


def test_form_widgets_respect_discords_limits(ctx):
    view, text = open_form(ctx)
    assert len(text) <= 2000 and len(view.children) <= 25
    assert {c.row for c in view.children} <= {0, 1, 2, 3}

    async def scenario():
        for label in ("Basics", "Times", "Cutoff", "Announce"):
            m = (await press(view, next(c for c in view.children if getattr(c, "label", "") == label))).response.modals[0]
            assert len(m.title) <= 45 and 1 <= len(m.children) <= 5
            for child in m.children:
                assert len(child.text) <= 45 and len(child.description or "") <= 100
        basics = (await press(view, next(c for c in view.children if getattr(c, "label", "") == "Basics"))).response.modals[0]
        assert len(basics.content.options) <= 25
    run_async(scenario())


def test_parse_range():
    from datetime import date
    assert event_form.parse_range("2026-11-02 to 2026-11-08") == (date(2026, 11, 2), date(2026, 11, 8))
    assert event_form.parse_range("2026-11-02") == (date(2026, 11, 2), date(2026, 11, 2))
    assert event_form.parse_range("2026-11-02 – 2026-11-03")[1] == date(2026, 11, 3)
    with pytest.raises(ValueError):
        event_form.parse_range("2026-11-05 to 2026-11-02")
    with pytest.raises(ValueError):
        event_form.parse_range("whenever")
