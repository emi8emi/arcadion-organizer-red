"""Day snapshots: each day's signups (name, roles, prog, limits) are frozen at that day's cutoff.

Roles, prog and limits belong to the player, not to a day, so without this a player editing their profile
for Wednesday after Monday's cutoff would change Monday's export and make Monday's draft stale.
Helper grants are deliberately not frozen: organizers grant them on purpose, and a grant after the cutoff
should make an earlier export stale.
"""
import logging
from datetime import timedelta

from .services import cutoff_of

log = logging.getLogger("raidbot")

# The loop runs every minute. A snapshot taken much later than that (bot was down) is flagged as late.
LATE_AFTER = timedelta(minutes=5)

LATE_WARNING = "Snapshot taken late: profile edits between the cutoff and now may be included."


def take_due_snapshots(db, now, cfg):
    """Snapshot every day whose cutoff has passed and has none yet. Pure apart from the database writes.

    Returns [(run_id, day, late)] for the snapshots taken. Idempotent: a second call does nothing.
    Days that were already exported without a snapshot (made before this feature) are left alone.
    """
    taken = []
    for d in db.snapshot_candidates(now):
        late = now - cutoff_of(d) > LATE_AFTER
        if db.take_snapshot(d["run_id"], d["day"], cfg, now, late=late):
            taken.append((d["run_id"], d["day"], late))
    return taken


def ensure_snapshot(ctx, run, day):
    """Lazy snapshot for a closed day that has none (the loop did not get to it yet, or was down).

    Used before export, import and status. Flagged late, because edits since the cutoff may be in it.
    Does nothing for a draft run, a day that still takes signups, an exported day, or an existing snapshot.
    """
    row = ctx.db.run_day(run["id"], day)
    if row is None or row["snapshot_utc"] or row["exported_utc"] or run["status"] == "draft":
        return False
    now = ctx.now()
    if run["status"] == "open" and now < cutoff_of(row):
        return False
    return ctx.db.take_snapshot(run["id"], day, ctx.cfg, now, late=True)
