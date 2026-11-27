"""Slash command logic. Each handler takes (ctx, interaction, ...) and replies ephemerally.

Kept free of discord.app_commands decorators so tests can call them with mock interactions.
"""
import io
import json
import re
from datetime import timedelta
from pathlib import Path
from zoneinfo import available_timezones

import discord

from . import characters as ch
from . import events, openings, run_edit
from . import confirmations as cf
from . import progscore
from .fflogs import FFLogsError
from . import heatmap
from .config import ROOT, role_label
from .db import DraftConflict, DuplicateRun, parse_iso
from .grid import parse_date, parse_local_datetime
from .services import (find_channel, snapshot_note, cutoff_of, day_label, day_state, day_summary_lines, limits_text, locked_prog_lines, open_days,
                       profile_text, server_setting, signup_open, tick_lines, valid_timezone, validate_limits)
from .snapshots import ensure_snapshot
from .solver_io import DraftError, build_signups, fingerprint, import_draft, is_note
from .posting import post_day_parties
from .parties import apply_swap, plan_import
from .views import AvailabilityView, RateListView, RunPickerView, double_booking_lines
from .votes import (FEEDBACK_CATEGORIES, feedback_counts, party_rating_counts, rateable_parties, slot_end,
                    summary_counts)

MAX_DRAFT_BYTES = 2_000_000


# ---------------------------------------------------------------- small helpers

async def say(interaction, text, **kwargs):
    """Reply once, or follow up if the interaction was already answered or deferred."""
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True, **kwargs)
    else:
        await interaction.response.send_message(text, ephemeral=True, **kwargs)


def is_organizer(ctx, user):
    """Manage Server / Administrator, or the configured organizer role."""
    perms = getattr(user, "guild_permissions", None)
    if perms is not None and (getattr(perms, "manage_guild", False) or getattr(perms, "administrator", False)):
        return True
    guild_id = getattr(getattr(user, "guild", None), "id", None)
    role_id = server_setting(ctx, guild_id, "organizer_role_id") if guild_id else None
    return bool(role_id) and any(getattr(r, "id", None) == int(role_id) for r in getattr(user, "roles", []))


async def require_organizer(ctx, interaction):
    if is_organizer(ctx, interaction.user):
        return True
    await say(interaction, "Only organizers can use this command.")
    return False


async def find_run(ctx, interaction, name=None):
    """The run named `name`. Without a name: the newest run, unless several runs are live (draft or open) at
    once, because guessing which event an organizer means could change the wrong one."""
    if not name:
        live = ctx.db.live_runs(interaction.guild_id)
        if len(live) > 1:
            names = ", ".join(f"**{r['name']}** ({r['status']})" for r in live[:10])
            await say(interaction, f"Several events are live: {names}. Give the event's name in the `name` option.")
            return None
    run = ctx.db.get_run(interaction.guild_id, name)
    if run is None:
        await say(interaction, f"No run called '{name}' in this server." if name else "There are no runs yet. Use /run create.")
    return run


async def roles_only(ctx, interaction, run):
    """False (after telling the organizer) for an availability-only event: it has no roles, solver, drafts or parties."""
    if run["mode"] == "availability":
        await say(interaction, f"**{run['name']}** only collects availability (no roles, drafts or parties). "
                               "Use /run status to see who can make which slot.")
        return False
    return True


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "run"


# ---------------------------------------------------------------- /run

DAY_HINT = "Days look like 2026-11-02."


def parse_day(text):
    """'2026-11-02' (or today/tomorrow/yesterday) -> '2026-11-02'. Raises ValueError."""
    return parse_date(text).isoformat()


async def find_day(ctx, interaction, run, day):
    """The run_days row for `day`, or None after telling the organizer what went wrong."""
    try:
        key = parse_day(day)
    except ValueError:
        await say(interaction, DAY_HINT)
        return None
    row = ctx.db.run_day(run["id"], key)
    if row is None:
        days = ", ".join(d["day"] for d in ctx.db.run_days(run["id"]))
        await say(interaction, f"{key} is not a day of **{run['name']}**. Its days: {days}.")
    return row


MAX_SPEC_BYTES = 100_000


async def submit_event(ctx, interaction, spec):
    """Check an event spec and create the run (a draft). The one way events are created: the guided form and
    an attached JSON file both end here. Returns True when the run was created; the reply says what happened."""
    plan, errors = events.resolve(spec, ctx.cfg, ctx.grid, ctx.db, ctx.now())
    if plan is None:
        await say(interaction, "I could not create the event:\n" + "\n".join(f"• {e}" for e in errors[:15]))
        return False
    try:
        ctx.db.create_run(guild_id=interaction.guild_id, name=plan.name, fight_id=plan.fight["id"],
                          start_date=plan.days[0].day, end_date=plan.days[-1].day, cutoffs=plan.cutoffs,
                          slots=plan.slots, created_by=interaction.user.id, now=ctx.now(), spec=plan.spec,
                          open_at=plan.open_at_utc, mode=plan.mode, party_size=plan.party_size)
    except DuplicateRun:
        await say(interaction, f"A run called '{plan.name}' already exists here.")
        return False
    lines = ["Created the event as a draft.", events.describe(plan)]
    if plan.open_at_utc is None:
        lines.append("Use /run edit to open it for signups.")
    reminder = server_setting(ctx, interaction.guild_id, "reminder_channel_id")
    if not reminder:
        lines.append("⚠ No reminder channel is set (use /settings set), "
                     "so the bot will not remind organizers to run /draft export after each cutoff.")
    elif await find_channel(ctx, reminder) is None:
        lines.append("⚠ The reminder channel was deleted or I cannot see it, so the bot cannot remind organizers to "
                     "run /draft export. Set it again with /settings set.")
    await say(interaction, "\n".join(lines)[:1990])
    return True


