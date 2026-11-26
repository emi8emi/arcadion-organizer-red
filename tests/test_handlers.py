"""Command handlers and the /availability menu, driven with fake interactions (no Discord connection)."""
import json
from datetime import timedelta
from types import SimpleNamespace

import discord

from bot.services import MAX_PER_RUN
from bot import handlers
from bot.db import split_roles
from bot.solver_io import build_signups, import_draft
from bot.views import AvailabilityView, DoneButton, LimitsButton
from conftest import (pick_roles, add_party_players, draft_for_day, NOW, ORGANIZER_ROLE_ID, SLOT, FakeAttachment, add_player, draft_for, fill_one_party,
                      organizer, player, run_async)


def create_spec(**kw):
    """An event spec (bot/events.py): three days, everything else default."""
    spec = {"name": "UWU week 1", "content": "Ultima Weapon (Ultimate)",
            "days": {"from": "2026-11-01", "to": "2026-11-03"}}
    spec.update(kw)
    return spec


def create(ctx, it, **kw):
    run_async(handlers.submit_event(ctx, it, create_spec(**kw)))
    return it


def do(ctx, handler, it, *args, **kwargs):
    run_async(handler(ctx, it, *args, **kwargs))
    return it


# ---------------------------------------------------------------- permissions

def test_organizer_commands_refuse_regular_players(ctx, make_run):
    make_run()
    for handler, args in [(handlers.run_create, ()), (handlers.run_open, ()), (handlers.run_close, ()),
                          (handlers.run_status, ()), (handlers.run_spec, ()), (handlers.draft_export, ())]:
        it = player(5)
        do(ctx, handler, it, *args)
        assert "Only organizers" in it.text, handler.__name__
    it = do(ctx, handlers.draft_import, player(5), FakeAttachment(b"{}"))
    assert "Only organizers" in it.text
    it = do(ctx, handlers.helper_grant, player(5), SimpleNamespace(id=9, display_name="X"))
    assert "Only organizers" in it.text


def test_organizer_role_id_counts_as_organizer(ctx):
    ctx.cfg["organizer_role_id"] = ORGANIZER_ROLE_ID
    it = do(ctx, handlers.run_create, player(5, role_ids=(ORGANIZER_ROLE_ID,)))          # no file: opens the form
    assert it.response.sent and "New event" in it.response.sent[0]["content"]
    it = do(ctx, handlers.run_create, player(6, role_ids=(1,)))
    assert "Only organizers" in it.text


# ---------------------------------------------------------------- /run

def test_creating_an_event_builds_the_slot_grid(ctx):
    it = create(ctx, organizer())
    assert "33 candidate slots" in it.text and "3 days" in it.text and "Created" in it.text
    run = ctx.db.get_run(100, "uwu week 1")
    assert run["status"] == "draft" and run["created_by"] == "1"
    assert len(ctx.db.run_slots(run["id"])) == 33
    assert run["spec"]["name"] == "UWU week 1" and [d["date"] for d in run["spec"]["days"]] == [
        "2026-11-01", "2026-11-02", "2026-11-03"]                              # the spec is kept on the run


def test_creating_an_event_sets_one_cutoff_per_day_the_evening_before(ctx):
    create(ctx, organizer())
    run = ctx.db.get_run(100, "uwu week 1")
    # 23:59 Eastern the evening before; 2026-11-01 is the day the clocks fall back (EDT before it, EST after)
    assert {d["day"]: d["cutoff_utc"] for d in ctx.db.run_days(run["id"])} == {
        "2026-11-01": "2026-11-01T03:59:00+00:00",     # Oct 31 23:59 EDT
        "2026-11-02": "2026-11-02T04:59:00+00:00",     # Nov 1 23:59 EST
        "2026-11-03": "2026-11-03T04:59:00+00:00"}


def test_cutoff_options(ctx):
    create(ctx, organizer(), cutoff={"time": "20:30", "days_before": 2})
    run = ctx.db.get_run(100, "uwu week 1")
    assert ctx.db.run_day(run["id"], "2026-11-03")["cutoff_utc"] == "2026-11-02T01:30:00+00:00"   # Nov 1 20:30 EST
    it = create(ctx, organizer(), name="Same day", cutoff={"time": "07:00", "days_before": 0})
    assert "Created" in it.text
    assert ctx.db.run_day(ctx.db.get_run(100, "same day")["id"], "2026-11-02")["cutoff_utc"] == "2026-11-02T12:00:00+00:00"


