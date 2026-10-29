"""Seed fake test players into the bot's database, so one person can test the whole flow alone.

    uv run python -m bot.seed --run "test1"               # 7 fake players; you are the 8th and play dps
    uv run python -m bot.seed --run "test1" --you tank    # you play tank, the fakes fill the other seats
    uv run python -m bot.seed --remove                    # delete every fake player and all their rows

The fakes get roles, a prog checkpoint and ticks on the run's slots (all of them, or just --slot KEY).
Run it BEFORE a day's cutoff: after the cutoff a day's signups are frozen in a snapshot, and a day that
already has one is skipped. The fakes are not real Discord members, so in posted parties they show up as
"unknown user" mentions. Their ids are 19-digit numbers above 9e18, which no real account can have.
"""
import argparse
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

from .config import ROLES, ROOT, load_config
from .db import DB, parse_iso

FAKE_BASE = 9_000_000_000_000_000_000   # a snowflake timestamp in 2083: never a real account
FAKE_MAX = FAKE_BASE + 9_999
LABEL = {"tank": "Tank", "healer": "Healer", "dps": "DPS"}
ID_COLUMNS = ("discord_id", "voter_id", "target_id")


class SeedError(Exception):
    """A problem the user can fix (unknown run, bad option)."""


def fake_id(n):
    return str(FAKE_BASE + n)


def is_fake(discord_id):
    text = str(discord_id)
    return text.isdigit() and FAKE_BASE <= int(text) <= FAKE_MAX


def fake_roles(party, you, count):
    """Roles for the fakes: a full party minus the seat `you` take, then more full parties if count is larger."""
    full = [r for r in ROLES for _ in range(party[r])]
    first = list(full)
    first.remove(you)
    roles = list(first)
    while len(roles) < count:
        roles += full
    return roles[:count]


def find_run(db, name, guild_id=None):
    rows = db.conn.execute("SELECT * FROM runs WHERE name = ? COLLATE NOCASE", (name,)).fetchall()
    if guild_id is not None:
        rows = [r for r in rows if str(r["guild_id"]) == str(guild_id)]
    if not rows:
        raise SeedError(f"No run called '{name}' in this database. Create it first with /run create.")
    if len(rows) > 1:
        ids = ", ".join(str(r["guild_id"]) for r in rows)
        raise SeedError(f"'{name}' exists in several servers ({ids}); pass --guild-id.")
    return rows[0]


def seed(db, cfg, run, *, count=None, you="dps", prog=0, helpers=1, slots=None, now=None):
    """Create or refresh the fake players for `run`. Returns a report dict; safe to run again."""
    now = now or datetime.now(timezone.utc)
    party = cfg["party"]
    if you not in ROLES:
        raise SeedError(f"--you must be one of {', '.join(ROLES)}.")
    count = sum(party.values()) - 1 if count is None else count
    if not 1 <= count <= 99:
        raise SeedError("--count must be between 1 and 99.")
    checkpoints = {c["idx"]: c for c in db.checkpoints(run["fight_id"])}
    if prog not in checkpoints:
        raise SeedError(f"--prog must be a checkpoint index from 0 to {max(checkpoints)}.")
    helpers = max(0, min(helpers, count))

    all_keys = [s["slot_key"] for s in db.run_slots(run["id"])]
    if slots:
        unknown = sorted(set(slots) - set(all_keys))
        if unknown:
            raise SeedError(f"Not slots of this run: {', '.join(unknown)}. Slot keys look like {all_keys[0]}.")
        keys = [k for k in all_keys if k in set(slots)]
    else:
        keys = all_keys
    by_day = defaultdict(list)
    for key in keys:
        by_day[key[:10]].append(key)

    warnings, skipped = [], []
    if run["status"] == "draft":
        warnings.append("The run is still a draft: /run open lets real players sign up (the fakes are saved anyway).")
    for day in sorted(by_day):
        row = db.run_day(run["id"], day)
        if row is not None and row["snapshot_utc"]:
            skipped.append(day)
            warnings.append(f"{day} already has a snapshot (its signups are frozen), so the fakes were NOT added there.")
        elif row is not None and parse_iso(row["cutoff_utc"]) <= now:
            warnings.append(f"The cutoff for {day} has passed, so its snapshot will be taken late and include the fakes.")
    days = [d for d in sorted(by_day) if d not in skipped]

    roles = fake_roles(party, you, count)
    seen = Counter()
    fakes = []
    for i, role in enumerate(roles, start=1):
        seen[role] += 1
        fakes.append({"id": fake_id(i), "name": f"Test {LABEL[role]} {seen[role]}", "role": role, "helper": False})
    for fake in fakes[len(fakes) - helpers:]:        # the last ones (mostly dps) are the helpers
        fake["helper"] = True

    for fake in fakes:
        db.upsert_player(fake["id"], fake["name"], roles=[fake["role"]], prog_index=prog, now=now)
        for day in days:
            db.set_day_availability(run["id"], fake["id"], day, by_day[day])
        if fake["helper"]:
            db.grant_helper(run["fight_id"], fake["id"], "seed", now)
        else:
            db.revoke_helper(run["fight_id"], fake["id"])
    return {"fakes": fakes, "days": days, "skipped_days": skipped, "slots_ticked": sum(len(by_day[d]) for d in days),
            "prog": prog, "prog_name": f"{checkpoints[prog]['phase']} {checkpoints[prog]['name']}",
            "you": you, "warnings": warnings}


