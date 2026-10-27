"""Per-day signup cutoffs: the cutoff clock, gating, /run close and /run extend, status, the old-schema migration."""
import re
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import discord

from bot import handlers
from bot.config import load_config
from bot.db import DB
from bot.grid import GridConfig, day_cutoff, parse_hm
from bot.services import Ctx, day_state, signup_open
from conftest import NOW, S1, S2, at, organizer, player, run_async

UTC = timezone.utc
NY = "America/New_York"
OLD_SCHEMA = Path(__file__).resolve().parent / "fixtures" / "schema_before_run_days.sql"


def do(ctx, handler, it, *args, **kwargs):
    run_async(handler(ctx, it, *args, **kwargs))
    return it


# ---------------------------------------------------------------- the cutoff clock

def test_cutoff_is_a_fixed_clock_time_across_dst_changes():
    hm = parse_hm("23:59")
    # Clocks fall back on 2026-11-01 (EDT -> EST) ...
    assert day_cutoff(date(2026, 11, 1), hm, 1, NY) == datetime(2026, 11, 1, 3, 59, tzinfo=UTC)    # Oct 31 23:59 EDT
    assert day_cutoff(date(2026, 11, 2), hm, 1, NY) == datetime(2026, 11, 2, 4, 59, tzinfo=UTC)    # Nov 1 23:59 EST
    # ... and spring forward on 2026-03-08 (EST -> EDT).
    assert day_cutoff(date(2026, 3, 8), hm, 1, NY) == datetime(2026, 3, 8, 4, 59, tzinfo=UTC)      # Mar 7 23:59 EST
    assert day_cutoff(date(2026, 3, 9), hm, 1, NY) == datetime(2026, 3, 9, 3, 59, tzinfo=UTC)      # Mar 8 23:59 EDT
    for day in (date(2026, 11, 1), date(2026, 11, 2), date(2026, 3, 8), date(2026, 3, 9)):
        assert day_cutoff(day, hm, 1, NY).astimezone(ZoneInfo(NY)).strftime("%H:%M") == "23:59"


def test_cutoff_days_before_and_clock_time():
    assert day_cutoff(date(2026, 11, 5), parse_hm("20:30"), 2, NY) == datetime(2026, 11, 4, 1, 30, tzinfo=UTC)   # Nov 3 20:30 EST
    assert day_cutoff(date(2026, 11, 5), parse_hm("07:00"), 0, NY) == datetime(2026, 11, 5, 12, 0, tzinfo=UTC)   # the day itself
    assert day_cutoff(date(2026, 11, 5), parse_hm("23:59"), 1, "America/Sao_Paulo") == datetime(2026, 11, 5, 2, 59, tzinfo=UTC)


# ---------------------------------------------------------------- day state and gating

def test_day_state_follows_the_cutoff_the_run_status_and_the_export(ctx, make_run, clock):
    run = make_run(per_day=True)                                    # Nov 1 closes 03:59Z, Nov 2 04:59Z, Nov 3 04:59Z
    at(clock, "2026-11-01T12:00")
    state = lambda day: day_state(ctx, ctx.db.run(run["id"]), ctx.db.run_day(run["id"], day))
    assert [state(d) for d in ("2026-11-01", "2026-11-02", "2026-11-03")] == ["closed", "open", "open"]
    ctx.db.mark_exported(run["id"], "2026-11-01", clock["now"])
    assert state("2026-11-01") == "exported"
    ctx.db.set_run_status(run["id"], "closed")                      # closing the run closes every day
    assert state("2026-11-02") == "closed"
    ctx.db.set_run_status(run["id"], "draft")
    assert state("2026-11-02") == "draft"


def test_signup_open_is_checked_per_day(ctx, make_run, clock):
    run = make_run(per_day=True)
    at(clock, "2026-11-01T12:00")
    assert signup_open(ctx, run)[0] and signup_open(ctx, run, "2026-11-02")[0]
    ok, why = signup_open(ctx, run, "2026-11-01")
    assert not ok and "Sun 01 Nov closed" in why and "<t:" in why
    assert not signup_open(ctx, run, "2026-12-25")[0]
    at(clock, "2026-11-05T12:00")
    assert signup_open(ctx, run) == (False, "Signups are closed for every day of this run.")


