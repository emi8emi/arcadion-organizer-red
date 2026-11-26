"""Availability-only events (when2meet style): the spec, the form, the player view, the heatmap and the guards."""
from datetime import datetime, timedelta, timezone

import discord

from bot import event_form, events, handlers, heatmap
from bot.reminders import due_reminders
from bot.views import AvailabilityView
from conftest import NOW, SLOT, add_player, organizer, player, run_async

UTC = timezone.utc


def spec(**kw):
    base = {"name": "Game night", "mode": "availability", "days": ["2026-11-02", "2026-11-03"]}
    base.update(kw)
    return base


def resolve(ctx, s):
    return events.resolve(s, ctx.cfg, ctx.grid, ctx.db, NOW)


def make(ctx, **kw):
    it = organizer()
    assert run_async(handlers.submit_event(ctx, it, spec(**kw)))
    run = ctx.db.get_run(100, kw.get("name", "Game night"))
    ctx.db.set_run_status(run["id"], "open")
    return ctx.db.run(run["id"])


# ---------------------------------------------------------------- the spec

def test_an_availability_event_needs_no_fight_and_defaults_to_groups_of_eight(ctx):
    plan, errors = resolve(ctx, spec())
    assert errors == [] and plan.mode == "availability" and plan.party_size == 8
    assert "content" not in plan.spec and plan.spec["party_size"] == 8 and plan.spec["mode"] == "availability"
    assert "availability only, groups of 8" in events.describe(plan)


def test_party_size_is_checked(ctx):
    for bad in (0, 101, "8", True, 2.5):
        _, errors = resolve(ctx, spec(party_size=bad))
        assert any("party_size must be a whole number from 1 to 100" in e for e in errors), bad
    plan, errors = resolve(ctx, spec(party_size=5))
    assert errors == [] and plan.party_size == 5


def test_role_events_still_need_a_fight_and_take_no_party_size(ctx):
    _, errors = resolve(ctx, {"name": "x", "days": ["2026-11-02"]})
    assert any("Pick the fight" in e for e in errors)
    _, errors = resolve(ctx, {"name": "x", "content": "Ultima Weapon (Ultimate)", "days": ["2026-11-02"], "party_size": 8})
    assert any("party_size only applies to availability events" in e for e in errors)
    _, errors = resolve(ctx, spec(mode="bingo"))
    assert any("mode must be one of" in e for e in errors)


def test_the_run_stores_its_mode_and_size_and_the_spec_roundtrips(ctx):
    run = make(ctx, party_size=12)
    assert run["mode"] == "availability" and run["party_size"] == 12
    assert events.spec_from_run(ctx.db, run, ctx.cfg, ctx.grid)["party_size"] == 12
    other = make(ctx, name="Normal", mode="roles", content="Ultima Weapon (Ultimate)")
    assert other["mode"] == "roles" and other["party_size"] is None


# ---------------------------------------------------------------- the form

def test_the_form_builds_an_availability_spec(ctx):
    d = event_form.EventDraft(name="Game night", mode="availability", party_size="6", days=["2026-11-02"])
    assert d.ready() and d.to_spec() == {"version": 1, "name": "Game night", "mode": "availability", "party_size": 6,
                                         "days": ["2026-11-02"]}
    roles = event_form.EventDraft(name="x", content="Ultima Weapon (Ultimate)", days=["2026-11-02"])
    assert roles.to_spec()["content"] == "Ultima Weapon (Ultimate)" and "mode" not in roles.to_spec()
    assert not event_form.EventDraft(name="x", days=["2026-11-02"]).ready()           # roles mode needs a fight


def test_the_basics_modal_sets_the_type_and_size(ctx):
    it = organizer()
    run_async(handlers.run_create(ctx, it))
    view = it.response.sent[0]["view"]
    modal = event_form.BasicsModal(view)
    modal.name._value, modal.size._value, modal.dates._value = "Game night", "6", "2026-11-02 to 2026-11-03"
    modal.kind._values, modal.content._values = ["availability"], []
    run_async(modal.on_submit(organizer()))
    assert view.draft.mode == "availability" and view.draft.party_size == "6" and view.draft.content == ""
    assert view.plan is not None and view.plan.party_size == 6


# ---------------------------------------------------------------- what players see

def test_players_only_get_days_and_slots(ctx):
    run = make(ctx)
    view = AvailabilityView(ctx, run, 1, "P1")
    kinds = {type(c).__name__ for c in view.children}
    assert kinds == {"DaySelect", "SlotSelect", "DoneButton"}
    text = view.render()
    assert "Looking for groups of 8" in text and "No roles" not in text and "Prog" not in text
    view.slot_select._values = [SLOT]
    run_async(view.slot_select.callback(player(1)))
    assert ctx.db.ticked_keys(run["id"], 1) == [SLOT]


def test_limits_and_myschedule_for_an_availability_event(ctx):
    run = make(ctx)
    it = player(1)
    run_async(handlers.limits(ctx, it, 1, None, None))
    assert "no limits to set" in it.text
    ctx.db.upsert_player(1, "P1", now=NOW)
    ctx.db.set_day_availability(run["id"], 1, "2026-11-02", [SLOT])
    mine = player(1)
    run_async(handlers.myschedule(ctx, mine))
    assert "availability only" in mine.text and "Slots you ticked (1)" in mine.text and "Roles" not in mine.text


# ---------------------------------------------------------------- the organizer's view

def test_status_counts_people_per_slot_and_groups(ctx):
    run = make(ctx, party_size=3)
    for uid in (1, 2, 3):
        ctx.db.upsert_player(uid, f"P{uid}", now=NOW)
        ctx.db.set_day_availability(run["id"], uid, "2026-11-02", [SLOT, "2026-11-02T18:30"] if uid < 3 else [SLOT])
    it = organizer()
    run_async(handlers.run_status(ctx, it))
    assert "3 people have ticked slots" in it.text and "groups of 3" in it.text
    assert "17:00       3  ✓ 1" in it.text and "18:30       2  --" in it.text
    one = organizer()
    run_async(handlers.run_status(ctx, one, None, "2026-11-02"))
    assert "17:00: P1, P2, P3" in one.text and "18:30: P1, P2" in one.text


def test_status_after_the_cutoff_still_works(ctx, clock):
    run = make(ctx)
    ctx.db.upsert_player(1, "P1", now=NOW)
    ctx.db.set_day_availability(run["id"], 1, "2026-11-02", [SLOT])
    clock["now"] = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    it = organizer()
    run_async(handlers.run_status(ctx, it))
    assert "1 people have ticked slots" in it.text


def test_roles_only_commands_refuse_an_availability_event(ctx):
    make(ctx)
    for call in (lambda it: handlers.draft_export(ctx, it), lambda it: handlers.draft_confirm(ctx, it),
                 lambda it: handlers.draft_approve(ctx, it), lambda it: handlers.party_dashboard(ctx, it),
                 lambda it: handlers.draft_import(ctx, it, SimpleAttachment())):
        it = organizer()
        run_async(call(it))
        assert "only collects availability" in it.text


class SimpleAttachment:
    size, filename = 10, "draft.json"

    async def read(self):
        return b"{}"


def test_no_export_reminders_for_an_availability_event(ctx):
    make(ctx)
    assert due_reminders(ctx.db, datetime(2026, 11, 2, 6, 0, tzinfo=UTC), ctx.cfg) == []