def remove(db):
    """Delete every fake player and every row that mentions one. Returns {table: rows deleted}."""
    conn = db.conn
    ids = [r["discord_id"] for r in conn.execute("SELECT discord_id FROM players") if is_fake(r["discord_id"])]
    report = {}
    if not ids:
        return report
    marks = ",".join("?" for _ in ids)
    tables = [r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")        # rows are removed from every table, in any order
    try:
        with conn:
            for table in tables:
                cols = [c["name"] for c in conn.execute(f'PRAGMA table_info("{table}")') if c["name"] in ID_COLUMNS]
                if not cols:
                    continue
                where = " OR ".join(f'"{c}" IN ({marks})' for c in cols)
                deleted = conn.execute(f'DELETE FROM "{table}" WHERE {where}', ids * len(cols)).rowcount
                if deleted:
                    report[table] = deleted
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    return report


def describe(report, run):
    lines = [f"Seeded {len(report['fakes'])} fake players into run '{run['name']}':"]
    for f in report["fakes"]:
        lines.append(f"  {f['name']}: {f['role']}, prog {report['prog']} ({report['prog_name']})"
                     f"{', helper' if f['helper'] else ''}")
    lines.append(f"Ticked {report['slots_ticked']} slot(s) on {len(report['days'])} day(s).")
    lines.append(f"You are the missing seat: in /availability pick {report['you']}, set prog to "
                 f"{report['prog_name']} and tick the same slot(s).")
    lines += [f"Warning: {w}" for w in report["warnings"]]
    lines.append("Run this again to reset them; `python -m bot.seed --remove` deletes them. "
                 "They are not real members, so posted parties show them as unknown users.")
    return "\n".join(lines)


def open_db(path_override=None):
    cfg = load_config()
    path = path_override or cfg["db_path"]
    if path != ":memory:" and not os.path.isabs(path):
        path = str(ROOT / path)
    return cfg, DB(path), path


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", help="name of an existing run to sign the fakes up for")
    ap.add_argument("--remove", action="store_true", help="delete all fake players and exit")
    ap.add_argument("--count", type=int, help="how many fakes (default: a full party minus you, 7)")
    ap.add_argument("--you", default="dps", help="the role you will play yourself (default dps)")
    ap.add_argument("--prog", type=int, default=0, help="checkpoint index for every fake (default 0, P1 fresh)")
    ap.add_argument("--helpers", type=int, default=1, help="how many fakes are helpers (default 1)")
    ap.add_argument("--slot", action="append", help="only tick this slot key (repeatable), e.g. 2026-11-02T17:00")
    ap.add_argument("--guild-id", help="needed only if the run name exists in several servers")
    ap.add_argument("--db", help="database file (default: the bot's own)")
    args = ap.parse_args(argv)
    try:
        cfg, db, path = open_db(args.db)
        if args.remove:
            deleted = remove(db)
            print(f"Database: {path}")
            print("No fake players found." if not deleted else
                  "Removed fake players. Rows deleted: " + ", ".join(f"{t} {n}" for t, n in sorted(deleted.items())))
            if "party_members" in deleted:
                print("Some parties had fake members; re-run the solver and /draft import for those days.")
            return 0
        if not args.run:
            raise SeedError("Pass --run NAME (or --remove).")
        run = find_run(db, args.run, args.guild_id)
        report = seed(db, cfg, run, count=args.count, you=args.you, prog=args.prog, helpers=args.helpers,
                      slots=args.slot)
        print(f"Database: {path}")
        print(describe(report, run))
        return 0
    except SeedError as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