def test_closed_day_is_locked_but_other_days_still_save(ctx, make_run, clock):
    run = make_run(per_day=True)
    at(clock, "2026-11-01T12:00")                                   # Sun is closed, Mon and Tue are open

    async def scenario():
        it = player(5, "Ann")
        await handlers.availability(ctx, it)
        view = it.response.sent[0]["view"]
        assert view.day == "2026-11-02"                             # starts on the first day still open
        ctx.db.set_day_availability(run["id"], 5, "2026-11-01", [S1])   # ticked before Sunday's cutoff

        view.day_select._values = ["2026-11-01"]
        await view.day_select.callback(player(5))
        assert view.slot_select.disabled and "closed" in view.slot_select.placeholder
        late = player(5)                                            # a client that sends the edit anyway
        view.slot_select._values = []
        await view.slot_select.callback(late)
        assert ctx.db.ticked_keys(run["id"], 5) == [S1]             # nothing changed
        assert "Signups for Sun 01 Nov closed" in late.text and "Nothing was changed" in late.text
        assert late.response.edits[-1]["view"] is view              # the menu stays open

        roles = player(5)                                           # roles and prog are not per day
        view.role_select._values = ["tank"]
        await view.role_select.callback(roles)
        assert ctx.db.get_player(5)["roles"] == "tank"

        view.day_select._values = ["2026-11-02"]
        await view.day_select.callback(player(5))
        assert not view.slot_select.disabled
        view.slot_select._values = [S2]
        await view.slot_select.callback(player(5))
        assert ctx.db.ticked_keys(run["id"], 5) == [S1, S2]
    run_async(scenario())


def test_menu_shows_each_days_cutoff_and_the_current_prog(ctx, make_run, clock):
    run = make_run(per_day=True)
    at(clock, "2026-11-01T12:00")
    ctx.db.upsert_player(5, "Ann", roles=["tank"], prog_index=11, now=NOW)
    ctx.db.set_day_availability(run["id"], 5, "2026-11-02", [S2])

    async def scenario():
        it = player(5, "Ann")
        await handlers.availability(ctx, it)
        msg = it.response.sent[0]
        text, view = msg["content"], msg["view"]
        assert "Your prog right now: **11: P3 Gaols cleanup**" in text
        assert "Editing **Mon 02 Nov**, signups close <t:" in text
        assert "Sun 01 Nov: closed" in text
        assert re.search(r"Mon 02 Nov: closes <t:\d+:R>", text) and re.search(r"Tue 03 Nov: closes <t:\d+:R>", text)
        desc = {o.value: o.description for o in view.day_select.options}
        assert desc == {"2026-11-01": "0 ticked · closed",
                        "2026-11-02": "1 ticked · closes Sun 23:59 EST",
                        "2026-11-03": "0 ticked · closes Mon 23:59 EST"}
    run_async(scenario())


def test_long_run_menu_message_stays_under_discords_limit(ctx, make_run, clock):
    run = make_run("Long", start=date(2026, 11, 1), end=date(2026, 11, 14), per_day=True)
    keys = [s["slot_key"] for s in ctx.db.run_slots(run["id"])]
    ctx.db.upsert_player(5, "Ann", roles=["tank"], prog_index=18, now=NOW)
    for day in sorted({k[:10] for k in keys}):
        ctx.db.set_day_availability(run["id"], 5, day, [k for k in keys if k[:10] == day])

    async def scenario():
        it = player(5, "Ann")
        await handlers.availability(ctx, it)
        view = it.response.sent[0]["view"]
        for state_time in ("2026-10-30T12:00", "2026-11-06T12:00"):    # nothing closed yet / half closed
            at(clock, state_time)
            text = view.render()
            assert len(text) <= 1990
            assert text.count("<t:") == len(re.findall(r"<t:\d+:[a-zA-Z]>", text))     # no timestamp cut in half
            for option in view.day_select.options:
                assert 1 <= len(option.label) <= 100 and len(option.description) <= 100
    run_async(scenario())


