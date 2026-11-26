"""Several events open at the same time: picking one, asking organizers which one, and double-booking warnings."""
from datetime import date

import discord

from bot import handlers
from bot.views import AvailabilityView, RunPickerView
from conftest import SLOT, add_player, organizer, player, run_async


def two_open(make_run):
    a = make_run("Event A")
    b = make_run("Event B", start=date(2026, 11, 1), end=date(2026, 11, 3))
    return a, b


def pick(it, view, run):
    """Choose `run` in the picker the way a click would."""
    view.select._values = [str(run["id"])]
    chosen = player(it.user.id)
    run_async(view.select.callback(chosen))
    return chosen


def test_one_open_run_needs_no_picker(ctx, make_run):
    run = make_run("Event A")
    it = player(1)
    run_async(handlers.availability(ctx, it))
    assert isinstance(it.response.sent[0]["view"], AvailabilityView) and it.response.sent[0]["view"].run_id == run["id"]


def test_availability_asks_which_event_when_several_are_open(ctx, make_run):
    a, b = two_open(make_run)
    it = player(1)
    run_async(handlers.availability(ctx, it))
    picker = it.response.sent[0]["view"]
    assert isinstance(picker, RunPickerView) and "Several events are open" in it.response.sent[0]["content"]
    assert [o.label for o in picker.select.options] == ["Event A", "Event B"]
    chosen = pick(it, picker, b)
    view = chosen.response.sent[0]["view"]
    assert isinstance(view, AvailabilityView) and view.run_id == b["id"]
    assert "You picked **Event B**" in it.response.edits[-1]["content"]          # the picker is retired


def test_the_picker_is_only_for_the_player_who_asked(ctx, make_run):
    two_open(make_run)
    it = player(1)
    run_async(handlers.availability(ctx, it))
    picker = it.response.sent[0]["view"]
    stranger = player(2)
    assert run_async(picker.interaction_check(stranger)) is False
    assert run_async(picker.interaction_check(player(1))) is True


def test_limits_keep_their_arguments_through_the_picker(ctx, make_run):
    a, b = two_open(make_run)
    add_player(ctx, b, 1, ["melee"], 0, [SLOT])
    it = player(1)
    run_async(handlers.limits(ctx, it, 1, None, None))
    chosen = pick(it, it.response.sent[0]["view"], b)
    assert "**Event B**" in chosen.text
    assert ctx.db.get_limits(b["id"], 1, ctx.cfg)["max_per_day"] == 1
    assert ctx.db.get_limits(a["id"], 1, ctx.cfg)["is_default"]


def test_myschedule_lists_every_open_event(ctx, make_run):
    a, b = two_open(make_run)
    it = player(1)
    run_async(handlers.myschedule(ctx, it))
    assert "**Event A**" in it.text and "**Event B**" in it.text


def test_organizer_commands_ask_for_the_name_when_several_events_are_live(ctx, make_run):
    a, b = two_open(make_run)
    it = organizer()
    run_async(handlers.run_status(ctx, it))
    assert "Several events are live" in it.text and "Event A" in it.text and "Event B" in it.text
    named = organizer()
    run_async(handlers.run_status(ctx, named, "Event A"))
    assert "Several events are live" not in named.text


def test_ticking_the_same_hour_in_two_events_shows_a_warning(ctx, make_run):
    a, b = two_open(make_run)
    add_player(ctx, a, 1, ["melee"], 0, [SLOT])
    assert ctx.db.double_bookings(b["id"], 1) == []                  # nothing ticked in B yet
    add_player(ctx, b, 1, ["melee"], 0, [SLOT])
    assert [n for _, n in ctx.db.double_bookings(b["id"], 1)] == ["Event A"]
    view = AvailabilityView(ctx, b, 1, "Player 1")
    assert "Double-booked" in view.render() and "Event A" in view.render()
    mine = player(1)
    run_async(handlers.myschedule(ctx, mine))
    assert mine.text.count("Double-booked") == 2                      # once per event, each names the other


def test_different_hours_and_closed_events_do_not_warn(ctx, make_run):
    a, b = two_open(make_run)
    add_player(ctx, a, 1, ["melee"], 0, ["2026-11-02T08:00"])
    add_player(ctx, b, 1, ["melee"], 0, [SLOT])
    assert ctx.db.double_bookings(b["id"], 1) == []
    add_player(ctx, a, 1, ["melee"], 0, [SLOT])
    ctx.db.set_run_status(a["id"], "closed")
    assert ctx.db.double_bookings(b["id"], 1) == []
