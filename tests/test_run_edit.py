"""/run edit: one panel to open, close and adjust an event."""
from datetime import date, datetime, timezone

from bot import handlers, run_edit
from bot.commands import RunGroup
from conftest import SLOT, add_player, organizer, player, run_async

UTC = timezone.utc


def panel(ctx, name=None):
    it = organizer()
    run_async(run_edit.start(ctx, it, name))
    sent = it.response.sent[0]
    return it, sent["view"], sent["content"]


def click(it, button, **kw):
    it2 = organizer()
    run_async(button.callback(it2))
    return it2


def text_of(it):
    return it.response.edits[-1]["content"] if it.response.edits else it.text


def test_the_commands_are_edit_only(ctx):
    names = {c.name for c in RunGroup(ctx).commands}
    assert {"create", "edit", "spec", "status"} <= names and not names & {"open", "close", "extend"}


def test_the_panel_shows_the_event_and_its_days(ctx, make_run):
    run = make_run("Event A", status="draft")
    _, view, content = panel(ctx)
    assert "**Event A** · draft" in content and content.count("•") + content.count("▶") == len(ctx.db.run_days(run["id"]))
    assert view.toggle.label == "Open event" and view.close_day_button.disabled


def test_open_and_close_event_from_the_panel(ctx, make_run):
    run = make_run("Event A", status="draft")
    _, view, _ = panel(ctx)
    it = click(None, view.toggle)
    assert ctx.db.run(run["id"])["status"] == "open" and "is open" in text_of(it)
    assert view.toggle.label == "Close event" and not view.close_day_button.disabled
    add_player(ctx, run, 1, ["melee"], 0, [SLOT])
    it = click(None, view.toggle)
    assert ctx.db.run(run["id"])["status"] == "closed" and "closed for every day, with 1 players" in text_of(it)
    assert view.toggle.disabled and view.close_day_button.disabled


def test_close_one_day_now(ctx, make_run):
    run = make_run("Event A")
    _, view, _ = panel(ctx)
    day = view.day
    it = click(None, view.close_day_button)
    assert "closed now" in text_of(it) and ctx.db.run(run["id"])["status"] == "open"
    assert ctx.db.run_day(run["id"], day)["snapshot_utc"] is not None
    assert view.close_day_button.disabled or view.day != day      # that day is done


def test_change_a_cutoff_and_reject_a_bad_one(ctx, make_run):
    run = make_run("Event A")
    _, view, _ = panel(ctx)
    day = view.day
    modal = run_edit.CutoffModal(view)
    modal.cutoff._value, modal.tz._value = "2026-10-26 18:00", ""
    it = organizer()
    run_async(modal.on_submit(it))
    assert "now close" in text_of(it)
    assert ctx.db.run_day(run["id"], day)["cutoff_utc"].startswith("2026-10-26T22:00")      # 18:00 EDT
    before = ctx.db.run_day(run["id"], day)["cutoff_utc"]
    modal = run_edit.CutoffModal(view)
    modal.cutoff._value = "soon"
    it = organizer()
    run_async(modal.on_submit(it))
    assert "looks like" in text_of(it) and ctx.db.run_day(run["id"], day)["cutoff_utc"] == before


def test_moving_a_cutoff_reopens_a_closed_event(ctx, make_run):
    run = make_run("Event A")
    _, view, _ = panel(ctx)
    click(None, view.toggle)                                      # close the event
    assert ctx.db.run(run["id"])["status"] == "closed"
    modal = run_edit.CutoffModal(view)
    modal.cutoff._value = "2026-10-27 18:00"
    run_async(modal.on_submit(organizer()))
    assert ctx.db.run(run["id"])["status"] == "open" and not view.toggle.disabled


