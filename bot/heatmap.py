"""Live signup heatmap for organizers: ticks per slot, split by role, plus a "could it fill?" hint."""
from collections import defaultdict
from datetime import date

from .config import ROLES
from .db import split_roles

MESSAGE_LIMIT = 1900     # Discord allows 2000 characters per message


def match_players(role_sets, party):
    """Greedy-free bipartite matching of players to party seats (Kuhn's algorithm).

    Returns the set of player indices that got a seat. A full party fits when its size
    equals the number of seats.
    """
    seats = [r for r in ROLES for _ in range(party[r])]
    owner = {}                                            # seat index -> player index

    def try_player(i, seen):
        for s, role in enumerate(seats):
            if role in role_sets[i] and s not in seen:
                seen.add(s)
                if s not in owner or try_player(owner[s], seen):
                    owner[s] = i
                    return True
        return False

    for i in range(len(role_sets)):
        try_player(i, set())
    return set(owner.values())


def estimate_parties(role_sets, party):
    """How many full parties the role mix alone could support (ignores prog and player limits)."""
    seats = sum(party.values())
    remaining = list(range(len(role_sets)))
    count = 0
    while len(remaining) >= seats:
        matched = match_players([role_sets[i] for i in remaining], party)
        if len(matched) < seats:
            break
        remaining = [p for k, p in enumerate(remaining) if k not in matched]
        count += 1
    return count


def slot_table(db, run, cfg):
    """One dict per slot that has at least one tick, in time order."""
    by_slot = defaultdict(list)
    for r in db.tick_rows(run["id"]):
        by_slot[r["slot_key"]].append(r)
    table = []
    for s in db.run_slots(run["id"]):
        rows = by_slot.get(s["slot_key"])
        if not rows:
            continue
        role_sets = [set(split_roles(r["roles"])) for r in rows]
        table.append({
            "key": s["slot_key"],
            "tank": sum("tank" in rs for rs in role_sets),
            "healer": sum("healer" in rs for rs in role_sets),
            "dps": sum("dps" in rs for rs in role_sets),
            "total": len(rows),
            "parties": estimate_parties(role_sets, cfg["party"]),
        })
    return table


def render(db, run, cfg, day=None):
    """The heatmap as a list of messages, each under the Discord length limit."""
    table = slot_table(db, run, cfg)
    ticks = db.tick_rows(run["id"])
    signed = {r["discord_id"] for r in ticks}
    incomplete = {r["discord_id"] for r in ticks if not split_roles(r["roles"]) or r["prog_index"] is None}

    head = [f"**{run['name']}** ({run['status']}): {len(signed)} players have ticked slots"
            + (f", {len(incomplete)} still need roles or prog" if incomplete else "") + ".",
            f"Times are {cfg['timezone']}. T/H/D = ticked players who can play that role (flex players count in each). "
            "\"fill\" = full parties the role mix could support, ignoring prog and limits; `--` = cannot fill."]
    days = defaultdict(list)
    for row in table:
        days[row["key"][:10]].append(row)
    if day:
        days = {d: v for d, v in days.items() if d == day}

    blocks = []
    for d in sorted(days):
        title = date.fromisoformat(d).strftime("%a %d %b")
        lines = [" time    T  H  D  all  fill"]
        for row in days[d]:
            mark = f"✓ ~{row['parties']}" if row["parties"] else "--"
            lines.append(f"{row['key'][11:16]}  {row['tank']:>2} {row['healer']:>2} {row['dps']:>2}  {row['total']:>3}  {mark}")
        blocks.append(f"**{title}**\n```\n" + "\n".join(lines) + "\n```")
    if not blocks:
        blocks.append("No one has ticked any slots yet." if not day else "No ticks on that day.")

    messages, cur = [], "\n".join(head)
    for b in blocks:
        if len(cur) + len(b) + 1 > MESSAGE_LIMIT:
            messages.append(cur)
            cur = b
        else:
            cur += "\n" + b
    messages.append(cur)
    return messages