# ---------------------------------------------------------------- /run close

def test_run_close_one_day_now(ctx, make_run, clock):
    run = make_run(per_day=True)
    it = do(ctx, handlers.run_close, organizer(), None, "2026-11-02")
    assert "Signups for Mon 02 Nov are closed now" in it.text and "/draft export day:2026-11-02" in it.text
    assert ctx.db.run_day(run["id"], "2026-11-02")["cutoff_utc"] == "2026-10-25T12:00:00+00:00"
    assert signup_open(ctx, run, "2026-11-03")[0] and not signup_open(ctx, run, "2026-11-02")[0]
    assert ctx.db.run(run["id"])["status"] == "open"                # the run itself stays open for the other days
    assert "already closed" in do(ctx, handlers.run_close, organizer(), None, "2026-11-02").text
    assert "not a day of" in do(ctx, handlers.run_close, organizer(), None, "2026-12-25").text
    assert "Days look like" in do(ctx, handlers.run_close, organizer(), None, "x").text


def test_run_close_without_a_day_closes_every_remaining_day(ctx, make_run, clock):
    run = make_run(per_day=True)
    at(clock, "2026-11-01T12:00")                                   # Sunday already closed on its own
    sunday = ctx.db.run_day(run["id"], "2026-11-01")["cutoff_utc"]
    it = do(ctx, handlers.run_close, organizer())
    assert "closed for every day" in it.text and ctx.db.run(run["id"])["status"] == "closed"
    days = {d["day"]: d["cutoff_utc"] for d in ctx.db.run_days(run["id"])}
    assert days["2026-11-01"] == sunday                             # a cutoff already in the past is not rewritten
    assert days["2026-11-02"] == days["2026-11-03"] == "2026-11-01T12:00:00+00:00"


# ---------------------------------------------------------------- /run extend

def test_run_extend_reopens_a_closed_day(ctx, make_run, clock):
    run = make_run(per_day=True)
    at(clock, "2026-11-01T04:30")                                   # Sunday's cutoff (03:59Z) has just passed
    assert not signup_open(ctx, run, "2026-11-01")[0]
    it = do(ctx, handlers.run_extend, organizer(), "2026-11-01", "2026-11-01 07:00")
    assert "Signups for Sun 01 Nov now close" in it.text
    assert ctx.db.run_day(run["id"], "2026-11-01")["cutoff_utc"] == "2026-11-01T12:00:00+00:00"   # 07:00 EST
    assert signup_open(ctx, run, "2026-11-01")[0]


def test_run_extend_after_an_export_forgets_the_export(ctx, make_run, clock):
    run = make_run(per_day=True)
    at(clock, "2026-11-01T04:30")
    ctx.db.mark_exported(run["id"], "2026-11-01", clock["now"])
    ctx.db.mark_reminded(run["id"], "2026-11-01", clock["now"])
    it = do(ctx, handlers.run_extend, organizer(), "2026-11-01", "2026-11-01 07:00")
    assert "had been exported" in it.text
    row = ctx.db.run_day(run["id"], "2026-11-01")
    assert row["exported_utc"] is None and row["last_reminded_utc"] is None


def test_run_extend_rejects_bad_times(ctx, make_run, clock):
    run = make_run(per_day=True)
    at(clock, "2026-11-01T04:30")
    cases = [("2026-11-01", "abc", "looks like"),
             ("2026-11-01", "2026-11-01 09:00", "before Sun 01 Nov's first slot"),    # first slot is 08:00
             ("2026-11-01", "2026-10-31 20:00", "already passed"),
             ("2026-12-25", "2026-11-01 07:00", "not a day of"),
             ("nope", "2026-11-01 07:00", "Days look like")]
    for day, when, message in cases:
        assert message in do(ctx, handlers.run_extend, organizer(), day, when).text, (day, when)
    assert ctx.db.run_day(run["id"], "2026-11-01")["cutoff_utc"] == "2026-11-01T03:59:00+00:00"   # untouched


