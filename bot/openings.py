"""Opening an event for signups: the checks, the announcement, and opening on schedule (`announce.open_at`).

`open_run` is the one place a draft becomes open; /run open and the scheduled opening both call it.
"""
import logging

import discord

from . import events
from .services import cutoff_of, day_label, find_channel, open_days, server_setting

log = logging.getLogger("raidbot")


def announcement_text(ctx, run, rows):
    """The message posted when an event opens: optional role ping, the organizer's text, the cutoff per day."""
    spec = events.spec_from_run(ctx.db, run, ctx.cfg, ctx.grid)
    a = spec.get("announce") or {}
    ping = f"<@&{a['ping_role_id']}> " if a.get("ping_role_id") else ""
    lines = [f"{ping}**{run['name']}** is open for signups!"]
    if a.get("message"):
        lines.append(a["message"])
    lines.append("Sign up with /availability. Signups close:")
    lines += [f"• {day_label(r['day'])}: {discord.utils.format_dt(cutoff_of(r), 'F')} "
              f"({discord.utils.format_dt(cutoff_of(r), 'R')})" for r in rows]
    return "\n".join(lines)[:2000]


async def announce(ctx, run, rows):
    """Post the announcement if the event has an announce channel. Returns a line about it ('' when none)."""
    spec = events.spec_from_run(ctx.db, run, ctx.cfg, ctx.grid)
    channel_id = (spec.get("announce") or {}).get("channel_id")
    if not channel_id or run["announce_message_id"]:
        return ""
    try:
        channel = await ctx.get_channel(int(channel_id))
        message = await channel.send(announcement_text(ctx, run, rows),
                                     allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False))
    except Exception:
        log.warning("could not post the announcement of %s in channel %s", run["name"], channel_id, exc_info=True)
        return (f"\n⚠ I could not post the announcement in <#{channel_id}>. I need View Channel and Send Messages "
                "there (and Mention Roles to ping).")
    ctx.db.set_announce_message(run["id"], message.id)
    return f"\nAnnounced in <#{channel_id}>."


async def open_run(ctx, guild_id, run):
    """Open a draft run. Returns (ok, text); the text is the reply for an organizer."""
    if run["status"] == "open":
        return False, f"**{run['name']}** is already open."
    other = ctx.db.open_run(guild_id)
    stale = ""
    if other:
        if open_days(ctx, other):
            return False, f"**{other['name']}** is still open. Close it first with /run close."
        ctx.db.set_run_status(other["id"], "closed")           # every day of it is past its cutoff anyway
        stale = f" (I closed **{other['name']}**: all its days were already past their cutoffs.)"
    rows = ctx.db.run_days(run["id"])
    now = ctx.now()
    still_open = [r for r in rows if now < cutoff_of(r)]
    if not still_open:
        return False, ("Every day of this run is already past its cutoff, so nobody could sign up. "
                       "Move a day with /run extend first.")
    ctx.db.set_run_status(run["id"], "open")
    ctx.db.clear_open_at(run["id"])
    closed = len(rows) - len(still_open)
    text = (f"**{run['name']}** is open. Players sign up with /availability; "
            f"{len(still_open)} of {len(rows)} days take signups"
            + (f" ({closed} already closed)" if closed else "")
            + f", the next cutoff is {day_label(still_open[0]['day'])}, "
              f"{discord.utils.format_dt(cutoff_of(still_open[0]), 'F')}." + stale)
    return True, text + await announce(ctx, run, rows)


async def open_scheduled(ctx):
    """Open every draft whose `announce.open_at` has passed. A draft that cannot open (another event is still
    open, every day is past its cutoff) is left as a draft, `open_at` is cleared so it is not retried every
    minute, and the reminder channel is told. Returns the number opened."""
    opened = 0
    for run in ctx.db.due_scheduled_opens(ctx.now()):
        ok, text = await open_run(ctx, run["guild_id"], run)
        ctx.db.clear_open_at(run["id"])
        if ok:
            opened += 1
            continue
        channel_id = server_setting(ctx, run["guild_id"], "reminder_channel_id")
        log.warning("could not open %s on schedule: %s", run["name"], text)
        if not channel_id:
            continue
        try:
            channel = await ctx.get_channel(int(channel_id))
            await channel.send(f"⚠ I could not open **{run['name']}** at its scheduled time: {text} "
                               "It is still a draft; fix that and run /run open.")
        except Exception:
            log.warning("could not tell the reminder channel about %s", run["name"], exc_info=True)
    return opened
