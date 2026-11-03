"""Player confirmations: "are you coming?" for draft and approved parties.

Before approval the organizer runs /draft confirm and the bot posts one message per slot in the parties channel
pinging that slot's members. After approval the same kind of message goes into the slot's thread for whoever has not
confirmed, and again shortly before the slot. Each message has two buttons (see confirm_views.py): Confirm, and
Can't make it, which asks for a reason (default: unavailable at that slot) and unticks the slot or day so the next
export, solver run and /draft import can fill the party.

Rows are per (slot, player), not per party, so a new draft keeps the confirmations of everyone still seated in the
same slot. Messages are best effort: a Discord failure is logged or returned, the database change stays.
"""
import logging
from datetime import timedelta

import discord

from .config import ROLES
from .db import iso, parse_iso
from .votes import slot_end

log = logging.getLogger("raidbot")

CANCEL_REASONS = {"slot": "Unavailable at this slot", "day": "Unavailable the whole day",
                  "role": "Don't want this role or party", "other": "Other (write the reason)"}
DEFAULT_CANCEL = "slot"
MARKS = {"confirmed": "✅", "pending": "⏳", "declined": "❌", None: "▫️"}


def cancel_label(code, text=None):
    label = CANCEL_REASONS.get(code, code or "")
    return f"{label}: {text}" if text else label


# ---------------------------------------------------------------- database

def get_slot(db, slot_id):
    return db.conn.execute("SELECT * FROM slots WHERE id = ?", (slot_id,)).fetchone()


def slot_parties(db, slot_id, statuses=("draft", "confirmed", "done")):
    """[(party row, [member rows])] of the slot, in party order."""
    marks = ",".join("?" for _ in statuses)
    rows = db.conn.execute(f"SELECT * FROM parties WHERE slot_id = ? AND status IN ({marks}) ORDER BY party_index",
                           (slot_id, *statuses)).fetchall()
    return [(p, db.party_members(p["id"])) for p in rows]


def member_ids(db, slot_id, statuses=("draft", "confirmed", "done")):
    return [m["discord_id"] for _, ms in slot_parties(db, slot_id, statuses) for m in ms]


def statuses(db, slot_id):
    """{discord_id: confirmations row} for the slot."""
    return {r["discord_id"]: r for r in db.conn.execute(
        "SELECT * FROM confirmations WHERE slot_id = ?", (slot_id,))}


def request(db, run_id, slot_id, ids, now):
    """Make sure every id has a row. Missing rows become pending; confirmed and declined ones are kept.
    Returns the ids whose status is pending afterwards (the ones worth pinging)."""
    have = statuses(db, slot_id)
    with db.conn:
        for did in ids:
            if did not in have:
                db.conn.execute("INSERT INTO confirmations (run_id, slot_id, discord_id, status, requested_utc) "
                                "VALUES (?, ?, ?, 'pending', ?)", (run_id, slot_id, did, iso(now)))
    after = statuses(db, slot_id)
    return [d for d in ids if after[d]["status"] == "pending"]


