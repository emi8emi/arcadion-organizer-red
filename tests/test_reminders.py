"""Export reminders: what is due and when, restarts, and the thin Discord sending layer."""
from datetime import date, datetime, timedelta, timezone

import pytest

from bot.db import DB
from bot.grid import run_slots
from bot.reminders import due_reminders, format_reminder, send_due_reminders
from conftest import NOW, S2, add_party_players, at, run_async

UTC = timezone.utc
# Mon 2 Nov: signups closed Sun 4:59Z (23:59 EST), first slot 13:00Z (08:00 EST)
CUTOFF = datetime(2026, 11, 2, 4, 59, tzinfo=UTC)
FIRST_SLOT = datetime(2026, 11, 2, 13, 0, tzinfo=UTC)


@pytest.fixture
def world(ctx, make_run):
    """A run whose Monday has 8 ready players; Sunday and Tuesday are empty."""
    run = make_run(per_day=True)
    add_party_players(ctx, run, [S2])
    return run


def due(ctx, when):
    if isinstance(when, str):
        when = datetime.fromisoformat(when).replace(tzinfo=UTC)
    return due_reminders(ctx.db, when, ctx.cfg)


def test_nothing_is_due_before_the_cutoff(ctx, world):
    assert due(ctx, CUTOFF - timedelta(minutes=1)) == []


def test_first_reminder_goes_out_at_the_cutoff_with_the_numbers(ctx, world):
    items = due(ctx, CUTOFF + timedelta(minutes=1))
    assert [(i["day"], i["ready"], i["incomplete"], i["fillable"]) for i in items] == [("2026-11-02", 8, 0, 1)]
    text = format_reminder(items[0], ctx.cfg)
    assert text == ("Signups for **UWU week 1**, Mon 02 Nov closed: 8 signups, 1 slots that could fill. "
                    "Run `/draft export day:2026-11-02`.")


def test_it_repeats_every_thirty_minutes_until_the_export(ctx, world):
    first = CUTOFF + timedelta(minutes=1)
    assert len(due(ctx, first)) == 1
    ctx.db.mark_reminded(world["id"], "2026-11-02", first)
    assert due(ctx, first + timedelta(minutes=29)) == []
    assert len(due(ctx, first + timedelta(minutes=30))) == 1
    ctx.db.mark_reminded(world["id"], "2026-11-02", first + timedelta(minutes=30))
    assert due(ctx, first + timedelta(minutes=59)) == []
    assert len(due(ctx, first + timedelta(minutes=60))) == 1


def test_the_interval_comes_from_the_config(ctx, world):
    ctx.cfg["reminder_interval_minutes"] = 10
    first = CUTOFF + timedelta(minutes=1)
    ctx.db.mark_reminded(world["id"], "2026-11-02", first)
    assert due(ctx, first + timedelta(minutes=9)) == [] and len(due(ctx, first + timedelta(minutes=10))) == 1


def test_it_stops_once_the_day_is_exported(ctx, world):
    first = CUTOFF + timedelta(minutes=1)
    ctx.db.mark_reminded(world["id"], "2026-11-02", first)
    ctx.db.mark_exported(world["id"], "2026-11-02", first + timedelta(minutes=5))
    assert due(ctx, first + timedelta(hours=3)) == []


def test_it_stops_when_the_days_first_slot_has_started(ctx, world):
    ctx.db.mark_reminded(world["id"], "2026-11-02", CUTOFF)
    assert len(due(ctx, FIRST_SLOT - timedelta(minutes=1))) == 1
    assert due(ctx, FIRST_SLOT) == []
    assert due(ctx, FIRST_SLOT + timedelta(hours=2)) == []


def test_each_closed_day_is_reminded_separately(ctx, make_run):
    run = make_run(per_day=True)
    add_party_players(ctx, run, ["2026-11-01T17:00", S2], first_id=1)
    # Sunday and Monday are both closed and both have signups; Sunday's first slot is still ahead
    now = datetime(2026, 11, 2, 4, 59, tzinfo=UTC) + timedelta(minutes=1)
    got = due_reminders(ctx.db, datetime(2026, 11, 1, 5, 0, tzinfo=UTC), ctx.cfg)      # Sunday closed, Monday still open
    assert [i["day"] for i in got] == ["2026-11-01"]
    got = due_reminders(ctx.db, datetime(2026, 11, 1, 12, 59, tzinfo=UTC), ctx.cfg)
    assert [i["day"] for i in got] == ["2026-11-01"]
    ctx.db.mark_reminded(run["id"], "2026-11-01", now)
    got = due_reminders(ctx.db, FIRST_SLOT - timedelta(minutes=1), ctx.cfg)            # Sunday's slots started, Monday closed
    assert [i["day"] for i in got] == ["2026-11-02"]


def test_a_day_nobody_is_ready_for_gets_one_notice_and_no_repeats(ctx, make_run):
    run = make_run(per_day=True)                                   # nobody signed up at all
    when = datetime(2026, 11, 2, 5, 0, tzinfo=UTC)
    items = due_reminders(ctx.db, when, ctx.cfg)
    assert [(i["day"], i["ready"]) for i in items] == [("2026-11-02", 0)]
    assert "nobody ready to play" in format_reminder(items[0], ctx.cfg)
    ctx.db.mark_reminded(run["id"], "2026-11-02", when)
    assert due_reminders(ctx.db, when + timedelta(hours=3), ctx.cfg) == []


