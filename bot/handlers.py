"""Slash command logic. Each handler takes (ctx, interaction, ...) and replies ephemerally.

Kept free of discord.app_commands decorators so tests can call them with mock interactions.
"""
import io
import json
import re
from pathlib import Path
from zoneinfo import available_timezones

import discord

from . import heatmap
from .config import ROOT
from .db import DraftConflict, DuplicateRun, parse_iso
from .grid import day_cutoff, parse_date, parse_hm, parse_local_datetime, run_slots
from .services import (snapshot_note, cutoff_of, day_label, day_state, day_summary_lines, limits_text, locked_prog_lines, open_days,
                       profile_text, signup_open, tick_lines, valid_timezone, validate_limits)
from .snapshots import ensure_snapshot
from .solver_io import DraftError, build_signups, fingerprint, import_draft, is_note
from .posting import post_day_parties
from .views import AvailabilityView, RateListView
from .votes import rateable_parties, summary_counts

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
    role_id = ctx.cfg.get("organizer_role_id")
    return bool(role_id) and any(getattr(r, "id", None) == int(role_id) for r in getattr(user, "roles", []))


async def require_organizer(ctx, interaction):
    if is_organizer(ctx, interaction.user):
        return True
    await say(interaction, "Only organizers can use this command.")
    return False


async def find_run(ctx, interaction, name=None):
    run = ctx.db.get_run(interaction.guild_id, name)
    if run is None:
        await say(interaction, f"No run called '{name}' in this server." if name else "There are no runs yet. Use /run create.")
    return run


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


async def run_create(ctx, interaction, name, content, start_date, end_date,
                     earliest_start=None, latest_start=None, timezone=None,
                     cutoff_time=None, cutoff_days_before=None):
    if not await require_organizer(ctx, interaction):
        return
    db = ctx.db

    # 1. Validate user_tz and fall back to ctx.cfg["timezone"] if invalid/missing
    if timezone and timezone.strip() in available_timezones():
        tz = timezone.strip()
    else:
        # Optional: Send a warning to the user if they provided a broken timezone string
        if timezone:
            await interaction.response.send_message(
                f"⚠️ `{timezone}` is not a valid timezone. Falling back to default: `{ctx.cfg['timezone']}`",
                ephemeral=True
            )
        tz = ctx.cfg["timezone"]

    name = name.strip()
    if not name or len(name) > 60:
        return await say(interaction, "Give the run a name of 1 to 60 characters.")
    fight = db.find_fight(content)
    if fight is None:
        known = ", ".join(f["name"] for f in db.fights())
        return await say(interaction, f"I don't know the content '{content}'. Known: {known}.")
    try:
        start, end = parse_date(start_date), parse_date(end_date)
    except ValueError:
        return await say(interaction, "Dates look like 2026-11-02 (year-month-day). You can also use today, tomorrow and yesterday.")
    if end < start:
        return await say(interaction, "The end date is before the start date.")
    if (end - start).days + 1 > ctx.cfg["max_run_days"]:
        return await say(interaction, f"Runs can be at most {ctx.cfg['max_run_days']} days long.")
    try:
        cutoff_hm = parse_hm(cutoff_time or ctx.cfg["cutoff_time"])
    except ValueError:
        return await say(interaction, "The cutoff time looks like 23:59 (the clock time each day's signups close).")
    days_before = ctx.cfg["cutoff_days_before"] if cutoff_days_before is None else cutoff_days_before
    if not 0 <= days_before <= 7:
        return await say(interaction, "Cutoff days before must be between 0 (the day itself) and 7.")
    try:
        for hm in (earliest_start, latest_start):
            if hm:
                parse_hm(hm)
    except ValueError:
        return await say(interaction, "Earliest/latest start look like 17:00.")

    slots = run_slots(start, end, ctx.grid, earliest_start, latest_start)
    if not slots:
        return await say(interaction, "Those dates and start times leave no slots.")
    first_on = {}
    for sl in slots:
        first_on.setdefault(sl.key[:10], sl.start_utc)
    cutoffs = {}
    for day, first_start in first_on.items():
        cutoffs[day] = day_cutoff(parse_date(day), cutoff_hm, days_before, tz)
        if cutoffs[day] >= first_start:
            return await say(interaction, f"The cutoff for {day_label(day)} "
                                          f"({discord.utils.format_dt(cutoffs[day], 'F')}) must be before that day's first slot. "
                                          "Use a cutoff earlier in the day, or more days before.")
    try:
        db.create_run(guild_id=interaction.guild_id, name=name, fight_id=fight["id"], start_date=start,
                      end_date=end, cutoffs=cutoffs, slots=slots, created_by=interaction.user.id,
                      earliest=earliest_start, latest=latest_start, now=ctx.now())
    except DuplicateRun:
        return await say(interaction, f"A run called '{name}' already exists here.")
    ordered = [cutoffs[d] for d in sorted(cutoffs)]
    when = ("the evening before" if days_before == 1 else "the day itself" if days_before == 0
            else f"{days_before} days before")
    lines = [f"Created **{name}** for {fight['name']}: {len(cutoffs)} days, {len(slots)} candidate slots. "
             f"Each day's signups close at {cutoff_hm.strftime('%H:%M')} ({tz}) {when}: the first is "
             f"{discord.utils.format_dt(ordered[0], 'F')}, the last {discord.utils.format_dt(ordered[-1], 'F')}. "
             "It is a draft; use /run open to start signups."]
    if not ctx.cfg.get("reminder_channel_id"):
        lines.append("⚠ No reminder channel is set (`reminder_channel_id` in config.json or REMINDER_CHANNEL_ID in .env), "
                     "so the bot will not remind organizers to run /draft export after each cutoff.")
    await say(interaction, "\n".join(lines))


