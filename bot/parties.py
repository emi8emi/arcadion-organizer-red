"""Logic for approved parties: ending them, swapping members, the JSON round trip.

No Discord buttons here (see party_views.py); everything that needs the bot goes through `ctx.get_channel`.
Notices and message edits are best effort: a failure is returned or logged, the database change stays.
"""
import json
import logging
from datetime import timedelta

import discord

from .config import ROLES
from .db import parse_iso, split_roles
from .services import checkpoint_label
from .votes import slot_end

log = logging.getLogger("raidbot")

# What the organizer picks when swapping someone out. The code is what gets analyzed later.
REASONS = {"no_show": "Did not show up", "cant_make_it": "Can't make it", "wrong_role": "Role did not fit",
           "prog_mismatch": "Prog did not fit", "other": "Other (write the reason)"}


def minutes_played(party, now):
    """Minutes between the slot start and `now`, never more than the slot's length."""
    scheduled = party["actual_minutes"] or party["minutes"]
    elapsed = (now - parse_iso(party["start_utc"])).total_seconds() / 60
    return max(0, min(scheduled, round(elapsed)))


def name_of(db, discord_id):
    player = db.get_player(discord_id)
    return player["display_name"] if player else str(discord_id)


def reason_label(code, text):
    label = REASONS.get(code, code or "")
    return f"{label}: {text}" if text and code == "other" else (f"{label}. {text}" if text else label)


# ---------------------------------------------------------------- validation of a swap

def swap_warnings(ctx, party, in_id, in_role):
    """Plain-language problems with putting `in_id` into `party` as `in_role`. Empty means nothing to warn about.

    Warnings never block: an organizer can force the swap.
    """
    db, cfg = ctx.db, ctx.cfg
    in_id, run_id, slot_key = str(in_id), party["run_id"], party["slot_key"]
    out = []
    player = db.get_player(in_id)
    roles = split_roles(player["roles"]) if player else []
    if not roles:
        out.append("They have not signed up (no roles set with /availability).")
    elif in_role not in roles:
        out.append(f"They do not list {in_role} among their roles ({', '.join(roles)}).")
    prog = player["prog_index"] if player else None
    if prog is not None and party["target_idx"] is not None and prog < party["target_idx"]:
        cps = db.checkpoints(db.run(run_id)["fight_id"])
        out.append(f"Their prog ({checkpoint_label(cps, prog)}) is below the party's target "
                   f"({checkpoint_label(cps, party['target_idx'])}).")
    if slot_key not in db.ticked_keys(run_id, in_id):
        out.append("They did not tick this slot.")
    mine = db.player_parties(run_id, in_id)
    for p in mine:
        if p["slot_key"] == slot_key:
            out.append(f"They are already in party {p['party_index'] + 1} of this same slot.")
    lim = db.get_limits(run_id, in_id, cfg)
    today = sum(p["slot_key"][:10] == slot_key[:10] for p in mine)
    if today >= lim["max_per_day"]:
        out.append(f"This would be party {today + 1} for them today (their max is {lim['max_per_day']}).")
    if len(mine) >= lim["max_per_run"]:
        out.append(f"This would be party {len(mine) + 1} of the run for them (their max is {lim['max_per_run']}).")
    return out


def comp_warnings(ctx, party, changes):
    """Warn when the party's role mix would stop matching the configured composition.

    `changes` is {discord_id: new_role} applied to the current members (a swap is the new id with the old removed)."""
    members = {m["discord_id"]: m["role"] for m in ctx.db.party_members(party["id"])}
    for did, role in changes.items():
        if role is None:
            members.pop(did, None)
        else:
            members[did] = role
    want = ctx.cfg["party"]
    have = {r: sum(v == r for v in members.values()) for r in ROLES}
    if all(have[r] == want.get(r, 0) for r in ROLES):
        return []
    return ["The roles would be " + ", ".join(f"{have[r]} {r}" for r in ROLES)
            + " instead of " + ", ".join(f"{want.get(r, 0)} {r}" for r in ROLES) + "."]


# ---------------------------------------------------------------- Discord side effects

async def thread_of(ctx, party):
    if not party["thread_id"] or ctx.get_channel is None:
        return None
    try:
        return await ctx.get_channel(int(party["thread_id"]))
    except Exception:
        log.exception("could not open thread %s", party["thread_id"])
        return None


