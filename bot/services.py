"""Shared pieces for command handlers and the signup views."""
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Callable, Optional
from zoneinfo import ZoneInfo

import discord

from .db import DB, parse_iso, split_roles
from .grid import GridConfig
from .characters import characters_line

MAX_PER_DAY = 8
MAX_PER_RUN = 40


@dataclass
class Ctx:
    db: DB
    cfg: dict
    grid: GridConfig
    now: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc))
    lodestone: object = None
    fflogs: object = None
    get_channel: Optional[Callable] = None        # async (channel_id) -> channel or thread; set by the bot


def day_label(day):
    """'2026-11-02' -> 'Mon 02 Nov'"""
    return date.fromisoformat(day).strftime("%a %d %b")


def cutoff_of(day_row):
    return parse_iso(day_row["cutoff_utc"])


def day_state(ctx, run, day_row):
    """'draft' (run not opened), 'open', 'closed' (signups over, not exported) or 'exported'."""
    if day_row["exported_utc"]:
        return "exported"
    if run["status"] == "draft":
        return "draft"
    if run["status"] != "open" or ctx.now() >= cutoff_of(day_row):
        return "closed"
    return "open"


def day_accepts_signups(ctx, run, day_row):
    """Players may change this day's ticks: the run is open and the day's cutoff has not passed.

    Deliberately independent of the export mark, so a day exported early with force can still change
    until its cutoff (the draft then goes stale and import refuses it).
    """
    return run["status"] == "open" and ctx.now() < cutoff_of(day_row)


def open_days(ctx, run):
    return [d for d in ctx.db.run_days(run["id"]) if day_accepts_signups(ctx, run, d)]


def signup_open(ctx, run, day=None):
    """(True, '') when players may change their signup, else (False, reason).

    Without `day`: is any day of the run still taking signups (roles, prog and limits are per player,
    so they only need one open day). With `day`: is that specific day open.
    """
    if run["status"] != "open":
        return False, "Signups for this run are not open."
    if day is None:
        if open_days(ctx, run):
            return True, ""
        return False, "Signups are closed for every day of this run."
    row = ctx.db.run_day(run["id"], day)
    if row is None:
        return False, "That day is not part of this run."
    if not day_accepts_signups(ctx, run, row):
        return False, f"Signups for {day_label(day)} closed {discord.utils.format_dt(cutoff_of(row), 'F')}."
    return True, ""


def day_summary_lines(ctx, run, only_day=None):
    """One line per day: its state and cutoff (Discord timestamps show in each viewer's local time)."""
    lines = []
    for row in ctx.db.run_days(run["id"]):
        if only_day and row["day"] != only_day:
            continue
        state = day_state(ctx, run, row)
        cutoff = discord.utils.format_dt(cutoff_of(row), "R")
        when = {"open": f"open, closes {cutoff}", "draft": f"not open yet, would close {cutoff}",
                "closed": f"closed {cutoff}, not exported yet", "exported": f"exported (cutoff {cutoff})"}[state]
        lines.append(f"{day_label(row['day'])}: {when}")
    return lines


def clip_message(text, limit=1990):
    """Cut a message to Discord's length limit at a line break, so a timestamp token is never split."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    return cut[:cut.rfind("\n")] if "\n" in cut else cut


def validate_limits(per_day, per_run, drop, n_checkpoints):
    """Error text for out-of-range limits, or None. None values are 'not changing' and pass."""
    if per_day is not None and not 1 <= per_day <= MAX_PER_DAY:
        return f"Max parties per day must be between 1 and {MAX_PER_DAY}."
    if per_run is not None and not 1 <= per_run <= MAX_PER_RUN:
        return f"Max parties per run must be between 1 and {MAX_PER_RUN}."
    if per_day is not None and per_run is not None and per_run < per_day:
        return "Max per run cannot be smaller than max per day."
    if drop is not None and not 0 <= drop < n_checkpoints:
        return f"Placing lower must be between 0 (not OK) and {n_checkpoints - 1} checkpoints."
    return None


def checkpoint_label(checkpoints, idx):
    for c in checkpoints:
        if c["idx"] == idx:
            return f"{idx}: {c['phase']} {c['name']}"
    return "not set"


def limits_text(lim):
    drop = (f"OK with being placed up to {lim['drop_checkpoints']} checkpoint(s) below your prog"
            if lim["drop_checkpoints"] else "not OK with being placed below your prog")
    return f"Limits: up to {lim['max_per_day']} per day, {lim['max_per_run']} per run; {drop}."


def profile_text(ctx, run, discord_id):
    """Roles, prog and limits for one player, as short lines."""
    player = ctx.db.get_player(discord_id)
    cps = ctx.db.checkpoints(run["fight_id"])
    roles = split_roles(player["roles"]) if player else []
    prog = player["prog_index"] if player else None
    lines = [f"Roles: {', '.join(roles) if roles else 'not set'} · Prog: {checkpoint_label(cps, prog)}",
             limits_text(ctx.db.get_limits(run["id"], discord_id, ctx.cfg))]
    lines.append(characters_line(ctx.db, discord_id))
    if not roles or prog is None:
        lines.append("⚠ Set your roles and prog, or organizers cannot use your signup.")
    return "\n".join(lines)


def tick_lines(ctx, run, keys, limit=30):
    """Ticked slots as Discord timestamps (each viewer sees their own local time)."""
    start = {s["slot_key"]: parse_iso(s["start_utc"]) for s in ctx.db.run_slots(run["id"])}
    lines = [f"• {discord.utils.format_dt(start[k], 'F')}" for k in keys if k in start]
    if len(lines) > limit:
        extra = len(lines) - limit
        lines = lines[:limit] + [f"… and {extra} more"]
    return lines


def valid_timezone(name):
    try:
        ZoneInfo(name)
        return True
    except Exception:
        return False


def snapshotted_days_of(ctx, run, discord_id):
    """Days this player signed up for whose signups were already frozen at the cutoff, in order."""
    mine = {k[:10] for k in ctx.db.ticked_keys(run["id"], discord_id)}
    return [d["day"] for d in ctx.db.run_days(run["id"]) if d["snapshot_utc"] and d["day"] in mine]


def snapshot_note(ctx, run, discord_id):
    """Shown after a player edits roles, prog or limits, when some of their days are already frozen.

    Empty string when it does not apply.
    """
    days = snapshotted_days_of(ctx, run, discord_id)
    if not days:
        return ""
    names = ", ".join(day_label(d) for d in days[:3]) + (f" and {len(days) - 3} more" if len(days) > 3 else "")
    first_open = next(iter(open_days(ctx, run)), None)
    applies = f" This change applies from {day_label(first_open['day'])}." if first_open else ""
    return f"Your roles, prog and limits for {names} were saved at their cutoffs.{applies}"


def locked_prog_lines(ctx, run, discord_id, limit=10):
    """For /myschedule: the prog checkpoint locked in for each closed day the player signed up for."""
    cps = ctx.db.checkpoints(run["fight_id"])
    lines = []
    for d in snapshotted_days_of(ctx, run, discord_id):
        snap = ctx.db.snapshot_rows(run["id"], d).get(str(discord_id))
        if snap:
            lines.append(f"• {day_label(d)}: {checkpoint_label(cps, snap['prog_index'])}")
    if len(lines) > limit:
        lines = lines[:limit] + [f"… and {len(lines) - limit} more"]
    return ["Prog locked in at each closed day's cutoff:"] + lines if lines else []
