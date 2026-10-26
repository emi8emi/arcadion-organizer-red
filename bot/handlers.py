"""Slash command logic. Each handler takes (ctx, interaction, ...) and replies ephemerally.

Kept free of discord.app_commands decorators so tests can call them with mock interactions.
"""
import io
import json
import re
from pathlib import Path

import discord

from . import heatmap
from .config import ROOT
from .db import DraftConflict, DuplicateRun, parse_iso
from .grid import parse_date, parse_hm, parse_local_datetime, run_slots
from .services import (limits_text, profile_text, signup_open, tick_lines, valid_timezone,
                       validate_limits)
from .solver_io import DraftError, build_signups, fingerprint, import_draft
from .views import AvailabilityView

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

async def run_create(ctx, interaction, name, content, start_date, end_date, signup_deadline,
                     earliest_start=None, latest_start=None):
    if not await require_organizer(ctx, interaction):
        return
    db, tz = ctx.db, ctx.cfg["timezone"]
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
        return await say(interaction, "Dates look like 2026-11-02 (year-month-day).")
    if end < start:
        return await say(interaction, "The end date is before the start date.")
    if (end - start).days + 1 > ctx.cfg["max_run_days"]:
        return await say(interaction, f"Runs can be at most {ctx.cfg['max_run_days']} days long.")
    try:
        deadline = parse_local_datetime(signup_deadline, tz)
    except ValueError:
        return await say(interaction, f"The deadline looks like 2026-10-30 18:00 (time in {tz}).")
    try:
        for hm in (earliest_start, latest_start):
            if hm:
                parse_hm(hm)
    except ValueError:
        return await say(interaction, "Earliest/latest start look like 17:00.")

    slots = run_slots(start, end, ctx.grid, earliest_start, latest_start)
    if not slots:
        return await say(interaction, "Those dates and start times leave no slots.")
    if deadline >= slots[0].start_utc:
        return await say(interaction, "The signup deadline must be before the first slot.")
    try:
        db.create_run(guild_id=interaction.guild_id, name=name, fight_id=fight["id"], start_date=start,
                      end_date=end, deadline_utc=deadline, slots=slots, created_by=interaction.user.id,
                      earliest=earliest_start, latest=latest_start, now=ctx.now())
    except DuplicateRun:
        return await say(interaction, f"A run called '{name}' already exists here.")
    await say(interaction,
              f"Created **{name}** for {fight['name']}: {(end - start).days + 1} days, {len(slots)} candidate slots. "
              f"Signups would close {discord.utils.format_dt(deadline, 'F')}. It is a draft; use /run open to start signups.")


async def run_open(ctx, interaction, name=None):
    if not await require_organizer(ctx, interaction):
        return
    run = await find_run(ctx, interaction, name)
    if run is None:
        return
    if run["status"] == "open":
        return await say(interaction, f"**{run['name']}** is already open.")
    other = ctx.db.open_run(interaction.guild_id)
    if other:
        return await say(interaction, f"**{other['name']}** is still open. Close it first with /run close.")
    deadline = parse_iso(run["signup_deadline_utc"])
    if ctx.now() >= deadline:
        return await say(interaction, f"The signup deadline ({discord.utils.format_dt(deadline, 'F')}) has passed. "
                                      "Create a new run with a later deadline.")
    ctx.db.set_run_status(run["id"], "open")
    await say(interaction, f"**{run['name']}** is open. Players sign up with /availability until "
                           f"{discord.utils.format_dt(deadline, 'F')}.")


async def run_close(ctx, interaction, name=None):
    if not await require_organizer(ctx, interaction):
        return
    run = await find_run(ctx, interaction, name)
    if run is None:
        return
    if run["status"] != "open":
        return await say(interaction, f"**{run['name']}** is not open.")
    ctx.db.set_run_status(run["id"], "closed")
    n = len({r["discord_id"] for r in ctx.db.tick_rows(run["id"])})
    await say(interaction, f"**{run['name']}** is closed with {n} players signed up. "
                           "Check /run status, then /draft export.")


async def run_status(ctx, interaction, name=None, day=None):
    if not await require_organizer(ctx, interaction):
        return
    run = await find_run(ctx, interaction, name)
    if run is None:
        return
    if day:
        try:
            parse_date(day)
        except ValueError:
            return await say(interaction, "The day looks like 2026-11-02.")
    first, *rest = heatmap.render(ctx.db, run, ctx.cfg, day)
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
    await say(interaction, f"**{run['name']}**\n{limits_text(lim)}{note}")


async def myschedule(ctx, interaction):
    run = ctx.db.open_run(interaction.guild_id) or ctx.db.get_run(interaction.guild_id)
    if run is None:
        return await say(interaction, "There are no runs yet.")
    uid = interaction.user.id
    keys = ctx.db.ticked_keys(run["id"], uid)
    lines = [f"**{run['name']}** ({run['status']})", profile_text(ctx, run, uid),
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

async def draft_export(ctx, interaction, name=None):
    if not await require_organizer(ctx, interaction):
        return
    run = await find_run(ctx, interaction, name)
    if run is None:
        return
    data, warnings = build_signups(ctx.db, run, ctx.cfg)
    body = json.dumps(data, indent=1).encode()
    out_dir = Path(ctx.cfg["export_dir"])
    out_dir = out_dir if out_dir.is_absolute() else ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{slug(run['name'])}-signups.json"
    (out_dir / filename).write_bytes(body)

    lines = [f"Exported **{run['name']}**: {len(data['players'])} players, {len(data['slots'])} slots, "
             f"fingerprint `{fingerprint(data)[:12]}`.",
             f"Also saved to {out_dir / filename}.",
             f"Next: `python solve.py {filename} -o draft.json`, then /draft import with draft.json."]
    if run["status"] == "open":
        lines.append("⚠ Signups are still open. Any change after this export makes the draft stale, and import will refuse it.")
    if warnings:
        lines.append("Left out:\n" + "\n".join(f"• {w}" for w in warnings[:10])
                     + (f"\n… and {len(warnings) - 10} more" if len(warnings) > 10 else ""))
    await say(interaction, "\n".join(lines)[:1990], file=discord.File(io.BytesIO(body), filename=filename))


async def draft_import(ctx, interaction, draft_file, name=None):
    if not await require_organizer(ctx, interaction):
        return
    run = await find_run(ctx, interaction, name)
    if run is None:
        return
    if draft_file.size > MAX_DRAFT_BYTES:
        return await say(interaction, "That file is too large to be a draft.")
    await interaction.response.defer(ephemeral=True)
    try:
        draft = json.loads(await draft_file.read())
    except (ValueError, UnicodeDecodeError):
        return await say(interaction, "That file is not valid JSON.")
    try:
        s = import_draft(ctx.db, run, draft, ctx.cfg)
    except DraftError as e:
        return await say(interaction, f"Draft refused: {e}")
    except DraftConflict:
        return await say(interaction, "This run already has confirmed parties, so a new draft cannot replace them.")
    lines = [f"Draft stored for **{run['name']}** (status: draft, nothing is posted to players): "
             f"{s['parties']} parties in {s['slots']} slots, {s['seated']} players seated, "
             f"{s['no_party']} without a party, {s['placed_down']} placed below their prog."]
    for _, slot, reason in s["slot_notes"][:5]:
        lines.append(f"• {slot}: {reason}")
    if len(s["slot_notes"]) > 5:
        lines.append(f"… and {len(s['slot_notes']) - 5} more unfilled slots.")
    await say(interaction, "\n".join(lines)[:1990])


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
