"""Command handlers and the /availability menu, driven with fake interactions (no Discord connection)."""
import json
from datetime import timedelta
from types import SimpleNamespace

import discord

from bot import handlers
from bot.db import split_roles
from bot.solver_io import build_signups, import_draft
from bot.views import AvailabilityView, DoneButton, LimitsButton
from conftest import (NOW, ORGANIZER_ROLE_ID, SLOT, FakeAttachment, add_player, draft_for, fill_one_party,
                      organizer, player, run_async)


def create_args(**kw):
    args = dict(name="UWU week 1", content="Ultima Weapon (Ultimate)", start_date="2026-11-01",
                end_date="2026-11-03", signup_deadline="2026-10-30 18:00")
    args.update(kw)
    return args


def do(ctx, handler, it, *args, **kwargs):
    run_async(handler(ctx, it, *args, **kwargs))
    return it


# ---------------------------------------------------------------- permissions

def test_organizer_commands_refuse_regular_players(ctx, make_run):
    make_run()
    for handler, args in [(handlers.run_create, ()), (handlers.run_open, ()), (handlers.run_close, ()),
                          (handlers.run_status, ()), (handlers.draft_export, ())]:
        it = player(5)
        kwargs = create_args() if handler is handlers.run_create else {}
        do(ctx, handler, it, *args, **kwargs)
        assert "Only organizers" in it.text, handler.__name__
    it = do(ctx, handlers.draft_import, player(5), FakeAttachment(b"{}"))
    assert "Only organizers" in it.text
    it = do(ctx, handlers.helper_grant, player(5), SimpleNamespace(id=9, display_name="X"))
    assert "Only organizers" in it.text


def test_organizer_role_id_counts_as_organizer(ctx):
    ctx.cfg["organizer_role_id"] = ORGANIZER_ROLE_ID
    it = do(ctx, handlers.run_create, player(5, role_ids=(ORGANIZER_ROLE_ID,)), **create_args())
    assert "Created" in it.text
    it = do(ctx, handlers.run_create, player(6, role_ids=(1,)), **create_args(name="Other"))
    assert "Only organizers" in it.text


# ---------------------------------------------------------------- /run

def test_run_create_builds_the_slot_grid(ctx):
    it = do(ctx, handlers.run_create, organizer(), **create_args())
    assert "33 candidate slots" in it.text and "3 days" in it.text and "<t:" in it.text
    run = ctx.db.get_run(100, "uwu week 1")
    assert run["status"] == "draft" and run["created_by"] == "1"
    assert len(ctx.db.run_slots(run["id"])) == 33


def test_run_create_can_limit_the_candidate_slots(ctx):
    it = do(ctx, handlers.run_create, organizer(), **create_args(earliest_start="17:00", latest_start="20:00"))
    assert "9 candidate slots" in it.text


def test_run_create_rejects_bad_input(ctx, make_run):
    cases = [
        (dict(content="Some Other Raid"), "I don't know the content"),
        (dict(start_date="11/01/2026"), "Dates look like"),
        (dict(end_date="2026-10-30"), "end date is before"),
        (dict(end_date="2026-12-30"), "at most 14 days"),
        (dict(signup_deadline="abc"), "deadline looks like"),
        (dict(signup_deadline="2026-11-02 09:00"), "before the first slot"),
        (dict(earliest_start="5pm"), "look like 17:00"),
        (dict(name=" "), "name of 1 to 60"),
        (dict(earliest_start="23:30", latest_start="23:45"), "no slots"),
    ]
    for override, message in cases:
        it = do(ctx, handlers.run_create, organizer(), **create_args(**override))
        assert message in it.text, (override, it.text)
    assert ctx.db.get_run(100) is None                                  # nothing was created
    make_run("Taken")
    it = do(ctx, handlers.run_create, organizer(), **create_args(name="taken"))
    assert "already exists" in it.text


def test_open_and_close_lifecycle(ctx, make_run):
    run = make_run(status="draft")
    it = do(ctx, handlers.run_open, organizer())
    assert "is open" in it.text and ctx.db.run(run["id"])["status"] == "open"
    assert "already open" in do(ctx, handlers.run_open, organizer()).text

    second = make_run("Second", status="draft")
    assert "still open" in do(ctx, handlers.run_open, organizer(), "Second").text

    add_player(ctx, run, 1, ["dps"], 0, [SLOT])
    it = do(ctx, handlers.run_close, organizer())              # newest run is "Second", which is not open
    assert "is not open" in it.text
    it = do(ctx, handlers.run_close, organizer(), "UWU week 1")
    assert "closed with 1 players" in it.text and ctx.db.run(run["id"])["status"] == "closed"
    assert ctx.db.run(second["id"])["status"] == "draft"


