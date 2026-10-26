"""Hand-off to raid-solver: export current signups, import a finished draft.

The export is the exact JSON that raid-solver/solve.py reads (see gen_fake.py there).
The solver stamps its draft with a fingerprint of that input. On import we rebuild the
signups from the database, fingerprint them the same way and refuse the draft if the two
differ, so a draft made from outdated signups can never be applied.
"""
import hashlib
import json
from collections import Counter
from zoneinfo import ZoneInfo

from .db import parse_iso, split_roles
from .config import ROLES

EXPORT_VERSION = 1


class DraftError(Exception):
    """The draft file cannot be applied; the message is shown to the organizer."""


def fingerprint(obj):
    """Same recipe as solve.py: SHA-256 of canonical JSON, so key order and whitespace never matter."""
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def phase_positions(checkpoints, cross_cost):
    """Checkpoint idx -> position on the solver's line (moving into a later phase costs cross_cost extra)."""
    phases = []
    for c in checkpoints:
        if c["phase"] not in phases:
            phases.append(c["phase"])
    return {c["idx"]: c["idx"] + cross_cost * phases.index(c["phase"]) for c in checkpoints}


def drop_units(pos, prog, n_checkpoints):
    """'OK with N checkpoints below my prog' -> the solver's distance units for that player."""
    if n_checkpoints <= 0:
        return 0
    return abs(pos[prog] - pos[max(prog - n_checkpoints, 0)])


def _player_sort_key(discord_id):
    return (len(discord_id), discord_id)


def build_signups(db, run, cfg):
    """Current signups for `run` as solver input. Returns (data, warnings).

    Deterministic: nothing in it depends on the clock, so the same database state always
    gives the same fingerprint. Players who ticked slots but have no roles or prog yet are
    left out and listed in `warnings`.
    """
    tz = ZoneInfo(cfg["timezone"])
    checkpoints = db.checkpoints(run["fight_id"])
    pos = phase_positions(checkpoints, cfg["solver_phase_cross_cost"])
    helpers = db.helper_ids(run["fight_id"])
    unmatched = db.previous_unmatched(run)

    slots = [{"id": s["slot_key"],
              "start": parse_iso(s["start_utc"]).astimezone(tz).isoformat(),
              "minutes": s["minutes"]} for s in db.run_slots(run["id"])]

    ticks = {}
    for r in db.tick_rows(run["id"]):
        ticks.setdefault(r["discord_id"], {"row": r, "keys": []})["keys"].append(r["slot_key"])

    players, warnings = [], []
    for did in sorted(ticks, key=_player_sort_key):
        row = ticks[did]["row"]
        roles = [r for r in ROLES if r in split_roles(row["roles"])]
        if not roles or row["prog_index"] is None:
            warnings.append(f"{row['display_name']} ({did}) left out: set roles and prog with /availability")
            continue
        lim = db.get_limits(run["id"], did, cfg)
        n_drop = min(lim["drop_checkpoints"], row["prog_index"])
        players.append({
            "id": did,
            "name": row["display_name"],
            "roles": roles,
            "helper": did in helpers,
            "prog": row["prog_index"],
            "max_per_day": lim["max_per_day"],
            "max_per_run": lim["max_per_run"],
            "drop": {"opt_in": n_drop > 0, "max_drop": drop_units(pos, row["prog_index"], n_drop)},
            "unmatched_last_run": did in unmatched,
            "available": sorted(set(ticks[did]["keys"])),
        })

    fight = db.fight(run["fight_id"])
    data = {
        "version": EXPORT_VERSION,
        "run": {"name": run["name"], "timezone": cfg["timezone"]},
        "fight": {"name": fight["name"],
                  "checkpoints": [{"index": c["idx"], "phase": c["phase"], "name": c["name"]} for c in checkpoints]},
        "slots": slots,
        "players": players,
    }
    return data, warnings


def import_draft(db, run, draft, cfg):
    """Validate a solver draft against the current signups and store it as 'draft' parties.

    Raises DraftError (bad or stale file) or db.DraftConflict (run already has confirmed parties).
    Returns a summary dict.
    """
    if not isinstance(draft, dict) or "parties" not in draft or "input_fingerprint" not in draft:
        raise DraftError("That file does not look like a solver draft (no parties or fingerprint).")
    if draft.get("version") != 1:
        raise DraftError(f"Unsupported draft version {draft.get('version')!r}; this bot reads version 1.")

    data, warnings = build_signups(db, run, cfg)
    current = fingerprint(data)
    if draft["input_fingerprint"] != current:
        raise DraftError(
            "This draft was built from different signups than the current ones "
            f"(draft {str(draft['input_fingerprint'])[:12]}, now {current[:12]}). "
            "Someone changed their signup, or the draft is for another run. "
            "Run /draft export again, re-run the solver, and import the new draft.")

    players = {p["id"]: p for p in data["players"]}
    slot_ids = {s["id"] for s in data["slots"]}
    n_checkpoints = len(data["fight"]["checkpoints"])
    seat_plan = cfg["party"]

    parties, seen_party, seen_in_slot = [], set(), set()
    for pa in draft["parties"]:
        slot, idx = pa.get("slot"), pa.get("party")
        label = f"{slot} #{idx}"
        if slot not in slot_ids:
            raise DraftError(f"Party {label}: unknown slot.")
        if (slot, idx) in seen_party:
            raise DraftError(f"Party {label} appears twice.")
        seen_party.add((slot, idx))
        target = (pa.get("target") or {}).get("index")
        if not isinstance(target, int) or not 0 <= target < n_checkpoints:
            raise DraftError(f"Party {label}: bad target checkpoint.")
        members = pa.get("members") or []
        if dict(Counter(m.get("role") for m in members)) != seat_plan:
            raise DraftError(f"Party {label}: roles do not match the party plan {seat_plan}.")
        out = []
        for m in members:
            pid = m.get("player")
            p = players.get(pid)
            if p is None:
                raise DraftError(f"Party {label}: player {pid} is not in the current signups.")
            if slot not in p["available"]:
                raise DraftError(f"Party {label}: {p['name']} did not tick that slot.")
            if m["role"] not in p["roles"]:
                raise DraftError(f"Party {label}: {p['name']} cannot play {m['role']}.")
            if (slot, pid) in seen_in_slot:
                raise DraftError(f"{p['name']} is in two parties of slot {slot}.")
            seen_in_slot.add((slot, pid))
            out.append({"discord_id": pid, "role": m["role"], "helper": bool(m.get("helper")),
                        "placed_down": bool(m.get("placed_down")), "distance": int(m.get("distance") or 0)})
        parties.append({"slot_key": slot, "party_index": idx, "target_idx": target, "members": out})

    notes = [("slot", s["slot"], s.get("reason") or "") for s in draft.get("unfilled_slots", []) if s.get("slot")]
    notes += [("player", u["player"], u.get("reason") or "") for u in draft.get("unmatched_players", [])
              if u.get("player")]
    db.replace_draft(run["id"], parties, notes)

    seated = {m["discord_id"] for pa in parties for m in pa["members"]}
    return {
        "parties": len(parties),
        "slots": len({pa["slot_key"] for pa in parties}),
        "seated": len(seated),
        "no_party": len(set(players) - seated),
        "placed_down": sum(1 for pa in parties for m in pa["members"] if m["placed_down"]),
        "warnings": warnings,
        "slot_notes": [n for n in notes if n[0] == "slot"],
    }
