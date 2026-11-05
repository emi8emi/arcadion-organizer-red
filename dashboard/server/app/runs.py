import calendar
import re

import psycopg
from fastapi import APIRouter, Depends, HTTPException

from . import config
from .auth import current_user
from .db import get_conn

router = APIRouter(prefix="/api")

MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


@router.get("/runs")
def list_runs(
    month: str | None = None,
    user: dict = Depends(current_user),
    conn: psycopg.Connection = Depends(get_conn),
):
    """The server's raid runs. With ?month=2026-11, only the runs that touch that month.

    Besides the basics, each run says which of its days are skipped and which days need checking
    (a day with a draft party that nobody has approved yet).
    """
    where = "r.guild_id = %s"
    params: list = [config.DISCORD_GUILD_ID]
    if month is not None:
        if not MONTH_RE.match(month):
            raise HTTPException(status_code=400, detail="month must look like 2026-11")
        year, mon = int(month[:4]), int(month[5:])
        first = f"{month}-01"
        last = f"{month}-{calendar.monthrange(year, mon)[1]:02d}"
        where += " AND r.start_date <= %s AND r.end_date >= %s"
        params += [last, first]

    rows = conn.execute(
        f"""SELECT r.id, r.name, r.fight_id, f.name AS fight, r.start_date, r.end_date, r.status,
                   (SELECT COUNT(*) FROM slots s WHERE s.run_id = r.id) AS slots,
                   (SELECT COUNT(*) FROM parties p WHERE p.run_id = r.id) AS parties
            FROM runs r JOIN fights f ON f.id = r.fight_id
            WHERE {where}
            ORDER BY r.start_date, r.id""",
        params,
    ).fetchall()

    ids = [r["id"] for r in rows]
    skipped: dict[int, list[str]] = {}
    check: dict[int, list[str]] = {}
    if ids:
        for d in conn.execute(
            "SELECT run_id, day FROM run_days WHERE run_id = ANY(%s) AND skipped = 1 ORDER BY day",
            (ids,),
        ):
            skipped.setdefault(d["run_id"], []).append(d["day"])
        # slot_key looks like 2026-11-02T17:00, so its first 10 characters are the day
        for d in conn.execute(
            """SELECT DISTINCT p.run_id, substr(s.slot_key, 1, 10) AS day
               FROM parties p JOIN slots s ON s.id = p.slot_id
               WHERE p.run_id = ANY(%s) AND p.status = 'draft'
               ORDER BY day""",
            (ids,),
        ):
            check.setdefault(d["run_id"], []).append(d["day"])

    out = []
    for r in rows:
        run = dict(r)
        run["skipped_days"] = skipped.get(r["id"], [])
        run["check_days"] = [d for d in check.get(r["id"], []) if d not in run["skipped_days"]]
        out.append(run)
    return out