def test_players_without_roles_or_prog_do_not_count_as_ready(ctx, world):
    ctx.db.upsert_player(77, "No Profile")
    ctx.db.set_day_availability(world["id"], 77, "2026-11-02", [S2])
    items = due(ctx, CUTOFF + timedelta(minutes=1))
    assert (items[0]["ready"], items[0]["incomplete"]) == (8, 1)
    assert "8 signups (1 more still need roles or prog)" in format_reminder(items[0], ctx.cfg)


def test_the_organizer_role_is_pinged_when_configured(ctx, world):
    ctx.cfg["organizer_role_id"] = 777
    item = due(ctx, CUTOFF + timedelta(minutes=1))[0]
    assert format_reminder(item, ctx.cfg).startswith("<@&777> Signups for")


def test_draft_runs_are_never_reminded_but_closed_runs_are(ctx, world):
    ctx.db.set_run_status(world["id"], "draft")
    assert due(ctx, CUTOFF + timedelta(minutes=1)) == []
    ctx.db.set_run_status(world["id"], "closed")                   # /run close
    assert len(due(ctx, CUTOFF + timedelta(minutes=1))) == 1


# ---------------------------------------------------------------- restarts (state lives in the database file)

def make_file_run(db):
    slots = run_slots(date(2026, 11, 2), date(2026, 11, 2), __import__("bot.grid", fromlist=["GridConfig"]).GridConfig())
    cutoffs = {"2026-11-02": CUTOFF}
    run_id = db.create_run(guild_id=100, name="File run", fight_id=1, start_date=date(2026, 11, 2),
                           end_date=date(2026, 11, 2), cutoffs=cutoffs, slots=slots, created_by=1, now=NOW)
    db.set_run_status(run_id, "open")
    for i in range(8):
        db.upsert_player(i + 1, f"P{i}", roles=[["tank", "tank", "healer", "healer", "dps", "dps", "dps", "dps"][i]],
                         prog_index=0, now=NOW)
        db.set_day_availability(run_id, i + 1, "2026-11-02", [S2])
    return run_id


def test_a_restart_neither_forgets_nor_repeats(tmp_path, cfg):
    path = str(tmp_path / "bot.sqlite3")
    first = DB(path)
    run_id = make_file_run(first)
    sent_at = CUTOFF + timedelta(minutes=1)
    assert len(due_reminders(first, sent_at, cfg)) == 1
    first.mark_reminded(run_id, "2026-11-02", sent_at)
    first.close()                                                   # the bot restarts

    second = DB(path)
    assert due_reminders(second, sent_at + timedelta(minutes=5), cfg) == []             # no double send
    assert len(due_reminders(second, sent_at + timedelta(minutes=30), cfg)) == 1        # and no forgetting


def test_a_long_outage_catches_up_with_one_reminder_not_a_burst(tmp_path, cfg):
    path = str(tmp_path / "bot.sqlite3")
    db = DB(path)
    run_id = make_file_run(db)
    back_online = CUTOFF + timedelta(hours=3)                       # the bot was down since before the cutoff
    items = due_reminders(db, back_online, cfg)
    assert len(items) == 1                                          # one message, not six
    db.mark_reminded(run_id, "2026-11-02", back_online)
    assert due_reminders(db, back_online + timedelta(minutes=1), cfg) == []
    assert due_reminders(db, FIRST_SLOT + timedelta(minutes=1), cfg) == []             # too late once the slots started


# ---------------------------------------------------------------- the Discord side

class FakeChannel:
    def __init__(self, fail=0):
        self.sent, self.fail = [], fail

    async def send(self, content, **kwargs):
        if self.fail:
            self.fail -= 1
            raise RuntimeError("Missing Permissions")
        self.sent.append((content, kwargs))


def run_send(ctx, channel):
    async def get_channel(channel_id):
        assert channel_id == 555
        return channel
    return run_async(send_due_reminders(ctx, get_channel))


def test_sending_marks_the_day_as_reminded(ctx, world, clock):
    ctx.cfg["reminder_channel_id"] = 555
    at(clock, "2026-11-02T05:00")
    channel = FakeChannel()
    assert run_send(ctx, channel) == 1
    assert "Run `/draft export day:2026-11-02`" in channel.sent[0][0]
    assert channel.sent[0][1]["allowed_mentions"].roles is True
    assert ctx.db.run_day(world["id"], "2026-11-02")["last_reminded_utc"] == "2026-11-02T05:00:00+00:00"
    assert run_send(ctx, channel) == 0 and len(channel.sent) == 1             # a minute later: nothing new
    at(clock, "2026-11-02T05:30")
    assert run_send(ctx, channel) == 1 and len(channel.sent) == 2


def test_a_failed_send_is_retried_and_not_marked(ctx, world, clock):
    ctx.cfg["reminder_channel_id"] = 555
    at(clock, "2026-11-02T05:00")
    channel = FakeChannel(fail=1)
    assert run_send(ctx, channel) == 0
    assert ctx.db.run_day(world["id"], "2026-11-02")["last_reminded_utc"] is None
    at(clock, "2026-11-02T05:01")
    assert run_send(ctx, channel) == 1


def test_no_channel_configured_sends_and_marks_nothing(ctx, world, clock):
    at(clock, "2026-11-02T05:00")
    assert ctx.cfg["reminder_channel_id"] is None

    async def never(_):
        raise AssertionError("must not look up a channel")
    assert run_async(send_due_reminders(ctx, never)) == 0
    assert ctx.db.run_day(world["id"], "2026-11-02")["last_reminded_utc"] is None


def test_a_missing_channel_is_not_fatal(ctx, world, clock):
    ctx.cfg["reminder_channel_id"] = 555
    at(clock, "2026-11-02T05:00")
    assert run_send(ctx, None) == 0
    assert ctx.db.run_day(world["id"], "2026-11-02")["last_reminded_utc"] is None