def test_cutoff_can_use_another_timezone(ctx):
    create(ctx, organizer(), cutoff_timezone="America/Sao_Paulo")
    run = ctx.db.get_run(100, "uwu week 1")
    assert ctx.db.run_day(run["id"], "2026-11-02")["cutoff_utc"] == "2026-11-02T02:59:00+00:00"   # Nov 1 23:59 BRT (UTC-3)


def test_warns_when_no_reminder_channel_is_set(ctx):
    assert "No reminder channel" in create(ctx, organizer()).text
    ctx.cfg["reminder_channel_id"] = 123
    assert "No reminder channel" not in create(ctx, organizer(), name="B").text


def test_the_start_window_limits_the_candidate_slots(ctx):
    it = create(ctx, organizer(), first_start="17:00", last_start="20:00")
    assert "9 candidate slots" in it.text                                        # 17:00, 18:30 and 20:00 on three days


def test_creating_rejects_bad_input_and_says_everything_wrong_at_once(ctx, make_run):
    cases = [
        (dict(content="Some Other Raid"), "I don't know the content"),
        (dict(days={"from": "11/01/2026", "to": "2026-11-03"}), "days.from must be a date"),
        (dict(days={"from": "2026-11-03", "to": "2026-10-30"}), "days.to is before"),
        (dict(days={"from": "2026-11-01", "to": "2026-12-30"}), "at most 14 days"),
        (dict(cutoff={"time": "abc"}), "cutoff.time must look like"),
        (dict(cutoff={"days_before": 9}), "from 0"),
        (dict(cutoff={"days_before": 0}), "before that day's first slot"),       # 23:59 on the day itself
        (dict(first_start="5pm"), "first_start must look like"),
        (dict(name=" "), "name of 1 to 60"),
        (dict(first_start="23:30", last_start="23:45", slot_minutes=90), None),   # one slot is fine
        (dict(first_start="21:00", last_start="20:00"), "last_start is before first_start"),
        (dict(days=[]), "at least one day"),
        (dict(days=["2026-11-01", "2026-11-01"]), "listed twice"),
        (dict(slot_minutes=5), "between 15 and 480"),
        (dict(version=2), "version 1"),
        (dict(colour="red"), "Unknown field"),
    ]
    for override, message in cases:
        it = create(ctx, organizer(), **{"name": "X" + str(len(ctx.db.conn.execute("SELECT id FROM runs").fetchall())),
                                         **override})
        if message:
            assert message in it.text, (override, it.text)
    # several problems are reported together
    it = create(ctx, organizer(), name=" ", content="Nope", first_start="x")
    assert it.text.count("•") >= 3
    make_run("Taken")
    assert "already exists" in create(ctx, organizer(), name="taken").text


def test_an_uploaded_json_file_creates_the_event(ctx):
    import json
    it = do(ctx, handlers.run_create, organizer(), FakeAttachment(json.dumps(create_spec()).encode()))
    assert "Created" in it.text and ctx.db.get_run(100, "uwu week 1") is not None
    bad = do(ctx, handlers.run_create, organizer(), FakeAttachment(b"{nope"))
    assert "not valid JSON" in bad.text
    big = do(ctx, handlers.run_create, organizer(), FakeAttachment(b"{}", size=10**7))
    assert "too big" in big.text
    empty = do(ctx, handlers.run_create, organizer(), FakeAttachment(b"{}"))
    assert "I could not create the event" in empty.text and "name" in empty.text


def test_open_and_close_lifecycle(ctx, make_run):
    run = make_run(status="draft")
    it = do(ctx, handlers.run_open, organizer())
    assert "is open" in it.text and ctx.db.run(run["id"])["status"] == "open"
    assert "already open" in do(ctx, handlers.run_open, organizer()).text

    second = make_run("Second", status="draft")
    assert "Several events are live" in do(ctx, handlers.run_close, organizer()).text    # which one? it asks
    assert "is open" in do(ctx, handlers.run_open, organizer(), "Second").text           # a second event at once
    assert {r["name"] for r in ctx.db.open_runs(100)} == {"UWU week 1", "Second"}

    add_player(ctx, run, 1, ["melee"], 0, [SLOT])
    it = do(ctx, handlers.run_close, organizer(), "UWU week 1")
    assert "closed for every day, with 1 players" in it.text and ctx.db.run(run["id"])["status"] == "closed"
    assert ctx.db.run(second["id"])["status"] == "open"
    assert "is not open" in do(ctx, handlers.run_close, organizer(), "UWU week 1").text