def test_rename_and_group_size(ctx, make_run):
    a = make_run("Event A")
    make_run("Event B", start=date(2026, 11, 1), end=date(2026, 11, 3))
    _, view, _ = panel(ctx, "Event A")
    modal = run_edit.DetailsModal(view)
    assert modal.size is None                                       # roles event: no group size
    modal.name._value = "event b"
    it = organizer()
    run_async(modal.on_submit(it))
    assert "already an event called" in text_of(it) and ctx.db.run(a["id"])["name"] == "Event A"
    modal = run_edit.DetailsModal(view)
    modal.name._value = "Renamed"
    run_async(modal.on_submit(organizer()))
    assert ctx.db.run(a["id"])["name"] == "Renamed" and "**Renamed**" in view.text()


def test_an_availability_event_can_change_its_group_size(ctx):
    from test_availability_events import make
    run = make(ctx, party_size=4)
    _, view, _ = panel(ctx)
    modal = run_edit.DetailsModal(view)
    assert modal.size is not None
    modal.name._value, modal.size._value = "Game night 2", "6"
    run_async(modal.on_submit(organizer()))
    run = ctx.db.run(run["id"])
    assert run["name"] == "Game night 2" and run["party_size"] == 6
    assert run["spec"]["name"] == "Game night 2" and run["spec"]["party_size"] == 6
    modal = run_edit.DetailsModal(view)
    modal.name._value, modal.size._value = "Game night 2", "0"
    it = organizer()
    run_async(modal.on_submit(it))
    assert "whole number from 1 to 100" in text_of(it) and ctx.db.run(run["id"])["party_size"] == 6


def test_several_live_events_show_a_picker_first(ctx, make_run):
    a = make_run("Event A")
    b = make_run("Event B", start=date(2026, 11, 1), end=date(2026, 11, 3))
    it = organizer()
    run_async(run_edit.start(ctx, it))
    picker = it.response.sent[0]["view"]
    assert [o.label for o in picker.select.options] == ["Event A", "Event B"]
    picker.select._values = [str(b["id"])]
    chosen = organizer()
    run_async(picker.select.callback(chosen))
    assert "**Event B**" in chosen.response.edits[-1]["content"]


def test_only_organizers_and_only_the_panels_owner(ctx, make_run):
    make_run("Event A")
    it = player(5)
    run_async(run_edit.start(ctx, it))
    assert "Only organizers" in it.text
    _, view, _ = panel(ctx)
    stranger = organizer(user_id=999)
    assert run_async(view.interaction_check(stranger)) is False
    assert run_async(view.interaction_check(organizer())) is True


def test_an_unknown_name_is_reported(ctx, make_run):
    make_run("Event A")
    it = organizer()
    run_async(run_edit.start(ctx, it, "nope"))
    assert "No run called 'nope'" in it.text


# ---------------------------------------------------------------- change days and delete

def days_of(ctx, run):
    return [r["day"] for r in ctx.db.run_days(run["id"])]


def submit_days(view, add="", remove=()):
    modal = run_edit.DaysModal(view)
    modal.add._value = add
    modal.remove._values = list(remove)
    it = organizer()
    run_async(modal.on_submit(it))
    return text_of(it)


def test_add_days_copy_the_first_days_window_and_get_the_default_cutoff(ctx, make_run):
    run = make_run("Event A")
    before = days_of(ctx, run)
    _, view, _ = panel(ctx)
    text = submit_days(view, "2026-11-20 to 2026-11-21, 2026-11-25")
    assert "added 3 day(s)" in text
    assert days_of(ctx, run) == before + ["2026-11-20", "2026-11-21", "2026-11-25"]
    keys = [s["slot_key"] for s in ctx.db.run_slots(run["id"]) if s["slot_key"].startswith("2026-11-20")]
    assert keys and keys[0] == "2026-11-20T08:00"
    assert ctx.db.run(run["id"])["end_date"] == "2026-11-25"
    assert ctx.db.run_day(run["id"], "2026-11-20")["cutoff_utc"].startswith("2026-11-20T04:59")     # 23:59 the day before
    assert "2026-11-25" in {o.value for o in view.day_select.options}


