"""Editing the composition of approved parties: one operations core for every front end.

An edit is a list of operations on the parties of ONE slot (they share a thread):

    {"op": "swap",      "party": id, "out": discord_id, "in": discord_id, "role": r?, "reason": {"code", "text"}}
    {"op": "role",      "party": id, "player": discord_id, "role": r}
    {"op": "move",      "player": discord_id, "to": id, "role": r?}        # to another party of the same slot
    {"op": "trade",     "a": discord_id, "b": discord_id}                  # two players swap parties, keeping roles
    {"op": "remove",    "party": id, "player": discord_id, "reason": {...}}  # the party is left short
    {"op": "add",       "party": id, "player": discord_id, "role": r}      # into a short party
    {"op": "pf_add",    "party": id, "role": r, "name": text?}             # a seat filled from Party Finder
    {"op": "pf_remove", "party": id, "pf": index}                          # index into that party's PF seats

`plan` checks them against the current parties without touching anything and returns the resulting state, the
changes to log, warnings (never block: an organizer can force) and errors (always block). `apply` plans again,
writes everything in one transaction (`db.apply_slot_edit`) and then does the Discord side: party messages, thread
members, ONE notice per affected player. The Discord panel (party_views.py) stages operations and calls these;
`/party import` and the old single swap call them too, so the rules live here once.
"""
import copy
import logging
from dataclasses import dataclass, field

import discord

from .config import ROLES, role_label
from .parties import REASONS, character_text, comp_text, name_of, refresh_party_message, swap_warnings, thread_of
from .services import find_channel, server_setting
from .db import parse_iso

log = logging.getLogger("raidbot")

MAX_PF_NAME = 40


def party_size(ctx):
    return sum(ctx.cfg["party"].values())


@dataclass
class Plan:
    slot_id: int
    before: dict                      # party_id -> state, as loaded
    after: dict                       # party_id -> state, after the operations
    changes: list = field(default_factory=list)      # rows for party_changes
    warnings: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    leaving: dict = field(default_factory=dict)      # discord_id -> {"party": id, "change": index}: out of the slot
    entering: dict = field(default_factory=dict)     # discord_id -> {"party": id, "role": r}: new to the slot
    lines: list = field(default_factory=list)        # what happened, per operation, for the organizer and the thread

    @property
    def changed(self):
        return [pid for pid in self.after if self.after[pid] != self.before[pid]]


def load(ctx, slot_id):
    """{party_id: {"index", "members": {id: {"role", "is_helper"}}, "pf": [{"id", "role", "name"}]}} for the
    slot's approved parties."""
    db = ctx.db
    return {p["id"]: {
        "index": p["party_index"],
        "members": {m["discord_id"]: {"role": m["role"], "is_helper": bool(m["is_helper"])}
                    for m in db.party_members(p["id"])},
        "pf": [{"id": r["id"], "role": r["role"], "name": r["name"]} for r in db.party_pf(p["id"])]}
        for p in db.active_slot_parties(slot_id)}


def seats(state):
    return len(state["members"]) + len(state["pf"])


def roles_of(state):
    return [m["role"] for m in state["members"].values()] + [x["role"] for x in state["pf"]]


def short_by(ctx, state):
    return max(0, party_size(ctx) - seats(state))


def _name(ctx, did):
    return name_of(ctx.db, did)


def _reason(op, errors, who):
    r = op.get("reason") or {}
    code, text = r.get("code"), str(r.get("text") or "")[:300]
    if code not in REASONS or (code == "other" and not text):
        errors.append(f"{who}: pick why they are leaving ({', '.join(REASONS.values())}); 'Other' needs a written reason.")
    return code, text


def _find(after, did):
    for pid, st in after.items():
        if did in st["members"]:
            return pid
    return None