def test_open_refuses_after_the_deadline(ctx, make_run, clock):
    make_run(status="draft")
    clock["now"] = NOW + timedelta(days=10)
    assert "past its cutoff" in do(ctx, handlers.run_open, organizer()).text


def test_unknown_run_name(ctx):
    assert "There are no runs yet" in do(ctx, handlers.run_open, organizer()).text
    assert "No run called 'x'" in do(ctx, handlers.run_close, organizer(), "x").text


def test_run_status_shows_the_heatmap(ctx, make_run):
    run = make_run()
    fill_one_party(ctx, run, SLOT)
    it = do(ctx, handlers.run_status, organizer())
    assert "8 players have ticked slots" in it.text and "17:00   1  1  1  1  2  1  1    8  ✓ ~1" in it.text
    assert it.response.sent[0]["ephemeral"] is True
    assert "Days look like" in do(ctx, handlers.run_status, organizer(), None, "bad").text


def test_run_status_sends_extra_messages_for_long_runs(ctx, make_run):
    from datetime import date
    run = make_run("Long", start=date(2026, 11, 1), end=date(2026, 11, 14))
    keys = [s["slot_key"] for s in ctx.db.run_slots(run["id"])]
    add_player(ctx, run, 1, ["melee"], 0, keys)
    it = do(ctx, handlers.run_status, organizer())
    assert len(it.response.sent) == 1 and len(it.followup.sent) >= 1
    assert all(m["ephemeral"] for m in it.followup.sent)


# ---------------------------------------------------------------- /availability menu

def test_availability_needs_an_open_run(ctx, make_run, clock):
    assert "No run is open" in do(ctx, handlers.availability, player(5)).text
    make_run()
    clock["now"] = NOW + timedelta(days=10)
    assert "closed for every day" in do(ctx, handlers.availability, player(5)).text


def test_availability_rejects_unknown_timezone(ctx, make_run):
    make_run()
    assert "not a timezone name" in do(ctx, handlers.availability, player(5), "Mars/Base").text
    it = do(ctx, handlers.availability, player(5), "America/Chicago")
    assert ctx.db.get_player(5)["timezone"] == "America/Chicago" and it.response.sent[0]["view"]


def test_menu_saves_every_click(ctx, make_run):
    run = make_run()

    async def scenario():
        ctx.db.upsert_player(5, "Ann", now=NOW)
        with ctx.db.conn:                                    # one verified character: no "pick a character" warning
            ctx.db.conn.execute("INSERT INTO characters (discord_id, lodestone_id, name, world, verified_utc) "
                                "VALUES ('5', '111', 'Ann Tank', 'Exodus', 'x')")
        it = player(5, "Ann")
        await handlers.availability(ctx, it)
        msg = it.response.sent[0]
        view = msg["view"]
        assert isinstance(view, AvailabilityView) and msg["ephemeral"] is True
        assert len(view.children) == 7           # Roles, Limits, Done, prog, day, character, slots
        assert [o.value for o in view.prog_select.options] == [str(i) for i in range(20)]
        assert [o.value for o in view.day_select.options] == ["2026-11-01", "2026-11-02", "2026-11-03"]
        assert "Prog: **not set**" in msg["content"] and msg["content"].count("<t:") == 4   # the picked day's cutoff plus one per day

        assert view.roles_button.style == discord.ButtonStyle.danger and "none" in view.roles_button.label   # nothing selected
        assert "No roles selected yet" in msg["content"]
        await pick_roles(view, player(5), ["melee", "main_tank"])        # the Roles modal; green once any role is set
        assert split_roles(ctx.db.get_player(5)["roles"]) == ["main_tank", "melee"]
        assert view.roles_button.style == discord.ButtonStyle.success and view.roles_button.label == "Roles (2)"

        view.prog_select._values = ["11"]
        await view.prog_select.callback(player(5))
        assert ctx.db.get_player(5)["prog_index"] == 11
        assert [o.value for o in view.prog_select.options if o.default] == ["11"]

        view.day_select._values = ["2026-11-02"]
        await view.day_select.callback(player(5))
        assert view.day == "2026-11-02"
        assert [o.label for o in view.slot_select.options][6] == "17:00 EST"
        assert all(o.value.startswith("2026-11-02") for o in view.slot_select.options)

        it2 = player(5)
        view.slot_select._values = [SLOT, "2026-11-02T18:30"]
        await view.slot_select.callback(it2)
        assert ctx.db.ticked_keys(run["id"], 5) == [SLOT, "2026-11-02T18:30"]
        shown = it2.response.edits[-1]["content"]
        assert shown.count("• <t:") == 2        # one bullet per ticked slot, as Discord timestamps
        assert "⚠" not in shown
        assert [o.default for o in view.slot_select.options].count(True) == 2

        view.day = "2026-11-03"                              # ticking another day leaves day two alone
        view.slot_select._values = ["2026-11-03T20:00"]
        await view.slot_select.callback(player(5))
        assert ctx.db.ticked_keys(run["id"], 5) == [SLOT, "2026-11-02T18:30", "2026-11-03T20:00"]
        view.day = "2026-11-02"
        view.slot_select._values = []
        await view.slot_select.callback(player(5))
        assert ctx.db.ticked_keys(run["id"], 5) == ["2026-11-03T20:00"]

        assert await view.interaction_check(player(5)) and not await view.interaction_check(player(6))
    run_async(scenario())