async def refresh_party_message(ctx, party_id):
    """Rewrite the party's announcement from the database (members, ended line). Returns an error text or ''."""
    from .posting import NO_PING, party_message
    from .party_views import PartyActionsView
    party = ctx.db.party(party_id)
    if party is None or not party["message_id"]:
        return ""
    thread = await thread_of(ctx, party)
    if thread is None:
        return "I could not find the party's thread to update its message."
    run = ctx.db.run(party["run_id"])
    try:
        await thread.get_partial_message(int(party["message_id"])).edit(
            content=party_message(run, party, party, ctx.db.party_members(party_id), ctx.db.checkpoints(run["fight_id"])),
            allowed_mentions=NO_PING, view=PartyActionsView(party_id))
    except Exception as e:
        log.exception("could not edit the message of party %s", party_id)
        return f"I could not update the party message ({e})."
    return ""


async def post_party_rating_prompt(ctx, party_id):
    """Put this party's own "Rate" button into its thread (used when it ends before the slot does)."""
    from .views import RatePromptView
    party = ctx.db.party(party_id)
    thread = await thread_of(ctx, party) if party else None
    if thread is None:
        return False
    try:
        await thread.send(f"Party {party['party_index'] + 1} has ended. Rate your party: press the button. "
                          "Ratings are private; nobody else sees what you pick.", view=RatePromptView([party]))
    except Exception:
        log.exception("could not post the rating prompt of party %s", party_id)
        return False
    ctx.db.mark_party_rating_posted(party_id, ctx.now())
    return True


async def auto_end_parties(ctx):
    """Minute loop: every confirmed party whose slot has run out becomes done (ended at the slot's end)."""
    ended = []
    for row in ctx.db.parties_to_auto_end():
        end = slot_end(row)
        if end <= ctx.now() and ctx.db.end_party(row["id"], end, "auto", row["actual_minutes"] or row["minutes"]):
            ended.append(row["id"])
    for party_id in ended:
        await refresh_party_message(ctx, party_id)
    return ended


async def apply_swap(ctx, guild_id, party_id, out_id, in_id, in_role, actor_id, *, reason_code, reason_text,
                     warnings=None, in_name=None, source="panel"):
    """Do a swap and tell everyone. Returns (change_id, problems), problems being Discord steps that failed."""
    from .posting import NO_PING
    from .party_views import WhySwappedView
    db = ctx.db
    party = db.party(party_id)
    if db.get_player(in_id) is None:                       # anyone in the server may be added
        db.upsert_player(in_id, in_name or str(in_id), now=ctx.now())
    helpers = db.helper_ids(db.run(party["run_id"])["fight_id"])
    change_id = db.swap_member(party_id, out_id, in_id, in_role, actor_id, ctx.now(), reason_code=reason_code,
                               reason_text=reason_text, warnings=warnings, is_helper=str(in_id) in helpers,
                               source=source)
    problems = []
    err = await refresh_party_message(ctx, party_id)
    if err:
        problems.append(err)
    thread = await thread_of(ctx, party)
    n = party["party_index"] + 1
    if thread is None:
        problems.append("I could not find the party's thread, so nobody was added or removed there.")
    else:
        for action, uid in ((thread.remove_user, out_id), (thread.add_user, in_id)):
            try:
                await action(discord.Object(id=int(uid)))
            except Exception as e:
                log.exception("thread member change failed")
                problems.append(f"Could not {'remove' if uid == out_id else 'add'} <@{uid}> "
                                f"{'from' if uid == out_id else 'to'} the thread ({e}). I need Manage Threads.")
        try:                                                # the new player gets a real ping
            await thread.send(f"<@{in_id}> joins party {n} as {in_role}, replacing <@{out_id}>.",
                              allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=int(in_id))]))
        except Exception:
            log.exception("could not announce the swap")
            problems.append(f"Could not ping <@{in_id}> in the thread.")
    channel_id = db.guild_setting(guild_id, "parties_channel_id", ctx.cfg.get("parties_channel_id"))
    channel = await ctx.get_channel(int(channel_id)) if channel_id and ctx.get_channel else None
    if channel is None:
        problems.append("No parties channel found, so <@%s> was not notified. Tell them yourself." % out_id)
    else:
        try:
            await channel.send(
                f"<@{out_id}> you were swapped out of party {n} on "
                f"{discord.utils.format_dt(parse_iso(party['start_utc']), 'F')} and are back in the standby pool "
                "for that slot. Press the button to see why.", view=WhySwappedView(change_id),
                allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=int(out_id))]))
        except Exception as e:
            log.exception("could not post the swap notice")
            problems.append(f"Could not notify <@{out_id}> in the parties channel ({e}).")
    return change_id, problems