def plan(ctx, slot_id, ops, *, is_helper=None):
    """Check `ops` against the slot's current parties. Never writes. See the module docstring for the operations."""
    db = ctx.db
    before = load(ctx, slot_id)
    after = copy.deepcopy(before)
    out = Plan(slot_id, before, after)
    err = out.errors.append
    size = party_size(ctx)
    run = None
    helpers = set()
    rows = {p["id"]: db.party(p["id"]) for p in db.active_slot_parties(slot_id)}
    if rows:
        run = db.run(next(iter(rows.values()))["run_id"])
        helpers = set(db.helper_ids(run["fight_id"]))
    label = lambda pid: f"party {after[pid]['index'] + 1}"                                           # noqa: E731

    def party_ok(pid, what):
        if pid not in after:
            err(f"{what}: that party is not an approved party of this slot.")
            return False
        return True

    def warn(prefix, items):
        out.warnings.extend(f"{prefix}: {w}" for w in items)

    def role_of(op, default):
        role = op.get("role") or default
        if role not in ROLES:
            err(f"Unknown role '{role}'. Use {', '.join(ROLES)}.")
            return None
        return role

    for n, op in enumerate(ops, 1):
        kind = op.get("op")
        where = f"Step {n} ({kind})"
        if kind == "swap":
            pid, o, i = op.get("party"), str(op.get("out")), str(op.get("in"))
            if not party_ok(pid, where):
                continue
            st = after[pid]
            if o not in st["members"]:
                err(f"{where}: {_name(ctx, o)} is not in {label(pid)}.")
                continue
            if _find(after, i) is not None:
                err(f"{where}: {_name(ctx, i)} is already in a party of this slot; use move or trade.")
                continue
            role = role_of(op, st["members"][o]["role"])
            code, text = _reason(op, out.errors, f"{where}")
            if role is None:
                continue
            old_role = st["members"].pop(o)["role"]
            st["members"][i] = {"role": role, "is_helper": i in helpers}
            out.changes.append(dict(party_id=pid, kind="swap", player_id=o, other_player_id=i, reason_code=code,
                                    reason_text=text, detail={"out_role": old_role, "in_role": role,
                                                              "source": op.get("source", "panel")}))
            out.leaving[o] = {"party": pid, "change": len(out.changes) - 1}
            out.entering[i] = {"party": pid, "role": role, "replacing": o}
            warn(_name(ctx, i), swap_warnings(ctx, rows[pid], i, role))
            out.lines.append(f"✅ <@{o}> → <@{i}> as {role_label(role)} ({label(pid)})")
        elif kind == "role":
            pid, did = op.get("party"), str(op.get("player"))
            if not party_ok(pid, where):
                continue
            st = after[pid]
            role = role_of(op, None)
            if did not in st["members"] or role is None:
                if role is not None:
                    err(f"{where}: {_name(ctx, did)} is not in {label(pid)}.")
                continue
            old_role = st["members"][did]["role"]
            if old_role != role:
                st["members"][did]["role"] = role
                out.changes.append(dict(party_id=pid, kind="role", player_id=did, reason_code="other", reason_text="",
                                        detail={"old_role": old_role, "new_role": role,
                                                "source": op.get("source", "panel")}))
                out.lines.append(f"✅ <@{did}> is now {role_label(role)} ({label(pid)})")
        elif kind in ("move", "trade"):
            if kind == "trade":
                a_id, b_id = str(op.get("a")), str(op.get("b"))
                pa, pb = _find(after, a_id), _find(after, b_id)
                if pa is None or pb is None or pa == pb:
                    err(f"{where}: pick two players from two different parties.")
                    continue
                moves = [(a_id, pb, None), (b_id, pa, None)]
            else:
                moves = [(str(op.get("player")), op.get("to"), op.get("role"))]
            staged, bad = [], False
            for did, to, role_arg in moves:
                src = _find(after, did)
                if src is None:
                    err(f"{where}: {_name(ctx, did)} is not in a party of this slot.")
                elif not party_ok(to, where):
                    pass
                elif to == src:
                    err(f"{where}: {_name(ctx, did)} is already in {label(to)}.")
                else:
                    staged.append((did, src, to, role_arg))
                    continue
                bad = True
                break
            if bad:
                continue
            if kind == "move" and seats(after[staged[0][2]]) >= size:
                err(f"{where}: {label(staged[0][2])} is full. Use trade to swap two players instead.")
                continue
            roles = [role_of({"role": r}, after[src]["members"][did]["role"]) for did, src, _, r in staged]
            if None in roles:
                continue
            taken = [(did, src, to, after[src]["members"].pop(did), role)
                     for (did, src, to, _), role in zip(staged, roles)]
            for did, src, to, m, role in taken:
                after[to]["members"][did] = {"role": role, "is_helper": m["is_helper"]}
                out.changes.append(dict(party_id=to, kind="move", player_id=did,
                                        detail={"from_party": before[src]["index"], "to_party": after[to]["index"],
                                                "old_role": m["role"], "new_role": role,
                                                "source": op.get("source", "panel")}))
                out.lines.append(f"✅ <@{did}> moves from party {before[src]['index'] + 1} to {label(to)} "
                                 f"as {role_label(role)}")
                if kind == "move":
                    warn(_name(ctx, did), swap_warnings(ctx, rows[to], did, role, moving=True))
        elif kind == "remove":
            pid, did = op.get("party"), str(op.get("player"))
            if not party_ok(pid, where):
                continue
            st = after[pid]
            code, text = _reason(op, out.errors, where)
            if did not in st["members"]:
                err(f"{where}: {_name(ctx, did)} is not in {label(pid)}.")
                continue
            role = st["members"].pop(did)["role"]
            out.changes.append(dict(party_id=pid, kind="remove", player_id=did, reason_code=code, reason_text=text,
                                    detail={"role": role, "source": op.get("source", "panel")}))
            out.leaving[did] = {"party": pid, "change": len(out.changes) - 1}
            out.lines.append(f"✅ <@{did}> was taken out of {label(pid)} ({role_label(role)} is open)")
        elif kind == "add":
            pid, did = op.get("party"), str(op.get("player"))
            if not party_ok(pid, where):
                continue
            st = after[pid]
            role = role_of(op, None)
            if role is None:
                continue
            if _find(after, did) is not None:
                err(f"{where}: {_name(ctx, did)} is already in a party of this slot; use move.")
                continue
            if seats(st) >= size:
                err(f"{where}: {label(pid)} is full.")
                continue
            st["members"][did] = {"role": role, "is_helper": did in helpers}
            out.changes.append(dict(party_id=pid, kind="add", other_player_id=did,
                                    detail={"in_role": role, "source": op.get("source", "panel")}))
            out.entering[did] = {"party": pid, "role": role, "replacing": None}
            warn(_name(ctx, did), swap_warnings(ctx, rows[pid], did, role))
            out.lines.append(f"✅ <@{did}> joins {label(pid)} as {role_label(role)}")
        elif kind == "pf_add":
            pid = op.get("party")
            if not party_ok(pid, where):
                continue
            st = after[pid]
            role = role_of(op, None)
            name = str(op.get("name") or "").strip()[:MAX_PF_NAME] or None
            if role is None:
                continue
            if seats(st) >= size:
                err(f"{where}: {label(pid)} is full.")
                continue
            st["pf"].append({"id": None, "role": role, "name": name})
            out.changes.append(dict(party_id=pid, kind="pf_add", detail={"role": role, "name": name,
                                                                         "source": op.get("source", "panel")}))
            out.lines.append(f"✅ A Party Finder player fills {role_label(role)} in {label(pid)}"
                             + (f" ({name})" if name else ""))
        elif kind == "pf_remove":
            pid, idx = op.get("party"), op.get("pf")
            if not party_ok(pid, where):
                continue
            st = after[pid]
            if not isinstance(idx, int) or not 0 <= idx < len(st["pf"]):
                err(f"{where}: there is no such Party Finder seat in {label(pid)}.")
                continue
            gone = st["pf"].pop(idx)
            out.changes.append(dict(party_id=pid, kind="pf_remove",
                                    detail={"role": gone["role"], "name": gone["name"],
                                            "source": op.get("source", "panel")}))
            out.lines.append(f"✅ The Party Finder seat ({role_label(gone['role'])}) in {label(pid)} is open again")
        else:
            err(f"{where}: unknown operation.")

    # a player cannot leave the slot and come back in the same edit, and no one may be seated twice
    seen = {}
    for pid, st in after.items():
        for did in st["members"]:
            if did in seen:
                err(f"{_name(ctx, did)} would be in two parties of the same slot.")
            seen[did] = pid
    for did in list(out.leaving):
        if did in seen:
            out.leaving.pop(did)                                    # moved or swapped back in: still in the slot
    for did in list(out.entering):
        if did in before_members(before):
            out.entering.pop(did)
    for pid in out.changed:
        st = after[pid]
        if seats(st) == size:
            want = ctx.cfg["party"]
            have = {r: sum(x == r for x in roles_of(st)) for r in ROLES}
            if any(have[r] != want.get(r, 0) for r in ROLES):
                out.warnings.append(f"Party {st['index'] + 1}: the roles would be {comp_text(have)} instead of "
                                    f"{comp_text(want)}.")
    return out