async def run_open(ctx, interaction, name=None):
    if not await require_organizer(ctx, interaction):
        return
    run = await find_run(ctx, interaction, name)
    if run is None:
        return
    if run["status"] == "open":
        return await say(interaction, f"**{run['name']}** is already open.")
    other = ctx.db.open_run(interaction.guild_id)
    stale = ""
    if other:
        if open_days(ctx, other):
            return await say(interaction, f"**{other['name']}** is still open. Close it first with /run close.")
        ctx.db.set_run_status(other["id"], "closed")           # every day of it is past its cutoff anyway
        stale = f" (I closed **{other['name']}**: all its days were already past their cutoffs.)"
    rows = ctx.db.run_days(run["id"])
    now = ctx.now()
    still_open = [r for r in rows if now < cutoff_of(r)]
    if not still_open:
        return await say(interaction, "Every day of this run is already past its cutoff, so nobody could sign up. "
                                      "Move a day with /run extend first.")
    ctx.db.set_run_status(run["id"], "open")
    nxt = discord.utils.format_dt(cutoff_of(still_open[0]), "F")
    closed = len(rows) - len(still_open)
    await say(interaction, f"**{run['name']}** is open. Players sign up with /availability; "
                           f"{len(still_open)} of {len(rows)} days take signups"
                           + (f" ({closed} already closed)" if closed else "")
                           + f", the next cutoff is {day_label(still_open[0]['day'])}, {nxt}." + stale)


async def run_close(ctx, interaction, name=None, day=None):
    if not await require_organizer(ctx, interaction):
        return
    run = await find_run(ctx, interaction, name)
    if run is None:
        return
    if run["status"] != "open":
        return await say(interaction, f"**{run['name']}** is not open.")
    now = ctx.now()
    if day:
        row = await find_day(ctx, interaction, run, day)
        if row is None:
            return
        if now >= cutoff_of(row):
            return await say(interaction, f"{day_label(row['day'])} is already closed.")
        ctx.db.set_cutoff(run["id"], row["day"], now)
        ctx.db.take_snapshot(run["id"], row["day"], ctx.cfg, now)
        return await say(interaction, f"Signups for {day_label(row['day'])} are closed now and their roles, prog and "
                                      f"limits are saved. "
                                      f"Next: `/draft export day:{row['day']}`.")
    for row in ctx.db.run_days(run["id"]):
        if now < cutoff_of(row):
            ctx.db.set_cutoff(run["id"], row["day"], now)
            ctx.db.take_snapshot(run["id"], row["day"], ctx.cfg, now)
        else:
            ensure_snapshot(ctx, run, row["day"])          # closed earlier but the loop has not got to it
    ctx.db.set_run_status(run["id"], "closed")
    n = len({r["discord_id"] for r in ctx.db.tick_rows(run["id"])})
    await say(interaction, f"**{run['name']}** is closed for every day, with {n} players signed up. "
                           "Check /run status, then /draft export for each day.")