def test_menu_limits_modal_saves_and_validates(ctx, make_run):
    run = make_run()

    async def scenario():
        it = player(5, "Ann")
        await handlers.availability(ctx, it)
        view = it.response.sent[0]["view"]
        button = next(c for c in view.children if isinstance(c, LimitsButton))

        early = player(5)                                       # the lowest checkpoint is relative to the prog
        await button.callback(early)
        assert not early.response.modals and "Pick prog first" in early.text

        ctx.db.upsert_player(5, "Ann", prog_index=11, now=NOW)
        off = player(5)                                         # the per-run field is hidden unless enable_per_run_limit
        await button.callback(off)
        assert off.response.modals[0].per_run is None and len(off.response.modals[0].children) == 2
        ctx.cfg["enable_per_run_limit"] = True
        press = player(5)
        await button.callback(press)
        modal = press.response.modals[0]
        assert (modal.per_day.default, modal.per_run.default) == ("2", "99")
        assert [o.value for o in modal.min_select.options] == [str(i) for i in range(12)]    # checkpoints up to my prog
        assert [o.value for o in modal.min_select.options if o.default] == ["11"]            # default: nothing lower

        for per_day, per_run, message in [("x", "4", "whole numbers"), ("0", "4", "between 1 and 8"),
                                          ("5", "4", "cannot be smaller")]:
            modal.per_day._value, modal.per_run._value = per_day, per_run
            modal.min_select._values = ["11"]
            bad = player(5)
            await modal.on_submit(bad)
            assert message in bad.text and ctx.db.get_limits(run["id"], 5, ctx.cfg)["is_default"]

        modal.per_day._value, modal.per_run._value = "3", "6"
        modal.min_select._values = ["9"]                        # lowest I accept: 2 checkpoints below my prog
        ok = player(5)
        await modal.on_submit(ok)
        lim = ctx.db.get_limits(run["id"], 5, ctx.cfg)
        assert (lim["max_per_day"], lim["max_per_run"], lim["drop_checkpoints"]) == (3, 6, 2)
        assert ok.response.edits                                 # the menu is redrawn after the modal

        again = player(5)                                       # reopening preselects what was saved
        await button.callback(again)
        assert [o.value for o in again.response.modals[0].min_select.options if o.default] == ["9"]

        done = next(c for c in view.children if isinstance(c, DoneButton))
        fin = player(5)
        await done.callback(fin)
        assert "Run" in fin.text and fin.response.edits[-1]["view"] is None
    run_async(scenario())