def test_run_extend_can_use_another_timezone(ctx, make_run, clock):
    run = make_run(per_day=True)
    at(clock, "2026-11-01T04:30")
    do(ctx, handlers.run_extend, organizer(), "2026-11-01", "2026-11-01 09:00", None, "America/Sao_Paulo")
    assert ctx.db.run_day(run["id"], "2026-11-01")["cutoff_utc"] == "2026-11-01T12:00:00+00:00"   # 09:00 BRT is UTC-3


def test_run_extend_reopens_a_closed_run_unless_another_run_is_open(ctx, make_run, clock):
    run = make_run(per_day=True)
    at(clock, "2026-11-01T04:30")
    ctx.db.set_run_status(run["id"], "closed")
    other = make_run("Other", start=date(2026, 11, 10), end=date(2026, 11, 10), per_day=True)
    extend = lambda: do(ctx, handlers.run_extend, organizer(), "2026-11-01", "2026-11-01 07:00", "UWU week 1")
    assert "is open; close it" in extend().text
    assert ctx.db.run(run["id"])["status"] == "closed"
    ctx.db.set_run_status(other["id"], "closed")
    extend()
    assert ctx.db.run(run["id"])["status"] == "open"


def test_close_and_extend_are_organizer_only(ctx, make_run):
    make_run(per_day=True)
    assert "Only organizers" in do(ctx, handlers.run_close, player(5), None, "2026-11-02").text
    assert "Only organizers" in do(ctx, handlers.run_extend, player(5), "2026-11-02", "2026-11-02 07:00").text


# ---------------------------------------------------------------- /run open and /run status

def test_open_reports_closed_days_and_refuses_when_none_is_left(ctx, make_run, clock):
    run = make_run(per_day=True, status="draft")
    at(clock, "2026-11-01T12:00")
    it = do(ctx, handlers.run_open, organizer())
    assert "2 of 3 days take signups (1 already closed)" in it.text and "next cutoff is Mon 02 Nov" in it.text
    ctx.db.set_run_status(run["id"], "draft")
    at(clock, "2026-11-05T12:00")
    assert "past its cutoff" in do(ctx, handlers.run_open, organizer()).text


def test_open_closes_a_finished_run_automatically(ctx, make_run, clock):
    old = make_run("Old", status="open")                            # shared cutoff 2026-10-30 22:00Z
    new = make_run("New", start=date(2026, 11, 10), end=date(2026, 11, 10), status="draft", per_day=True)
    at(clock, "2026-11-05T12:00")
    it = do(ctx, handlers.run_open, organizer(), "New")
    assert "I closed **Old**" in it.text
    assert ctx.db.run(old["id"])["status"] == "closed" and ctx.db.run(new["id"])["status"] == "open"


def test_run_status_lists_each_days_state(ctx, make_run, clock):
    run = make_run(per_day=True)
    at(clock, "2026-11-01T12:00")
    ctx.db.mark_exported(run["id"], "2026-11-03", clock["now"])
    text = do(ctx, handlers.run_status, organizer()).text
    assert "Sun 01 Nov: closed" in text and "not exported yet" in text
    assert re.search(r"Mon 02 Nov: open, closes <t:\d+:R>", text) and "Tue 03 Nov: exported" in text
    one = do(ctx, handlers.run_status, organizer(), None, "2026-11-02").text
    assert "Mon 02 Nov: open" in one and "Sun 01 Nov" not in one


# ---------------------------------------------------------------- the old database

