"""Shared pieces for command handlers and the signup views."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable
from zoneinfo import ZoneInfo

import discord

from .db import DB, parse_iso, split_roles
from .grid import GridConfig

MAX_PER_DAY = 8
MAX_PER_RUN = 40


@dataclass
class Ctx:
    db: DB
    cfg: dict
    grid: GridConfig
    now: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc))


def signup_open(ctx, run):
    """(True, '') when players may change their signup, else (False, reason)."""
    if run["status"] != "open":
        return False, "Signups for this run are not open."
    deadline = parse_iso(run["signup_deadline_utc"])
    if ctx.now() >= deadline:
        return False, f"Signups closed {discord.utils.format_dt(deadline, 'F')}."
    return True, ""


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
            if lim["drop_checkpoints"] else "not OK with being placed below your prog (party finder is the alternative)")
    return f"Limits: up to {lim['max_per_day']} per day, {lim['max_per_run']} per run; {drop}."


def profile_text(ctx, run, discord_id):
    """Roles, prog and limits for one player, as short lines."""
    player = ctx.db.get_player(discord_id)
    cps = ctx.db.checkpoints(run["fight_id"])
    roles = split_roles(player["roles"]) if player else []
    prog = player["prog_index"] if player else None
    lines = [f"Roles: {', '.join(roles) if roles else 'not set'} · Prog: {checkpoint_label(cps, prog)}",
             limits_text(ctx.db.get_limits(run["id"], discord_id, ctx.cfg))]
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
