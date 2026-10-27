"""Export reminders: after a day's signup cutoff, nag organizers until someone runs /draft export.

The bot does not solve anything by itself. `due_reminders` decides what to say and is pure (it only
reads the database), so tests can drive it with a fake clock. `send_due_reminders` is the thin part
that talks to Discord.
"""
import logging
from datetime import timedelta

import discord

from . import heatmap
from .db import parse_iso
from .services import day_label

log = logging.getLogger("raidbot")


def due_reminders(db, now, cfg):
    """Reminders to send right now, as a list of dicts (one per run day).

    A day is due when its cutoff has passed, it has not been exported, and its first slot has not started.
    The first reminder goes out at once; after that one every `reminder_interval_minutes`
    (tracked by last_reminded_utc in the database, so a restart neither repeats nor forgets).
    A day with nobody ready to play gets one notice and no repeats.
    """
    interval = timedelta(minutes=cfg["reminder_interval_minutes"])
    due = []
    for day in db.reminder_candidates(now):
        first_slot = db.first_slot_start(day["run_id"], day["day"])
        if first_slot is None or now >= first_slot:
            continue
        last = parse_iso(day["last_reminded_utc"]) if day["last_reminded_utc"] else None
        run = db.run(day["run_id"])
        ready, incomplete, fillable = heatmap.day_summary(db, run, cfg, day["day"])
        if last is not None and (ready == 0 or now - last < interval):
            continue
        due.append({"run_id": day["run_id"], "run_name": day["run_name"], "day": day["day"],
                    "ready": ready, "incomplete": incomplete, "fillable": fillable})
    return due


def format_reminder(item, cfg):
    """The message text for one due reminder."""
    role_id = cfg.get("organizer_role_id")
    ping = f"<@&{role_id}> " if role_id else ""
    where = f"**{item['run_name']}**, {day_label(item['day'])}"
    if item["ready"] == 0:
        extra = f" ({item['incomplete']} ticked slots but never set roles or prog)" if item["incomplete"] else ""
        return f"{ping}Signups for {where} closed with nobody ready to play{extra}. Nothing to export."
    extra = f" ({item['incomplete']} more still need roles or prog)" if item["incomplete"] else ""
    return (f"{ping}Signups for {where} closed: {item['ready']} signups{extra}, "
            f"{item['fillable']} slots that could fill. Run `/draft export day:{item['day']}`.")


async def send_due_reminders(ctx, get_channel):
    """Send every due reminder to the configured channel. `get_channel` is an async callable returning it.

    A reminder is marked as sent only after Discord accepted it, so a failure is retried next minute.
    Returns the number sent.
    """
    channel_id = ctx.cfg.get("reminder_channel_id")
    if not channel_id:
        return 0
    items = due_reminders(ctx.db, ctx.now(), ctx.cfg)
    if not items:
        return 0
    channel = await get_channel(int(channel_id))
    if channel is None:
        log.warning("reminder channel %s not found; check reminder_channel_id", channel_id)
        return 0
    sent = 0
    for item in items:
        mentions = discord.AllowedMentions(roles=True, users=False, everyone=False)
        try:
            await channel.send(format_reminder(item, ctx.cfg), allowed_mentions=mentions)
        except Exception:                                   # missing permission, rate limit, outage
            log.exception("could not send the export reminder for %s %s", item["run_name"], item["day"])
            continue
        ctx.db.mark_reminded(item["run_id"], item["day"], ctx.now())
        sent += 1
    return sent