def test_menu_stops_saving_after_the_deadline(ctx, make_run, clock):
    make_run()

    async def scenario():
        it = player(5, "Ann")
        await handlers.availability(ctx, it)
        view = it.response.sent[0]["view"]
        clock["now"] = NOW + timedelta(days=10)                 # deadline passes while the menu is open
        late = player(5)
        await view.roles_button.callback(late)
        assert "closed for every day" in late.text and late.response.edits[-1]["view"] is None
        assert ctx.db.get_player(5)["roles"] == ""
    run_async(scenario())


def test_a_new_player_has_no_roles_and_is_warned_until_they_pick_some(ctx, make_run):
    make_run()

    async def scenario():
        it = player(7, "Bo")
        await handlers.availability(ctx, it)
        view = it.response.sent[0]["view"]
        assert ctx.db.get_player(7)["roles"] == ""                                  # nothing is selected for them
        assert it.response.sent[0]["content"].startswith("⚠ **No roles selected yet.**")
        assert "will not be placed in any party" in it.response.sent[0]["content"]
        modal_roles = player(7)
        await view.roles_button.callback(modal_roles)
        assert not [o for o in modal_roles.response.modals[0].select.options if o.default]
        quit_early = player(7)                                                      # Done without roles repeats the warning
        await next(c for c in view.children if isinstance(c, DoneButton)).callback(quit_early)
        assert "no roles selected" in quit_early.response.edits[-1]["content"]
        again = player(7)
        await handlers.availability(ctx, again)
        view = again.response.sent[0]["view"]
        await pick_roles(view, player(7), ["regen"])
        assert "No roles selected" not in view.render() and view.roles_button.style == discord.ButtonStyle.success
        third = player(7)
        await handlers.availability(ctx, third)                                     # a chosen set is kept
        assert split_roles(ctx.db.get_player(7)["roles"]) == ["regen"]
        assert "No roles selected" not in third.response.sent[0]["content"]
    run_async(scenario())


# ---------------------------------------------------------------- /limits and /myschedule

def test_limits_command_shows_and_changes_limits(ctx, make_run):
    run = make_run()
    it = do(ctx, handlers.limits, player(5))
    assert "up to 2 per day, 99 per run" in it.text and "defaults" in it.text
    it = do(ctx, handlers.limits, player(5), 3, None, 2)
    assert "up to 3 per day, 99 per run" in it.text and "2 checkpoint(s) below" in it.text and "defaults" not in it.text
    assert ctx.db.get_limits(run["id"], 5, ctx.cfg)["drop_checkpoints"] == 2


def test_limits_command_validates(ctx, make_run):
    make_run()
    for args, message in [((0, None, None), "between 1 and 8"), ((None, MAX_PER_RUN + 1, None), f"between 1 and {MAX_PER_RUN}"),
                          ((None, 1, None), "cannot be smaller than your max per day"),
                          ((None, None, 20), "between 0")]:
        assert message in do(ctx, handlers.limits, player(5), *args).text, args
    do(ctx, handlers.limits, player(5), 2, 4, None)          # the default per-run max is 99, so store a small one first
    assert "larger than your max per run" in do(ctx, handlers.limits, player(5), 5, None, None).text


def test_limits_command_needs_an_open_run(ctx, make_run, clock):
    assert "No run is open" in do(ctx, handlers.limits, player(5)).text
    make_run()
    clock["now"] = NOW + timedelta(days=10)
    assert "closed for every day" in do(ctx, handlers.limits, player(5), 3).text


def test_myschedule_shows_ticks_and_only_confirmed_parties(ctx, make_run):
    run = make_run()
    ids = fill_one_party(ctx, run, SLOT)
    it = do(ctx, handlers.myschedule, player(1))
    assert "Slots you ticked (1)" in it.text and "<t:" in it.text and "Roles: Main tank" in it.text
    assert "confirmed parties" not in it.text

    import_draft(ctx.db, run, draft_for(ctx, run, [str(i) for i in ids]), ctx.cfg)
    it = do(ctx, handlers.myschedule, player(1))
    assert "draft is with the organizers" in it.text and "as Main tank" not in it.text      # drafts stay private

    with ctx.db.conn:
        ctx.db.conn.execute("UPDATE parties SET status = 'confirmed'")
    it = do(ctx, handlers.myschedule, player(1))
    assert "Your confirmed parties" in it.text and "as Main tank" in it.text


def test_myschedule_without_runs(ctx):
    assert "no runs yet" in do(ctx, handlers.myschedule, player(1)).text