def before_members(before):
    return {did for st in before.values() for did in st["members"]}


# ---------------------------------------------------------------- applying

async def apply(ctx, guild_id, slot_id, ops, actor_id, *, warnings=None, names=None):
    """Plan again (the slot may have changed since the organizer staged the edit), write it, tell people.

    `warnings` are the validation warnings the organizer accepted; they are logged with each change. `names` maps
    a Discord id to a display name for players the bot has not seen yet. Returns (plan, change_ids, problems).
    A plan with errors is returned as is and nothing is written.
    """
    from . import confirmations
    from .party_views import WhySwappedView
    db = ctx.db
    result = plan(ctx, slot_id, ops)
    if result.errors or not result.changes:
        return result, [], []
    for did in result.entering:
        if db.get_player(did) is None:
            db.upsert_player(did, (names or {}).get(did) or str(did), now=ctx.now())
    accepted = warnings if warnings is not None else (result.warnings or None)
    for c in result.changes:
        c["warnings"] = accepted
    after = {pid: result.after[pid] for pid in result.changed}
    ids = db.apply_slot_edit(slot_id, after, result.changes, actor_id, ctx.now())
    problems = []
    slot_party = db.party(next(iter(result.after)))
    for pid in result.changed:
        err = await refresh_party_message(ctx, pid)
        if err and err not in problems:
            problems.append(err)
    thread = await thread_of(ctx, slot_party)
    if thread is None:
        if result.leaving or result.entering:
            problems.append("I could not find the slot's thread, so nobody was added or removed there.")
    else:
        for uid, add in [(u, False) for u in result.leaving] + [(u, True) for u in result.entering]:
            try:
                await (thread.add_user if add else thread.remove_user)(discord.Object(id=int(uid)))
            except discord.NotFound:
                log.info("user %s not found when changing thread members", uid)
            except Exception as e:
                log.exception("thread member change failed")
                problems.append(f"Could not {'add' if add else 'remove'} <@{uid}> "
                                f"{'to' if add else 'from'} the thread ({e}). I need Manage Threads.")
        notes = thread_lines(ctx, result)
        if notes:
            ping = [discord.Object(id=int(u)) for u in list(result.entering) + moved_players(result)]
            try:
                await thread.send("\n".join(notes)[:1990], allowed_mentions=discord.AllowedMentions(users=ping))
            except Exception:
                log.exception("could not announce the party changes")
                problems.append("Could not announce the changes in the thread.")
    if result.entering:
        problems += [p for p in [await confirmations.thread_prompt(ctx, slot_id, ping=[str(u) for u in result.entering])] if p]
    elif result.changes:
        await confirmations.refresh_prompts(ctx, slot_id)
    if result.leaving:
        channel = await find_channel(ctx, server_setting(ctx, guild_id, "parties_channel_id"))
        start = parse_iso(slot_party["start_utc"])
        for uid, info in result.leaving.items():
            n = result.before[info["party"]]["index"] + 1
            if channel is None:
                problems.append("No parties channel found (not set, deleted, or I lost access), so <@%s> was not "
                                "notified. Tell them yourself." % uid)
                continue
            verb = "swapped out of" if result.changes[info["change"]]["kind"] == "swap" else "taken out of"
            try:
                await channel.send(
                    f"<@{uid}> you were {verb} party {n} on {discord.utils.format_dt(start, 'F')} and are back in "
                    "the standby pool for that slot. Press the button to see why.",
                    view=WhySwappedView(ids[info["change"]]),
                    allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=int(uid))]))
            except Exception as e:
                log.exception("could not post the change notice")
                problems.append(f"Could not notify <@{uid}> in the parties channel ({e}).")
    return result, ids, problems