async def run_extend(ctx, interaction, day, new_cutoff, name=None, timezone=None):
    """Move one day's cutoff (also reopens a day that already closed)."""
    if not await require_organizer(ctx, interaction):
        return
    run = await find_run(ctx, interaction, name)
    if run is None:
        return
    row = await find_day(ctx, interaction, run, day)
    if row is None:
        return
    tz = timezone.strip() if timezone and timezone.strip() in available_timezones() else ctx.cfg["timezone"]
    try:
        cutoff = parse_local_datetime(new_cutoff, tz)
    except ValueError:
        return await say(interaction, f"The new cutoff looks like 2026-11-01 23:59 (time in {tz}). "
                                      "You can also use today and tomorrow with a time.")
    first = ctx.db.first_slot_start(run["id"], row["day"])
    if cutoff >= first:
        return await say(interaction, f"The cutoff must be before {day_label(row['day'])}'s first slot "
                                      f"({discord.utils.format_dt(first, 'F')}).")
    if cutoff <= ctx.now():
        return await say(interaction, "That time has already passed. To close a day now use /run close with its day.")
    if run["status"] == "closed":
        other = ctx.db.open_run(interaction.guild_id)
        if other and other["id"] != run["id"] and open_days(ctx, other):
            return await say(interaction, f"**{other['name']}** is open; close it before reopening **{run['name']}**.")
        ctx.db.set_run_status(run["id"], "open")
    was_exported = bool(row["exported_utc"])
    ctx.db.set_cutoff(run["id"], row["day"], cutoff, reset_export=True)
    ctx.db.clear_snapshot(run["id"], row["day"])         # the day reopens; a new snapshot is taken at the new cutoff
    note = (" It had been exported, so export again after the new cutoff; the stored draft for that day stays "
            "until you import a new one." if was_exported else "")
    if row["snapshot_utc"]:
        note += " The signups saved at the old cutoff were dropped; a new snapshot is taken at the new cutoff."
    await say(interaction, f"Signups for {day_label(row['day'])} now close {discord.utils.format_dt(cutoff, 'F')}." + note)


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

async def availability(ctx, interaction, timezone=None):
    run = ctx.db.open_run(interaction.guild_id)
    if run is None:
        return await say(interaction, "No run is open for signups right now.")
    ok, why = signup_open(ctx, run)
    if not ok:
        return await say(interaction, why)
    if timezone is not None and not valid_timezone(timezone):
        return await say(interaction, f"'{timezone}' is not a timezone name like America/Chicago.")
    ctx.db.upsert_player(interaction.user.id, interaction.user.display_name, timezone=timezone, now=ctx.now())
    view = AvailabilityView(ctx, run, interaction.user.id, interaction.user.display_name)
    await interaction.response.send_message(view.render(), view=view, ephemeral=True)


async def limits(ctx, interaction, max_per_day=None, max_per_run=None, drop_checkpoints=None):
    run = ctx.db.open_run(interaction.guild_id)
    if run is None:
        return await say(interaction, "No run is open for signups right now.")
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


async def myschedule(ctx, interaction):
    run = ctx.db.open_run(interaction.guild_id) or ctx.db.get_run(interaction.guild_id)
    if run is None:
        return await say(interaction, "There are no runs yet.")
    uid = interaction.user.id
    keys = ctx.db.ticked_keys(run["id"], uid)
    lines = [f"**{run['name']}** ({run['status']})", profile_text(ctx, run, uid),
             *locked_prog_lines(ctx, run, uid),
             f"Slots you ticked ({len(keys)}), in your local time:"]
    lines += tick_lines(ctx, run, keys) or ["• none"]
    mine = ctx.db.player_parties(run["id"], uid)
    if mine:
        lines.append("Your confirmed parties:")
        for p in mine:
            when = discord.utils.format_dt(parse_iso(p["start_utc"]), "F")
            lines.append(f"• {when} as {p['role']}")
    elif ctx.db.parties(run["id"]):
        lines.append("A draft is with the organizers; your parties show up here once they are confirmed.")
    await say(interaction, "\n".join(lines)[:1990])


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
    if run is None:
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
        return await say(interaction, f"{why} Close it with `/run close day:{day_key}`, or pass `force:true` to export "
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
    if run is None:
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
    lines = [f"Draft stored for **{run['name']}**{', ' + day_label(day_key) if day_key else ''} "
             f"(status: draft, nothing is posted to players): "
             f"{s['parties']} parties in {s['slots']} slots, {s['seated']} players seated, "
             f"{s['no_party']} without a party, {s['placed_down']} placed below their prog."]
    for _, slot, reason in s["slot_notes"][:5]:
        lines.append(f"• {slot}: {reason}")
    if len(s["slot_notes"]) > 5:
        lines.append(f"… and {len(s['slot_notes']) - 5} more unfilled slots.")
    await say(interaction, "\n".join(lines)[:1990])


async def draft_approve(ctx, interaction, name=None, day=None):
    """Confirm one day's draft parties and post them (one thread per slot, one message per party)."""
    if not await require_organizer(ctx, interaction):
        return
    run = await find_run(ctx, interaction, name)
    if run is None:
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
    channel_id = ctx.cfg.get("parties_channel_id")
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
        return await say(interaction, head + " No `parties_channel_id` is set in config.json, so nothing was posted. "
                                              "Players see their party under /myschedule.")
    channel = await ctx.get_channel(int(channel_id)) if ctx.get_channel else None
    if channel is None:
        return await say(interaction, head + f" I could not find channel {channel_id}, so nothing was posted. "
                                              "Players see their party under /myschedule. Run /draft approve again "
                                              "after fixing parties_channel_id to post them.")
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