async def run_create(ctx, interaction, spec_file=None):
    """Organizers: with a JSON file, create the event from it; without, start the guided form."""
    if not await require_organizer(ctx, interaction):
        return
    if spec_file is None:
        from . import event_form
        return await event_form.start(ctx, interaction)
    if spec_file.size > MAX_SPEC_BYTES:
        return await say(interaction, "That file is too big to be an event spec.")
    try:
        spec = json.loads((await spec_file.read()).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return await say(interaction, "That file is not valid JSON. /run spec shows the format; it is also in the README.")
    await submit_event(ctx, interaction, spec)


async def run_spec(ctx, interaction, name=None):
    """Organizers: an event's JSON spec as a file, to edit and use with /run create, or for the dashboard."""
    if not await require_organizer(ctx, interaction):
        return
    run = await find_run(ctx, interaction, name)
    if run is None:
        return
    spec = events.spec_from_run(ctx.db, run, ctx.cfg, ctx.grid)
    file = discord.File(io.BytesIO(json.dumps(spec, indent=2, ensure_ascii=False).encode("utf-8")),
                        filename=f"{slug(run['name'])}.event.json")
    await say(interaction, f"The spec of **{run['name']}**. Change the name, then upload it with /run create.", file=file)


async def run_open(ctx, interaction, name=None):
    """Plain entry point for opening an event (the /run edit panel calls run_edit.open_event directly)."""
    if not await require_organizer(ctx, interaction):
        return
    run = await find_run(ctx, interaction, name)
    if run is None:
        return
    await interaction.response.defer(ephemeral=True)             # posting the announcement can take a moment
    _, text = await run_edit.open_event(ctx, run)
    await say(interaction, text)


async def run_close(ctx, interaction, name=None, day=None):
    """Plain entry point: close one day now, or the whole event."""
    if not await require_organizer(ctx, interaction):
        return
    run = await find_run(ctx, interaction, name)
    if run is None:
        return
    if day:
        row = await find_day(ctx, interaction, run, day)
        if row is None:
            return
        return await say(interaction, run_edit.close_day(ctx, run, row))
    await say(interaction, run_edit.close_event(ctx, run))


async def run_extend(ctx, interaction, day, new_cutoff, name=None, timezone=None):
    """Plain entry point: move one day's cutoff (also reopens a day that already closed)."""
    if not await require_organizer(ctx, interaction):
        return
    run = await find_run(ctx, interaction, name)
    if run is None:
        return
    row = await find_day(ctx, interaction, run, day)
    if row is None:
        return
    await say(interaction, run_edit.move_cutoff(ctx, run, row, new_cutoff, timezone))


async def run_status(ctx, interaction, name=None, day=None):
    if not await require_organizer(ctx, interaction):
        return
    run = await find_run(ctx, interaction, name)
    if run is None:
        return
    key = None
    if day:
        try:
            key = parse_day(day)
        except ValueError:
            return await say(interaction, DAY_HINT)
    _ensure_snapshots(ctx, run, key)
    first, *rest = heatmap.render(ctx.db, run, ctx.cfg, key, preface=day_summary_lines(ctx, run, key))
    await say(interaction, first)
    for msg in rest:
        await interaction.followup.send(msg, ephemeral=True)


# ---------------------------------------------------------------- player commands

def signup_runs(ctx, guild_id):
    """The events players can sign up for now: open, with at least one day still taking signups."""
    return [r for r in ctx.db.open_runs(guild_id) if open_days(ctx, r)]


async def choose_run(ctx, interaction, proceed):
    """Run `proceed(interaction, run)` for the event the player means: the only one open, or the one they pick
    when several are open at once. Tells the player when none is open."""
    runs = signup_runs(ctx, interaction.guild_id)
    if not runs:
        stale = ctx.db.open_runs(interaction.guild_id)
        if stale:
            return await say(interaction, signup_open(ctx, stale[0])[1])
        return await say(interaction, "No run is open for signups right now.")
    if len(runs) == 1:
        return await proceed(interaction, runs[0])

    async def picked(pick_interaction, run):
        try:
            await interaction.edit_original_response(content=f"You picked **{run['name']}**.", view=None)
        except discord.HTTPException:
            pass
        await proceed(pick_interaction, run)
    view = RunPickerView(runs, interaction.user.id, picked)
    await interaction.response.send_message("Several events are open. Which one is this about?", view=view,
                                            ephemeral=True)


async def availability(ctx, interaction, timezone=None):
    if timezone is not None and not valid_timezone(timezone):
        return await say(interaction, f"'{timezone}' is not a timezone name like America/Chicago.")

    async def proceed(it, run):
        ok, why = signup_open(ctx, run)
        if not ok:
            return await say(it, why)
        ctx.db.upsert_player(it.user.id, it.user.display_name, timezone=timezone, now=ctx.now())
        view = AvailabilityView(ctx, run, it.user.id, it.user.display_name)
        await it.response.send_message(view.render(), view=view, ephemeral=True)
    await choose_run(ctx, interaction, proceed)


async def limits(ctx, interaction, max_per_day=None, max_per_run=None, drop_checkpoints=None):
    async def proceed(it, run):
        await _limits_for(ctx, it, run, max_per_day, max_per_run, drop_checkpoints)
    await choose_run(ctx, interaction, proceed)


async def _limits_for(ctx, interaction, run, max_per_day, max_per_run, drop_checkpoints):
    if run["mode"] == "availability":
        return await say(interaction, f"**{run['name']}** only collects availability: there are no limits to set.")
    changing = any(v is not None for v in (max_per_day, max_per_run, drop_checkpoints))
    if changing:
        ok, why = signup_open(ctx, run)
        if not ok:
            return await say(interaction, why)
        cur = ctx.db.get_limits(run["id"], interaction.user.id, ctx.cfg)
        err = validate_limits(max_per_day, max_per_run, drop_checkpoints, len(ctx.db.checkpoints(run["fight_id"])))
        if not err and max_per_day is not None and max_per_run is None and cur["max_per_run"] < max_per_day:
            err = "Max per day cannot be larger than your max per run."
        if not err and max_per_run is not None and max_per_day is None and max_per_run < cur["max_per_day"]:
            err = "Max per run cannot be smaller than your max per day."
        if err:
            return await say(interaction, err)
        ctx.db.upsert_player(interaction.user.id, interaction.user.display_name, now=ctx.now())
        ctx.db.set_limits(run["id"], interaction.user.id, ctx.cfg, max_per_day=max_per_day,
                          max_per_run=max_per_run, drop_checkpoints=drop_checkpoints)
    lim = ctx.db.get_limits(run["id"], interaction.user.id, ctx.cfg)
    note = " (defaults until you change them)" if lim["is_default"] else ""
    saved = snapshot_note(ctx, run, interaction.user.id) if changing else ""
    await say(interaction, f"**{run['name']}**\n{limits_text(lim)}{note}" + (f"\nℹ {saved}" if saved else ""))


def schedule_lines(ctx, run, uid):
    keys = ctx.db.ticked_keys(run["id"], uid)
    if run["mode"] == "availability":
        lines = [f"**{run['name']}** ({run['status']}, availability only)",
                 f"Slots you ticked ({len(keys)}), in your local time:"]
        return lines + double_booking_lines(ctx.db.double_bookings(run["id"], uid)) + (tick_lines(ctx, run, keys) or ["• none"])
    lines = [f"**{run['name']}** ({run['status']})", profile_text(ctx, run, uid),
             *locked_prog_lines(ctx, run, uid),
             f"Slots you ticked ({len(keys)}), in your local time:"]
    lines += double_booking_lines(ctx.db.double_bookings(run["id"], uid))
    lines += tick_lines(ctx, run, keys) or ["• none"]
    mine = ctx.db.player_parties(run["id"], uid)
    if mine:
        lines.append("Your confirmed parties:")
        for p in mine:
            when = discord.utils.format_dt(parse_iso(p["start_utc"]), "F")
            char = ctx.db.chosen_character(run["id"], p["slot_key"][:10], uid)
            lines.append(f"• {when} as {role_label(p['role'])}" + (f" ({ch.label(char)})" if char else ""))
    elif ctx.db.parties(run["id"]):
        lines.append("A draft is with the organizers; your parties show up here once they are confirmed.")
    return lines


async def myschedule(ctx, interaction):
    """Every open event (several can run at once), else the newest run."""
    runs = ctx.db.open_runs(interaction.guild_id) or [r for r in [ctx.db.get_run(interaction.guild_id)] if r]
    if not runs:
        return await say(interaction, "There are no runs yet.")
    blocks = ["\n".join(schedule_lines(ctx, run, interaction.user.id)) for run in runs]
    await say(interaction, "\n\n".join(blocks)[:1990])


# ---------------------------------------------------------------- /draft

def _ensure_snapshots(ctx, run, only_day=None):
    """Take the lazy snapshot for closed days that the once-a-minute loop has not reached yet."""
    for row in ctx.db.run_days(run["id"]):
        if only_day is None or row["day"] == only_day:
            ensure_snapshot(ctx, run, row["day"])


def _split_warnings(warnings):
    """(notes about the export such as a late snapshot, players left out)."""
    return [w for w in warnings if is_note(w)], [w for w in warnings if not is_note(w)]


def _next_day_to_export(ctx, run):
    """Earliest closed day that still waits for an export and has someone ready to play."""
    for row in ctx.db.run_days(run["id"]):
        if day_state(ctx, run, row) == "closed" and heatmap.day_summary(ctx.db, run, ctx.cfg, row["day"])[0]:
            return row
    return None


async def draft_export(ctx, interaction, name=None, day=None, force=False):
    if not await require_organizer(ctx, interaction):
        return
    run = await find_run(ctx, interaction, name)
    if run is None or not await roles_only(ctx, interaction, run):
        return
    picked = ""
    if day:
        row = await find_day(ctx, interaction, run, day)
        if row is None:
            return
    else:
        row = _next_day_to_export(ctx, run)
        if row is None:
            lines = ["No closed day is waiting for an export (every closed day is exported or has nobody signed up)."]
            lines += day_summary_lines(ctx, run)
            lines.append("To export a day that is still open use `/draft export day:<day> force:true`.")
            return await say(interaction, "\n".join(lines)[:1990])
        picked = f"No day given, so I picked the earliest closed day not exported yet: {day_label(row['day'])}.\n"
    day_key = row["day"]
    state = day_state(ctx, run, row)
    if state in ("open", "draft") and not force:
        why = ("The run is still a draft." if state == "draft" else
               f"Signups for {day_label(day_key)} are still open until {discord.utils.format_dt(cutoff_of(row), 'F')}.")
        return await say(interaction, f"{why} Close that day in `/run edit`, or pass `force:true` to export "
                                      "anyway (the draft goes stale if anyone changes their signup afterwards).")

    _ensure_snapshots(ctx, run)
    data, warnings = build_signups(ctx.db, run, ctx.cfg, day_key, ctx.now())
    late, warnings = _split_warnings(warnings)
    if not data["players"]:
        lines = [f"Nobody is ready to play on {day_label(day_key)} (no one ticked slots with roles and prog set), "
                 "so there is nothing to export."]
        lines += [f"⚠ {w}" for w in late]
        if warnings:
            lines.append("Left out:\n" + "\n".join(f"• {w}" for w in warnings[:10]))
        return await say(interaction, "\n".join(lines)[:1990])
    body = json.dumps(data, indent=1).encode()
    out_dir = Path(ctx.cfg["export_dir"])
    out_dir = out_dir if out_dir.is_absolute() else ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{slug(run['name'])}-{day_key}-signups.json"
    (out_dir / filename).write_bytes(body)
    if state != "open":                         # a forced early export must not silence the reminders
        ctx.db.mark_exported(run["id"], day_key, ctx.now())

    lines = [picked + f"Exported **{run['name']}**, {day_label(day_key)}: {len(data['players'])} players, "
             f"{len(data['slots'])} slots, fingerprint `{fingerprint(data)[:12]}`.",
             f"Also saved to {out_dir / filename}.",
             f"Next: `python solve.py {filename} -o draft.json`, then /draft import with draft.json (day:{day_key} if it asks)."]
    if state == "open":
        lines.append("⚠ Signups for that day are still open. Any change after this export makes the draft stale, "
                     "and import will refuse it. Reminders will still come after the cutoff.")
    lines += [f"⚠ {w}" for w in late]
    if warnings:
        lines.append("Left out:\n" + "\n".join(f"• {w}" for w in warnings[:10])
                     + (f"\n… and {len(warnings) - 10} more" if len(warnings) > 10 else ""))
    await say(interaction, "\n".join(lines)[:1990], file=discord.File(io.BytesIO(body), filename=filename))


def _day_of_draft(ctx, run, draft):
    """Which exported day a draft file belongs to, found by matching its fingerprint to each day's export."""
    exported = sorted((r for r in ctx.db.run_days(run["id"]) if r["exported_utc"]),
                      key=lambda r: r["exported_utc"], reverse=True)
    for row in exported:
        data, _ = build_signups(ctx.db, run, ctx.cfg, row["day"], ctx.now())
        if fingerprint(data) == draft.get("input_fingerprint"):
            return row["day"]
    return exported[0]["day"] if exported else None      # none match: report staleness for the latest export


async def draft_import(ctx, interaction, draft_file, name=None, day=None):
    if not await require_organizer(ctx, interaction):
        return
    run = await find_run(ctx, interaction, name)
    if run is None or not await roles_only(ctx, interaction, run):
        return
    day_key = None
    if day:
        row = await find_day(ctx, interaction, run, day)
        if row is None:
            return
        day_key = row["day"]
    _ensure_snapshots(ctx, run, day_key)
    if draft_file.size > MAX_DRAFT_BYTES:
        return await say(interaction, "That file is too large to be a draft.")
    await interaction.response.defer(ephemeral=True)
    try:
        draft = json.loads(await draft_file.read())
    except (ValueError, UnicodeDecodeError):
        return await say(interaction, "That file is not valid JSON.")
    if day_key is None and isinstance(draft, dict):
        day_key = _day_of_draft(ctx, run, draft)
    try:
        s = import_draft(ctx.db, run, draft, ctx.cfg, day_key, ctx.now())
    except DraftError as e:
        return await say(interaction, f"Draft refused: {e}")
    except DraftConflict:
        return await say(interaction, "That day already has confirmed parties, so a new draft cannot replace them.")
    if day_key:
        cf.after_import(ctx.db, run["id"], day_key)         # keep confirmations of people still in the same slot
    lines = [f"Draft stored for **{run['name']}**{', ' + day_label(day_key) if day_key else ''} "
             f"(status: draft, nothing is posted to players): "
             f"{s['parties']} parties in {s['slots']} slots, {s['seated']} players seated, "
             f"{s['no_party']} without a party, {s['placed_down']} placed below their prog."]
    for _, slot, reason in s["slot_notes"][:5]:
        lines.append(f"• {slot}: {reason}")
    if len(s["slot_notes"]) > 5:
        lines.append(f"… and {len(s['slot_notes']) - 5} more unfilled slots.")
    await say(interaction, "\n".join(lines)[:1990])


async def draft_confirm(ctx, interaction, name=None, day=None):
    """Ask the members of a day's draft parties to confirm: one message per slot in the parties channel."""
    if not await require_organizer(ctx, interaction):
        return
    run = await find_run(ctx, interaction, name)
    if run is None or not await roles_only(ctx, interaction, run):
        return
    if day:
        row = await find_day(ctx, interaction, run, day)
        if row is None:
            return
        day_key = row["day"]
    else:
        waiting = ctx.db.days_with_draft(run["id"])
        if not waiting:
            return await say(interaction, f"**{run['name']}** has no draft to confirm. Import one with /draft import.")
        day_key = waiting[0]
    drafts, approved = ctx.db.day_party_counts(run["id"], day_key)
    if drafts == 0:
        return await say(interaction, f"{day_label(day_key)} has no draft parties" +
                         (" (it is already approved: players are asked in the slot threads)." if approved else
                          ". Import one with /draft import."))
    channel_id = server_setting(ctx, interaction.guild_id, "parties_channel_id")
    channel = await find_channel(ctx, channel_id)
    if channel is None:
        return await say(interaction, "I need the parties channel to ask people, but it is not set or I cannot "
                                      "find it any more (deleted, or I lost access). Use /settings set parties_channel.")
    await interaction.response.defer(ephemeral=True)
    asked, pinged, errors = await cf.post_draft_prompts(ctx, run, day_key, channel)
    lines = [f"Asked for confirmation for **{run['name']}**, {day_label(day_key)}: {asked} slot(s), "
             f"{pinged} player(s) pinged. Follow it with /party dashboard."]
    if errors:
        lines.append("Some posts failed (run it again to retry):\n" + "\n".join(f"• {e}" for e in errors[:5]))
    await say(interaction, "\n".join(lines)[:1990])


async def party_dashboard(ctx, interaction, name=None, day=None):
    """Organizers: the status dashboard (parties and threads that need attention)."""
    from .dashboard import open_dashboard
    if not await require_organizer(ctx, interaction):
        return
    run = await find_run(ctx, interaction, name)
    if run is None or not await roles_only(ctx, interaction, run):
        return
    day_key = None
    if day:
        row = await find_day(ctx, interaction, run, day)
        if row is None:
            return
        day_key = row["day"]
    if not await open_dashboard(ctx, interaction, run, day_key):
        await say(interaction, f"**{run['name']}** has no parties yet. Import a draft with /draft import.")


async def draft_approve(ctx, interaction, name=None, day=None):
    """Confirm one day's draft parties and post them (one thread per slot, one message per party)."""
    if not await require_organizer(ctx, interaction):
        return
    run = await find_run(ctx, interaction, name)
    if run is None or not await roles_only(ctx, interaction, run):
        return
    if day:
        row = await find_day(ctx, interaction, run, day)
        if row is None:
            return
        day_key = row["day"]
    else:
        waiting = ctx.db.days_with_draft(run["id"])
        if not waiting:
            return await say(interaction, f"**{run['name']}** has no draft to approve. Import one with /draft import.")
        day_key = waiting[0]
    drafts, approved = ctx.db.day_party_counts(run["id"], day_key)
    channel_id = server_setting(ctx, interaction.guild_id, "parties_channel_id")
    missing = [s for s, ps in ctx.db.approved_slots(run["id"], day_key) if any(not p["message_id"] for p, _ in ps)]
    if drafts == 0 and approved == 0:
        return await say(interaction, f"{day_label(day_key)} has no draft to approve. Import one with /draft import.")
    if drafts == 0 and not (channel_id and missing):
        return await say(interaction, f"{day_label(day_key)} is already approved.")
    await interaction.response.defer(ephemeral=True)
    n = ctx.db.confirm_day(run["id"], day_key) if drafts else 0
    head = (f"Approved {n} parties for **{run['name']}**, {day_label(day_key)}." if drafts
            else f"{day_label(day_key)} was already approved; posting the parties that are missing.")
    if not channel_id:
        return await say(interaction, head + " No parties channel is set (use /settings set), so nothing was posted. "
                                              "Players see their party under /myschedule.")
    channel = await find_channel(ctx, channel_id)
    if channel is None:
        return await say(interaction, head + f" I could not find channel {channel_id} (deleted, or I lost access), so nothing was posted. "
                                              "Players see their party under /myschedule. Run /draft approve again "
                                              "after fixing the parties channel with /settings set to post them.")
    posted, errors = await post_day_parties(ctx, run, day_key, channel)
    lines = [head + f" Posted {posted} parties."]
    if errors:
        lines.append("Some posts failed (run /draft approve again to retry them):\n"
                     + "\n".join(f"• {e}" for e in errors[:5]))
    await say(interaction, "\n".join(lines)[:1990])


# ---------------------------------------------------------------- /rate and /votes

async def rate(ctx, interaction):
    """Fallback for a lost thread message: the parties this player can still rate."""
    parties = rateable_parties(ctx, interaction.user.id)
    if not parties:
        return await say(interaction, "You have no finished party to rate right now. Ratings open when a slot ends "
                                      f"and close {ctx.cfg['vote_window_hours']} hours later.")
    view = RateListView(ctx, parties)
    note = "\n(Showing the 20 most recent.)" if len(parties) > 20 else ""
    await interaction.response.send_message("Pick a party to rate. Only you can see your ratings." + note,
                                            view=view, ephemeral=True)


def _chunks(lines, limit=1900):
    out, cur = [], ""
    for line in lines:
        if cur and len(cur) + len(line) + 1 > limit:
            out.append(cur)
            cur = ""
        cur += ("\n" if cur else "") + line
    return out + [cur] if cur else out


async def votes_summary(ctx, interaction):
    """Organizers: likes and dislikes received per player, no voter identities, small totals hidden."""
    if not await require_organizer(ctx, interaction):
        return
    counts = summary_counts(ctx.db, ctx.cfg, ctx.now())
    floor = ctx.cfg["vote_floor"]
    shown, hidden = [], 0
    for pid, (likes, dislikes) in counts.items():
        if likes + dislikes < floor:
            hidden += 1
            continue
        player = ctx.db.get_player(pid)
        shown.append((player["display_name"] if player else pid, likes, dislikes))
    shown.sort(key=lambda t: t[0].lower())
    days = ctx.cfg["vote_expiry_days"]
    head = (f"Votes received in the last {days} days (like / dislike). Players with fewer than {floor} votes are "
            "hidden. No voter names are stored in this view.")
    if not shown:
        return await say(interaction, head + "\nNobody has enough votes to show yet.")
    lines = [f"{name}: {likes} / {dislikes}" for name, likes, dislikes in shown]
    if hidden:
        lines.append(f"({hidden} more with fewer than {floor} votes not shown)")
    first, *rest = _chunks([head, *lines])
    await say(interaction, first)
    for msg in rest:
        await interaction.followup.send(msg, ephemeral=True)


# ---------------------------------------------------------------- /settings (per server)

SETTING_LABELS = {"organizer_role_id": "Organizer role", "reminder_channel_id": "Reminder channel",
                  "parties_channel_id": "Parties channel", "user_panel_channel_id": "User panel channel"}


def _setting_mention(key, value):
    return f"<@&{value}>" if key == "organizer_role_id" else f"<#{value}>"


def settings_lines(ctx, guild_id):
    lines = []
    for key, label in SETTING_LABELS.items():
        own, default = ctx.db.guild_setting(guild_id, key), server_setting(ctx, guild_id, key)
        if own:
            lines.append(f"**{label}:** {_setting_mention(key, own)}")
        elif default:
            lines.append(f"**{label}:** {_setting_mention(key, default)} (bot default)")
        else:
            lines.append(f"**{label}:** not set")
    return lines


async def settings_problems(ctx, interaction):
    """Warnings for settings that point at something that is gone: a deleted channel, a deleted role."""
    out = []
    for key, label in SETTING_LABELS.items():
        value = server_setting(ctx, interaction.guild_id, key)
        if not value:
            continue
        if key == "organizer_role_id":
            guild = getattr(interaction, "guild", None)
            if guild is not None and guild.get_role(int(value)) is None:
                out.append(f"⚠ **{label}** points at a role that no longer exists. Set it again with /settings set.")
        elif await find_channel(ctx, value) is None:
            out.append(f"⚠ **{label}** points at a channel that was deleted or that I cannot see. "
                       "Set it again with /settings set (or /settings clear).")
    return out


async def require_manage_server(interaction):
    perms = interaction.user.guild_permissions
    if perms.manage_guild or perms.administrator:
        return True
    await say(interaction, "Only people with Manage Server can change these settings.")
    return False


async def settings_show(ctx, interaction):
    if not await require_organizer(ctx, interaction):
        return
    lines = ["**Server settings**", *settings_lines(ctx, interaction.guild_id)]
    lines += await settings_problems(ctx, interaction)
    await say(interaction, "\n".join(lines))


async def settings_set(ctx, interaction, organizer_role=None, reminder_channel=None, parties_channel=None,
                       user_panel_channel=None):
    if not await require_manage_server(interaction):
        return
    given = {"organizer_role_id": organizer_role, "reminder_channel_id": reminder_channel,
             "parties_channel_id": parties_channel, "user_panel_channel_id": user_panel_channel}
    given = {k: v for k, v in given.items() if v is not None}
    if not given:
        return await say(interaction, "Pick at least one option to set.\n"
                         + "\n".join(settings_lines(ctx, interaction.guild_id)))
    from . import panel                                         # panel imports handlers
    warnings = []
    me = interaction.guild.me
    for key, obj in given.items():
        if key == "user_panel_channel_id":
            await interaction.response.defer(ephemeral=True)     # posting and pinning can take a moment
            warnings.extend(await panel.install_panel(ctx, interaction.guild_id, obj))
            continue
        ctx.db.set_guild_setting(interaction.guild_id, key, obj.id)
        if key != "organizer_role_id" and me is not None:
            perms = obj.permissions_for(me)
            if not perms.view_channel or not perms.send_messages:
                warnings.append(f"⚠ I cannot send messages in {obj.mention}.")
            elif key == "parties_channel_id" and not (perms.create_public_threads and perms.send_messages_in_threads):
                warnings.append(f"⚠ I need Create Public Threads and Send Messages in Threads in {obj.mention}.")
            elif key == "parties_channel_id" and not perms.manage_threads:
                warnings.append(f"⚠ I need Manage Threads in {obj.mention} to remove swapped-out players from a party thread.")
    await say(interaction, "\n".join(["Saved.", *settings_lines(ctx, interaction.guild_id), *warnings])[:1990])


async def settings_clear(ctx, interaction, setting):
    if not await require_manage_server(interaction):
        return
    if setting not in SETTING_LABELS:
        return await say(interaction, "That is not a setting.")
    if setting == "user_panel_channel_id":
        from . import panel
        await panel.remove_panel(ctx, interaction.guild_id)
    ctx.db.set_guild_setting(interaction.guild_id, setting, None)
    await say(interaction, "\n".join([f"Cleared **{SETTING_LABELS[setting]}**.", *settings_lines(ctx, interaction.guild_id)]))


# ---------------------------------------------------------------- /feedback (organizers)

def _name(ctx, pid):
    player = ctx.db.get_player(pid)
    return player["display_name"] if player else str(pid)


async def feedback_summary(ctx, interaction):
    """Organizers: per player, how the parties they were in were rated and who reported what. Never names a reporter."""
    if not await require_organizer(ctx, interaction):
        return
    now, floor = ctx.now(), ctx.cfg["vote_floor"]
    ratings = party_rating_counts(ctx.db, ctx.cfg, now)
    reports, general = feedback_counts(ctx.db, ctx.cfg, now)
    lines, hidden = [], 0
    for pid in sorted(set(ratings) | set(reports), key=lambda p: _name(ctx, p).lower()):
        parts = []
        likes, dislikes = ratings.get(pid, (0, 0))
        if likes + dislikes >= floor:
            parts.append(f"parties 👍 {likes} / 👎 {dislikes}")
        elif likes + dislikes:
            hidden += 1
        cats = reports.get(pid, {})
        if cats:
            parts.append("reports: " + ", ".join(f"{FEEDBACK_CATEGORIES[c][0].lower()} ×{n}" for c, n in
                                                 sorted(cats.items(), key=lambda kv: -kv[1])))
        if parts:
            lines.append(f"**{_name(ctx, pid)}**: " + " · ".join(parts))
    days = ctx.cfg["vote_expiry_days"]
    head = (f"Party ratings and feedback from the last {days} days. Ratings of players in fewer than {floor} rated "
            "parties are hidden. ×N is the number of different players who reported it; names of reporters are never shown. "
            "Use /feedback player to read the notes.")
    if general:
        head += f"\n{general} report(s) named nobody: /feedback player without a player lists them."
    if hidden:
        lines.append(f"({hidden} more with fewer than {floor} ratings not shown)")
    if not lines:
        return await say(interaction, head + "\nNothing to show yet.")
    first, *rest = _chunks([head, *lines])
    await say(interaction, first)
    for msg in rest:
        await interaction.followup.send(msg, ephemeral=True)


async def feedback_player(ctx, interaction, player=None):
    """Organizers: the notes people sent about one player, or (no player) the ones that named nobody. Anonymous."""
    if not await require_organizer(ctx, interaction):
        return
    now = ctx.now()
    since = now - timedelta(days=ctx.cfg["vote_expiry_days"])
    target = None if player is None else str(player.id)
    rows = [r for r in ctx.db.feedback_rows(now, since) if r["target_id"] == target][::-1]
    who = "feedback that named nobody" if target is None else f"feedback about {_name(ctx, target)}"
    if not rows:
        return await say(interaction, f"No {who} in the last {ctx.cfg['vote_expiry_days']} days.")
    lines = [f"**{who.capitalize()}**, newest first (reporters are not shown):"]
    for r in rows[:30]:
        note = r["note"] or "no details"
        lines.append(f"• {day_label(r['slot_key'][:10])} party {r['party_index'] + 1} · "
                     f"{FEEDBACK_CATEGORIES[r['category']][0]}: {note}")
    first, *rest = _chunks(lines)
    await say(interaction, first)
    for msg in rest:
        await interaction.followup.send(msg, ephemeral=True)


# ---------------------------------------------------------------- /debug (testing only)

async def debug_complete(ctx, interaction, name=None, day=None):
    """Testing aid: make the approved slots of a run (or one day) count as just ended, so the
    "Rate your party" button is posted by the next minute-loop pass and voting can be tried.

    It moves each slot's start into the past, so the end (start + actual_minutes or minutes) is one
    minute ago. Slots that already ended are left alone."""
    if not await require_organizer(ctx, interaction):
        return
    run = await find_run(ctx, interaction, name)
    if run is None:
        return
    if day:
        row = await find_day(ctx, interaction, run, day)
        if row is None:
            return
        day_keys = [row["day"]]
    else:
        day_keys = [d["day"] for d in ctx.db.run_days(run["id"])]
    now = ctx.now()
    moved, already = [], 0
    for day_key in day_keys:
        for slot, _parties in ctx.db.approved_slots(run["id"], day_key):
            minutes = slot["actual_minutes"] or slot["minutes"]      # same rule as votes.slot_end
            if slot_end(slot) <= now:
                already += 1
                continue
            ctx.db.move_slot_start(slot["id"], now - timedelta(minutes=minutes + 1))
            moved.append(slot["slot_key"])
    if not moved and not already:
        return await say(interaction, "No approved parties to complete. Run /draft approve first.")
    lines = [f"Marked {len(moved)} slot(s) of **{run['name']}** as ended: {', '.join(moved) or 'none'}."]
    if already:
        lines.append(f"{already} slot(s) had already ended and were left alone.")
    lines.append("The rating button appears in each slot's thread within a minute (or use /rate).")
    await say(interaction, "\n".join(lines)[:1990])


async def _debug_fight(ctx, interaction, content):
    fight = _fight_for(ctx, content)
    if fight is None:
        known = ", ".join(f["short_name"] or f["name"] for f in ctx.db.fights())
        await say(interaction, f"Say which fight (content). Known: {known}.")
    return fight


def _prog_text(cps, idx):
    from .services import checkpoint_label
    return f"**{checkpoint_label(cps, idx)}** (#{idx})"


async def debug_setprog(ctx, interaction, member, checkpoint, content=None):
    """Testing aid: set a player's current prog point by checkpoint index."""
    if not await require_organizer(ctx, interaction):
        return
    fight = await _debug_fight(ctx, interaction, content)
    if fight is None:
        return
    cps = ctx.db.checkpoints(fight["id"])
    if checkpoint not in {c["idx"] for c in cps}:
        return await say(interaction, f"Checkpoint must be a number from 0 to {max(c['idx'] for c in cps)}.")
    ctx.db.upsert_player(member.id, member.display_name, prog_index=checkpoint, now=ctx.now())
    await say(interaction, f"{member.display_name}'s prog is now {_prog_text(cps, checkpoint)}.")


async def debug_sync_checkpoints(ctx, interaction, content=None):
    """Testing aid: re-read the fight's prog point file now (the bot also does this on every start).

    Stored prog numbers are moved to the new numbering by phase and name, never cleared."""
    if not await require_organizer(ctx, interaction):
        return
    fight = await _debug_fight(ctx, interaction, content)
    if fight is None:
        return
    try:
        doc = progscore.load_doc(ctx.cfg, fight)
        rep = progscore.sync_fight(ctx.db, doc, fight["short_name"])
    except progscore.ProgPointError as e:
        return await say(interaction, str(e))
    if not rep["changed"]:
        return await say(interaction, "The checkpoints already match the prog point file.")
    await say(interaction, f"Checkpoints replaced with the {len(doc['points'])} points of the prog point file; "
                           f"{rep['remapped']} stored prog number(s) moved to the new numbering.")


def _score_summary(doc, state, name, current=None):
    def show(h):
        return f"{h['phase']} {h['name']} (#{h['idx']})" if h else "nothing reached"

    lines = [f"**{name}**: {len(state['pulls'])} pull(s) stored.",
             f"Normal (cleanup tag ignored): {show(progscore.effective_highest(doc, state, 'normal'))}",
             f"Consistent (cleanup points must pass the check): {show(progscore.effective_highest(doc, state, 'consistent', current))}"]
    cl = progscore.cleanup_lines(doc, state)
    if cl:
        lines += ["Cleanup consistency:", *cl]
    return "\n".join(lines)


def _day(iso_text):
    return iso_text[:16].replace("T", " ") + " UTC" if iso_text else "?"


async def debug_score(ctx, interaction, member, mode="normal", count=10, character=None, reset=False, content=None,
                      hours=None):
    """Testing aid: score a player's recent FFLogs reports and raise their prog to the highest point found.

    mode 'normal' treats cleanup-tagged points like any other; 'consistent' skips cleanup points whose
    consistency check failed. hours = how far back to read reports (default cfg prog_fetch_hours = 48;
    0 = no limit). The prog never goes down: the highest confirmed point is stored and only raised."""
    if not await require_organizer(ctx, interaction):
        return
    if ctx.fflogs is None:
        return await say(interaction, "FFLogs is not configured (fflogs_client_id / fflogs_client_secret).")
    if mode not in progscore.MODES:
        return await say(interaction, f"Mode must be one of: {', '.join(progscore.MODES)}.")
    hours = ctx.cfg["prog_fetch_hours"] if hours is None else hours
    if not 1 <= count <= 30 or not 0 <= hours <= 24 * 365:
        return await say(interaction, "Count is how many recent reports to read: 1 to 30. Hours is how far back, "
                                      "0 for no limit.")
    fight = await _debug_fight(ctx, interaction, content)
    if fight is None:
        return
    try:
        doc = progscore.load_doc(ctx.cfg, fight)
    except progscore.ProgPointError as e:
        return await say(interaction, str(e))
    if not progscore.checkpoints_match(ctx.db, fight["id"], doc):
        return await say(interaction, "This fight's checkpoints do not match its prog point file, so the scored "
                                      "numbers would point at the wrong checkpoints. Run /debug sync_checkpoints first (the bot also syncs on every start).")
    chars = ch.list_characters(ctx.db, member.id)
    if character:
        chars = [c for c in chars if character.lower() in (c["name"].lower(), ch.label(c).lower())]
    if not chars:
        return await say(interaction, f"{member.display_name} has no matching verified character (/character add).")
    if len(chars) > 1:
        return await say(interaction, "More than one character: pick one with `character` ("
                                      + ", ".join(c["name"] for c in chars) + ").")
    char = chars[0]
    await interaction.response.defer(ephemeral=True)          # reading logs can take a while
    try:
        state, restarted = await progscore.score_character(ctx, char, doc, count=count, hours=hours, reset=reset)
    except FFLogsError as e:
        return await say(interaction, f"FFLogs problem: {e} Progress so far is kept; run it again to continue.")
    except progscore.ProgPointError as e:
        return await say(interaction, f"The prog point file is not usable: {e}")
    player = ctx.db.get_player(member.id)
    stored = progscore.get_scores(ctx.db, char["id"], doc["encounter_id"])
    known = [i for i in (player["prog_index"] if player else None, stored["confirmed_idx"] if stored else None)
             if i is not None]
    current = max(known, default=None)                       # where the character already is
    text = _score_summary(doc, state, ch.label(char), current)
    text += f"\nRead reports from the last {hours} hour(s)." if hours else "\nRead reports with no time limit."
    if restarted:
        text = f"Started over: {restarted}\n" + text
    chosen = progscore.effective_highest(doc, state, mode, current)
    row, raised = progscore.record_result(ctx.db, char["id"], doc["encounter_id"], chosen, mode, state, ctx.now())
    cps = {c["idx"]: c for c in ctx.db.checkpoints(fight["id"])}

    def label(idx):
        return f"{cps[idx]['phase']} {cps[idx]['name']} (#{idx})"
    text += (f"\nLatest highest ({mode}): {label(chosen['idx'])}" if chosen else f"\nLatest highest ({mode}): nothing in this window")
    if row["confirmed_idx"] is not None:
        text += f"\nHighest confirmed: {label(row['confirmed_idx'])}, {row['confirmed_mode']}, {_day(row['confirmed_utc'])}" \
                + (" (new)" if raised else "")
    current = ctx.db.get_player(member.id)["prog_index"] if ctx.db.get_player(member.id) else None
    if row["confirmed_idx"] is not None and (current is None or row["confirmed_idx"] > current):
        ctx.db.upsert_player(member.id, member.display_name, prog_index=row["confirmed_idx"], now=ctx.now())
        text += f"\nSet {member.display_name}'s prog to **{label(row['confirmed_idx'])}**."
    else:
        text += f"\n{member.display_name}'s prog stays at " + (label(current) if current is not None else "unset") + "."
    await say(interaction, text[:1990])


# ---------------------------------------------------------------- /party import

async def party_import(ctx, interaction, party_file):
    """Organizers: apply an edited party JSON (from the "Export JSON" button) to an approved party."""
    if not await require_organizer(ctx, interaction):
        return
    if party_file.size > 200_000:
        return await say(interaction, "That file is too big to be a party export.")
    try:
        data = json.loads((await party_file.read()).decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError):
        return await say(interaction, "That is not valid JSON.")
    if not isinstance(data, dict):
        return await say(interaction, "That is not a party export.")
    party_id, ops, warnings, errors = plan_import(ctx, data)
    if errors:
        return await say(interaction, "Import refused, nothing changed:\n" + "\n".join("• " + e for e in errors))
    if not ops:
        return await say(interaction, "Nothing differs from the current party.")
    if warnings and data.get("force") is not True:
        return await say(interaction, "Import refused, nothing changed. Warnings (set \"force\": true in the file to "
                         "accept them):\n" + "\n".join("• " + w for w in warnings))
    await interaction.response.defer(ephemeral=True)
    from . import party_edit
    party = ctx.db.party(party_id)
    edit = []
    for op in ops:
        if op["kind"] == "swap":
            edit.append({"op": "swap", "party": party_id, "out": op["out"], "in": op["in"], "role": op["role"],
                         "reason": {"code": op["code"], "text": op["text"]}, "source": "import"})
        else:
            edit.append({"op": "role", "party": party_id, "player": op["player"], "role": op["role"], "source": "import"})
    result, _, problems = await party_edit.apply(ctx, interaction.guild_id, party["slot_id"], edit, interaction.user.id,
                                                 warnings=warnings or None)
    if result.errors:
        return await say(interaction, "Import refused, nothing changed:\n" + "\n".join("• " + e for e in result.errors))
    await say(interaction, "\n".join(result.lines + ["⚠ " + p for p in problems])[:1990])


# ---------------------------------------------------------------- /helper

def _fight_for(ctx, content):
    if content:
        return ctx.db.find_fight(content)
    fights = ctx.db.fights()
    return fights[0] if len(fights) == 1 else None


async def helper_grant(ctx, interaction, member, content=None):
    if not await require_organizer(ctx, interaction):
        return
    fight = _fight_for(ctx, content)
    if fight is None:
        return await say(interaction, "Which content? Give its name.")
    ctx.db.grant_helper(fight["id"], member.id, interaction.user.id, ctx.now())
    await say(interaction, f"{member.display_name} is now a helper for {fight['name']}.")


async def helper_revoke(ctx, interaction, member, content=None):
    if not await require_organizer(ctx, interaction):
        return
    fight = _fight_for(ctx, content)
    if fight is None:
        return await say(interaction, "Which content? Give its name.")
    if ctx.db.revoke_helper(fight["id"], member.id):
        await say(interaction, f"{member.display_name} is no longer a helper for {fight['name']}.")
    else:
        await say(interaction, f"{member.display_name} was not a helper for {fight['name']}.")


async def helper_support(ctx, interaction, member, helpers, required=False, content=None):
    """Seat this player only with `helpers` helpers or higher-prog players (soft; `required` makes it a hard rule)."""
    if not await require_organizer(ctx, interaction):
        return
    fight = _fight_for(ctx, content)
    if fight is None:
        return await say(interaction, "Which content? Give its name.")
    if not 1 <= helpers <= 7:
        return await say(interaction, "Helpers must be between 1 and 7.")
    ctx.db.set_support_need(fight["id"], member.id, helpers, required, interaction.user.id, ctx.now())
    cap = ctx.cfg.get("max_helpers_per_party")
    note = (f"\nThe solver caps helpers per party at {cap}, so the rest has to come from players with a higher prog."
            if cap is not None and helpers > cap else "")
    await say(interaction, f"{member.display_name} will be seated with {helpers} helper(s) or higher-prog player(s) for "
                           f"{fight['name']} ({'required: left out if it cannot be met' if required else 'preferred: the solver tries its best'})."
                           + note + "\nExport again to send it to the solver.")


async def helper_unsupport(ctx, interaction, member, content=None):
    if not await require_organizer(ctx, interaction):
        return
    fight = _fight_for(ctx, content)
    if fight is None:
        return await say(interaction, "Which content? Give its name.")
    if ctx.db.clear_support_need(fight["id"], member.id):
        await say(interaction, f"{member.display_name} no longer needs support for {fight['name']}.")
    else:
        await say(interaction, f"{member.display_name} had no support need for {fight['name']}.")


async def helper_list(ctx, interaction, content=None):
    if not await require_organizer(ctx, interaction):
        return
    fight = _fight_for(ctx, content)
    if fight is None:
        return await say(interaction, "Which content? Give its name.")
    helpers, needs = sorted(ctx.db.helper_ids(fight["id"])), ctx.db.support_needs(fight["id"])
    lines = [f"**{fight['name']}**", "Helpers: " + (", ".join(f"<@{d}>" for d in helpers) or "nobody")]
    lines += [f"Needs support: <@{d}> · {n['helpers']} helper(s)" + (" · required" if n["required"] else "")
              for d, n in sorted(needs.items())] or ["Needs support: nobody"]
    await say(interaction, "\n".join(lines))