# ---------------------------------------------------------------- /draft

def test_export_attaches_and_saves_the_solver_input(ctx, make_run, cfg):
    run = make_run(status="closed")
    fill_one_party(ctx, run, SLOT)
    it = do(ctx, handlers.draft_export, organizer())
    msg = it.response.sent[0]
    assert "picked the earliest closed day not exported yet: Mon 02 Nov" in msg["content"]
    assert "Exported **UWU week 1**, Mon 02 Nov: 8 players, 11 slots" in msg["content"] and "fingerprint" in msg["content"]
    assert "still open" not in msg["content"]
    name = "uwu-week-1-2026-11-02-signups.json"
    assert isinstance(msg["file"], discord.File) and msg["file"].filename == name
    saved = json.loads((handlers.Path(cfg["export_dir"]) / name).read_text())
    assert saved == build_signups(ctx.db, run, ctx.cfg, "2026-11-02")[0]
    assert json.loads(msg["file"].fp.read()) == saved
    assert ctx.db.run_day(run["id"], "2026-11-02")["exported_utc"] is not None


def test_export_refuses_an_open_day_unless_forced(ctx, make_run):
    run = make_run(status="open")
    fill_one_party(ctx, run, SLOT)
    ctx.db.upsert_player(50, "No Profile")
    ctx.db.set_day_availability(run["id"], 50, "2026-11-02", [SLOT])

    assert "No closed day is waiting" in do(ctx, handlers.draft_export, organizer()).text
    refused = do(ctx, handlers.draft_export, organizer(), None, "2026-11-02").text
    assert "still open" in refused and "force:true" in refused and not ctx.db.run_day(run["id"], "2026-11-02")["exported_utc"]

    text = do(ctx, handlers.draft_export, organizer(), None, "2026-11-02", True).text
    assert "Signups for that day are still open" in text and "No Profile" in text and "8 players" in text
    # a forced early export must not silence the reminders that follow the cutoff
    assert ctx.db.run_day(run["id"], "2026-11-02")["exported_utc"] is None


def test_export_day_errors(ctx, make_run):
    run = make_run(status="closed")
    assert "Days look like" in do(ctx, handlers.draft_export, organizer(), None, "bad").text
    assert "not a day of" in do(ctx, handlers.draft_export, organizer(), None, "2026-12-25").text
    assert "Nobody is ready" in do(ctx, handlers.draft_export, organizer(), None, "2026-11-02").text
    assert ctx.db.run_day(run["id"], "2026-11-02")["exported_utc"] is None


def test_import_stores_a_draft(ctx, make_run):
    run = make_run(status="closed")
    ids = fill_one_party(ctx, run, SLOT)
    body = json.dumps(draft_for(ctx, run, [str(i) for i in ids])).encode()
    it = do(ctx, handlers.draft_import, organizer(), FakeAttachment(body))
    assert "Draft stored" in it.text and "1 parties in 1 slots, 8 players seated" in it.text
    assert "nothing is posted to players" in it.text and "Only 0 signed up" in it.text
    assert len(ctx.db.parties(run["id"])) == 1


def test_import_refuses_a_stale_draft(ctx, make_run):
    run = make_run(status="closed")
    ids = fill_one_party(ctx, run, SLOT)
    body = json.dumps(draft_for(ctx, run, [str(i) for i in ids])).encode()
    ctx.db.set_day_availability(run["id"], 8, "2026-11-02", [])
    it = do(ctx, handlers.draft_import, organizer(), FakeAttachment(body))
    assert "Draft refused" in it.text and "different signups" in it.text
    assert ctx.db.parties(run["id"]) == []


def test_import_rejects_bad_files(ctx, make_run):
    make_run(status="closed")
    assert "not valid JSON" in do(ctx, handlers.draft_import, organizer(), FakeAttachment(b"not json {")).text
    assert "not valid JSON" in do(ctx, handlers.draft_import, organizer(), FakeAttachment(b"\xff\xfe")).text
    assert "does not look like a solver draft" in do(ctx, handlers.draft_import, organizer(), FakeAttachment(b"[]")).text
    big = FakeAttachment(b"{}", size=handlers.MAX_DRAFT_BYTES + 1)
    assert "too large" in do(ctx, handlers.draft_import, organizer(), big).text