def moved_players(result):
    return [c["player_id"] for c in result.changes if c["kind"] == "move"]


def thread_lines(ctx, result):
    """What goes into the slot's thread: one message for the whole edit."""
    out = []
    for c in result.changes:
        n = result.after[c["party_id"]]["index"] + 1
        d = c.get("detail") or {}
        if c["kind"] == "swap":
            out.append(f"<@{c['other_player_id']}> joins party {n} as {role_label(d['in_role'])}, "
                       f"replacing <@{c['player_id']}>.")
        elif c["kind"] == "add":
            out.append(f"<@{c['other_player_id']}> joins party {n} as {role_label(d['in_role'])}.")
        elif c["kind"] == "remove":
            out.append(f"<@{c['player_id']}> leaves party {n}. A {role_label(d['role'])} seat is open.")
        elif c["kind"] == "move":
            out.append(f"<@{c['player_id']}> moves from party {d['from_party'] + 1} to party {n} "
                       f"as {role_label(d['new_role'])}.")
        elif c["kind"] == "role":
            out.append(f"<@{c['player_id']}> is now {role_label(d['new_role'])} in party {n}.")
        elif c["kind"] == "pf_add":
            out.append(f"A Party Finder player fills {role_label(d['role'])} in party {n}"
                       + (f" ({d['name']})." if d.get("name") else "."))
        elif c["kind"] == "pf_remove":
            out.append(f"The Party Finder seat ({role_label(d['role'])}) in party {n} is open again.")
    return out


# ---------------------------------------------------------------- showing a state

def party_lines(ctx, state, characters=None, mark=None):
    """Members, Party Finder seats and the short flag of one party state, as text lines."""
    order = {r: i for i, r in enumerate(ROLES)}
    lines = []
    for did, m in sorted(state["members"].items(), key=lambda kv: (order[kv[1]["role"]], kv[0])):
        char = f" · {character_text((characters or {}).get(did))}" if characters is not None else ""
        lines.append(f"• <@{did}> {role_label(m['role'])}{char}")
    for x in state["pf"]:
        lines.append(f"• Party Finder · {role_label(x['role'])}" + (f" · {x['name']}" if x.get("name") else ""))
    gap = short_by(ctx, state)
    if gap:
        lines.append(f"⚠ Short by {gap}: {open_roles(ctx, state)}")
    return lines


def open_roles(ctx, state):
    """Which roles the party still needs to match the configured composition (best effort text)."""
    want = ctx.cfg["party"]
    have = {r: sum(x == r for x in roles_of(state)) for r in ROLES}
    missing = [f"{want[r] - have[r]} {role_label(r)}" for r in ROLES if want.get(r, 0) > have[r]]
    return ", ".join(missing) or "any role"