def test_adding_a_day_that_exists_or_garbage_changes_nothing(ctx, make_run):
    run = make_run("Event A")
    before = days_of(ctx, run)
    _, view, _ = panel(ctx)
    assert "Nothing to change" in submit_days(view, before[0])
    assert "Add days:" in submit_days(view, "whenever")
    assert days_of(ctx, run) == before


def test_remove_a_day_drops_its_signups_and_keeps_the_others(ctx, make_run):
    run = make_run("Event A")
    days = days_of(ctx, run)
    add_player(ctx, run, 1, ["melee"], 0, [SLOT])
    gone = SLOT[:10]
    _, view, _ = panel(ctx)
    text = submit_days(view, "", [gone])
    assert "removed 1 day(s)" in text and "dropped 1 signed-up slot(s)" in text
    assert days_of(ctx, run) == [d for d in days if d != gone]
    assert ctx.db.ticked_keys(run["id"], 1) == []
    assert not [s for s in ctx.db.run_slots(run["id"]) if s["slot_key"].startswith(gone)]
    if view.day == gone:
        raise AssertionError("the panel still points at a removed day")


def test_a_day_with_parties_or_an_export_cannot_be_removed(ctx, make_run):
    from test_approve import setup_day, D2
    run, _ = setup_day(ctx, make_run)
    _, view, _ = panel(ctx)
    day = ctx.db.parties(run["id"])[0]["slot_key"][:10]
    before = days_of(ctx, run)
    assert "cannot be removed: it has parties" in submit_days(view, "", [day])
    assert days_of(ctx, run) == before
    ctx.db.mark_exported(run["id"], before[0], ctx.now())
    assert "it was exported" in submit_days(view, "", [before[0]])


def test_the_last_day_cannot_be_removed_and_the_limit_holds(ctx, make_run):
    run = make_run("Event A")
    _, view, _ = panel(ctx)
    assert "at least one day" in submit_days(view, "", days_of(ctx, run))
    ctx.cfg["max_run_days"] = len(days_of(ctx, run))
    assert "at most" in submit_days(view, "2026-12-01")


def test_the_stored_spec_follows_the_days(ctx):
    from test_availability_events import make
    run = make(ctx)
    _, view, _ = panel(ctx)
    submit_days(view, "2026-11-10", ["2026-11-02"])
    spec = ctx.db.run(run["id"])["spec"]
    assert [d["date"] for d in spec["days"]] == ["2026-11-03", "2026-11-10"]
    assert ctx.db.run(run["id"])["start_date"] == "2026-11-03"


def test_delete_asks_first_then_removes_everything(ctx, make_run):
    run = make_run("Event A")
    add_player(ctx, run, 1, ["melee"], 0, [SLOT])
    _, view, _ = panel(ctx)
    it = click(None, view.delete_button)
    confirm = it.response.edits[-1]["view"]
    assert "Delete **Event A** for good" in it.response.edits[-1]["content"] and "1 signed-up players" in confirm.text()
    assert ctx.db.run(run["id"]) is not None                     # nothing deleted yet
    back = organizer()
    run_async(confirm.children[1].callback(back))                 # Keep it
    assert ctx.db.run(run["id"]) is not None and "**Event A**" in back.response.edits[-1]["content"]
    done = organizer()
    run_async(confirm.children[0].callback(done))                 # Yes, delete it
    assert ctx.db.run(run["id"]) is None and "was deleted" in done.response.edits[-1]["content"]
    assert ctx.db.get_run(100) is None


def test_delete_works_on_a_closed_event_and_only_for_the_panels_owner(ctx, make_run):
    run = make_run("Event A", status="closed")
    _, view, _ = panel(ctx)
    it = click(None, view.delete_button)
    confirm = it.response.edits[-1]["view"]
    assert run_async(confirm.interaction_check(organizer(user_id=999))) is False
    run_async(confirm.children[0].callback(organizer()))
    assert ctx.db.run(run["id"]) is None
