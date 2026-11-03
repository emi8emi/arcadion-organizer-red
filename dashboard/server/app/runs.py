import sqlite3

from fastapi import APIRouter, Depends

from . import config
from .auth import current_user
from .db import get_conn

router = APIRouter(prefix="/api")


@router.get("/runs")
def list_runs(
    user: dict = Depends(current_user),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """The server's raid runs, newest first, with how many slots and parties each has."""
    rows = conn.execute(
        """SELECT r.id, r.name, f.name AS fight, r.start_date, r.end_date, r.status,
                  (SELECT COUNT(*) FROM slots s WHERE s.run_id = r.id) AS slots,
                  (SELECT COUNT(*) FROM parties p WHERE p.run_id = r.id) AS parties
           FROM runs r JOIN fights f ON f.id = r.fight_id
           WHERE r.guild_id = ?
           ORDER BY r.start_date DESC, r.id DESC""",
        (config.DISCORD_GUILD_ID,),
    ).fetchall()
    return [dict(r) for r in rows]