def test_open_refuses_after_the_deadline(ctx, make_run, clock):
    make_run(status="draft")
    clock["now"] = NOW + timedelta(days=10)
    assert "has passed" in do(ctx, handlers.run_open, organizer()).text


def test_unknown_run_name(ctx):
    assert "There are no runs yet" in do(ctx, handlers.run_open, organizer()).text
    assert "No run called 'x'" in do(ctx, handlers.run_close, organizer(), "x").text


def test_run_status_shows_the_heatmap(ctx, make_run):
    run = make_run()
    fill_one_party(ctx, run, SLOT)
    it = do(ctx, handlers.run_status, organizer())
    assert "8 players have ticked slots" in it.text and "17:00   2  2  4    8  ✓ ~1" in it.text
    assert it.response.sent[0]["ephemeral"] is True
    assert "day looks like" in do(ctx, handlers.run_status, organizer(), None, "bad").text


def test_run_status_sends_extra_messages_for_long_runs(ctx, make_run):
    from datetime import date
    run = make_run("Long", start=date(2026, 11, 1), end=date(2026, 11, 14))
    keys = [s["slot_key"] for s in ctx.db.run_slots(run["id"])]
    add_player(ctx, run, 1, ["dps"], 0, keys)
    it = do(ctx, handlers.run_status, organizer())
    assert len(it.response.sent) == 1 and len(it.followup.sent) >= 1
    assert all(m["ephemeral"] for m in it.followup.sent)


# ---------------------------------------------------------------- /availability menu

def test_availability_needs_an_open_run(ctx, make_run, clock):
    assert "No run is open" in do(ctx, handlers.availability, player(5)).text
    make_run()
    clock["now"] = NOW + timedelta(days=10)
    assert "Signups closed" in do(ctx, handlers.availability, player(5)).text


def test_availability_rejects_unknown_timezone(ctx, make_run):
    make_run()
    assert "not a timezone name" in do(ctx, handlers.availability, player(5), "Mars/Base").text
    it = do(ctx, handlers.availability, player(5), "America/Chicago")
    assert ctx.db.get_player(5)["timezone"] == "America/Chicago" and it.response.sent[0]["view"]


def test_menu_saves_every_click(ctx, make_run):
    run = make_run()

    async def scenario():
        it = player(5, "Ann")
        await handlers.availability(ctx, it)
        msg = it.response.sent[0]
        view = msg["view"]
        assert isinstance(view, AvailabilityView) and msg["ephemeral"] is True
        assert len(view.children) == 6
        assert [o.value for o in view.prog_select.options] == [str(i) for i in range(19)]
        assert [o.value for o in view.day_select.options] == ["2026-11-01", "2026-11-02", "2026-11-03"]
        assert "⚠ Set your roles and prog" in msg["content"] and msg["content"].count("<t:") == 1   # only the deadline

        view.role_select._values = ["dps", "tank"]
        await view.role_select.callback(player(5))
        assert split_roles(ctx.db.get_player(5)["roles"]) == ["tank", "dps"]
        assert [o.default for o in view.role_select.options] == [True, False, True]

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
        assert shown.count("<t:") >= 3 and "Slots ticked (2)" in shown     # deadline + two slots, as Discord timestamps
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
        press = player(5)
        await button.callback(press)
        modal = press.response.modals[0]
        assert (modal.per_day.default, modal.per_run.default, modal.drop.default) == ("2", "4", "0")

        for per_day, per_run, drop, message in [("x", "4", "0", "whole numbers"), ("0", "4", "0", "between 1 and 8"),
                                                ("5", "4", "0", "cannot be smaller"), ("2", "4", "19", "between 0")]:
            modal.per_day._value, modal.per_run._value, modal.drop._value = per_day, per_run, drop
            bad = player(5)
            await modal.on_submit(bad)
            assert message in bad.text and ctx.db.get_limits(run["id"], 5, ctx.cfg)["is_default"]

        modal.per_day._value, modal.per_run._value, modal.drop._value = "3", "6", "2"
        ok = player(5)
        await modal.on_submit(ok)
        lim = ctx.db.get_limits(run["id"], 5, ctx.cfg)
        assert (lim["max_per_day"], lim["max_per_run"], lim["drop_checkpoints"]) == (3, 6, 2)
        assert "up to 3 per day, 6 per run" in ok.response.edits[-1]["content"]

        done = next(c for c in view.children if isinstance(c, DoneButton))
        fin = player(5)
        await done.callback(fin)
        assert "Saved" in fin.text and fin.response.edits[-1]["view"] is None
    run_async(scenario())