def answer(db, run_id, slot_id, discord_id, status, now, code=None, text=None):
    with db.conn:
        cur = db.conn.execute(
            """UPDATE confirmations SET status = ?, answered_utc = ?, reason_code = ?, reason_text = ?
               WHERE run_id = ? AND slot_id = ? AND discord_id = ?""",
            (status, iso(now), code, text, run_id, slot_id, discord_id))
        if cur.rowcount == 0:
            db.conn.execute(
                """INSERT INTO confirmations (run_id, slot_id, discord_id, status, requested_utc, answered_utc,
                                              reason_code, reason_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, slot_id, discord_id, status, iso(now), iso(now), code, text))


def after_import(db, run_id, day):
    """A new draft was stored for `day`: forget declines (the solver has had its say) and the rows of players who
    are no longer seated in that slot. Confirmations of players still seated in the same slot are kept."""
    for slot in db.slots_on(run_id, day):
        seated = set(member_ids(db, slot["id"]))
        with db.conn:
            for did, row in statuses(db, slot["id"]).items():
                if row["status"] == "declined" or did not in seated:
                    db.conn.execute("DELETE FROM confirmations WHERE slot_id = ? AND discord_id = ?",
                                    (slot["id"], did))


def prompt_rows(db, slot_id):
    return {r["kind"]: r for r in db.conn.execute("SELECT * FROM confirm_prompts WHERE slot_id = ?", (slot_id,))}


def save_prompt(db, slot_id, kind, channel_id, message_id, now, reminded=False):
    old = prompt_rows(db, slot_id).get(kind)
    reminded_utc = iso(now) if reminded else (old["reminded_utc"] if old else None)
    with db.conn:
        db.conn.execute("INSERT OR REPLACE INTO confirm_prompts (slot_id, kind, channel_id, message_id, posted_utc, "
                        "reminded_utc) VALUES (?, ?, ?, ?, ?, ?)",
                        (slot_id, kind, str(channel_id), str(message_id), iso(now), reminded_utc))


def counts(db, slot_id, ids):
    """{'confirmed': n, 'pending': n, 'declined': n, 'unasked': n} over `ids`."""
    have = statuses(db, slot_id)
    out = {"confirmed": 0, "pending": 0, "declined": 0, "unasked": 0}
    for did in ids:
        out[have[did]["status"] if did in have else "unasked"] += 1
    return out


# ---------------------------------------------------------------- declining

def apply_decline(db, run, slot, discord_id, code, text, now):
    """Record the decline and change the signup so the next solver run can fill the seat.

    slot / other: untick this slot. day: untick every slot of that day. role: ticks are left alone.
    Returns a sentence for the player saying what changed."""
    if code not in CANCEL_REASONS:
        raise ValueError(f"unknown reason {code!r}")
    text = (text or "").strip()[:300] or None
    answer(db, run["id"], slot["id"], discord_id, "declined", now, code, text)
    day = slot["slot_key"][:10]
    keys = [k for k in db.ticked_keys(run["id"], discord_id) if k[:10] == day]
    if code in ("slot", "other"):
        db.set_day_availability(run["id"], discord_id, day, [k for k in keys if k != slot["slot_key"]])
        return "I removed this slot from your signup."
    if code == "day":
        db.set_day_availability(run["id"], discord_id, day, [])
        return "I removed every slot of that day from your signup."
    return "Your signup is unchanged; I told the organizers."


# ---------------------------------------------------------------- the messages

def _no_ping(ids=()):
    return discord.AllowedMentions(users=[discord.Object(id=int(i)) for i in ids], roles=False, everyone=False)


def prompt_text(ctx, run, slot, ask=None, reminder=False):
    """The text of a confirmation message: every member with a mark. `ask` is the headline sentence."""
    db = ctx.db
    start = parse_iso(slot["start_utc"])
    parties = slot_parties(db, slot["id"])
    approved = any(p["status"] != "draft" for p, _ in parties)
    have = statuses(db, slot["id"])
    when = f"{discord.utils.format_dt(start, 'F')} ({discord.utils.format_dt(start, 'R')})"
    if ask is None:
        if reminder:
            ask = "The slot starts soon. Please confirm if you have not yet:"
        elif approved:
            ask = "Your party is set. Please confirm that you will be there:"
        else:
            ask = "Draft parties are ready. Are you in? Please confirm:"
    lines = [f"**{run['name']}** · {when}", ask]
    order = {r: i for i, r in enumerate(ROLES)}
    for party, members in parties:
        lines.append(f"**Party {party['party_index'] + 1}**")
        for m in sorted(members, key=lambda m: (order[m["role"]], m["discord_id"])):
            row = have.get(m["discord_id"])
            note = f" · {cancel_label(row['reason_code'], row['reason_text'])}" if row and row["status"] == "declined" else ""
            lines.append(f"{MARKS[row['status'] if row else None]} <@{m['discord_id']}> {m['role']}{note}")
    lines.append("✅ confirmed · ⏳ waiting · ❌ can't make it" + (" · ▫️ not asked yet" if not approved else ""))
    return "\n".join(lines)[:1990]


async def _channel(ctx, channel_id):
    if ctx.get_channel is None or not channel_id:
        return None
    try:
        return await ctx.get_channel(int(channel_id))
    except Exception:
        log.exception("could not open channel %s", channel_id)
        return None


async def _forget(ctx, row):
    """Delete an old prompt message (best effort) so a slot has one live prompt."""
    chan = await _channel(ctx, row["channel_id"])
    if chan is None:
        return
    try:
        await chan.get_partial_message(int(row["message_id"])).delete()
    except Exception:
        log.info("could not delete the old confirmation message %s", row["message_id"])


async def refresh_prompts(ctx, slot_id):
    """Rewrite the live prompt messages of the slot from the database. Returns a list of problems (usually empty)."""
    from .confirm_views import ConfirmView
    slot = get_slot(ctx.db, slot_id)
    if slot is None:
        return []
    run = ctx.db.run(slot["run_id"])
    problems = []
    for kind, row in prompt_rows(ctx.db, slot_id).items():
        chan = await _channel(ctx, row["channel_id"])
        if chan is None:
            problems.append(f"I could not find the {kind} confirmation message to update.")
            continue
        try:
            await chan.get_partial_message(int(row["message_id"])).edit(
                content=prompt_text(ctx, run, slot), view=ConfirmView(slot_id), allowed_mentions=_no_ping())
        except Exception as e:
            log.exception("could not edit the confirmation message of slot %s", slot_id)
            problems.append(f"Could not update the {kind} confirmation message ({e}).")
    return problems


async def _post_prompt(ctx, run, slot, kind, destination, ping, *, reminder=False, ask=None):
    """Send a fresh prompt (pinging `ping`), drop the old one and remember the new one. Raises on Discord errors."""
    from .confirm_views import ConfirmView
    old = prompt_rows(ctx.db, slot["id"]).get(kind)
    msg = await destination.send(prompt_text(ctx, run, slot, ask=ask, reminder=reminder),
                                 allowed_mentions=_no_ping(ping), view=ConfirmView(slot["id"]))
    if old:
        await _forget(ctx, old)
    save_prompt(ctx.db, slot["id"], kind, destination.id, msg.id, ctx.now(), reminded=reminder)
    return msg


async def post_draft_prompts(ctx, run, day, channel):
    """/draft confirm: one message per slot of the day with draft parties, pinging whoever has not answered.
    Returns (slots asked, players pinged, errors)."""
    asked, pinged, errors = 0, 0, []
    for slot in ctx.db.slots_on(run["id"], day):
        ids = member_ids(ctx.db, slot["id"], ("draft",))
        if not ids:
            continue
        ping = request(ctx.db, run["id"], slot["id"], ids, ctx.now())
        try:
            if ping or not prompt_rows(ctx.db, slot["id"]).get("draft"):
                await _post_prompt(ctx, run, slot, "draft", channel, ping)
                asked += 1
                pinged += len(ping)
            else:
                errors += await refresh_prompts(ctx, slot["id"])
        except Exception as e:
            log.exception("could not post the confirmation request for slot %s", slot["slot_key"])
            errors.append(f"{slot['slot_key']}: {e}")
    return asked, pinged, errors


async def thread_prompt(ctx, slot_id, *, ping=None, reminder=False):
    """Approved parties: ask the members who have not confirmed, in the slot's thread. `ping` overrides who is
    pinged. Returns an error text or ''. Does nothing when everyone has confirmed (a live prompt is refreshed)."""
    db = ctx.db
    slot = get_slot(db, slot_id)
    if slot is None or not slot["thread_id"]:
        return ""
    run = db.run(slot["run_id"])
    ids = member_ids(db, slot_id, ("confirmed", "done"))
    pending = request(db, run["id"], slot_id, ids, ctx.now())
    ping = pending if ping is None else ping
    if not ping:
        return "; ".join(await refresh_prompts(ctx, slot_id))
    thread = await _channel(ctx, slot["thread_id"])
    if thread is None:
        return "I could not find the slot's thread to ask for confirmations."
    try:
        await _post_prompt(ctx, run, slot, "thread", thread, ping, reminder=reminder)
    except Exception as e:
        log.exception("could not post the confirmation request in thread %s", slot["thread_id"])
        return f"Could not ask for confirmations in the thread ({e})."
    return ""


async def send_confirmation_reminders(ctx):
    """Minute loop: approved slots starting within `confirm_reminder_hours` get one reminder in their thread
    for the players who still have not confirmed. Returns the number of reminders sent."""
    now, sent = ctx.now(), 0
    window = timedelta(hours=ctx.cfg.get("confirm_reminder_hours", 3))
    rows = ctx.db.conn.execute(
        """SELECT DISTINCT s.id FROM slots s JOIN parties p ON p.slot_id = s.id
           WHERE p.status = 'confirmed' AND s.thread_id IS NOT NULL""").fetchall()
    for r in rows:
        slot = get_slot(ctx.db, r["id"])
        start = parse_iso(slot["start_utc"])
        if not (now < start <= now + window):
            continue
        prompt = prompt_rows(ctx.db, slot["id"]).get("thread")
        if prompt and prompt["reminded_utc"]:
            continue
        if not [d for d in member_ids(ctx.db, slot["id"], ("confirmed",))
                if (statuses(ctx.db, slot["id"]).get(d) or {"status": "pending"})["status"] == "pending"]:
            continue
        if not await thread_prompt(ctx, slot["id"], reminder=True):
            sent += 1
    return sent


async def notify_decline(ctx, channel, run, slot, discord_id, code, text, approved):
    """Tell the organizers (a role ping if one is set) that someone declined. Best effort."""
    if channel is None:
        return
    role_id = ctx.db.guild_setting(run["guild_id"], "organizer_role_id", ctx.cfg.get("organizer_role_id"))
    ping = f"<@&{role_id}> " if role_id else ""
    what = ("Organizers: swap them out with **Edit composition** on their party."
            if approved else "Re-export, run the solver and /draft import to refill the party.")
    allowed = discord.AllowedMentions(users=False, everyone=False,
                                      roles=[discord.Object(id=int(role_id))] if role_id else False)
    try:
        await channel.send(f"{ping}<@{discord_id}> can't make it: {cancel_label(code, text)}. {what}",
                           allowed_mentions=allowed)
    except Exception:
        log.exception("could not tell the organizers about a decline")


def is_over(slot, now):
    return slot_end(slot) <= now


async def nudge_slot(ctx, slot_id):
    """Ping the members of a slot who have not answered. Approved parties: in the slot's thread. Draft parties:
    in the parties channel. Returns a sentence for the organizer."""
    db = ctx.db
    slot = get_slot(db, slot_id)
    run = db.run(slot["run_id"])
    parties = slot_parties(db, slot_id)
    if not parties:
        return "That slot has no parties."
    if any(p["status"] != "draft" for p, _ in parties):
        err = await thread_prompt(ctx, slot_id)
        return err or "Asked the unconfirmed players in the slot's thread (nobody is pinged when all have confirmed)."
    channel_id = db.guild_setting(run["guild_id"], "parties_channel_id", ctx.cfg.get("parties_channel_id"))
    channel = await _channel(ctx, channel_id)
    if channel is None:
        return "No parties channel is set or I could not find it (/settings set)."
    ping = request(db, run["id"], slot_id, member_ids(db, slot_id, ("draft",)), ctx.now())
    if not ping:
        return "Nobody is waiting: everyone has answered."
    try:
        await _post_prompt(ctx, run, slot, "draft", channel, ping)
    except Exception as e:
        log.exception("could not nudge slot %s", slot_id)
        return f"Could not post in the parties channel ({e})."
    return f"Pinged {len(ping)} player(s) in the parties channel."
