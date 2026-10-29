"""Posting approved parties to Discord and the "Rate your party" prompts. Thin layer over the database.

`ctx.get_channel` is an async callable (channel id -> channel or thread) that the bot sets. Tests pass fakes.
"""
import logging

import discord

from .config import ROLES
from .db import parse_iso
from .services import checkpoint_label, day_label
from .votes import slot_end, window_closes
from .views import RatePromptView

log = logging.getLogger("raidbot")

NO_PING = discord.AllowedMentions(users=True, roles=False, everyone=False)


def party_message(run, slot, party, members, checkpoints):
    """The announcement for one party: members as mentions with roles, target, helper and placed-down marks."""
    start = parse_iso(slot["start_utc"])
    lines = [f"**Party {party['party_index'] + 1}** · {discord.utils.format_dt(start, 'F')} "
             f"({discord.utils.format_dt(start, 'R')})",
             f"Target: {checkpoint_label(checkpoints, party['target_idx'])}"]
    order = {r: i for i, r in enumerate(ROLES)}
    for m in sorted(members, key=lambda m: (order[m["role"]], m["discord_id"])):
        marks = (" · helper" if m["is_helper"] else "") + (" · placed below their prog" if m["placed_down"] else "")
        lines.append(f"• <@{m['discord_id']}> {m['role']}{marks}")
    return "\n".join(lines)


async def post_day_parties(ctx, run, day, channel):
    """Create one thread per slot of the day and post one message per party. Returns (parties posted, errors).

    Safe to run again after a failure: a slot that has a thread reuses it and a party that was announced
    is skipped.
    """
    checkpoints = ctx.db.checkpoints(run["fight_id"])
    posted, errors = 0, []
    for slot, parties in ctx.db.approved_slots(run["id"], day):
        todo = [(p, m) for p, m in parties if not p["message_id"]]
        if not todo:
            continue
        try:
            if slot["thread_id"]:
                thread = await ctx.get_channel(int(slot["thread_id"]))
            else:
                name = f"{run['name']} · {day_label(day)} {slot['slot_key'][11:16]}"[:100]
                thread = await channel.create_thread(name=name, type=discord.ChannelType.public_thread,
                                                     auto_archive_duration=10080)
                ctx.db.set_slot_thread(slot["id"], thread.id)
            if thread is None:
                raise RuntimeError("thread not found")
        except Exception as e:
            log.exception("could not create the thread for slot %s", slot["slot_key"])
            errors.append(f"{slot['slot_key']}: {e}")
            continue
        for party, members in todo:
            try:
                msg = await thread.send(party_message(run, slot, party, members, checkpoints),
                                        allowed_mentions=NO_PING)
            except Exception as e:
                log.exception("could not post party %s of %s", party["party_index"], slot["slot_key"])
                errors.append(f"{slot['slot_key']} party {party['party_index'] + 1}: {e}")
                continue
            ctx.db.set_party_message(party["id"], msg.id)
            posted += 1
    return posted, errors


async def post_rating_prompts(ctx):
    """For every slot that has ended (and whose window is open) post the rate buttons once in its thread.

    Marked as posted only after Discord accepted the message, so a failure is retried next minute.
    Nothing about votes goes into the message. Returns the number of prompts posted.
    """
    now = ctx.now()
    sent = 0
    for slot in ctx.db.slots_awaiting_rating():
        if not (slot_end(slot) <= now <= window_closes(slot, ctx.cfg)) or ctx.get_channel is None:
            continue
        parties = ctx.db.approved_parties_of_slot(slot["id"])
        try:
            thread = await ctx.get_channel(int(slot["thread_id"]))
            if thread is None:
                log.warning("thread %s for slot %s not found", slot["thread_id"], slot["slot_key"])
                continue
            await thread.send("This slot has ended. Rate your party: press your party's button. "
                              "Ratings are private; nobody else sees what you pick.",
                              view=RatePromptView(parties))
        except Exception:
            log.exception("could not post the rating prompt for %s", slot["slot_key"])
            continue
        ctx.db.mark_rating_posted(slot["id"], now)
        sent += 1
    return sent