def test_import_refuses_to_replace_confirmed_parties(ctx, make_run):
    run = make_run(status="closed")
    ids = fill_one_party(ctx, run, SLOT)
    body = json.dumps(draft_for(ctx, run, [str(i) for i in ids])).encode()
    do(ctx, handlers.draft_import, organizer(), FakeAttachment(body))
    with ctx.db.conn:
        ctx.db.conn.execute("UPDATE parties SET status = 'confirmed'")
    assert "already has confirmed parties" in do(ctx, handlers.draft_import, organizer(), FakeAttachment(body)).text


def test_import_finds_the_day_from_the_fingerprint(ctx, make_run):
    run = make_run(status="closed", per_day=True)
    ids = add_party_players(ctx, run, ["2026-11-01T17:00", SLOT])
    for day in ("2026-11-01", "2026-11-02"):
        do(ctx, handlers.draft_export, organizer(), None, day)
    body = json.dumps(draft_for_day(ctx, run, "2026-11-01", "2026-11-01T17:00", ids)).encode()
    it = do(ctx, handlers.draft_import, organizer(), FakeAttachment(body))
    assert "Draft stored" in it.text
    assert [p["slot_key"] for p in ctx.db.parties(run["id"])] == ["2026-11-01T17:00"]
    other = json.dumps(draft_for_day(ctx, run, "2026-11-02", SLOT, ids)).encode()
    assert "Draft stored" in do(ctx, handlers.draft_import, organizer(), FakeAttachment(other)).text
    assert len(ctx.db.parties(run["id"])) == 2          # the second import did not wipe the first day


# ---------------------------------------------------------------- /helper

def test_helper_grant_and_revoke_feed_the_export(ctx, make_run):
    run = make_run(status="closed")
    fill_one_party(ctx, run, SLOT)
    member = SimpleNamespace(id=3, display_name="Cy")
    assert "is now a helper" in do(ctx, handlers.helper_grant, organizer(), member).text
    flags = {p["id"]: p["helper"] for p in build_signups(ctx.db, run, ctx.cfg)[0]["players"]}
    assert flags["3"] is True and sum(flags.values()) == 1
    assert "no longer a helper" in do(ctx, handlers.helper_revoke, organizer(), member).text
    assert "was not a helper" in do(ctx, handlers.helper_revoke, organizer(), member).text
    assert "Which content?" in do(ctx, handlers.helper_grant, organizer(), member, "Nope").text


def test_defaults_from_env_do_not_leak_into_another_server(ctx):
    """ORGANIZER_ROLE_ID belongs to the home server (100); in server 200 that role id means nothing."""
    from bot.handlers import is_organizer, settings_lines
    ctx.cfg["organizer_role_id"] = ORGANIZER_ROLE_ID
    home = player(5, role_ids=[ORGANIZER_ROLE_ID], guild_id=100)
    away = player(5, role_ids=[ORGANIZER_ROLE_ID], guild_id=200)
    assert is_organizer(ctx, home.user) and not is_organizer(ctx, away.user)
    ctx.db.set_guild_setting(200, "organizer_role_id", 4242)              # their own /settings
    assert is_organizer(ctx, player(6, role_ids=[4242], guild_id=200).user)
    assert not is_organizer(ctx, player(6, role_ids=[ORGANIZER_ROLE_ID], guild_id=200).user)
    assert "(bot default)" in "\n".join(settings_lines(ctx, 100)) and "(bot default)" not in "\n".join(settings_lines(ctx, 300))


def test_checkpoints_are_labelled_by_phase_number_not_boss_name():
    from bot.services import checkpoint_label
    cps = [{"idx": 0, "phase": "Garuda", "name": "Fresh"}, {"idx": 1, "phase": "Garuda", "name": "Cleanse"},
           {"idx": 2, "phase": "Ifrit", "name": "Fresh"}, {"idx": 3, "phase": "Titan", "name": "Gaols"}]
    assert [checkpoint_label(cps, i) for i in range(4)] == ["0: P1 Fresh", "1: P1 Cleanse", "2: P2 Fresh", "3: P3 Gaols"]
    assert checkpoint_label(list(reversed(cps)), 3) == "3: P3 Gaols"            # numbering follows idx, not list order
    assert checkpoint_label(cps, 9) == "not set" and checkpoint_label(cps, None) == "not set"