# ---------------------------------------------------------------- JSON round trip

def party_json(ctx, party_id):
    """The party as editable JSON, same member shape as the solver's draft, plus the solver's original."""
    db = ctx.db
    party = db.party(party_id)
    run = db.run(party["run_id"])
    cps = db.checkpoints(run["fight_id"])
    member = lambda m: {"player": m["discord_id"], "name": name_of(db, m["discord_id"]),   # noqa: E731
                        "role": m["role"], "helper": bool(m["is_helper"]), "placed_down": bool(m["placed_down"]),
                        "distance": m["distance"]}
    original = db.party_original(party_id)
    return {
        "version": 1, "party_id": party_id, "run": run["name"], "slot": party["slot_key"],
        "party": party["party_index"],
        "target": {"index": party["target_idx"], "name": checkpoint_label(cps, party["target_idx"])},
        "members": [member(m) for m in db.party_members(party_id)],
        "reasons": {},
        "force": False,
        "_help": "Edit members, then /party import. Every removed player needs an entry in reasons, like "
                 '{"123": {"code": "no_show", "text": ""}} (codes: ' + ", ".join(REASONS) + "). Set force to true "
                 "to accept validation warnings. Do not change party_id.",
        "original": None if original is None else {
            "target_index": original["target_idx"],
            "members": [{"player": m["discord_id"], "role": m["role"], "helper": bool(m["is_helper"]),
                         "placed_down": bool(m["placed_down"]), "distance": m["distance"]}
                        for m in original["members"]]},
    }


def plan_import(ctx, data):
    """Turn an edited party JSON into (party_id, ops, warnings, errors).

    ops: {"kind": "swap", out, in, role, code, text} or {"kind": "role", player, role}. Removed and added players
    are paired in file order, so their numbers must match. Warnings are the same validation a swap in the panel gets.
    """
    errors, warnings, ops = [], [], []
    try:
        party_id = int(data["party_id"])
        new = [(str(m["player"]), m["role"]) for m in data["members"]]
    except (KeyError, TypeError, ValueError):
        return None, [], [], ["The file is not a party export (needs party_id and members with player and role)."]
    party = ctx.db.party(party_id)
    if party is None or party["status"] not in ("confirmed", "done"):
        return party_id, [], [], ["That party is not approved, so it cannot be edited."]
    if any(role not in ROLES for _, role in new) or len({p for p, _ in new}) != len(new):
        return party_id, [], [], ["Every member needs a unique player and a role of tank, healer or dps."]
    old = {m["discord_id"]: m["role"] for m in ctx.db.party_members(party_id)}
    removed = [p for p in old if p not in dict(new)]
    added = [(p, r) for p, r in new if p not in old]
    if len(removed) != len(added):
        return party_id, [], [], [f"{len(removed)} player(s) removed but {len(added)} added: the party size must stay the same."]
    reasons = data.get("reasons") or {}
    for out_id, (in_id, role) in zip(removed, added):
        r = reasons.get(out_id) or {}
        code, text = r.get("code"), str(r.get("text") or "")[:300]
        if code not in REASONS or (code == "other" and not text):
            errors.append(f"{name_of(ctx.db, out_id)} ({out_id}) was removed: add reasons[\"{out_id}\"] with a code "
                          f"({', '.join(REASONS)}) and, for other, a text.")
        ops.append({"kind": "swap", "out": out_id, "in": in_id, "role": role, "code": code, "text": text})
        warnings += [f"{name_of(ctx.db, in_id)}: {w}" for w in swap_warnings(ctx, party, in_id, role)]
    for did, role in new:
        if did in old and old[did] != role:
            ops.append({"kind": "role", "player": did, "role": role})
    changes = {o["out"]: None for o in ops if o["kind"] == "swap"}
    changes.update({o["in"]: o["role"] for o in ops if o["kind"] == "swap"})
    changes.update({o["player"]: o["role"] for o in ops if o["kind"] == "role"})
    warnings += comp_warnings(ctx, party, changes)
    return party_id, ops, warnings, errors


def dumps(data):
    return json.dumps(data, indent=2, ensure_ascii=False).encode()