def test_menu_stops_saving_after_the_deadline(ctx, make_run, clock):
    make_run()

    async def scenario():
        it = player(5, "Ann")
        await handlers.availability(ctx, it)
        view = it.response.sent[0]["view"]
        clock["now"] = NOW + timedelta(days=10)                 # deadline passes while the menu is open
        late = player(5)
        view.role_select._values = ["tank"]
        await view.role_select.callback(late)
        assert "Signups closed" in late.text and late.response.edits[-1]["view"] is None
        assert ctx.db.get_player(5)["roles"] == ""
    run_async(scenario())


# ---------------------------------------------------------------- /limits and /myschedule

def test_limits_command_shows_and_changes_limits(ctx, make_run):
    run = make_run()
    it = do(ctx, handlers.limits, player(5))
    assert "up to 2 per day, 4 per run" in it.text and "defaults" in it.text
    it = do(ctx, handlers.limits, player(5), 3, None, 2)
    assert "up to 3 per day, 4 per run" in it.text and "2 checkpoint(s) below" in it.text and "defaults" not in it.text
    assert ctx.db.get_limits(run["id"], 5, ctx.cfg)["drop_checkpoints"] == 2


def test_limits_command_validates(ctx, make_run):
    make_run()
    for args, message in [((0, None, None), "between 1 and 8"), ((None, 99, None), "between 1 and 40"),
                          ((None, 1, None), "cannot be smaller than your max per day"),
                          ((5, None, None), "larger than your max per run"), ((None, None, 19), "between 0")]:
        assert message in do(ctx, handlers.limits, player(5), *args).text, args


def test_limits_command_needs_an_open_run(ctx, make_run, clock):
    assert "No run is open" in do(ctx, handlers.limits, player(5)).text
    make_run()
    clock["now"] = NOW + timedelta(days=10)
    assert "Signups closed" in do(ctx, handlers.limits, player(5), 3).text


def test_myschedule_shows_ticks_and_only_confirmed_parties(ctx, make_run):
    run = make_run()
    ids = fill_one_party(ctx, run, SLOT)
    it = do(ctx, handlers.myschedule, player(1))
    assert "Slots you ticked (1)" in it.text and "<t:" in it.text and "Roles: tank" in it.text
    assert "confirmed parties" not in it.text

    import_draft(ctx.db, run, draft_for(ctx, run, [str(i) for i in ids]), ctx.cfg)
    it = do(ctx, handlers.myschedule, player(1))
    assert "draft is with the organizers" in it.text and "as tank" not in it.text      # drafts stay private

    with ctx.db.conn:
        ctx.db.conn.execute("UPDATE parties SET status = 'confirmed'")
    it = do(ctx, handlers.myschedule, player(1))
    assert "Your confirmed parties" in it.text and "as tank" in it.text


def test_myschedule_without_runs(ctx):
    assert "no runs yet" in do(ctx, handlers.myschedule, player(1)).text


# ---------------------------------------------------------------- /draft

def test_export_attaches_and_saves_the_solver_input(ctx, make_run, cfg):
    run = make_run(status="closed")
    fill_one_party(ctx, run, SLOT)
    it = do(ctx, handlers.draft_export, organizer())
    msg = it.response.sent[0]
    assert "Exported **UWU week 1**: 8 players, 33 slots" in msg["content"] and "fingerprint" in msg["content"]
    assert "still open" not in msg["content"]
    assert isinstance(msg["file"], discord.File) and msg["file"].filename == "uwu-week-1-signups.json"
    saved = json.loads((handlers.Path(cfg["export_dir"]) / "uwu-week-1-signups.json").read_text())
    assert saved == build_signups(ctx.db, run, ctx.cfg)[0]
    assert json.loads(msg["file"].fp.read()) == saved


def test_export_warns_about_open_runs_and_skipped_players(ctx, make_run):
    run = make_run(status="open")
    fill_one_party(ctx, run, SLOT)
    ctx.db.upsert_player(50, "No Profile")
    ctx.db.set_day_availability(run["id"], 50, "2026-11-02", [SLOT])
    text = do(ctx, handlers.draft_export, organizer()).text
    assert "Signups are still open" in text and "No Profile" in text and "8 players" in text


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