def test_migration_gives_old_runs_one_row_per_day(tmp_path):
    path = tmp_path / "old.sqlite3"
    old = sqlite3.connect(path)
    old.executescript(OLD_SCHEMA.read_text())                       # the schema as it was before run_days existed
    assert old.execute("SELECT name FROM sqlite_master WHERE name = 'run_days'").fetchone() is None
    old.execute("""INSERT INTO runs (id, guild_id, name, fight_id, start_date, end_date, signup_deadline_utc,
                                     status, created_by, created_utc)
                   VALUES (1, '100', 'Old run', 1, '2026-11-01', '2026-11-02', '2026-10-30T22:00:00+00:00',
                           'closed', '1', '2026-10-20T00:00:00+00:00')""")
    old.executemany("INSERT INTO slots (id, run_id, slot_key, start_utc, minutes) VALUES (?, 1, ?, ?, 90)",
                    [(1, "2026-11-01T17:00", "2026-11-01T21:00:00+00:00"),
                     (2, "2026-11-01T18:30", "2026-11-01T22:30:00+00:00"),
                     (3, "2026-11-02T17:00", "2026-11-02T22:00:00+00:00")])
    old.execute("INSERT INTO parties (id, run_id, slot_id, party_index, status, target_idx) VALUES (1, 1, 3, 0, 'draft', 0)")
    old.execute("INSERT INTO party_members (party_id, discord_id, role) VALUES (1, '9', 'dps')")
    old.commit()
    old.close()

    db = DB(str(path))
    rows = {r["day"]: r for r in db.run_days(1)}
    assert sorted(rows) == ["2026-11-01", "2026-11-02"]
    assert {r["cutoff_utc"] for r in rows.values()} == {"2026-10-30T22:00:00+00:00"}     # the old deadline
    assert rows["2026-11-01"]["exported_utc"] is None
    assert rows["2026-11-02"]["exported_utc"] == "2026-10-20T00:00:00+00:00"            # it already had a draft
    assert len(db.run_slots(1)) == 3 and len(db.parties(1)) == 1                         # nothing else changed

    db.close()
    again = DB(str(path))                                           # running the migration twice changes nothing
    assert [r["day"] for r in again.run_days(1)] == ["2026-11-01", "2026-11-02"]
    assert again.run_day(1, "2026-11-02")["exported_utc"] == "2026-10-20T00:00:00+00:00"

    cfg = load_config(env={})
    ctx = Ctx(db=again, cfg=cfg, grid=GridConfig.from_config(cfg), now=lambda: NOW)
    run = again.run(1)
    assert [day_state(ctx, run, r) for r in again.run_days(1)] == ["closed", "exported"]
    new_id = again.create_run(guild_id=100, name="New run", fight_id=1, start_date=date(2026, 11, 8),
                              end_date=date(2026, 11, 8), cutoffs={"2026-11-08": NOW}, slots=[], created_by=1, now=NOW)
    assert [r["day"] for r in again.run_days(new_id)] == ["2026-11-08"]


def test_every_select_and_modal_item_respects_discords_limits(ctx, make_run, clock):
    from bot.views import LimitsModal
    run = make_run("Long", start=date(2026, 11, 1), end=date(2026, 11, 14), per_day=True)
    ctx.db.upsert_player(5, "Ann", roles=["tank"], prog_index=18, now=NOW)

    async def scenario():
        it = player(5, "Ann")
        await handlers.availability(ctx, it)
        view = it.response.sent[0]["view"]
        for state_time in ("2026-10-30T12:00", "2026-11-02T12:00"):
            at(clock, state_time)
            for day in (d["day"] for d in ctx.db.run_days(run["id"])):
                view.day = day
                view.sync()
                for sel in (c for c in view.children if isinstance(c, discord.ui.Select)):
                    assert len(sel.placeholder or "") <= 150 and len(sel.options) <= 25
                    for o in sel.options:
                        assert 1 <= len(o.label) <= 100 and len(o.description or "") <= 100 and len(o.value) <= 100
        modal = LimitsModal(view)
        for item in modal.children:
            assert 1 <= len(item.label) <= 45
    run_async(scenario())
