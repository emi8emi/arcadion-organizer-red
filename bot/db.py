"""SQLite access. Plain sqlite3 (synchronous): fine for a 20-60 person community bot.

Every method that changes data commits before returning. Times are UTC ISO strings.
"""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import ROLES

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"


class DuplicateRun(Exception):
    """A run with that name already exists in the server."""


class DraftConflict(Exception):
    """The run already has confirmed parties, so a draft must not overwrite them."""


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(text):
    return datetime.fromisoformat(text)


def join_roles(roles):
    """Store roles in a fixed order so equal sets always look the same."""
    return ",".join(r for r in ROLES if r in set(roles))


def split_roles(text):
    return [r for r in (text or "").split(",") if r]


class DB:
    def __init__(self, path=":memory:"):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA_PATH.read_text())
        self._migrate()
        self.conn.commit()

    def _migrate(self):
        """Bring a database made before per-day cutoffs up to date. Safe to run on every start.

        Runs without run_days rows get one row per day (taken from their slots) using the run's old
        signup_deadline_utc as every day's cutoff. A day that already has parties in the database counts
        as exported, so old runs do not trigger export reminders.
        """
        with self.conn:
            self.conn.execute(
                """INSERT OR IGNORE INTO run_days (run_id, day, cutoff_utc, exported_utc)
                   SELECT r.id, substr(s.slot_key, 1, 10), r.signup_deadline_utc,
                          CASE WHEN EXISTS (SELECT 1 FROM parties p JOIN slots ps ON ps.id = p.slot_id
                                            WHERE p.run_id = r.id
                                              AND substr(ps.slot_key, 1, 10) = substr(s.slot_key, 1, 10))
                               THEN r.created_utc END
                   FROM runs r JOIN slots s ON s.run_id = r.id
                   WHERE r.id NOT IN (SELECT run_id FROM run_days)
                   GROUP BY r.id, substr(s.slot_key, 1, 10)""")

    def close(self):
        self.conn.close()

    # ------------------------------------------------------------ fights

    def fights(self):
        return self.conn.execute("SELECT * FROM fights ORDER BY id").fetchall()

    def find_fight(self, text):
        """Exact name or short name first, then a unique 'contains' match. None if unclear."""
        t = text.strip().lower()
        rows = self.fights()
        for r in rows:
            if t in (r["name"].lower(), (r["short_name"] or "").lower()):
                return r
        hits = [r for r in rows if t and t in r["name"].lower()]
        return hits[0] if len(hits) == 1 else None

    def fight(self, fight_id):
        return self.conn.execute("SELECT * FROM fights WHERE id = ?", (fight_id,)).fetchone()

    def checkpoints(self, fight_id):
        return self.conn.execute(
            "SELECT idx, phase, name FROM checkpoints WHERE fight_id = ? ORDER BY idx", (fight_id,)
        ).fetchall()

    # ------------------------------------------------------------ runs

    def create_run(self, *, guild_id, name, fight_id, start_date, end_date, cutoffs, slots,
                   created_by, earliest=None, latest=None, now):
        """`cutoffs` maps each day ('2026-11-02') to the UTC datetime its signups close."""
        legacy_deadline = max(cutoffs.values())            # only kept so old code and old databases still make sense
        try:
            with self.conn:
                cur = self.conn.execute(
                    """INSERT INTO runs (guild_id, name, fight_id, start_date, end_date, earliest_start,
                                         latest_start, signup_deadline_utc, status, created_by, created_utc)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)""",
                    (str(guild_id), name, fight_id, str(start_date), str(end_date), earliest, latest,
                     iso(legacy_deadline), str(created_by), iso(now)))
                run_id = cur.lastrowid
                self.conn.executemany(
                    "INSERT INTO slots (run_id, slot_key, start_utc, minutes) VALUES (?, ?, ?, ?)",
                    [(run_id, s.key, iso(s.start_utc), s.minutes) for s in slots])
                self.conn.executemany(
                    "INSERT INTO run_days (run_id, day, cutoff_utc) VALUES (?, ?, ?)",
                    [(run_id, day, iso(cutoff)) for day, cutoff in sorted(cutoffs.items())])
        except sqlite3.IntegrityError as e:
            if "runs.guild_id" in str(e) or "UNIQUE" in str(e):
                raise DuplicateRun(name) from e
            raise
        return run_id

    def run(self, run_id):
        return self.conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()

    def get_run(self, guild_id, name=None):
        """A run by name (case-insensitive), or the newest run in the server when no name is given."""
        if name:
            return self.conn.execute(
                "SELECT * FROM runs WHERE guild_id = ? AND lower(name) = lower(?)",
                (str(guild_id), name.strip())).fetchone()
        return self.conn.execute(
            "SELECT * FROM runs WHERE guild_id = ? ORDER BY id DESC LIMIT 1", (str(guild_id),)).fetchone()

    def open_run(self, guild_id):
        return self.conn.execute(
            "SELECT * FROM runs WHERE guild_id = ? AND status = 'open' ORDER BY id DESC LIMIT 1",
            (str(guild_id),)).fetchone()

    def set_run_status(self, run_id, status):
        with self.conn:
            self.conn.execute("UPDATE runs SET status = ? WHERE id = ?", (status, run_id))

    def run_slots(self, run_id):
        return self.conn.execute(
            "SELECT * FROM slots WHERE run_id = ? ORDER BY start_utc", (run_id,)).fetchall()

    def slots_on(self, run_id, day):
        return self.conn.execute(
            "SELECT * FROM slots WHERE run_id = ? AND substr(slot_key, 1, 10) = ? ORDER BY start_utc",
            (run_id, day)).fetchall()

    # ------------------------------------------------------------ days (per-day cutoffs)

    def run_days(self, run_id):
        return self.conn.execute("SELECT * FROM run_days WHERE run_id = ? ORDER BY day", (run_id,)).fetchall()

    def run_day(self, run_id, day):
        return self.conn.execute(
            "SELECT * FROM run_days WHERE run_id = ? AND day = ?", (run_id, day)).fetchone()

    def set_cutoff(self, run_id, day, cutoff_utc, *, reset_export=False):
        """Move one day's cutoff. `reset_export` forgets the export and reminders (the day reopened)."""
        with self.conn:
            self.conn.execute("UPDATE run_days SET cutoff_utc = ? WHERE run_id = ? AND day = ?",
                              (iso(cutoff_utc), run_id, day))
            if reset_export:
                self.conn.execute("UPDATE run_days SET exported_utc = NULL, last_reminded_utc = NULL "
                                  "WHERE run_id = ? AND day = ?", (run_id, day))

    def mark_exported(self, run_id, day, now):
        with self.conn:
            self.conn.execute("UPDATE run_days SET exported_utc = ? WHERE run_id = ? AND day = ?",
                              (iso(now), run_id, day))

    def mark_reminded(self, run_id, day, now):
        with self.conn:
            self.conn.execute("UPDATE run_days SET last_reminded_utc = ? WHERE run_id = ? AND day = ?",
                              (iso(now), run_id, day))

    def first_slot_start(self, run_id, day):
        """UTC start of the day's first slot, or None."""
        row = self.conn.execute(
            "SELECT MIN(start_utc) AS t FROM slots WHERE run_id = ? AND substr(slot_key, 1, 10) = ?",
            (run_id, day)).fetchone()
        return parse_iso(row["t"]) if row and row["t"] else None

    def reminder_candidates(self, now):
        """Days that might need an export reminder: runs that were opened, cutoff passed, not exported yet."""
        return self.conn.execute(
            """SELECT d.*, r.guild_id, r.name AS run_name FROM run_days d JOIN runs r ON r.id = d.run_id
               WHERE r.status IN ('open', 'closed') AND d.exported_utc IS NULL AND d.cutoff_utc <= ?
               ORDER BY d.cutoff_utc, r.id, d.day""", (iso(now),)).fetchall()

    def played_counts(self, run_id, before_day):
        """Parties each player has in this run on days strictly before `before_day` (any draft or confirmed status)."""
        rows = self.conn.execute(
            """SELECT m.discord_id, COUNT(*) AS n
               FROM party_members m JOIN parties p ON p.id = m.party_id JOIN slots s ON s.id = p.slot_id
               WHERE p.run_id = ? AND substr(s.slot_key, 1, 10) < ? GROUP BY m.discord_id""",
            (run_id, before_day)).fetchall()
        return {r["discord_id"]: r["n"] for r in rows}

    # ------------------------------------------------------------ players

    def get_player(self, discord_id):
        return self.conn.execute(
            "SELECT * FROM players WHERE discord_id = ?", (str(discord_id),)).fetchone()

    def upsert_player(self, discord_id, display_name, *, roles=None, prog_index=None, timezone=None, now=None):
        """Create the player if new; update only the fields that are given."""
        did = str(discord_id)
        stamp = iso(now) if now else None
        with self.conn:
            if self.get_player(did) is None:
                self.conn.execute("INSERT INTO players (discord_id, display_name) VALUES (?, ?)", (did, display_name))
            self.conn.execute("UPDATE players SET display_name = ?, updated_utc = COALESCE(?, updated_utc) "
                              "WHERE discord_id = ?", (display_name, stamp, did))
            if roles is not None:
                self.conn.execute("UPDATE players SET roles = ? WHERE discord_id = ?", (join_roles(roles), did))
            if prog_index is not None:
                self.conn.execute("UPDATE players SET prog_index = ? WHERE discord_id = ?", (prog_index, did))
            if timezone is not None:
                self.conn.execute("UPDATE players SET timezone = ? WHERE discord_id = ?", (timezone, did))
        return self.get_player(did)

    # ------------------------------------------------------------ availability

    def ticked_keys(self, run_id, discord_id):
        rows = self.conn.execute(
            """SELECT s.slot_key FROM availability a JOIN slots s ON s.id = a.slot_id
               WHERE a.run_id = ? AND a.discord_id = ? ORDER BY s.start_utc""",
            (run_id, str(discord_id))).fetchall()
        return [r["slot_key"] for r in rows]

    def set_day_availability(self, run_id, discord_id, day, keys):
        """Replace this player's ticks for one local day (slot keys starting with 'YYYY-MM-DD')."""
        did = str(discord_id)
        with self.conn:
            self.conn.execute(
                """DELETE FROM availability WHERE run_id = ? AND discord_id = ?
                   AND slot_id IN (SELECT id FROM slots WHERE run_id = ? AND substr(slot_key, 1, 10) = ?)""",
                (run_id, did, run_id, day))
            for key in keys:
                if key[:10] != day:
                    continue
                row = self.conn.execute(
                    "SELECT id FROM slots WHERE run_id = ? AND slot_key = ?", (run_id, key)).fetchone()
                if row:
                    self.conn.execute(
                        "INSERT OR IGNORE INTO availability (run_id, discord_id, slot_id) VALUES (?, ?, ?)",
                        (run_id, did, row["id"]))

    def tick_rows(self, run_id):
        """One row per (slot, player) tick, with the player's roles and prog."""
        return self.conn.execute(
            """SELECT s.slot_key, p.discord_id, p.display_name, p.roles, p.prog_index
               FROM availability a
               JOIN slots s ON s.id = a.slot_id
               JOIN players p ON p.discord_id = a.discord_id
               WHERE a.run_id = ? ORDER BY s.start_utc, p.discord_id""", (run_id,)).fetchall()

    # ------------------------------------------------------------ limits

    def get_limits(self, run_id, discord_id, cfg):
        row = self.conn.execute(
            "SELECT * FROM limits WHERE run_id = ? AND discord_id = ?", (run_id, str(discord_id))).fetchone()
        if row:
            return {"max_per_day": row["max_per_day"], "max_per_run": row["max_per_run"],
                    "drop_checkpoints": row["drop_checkpoints"], "is_default": False}
        return {"max_per_day": cfg["default_max_per_day"], "max_per_run": cfg["default_max_per_run"],
                "drop_checkpoints": 0, "is_default": True}

    def set_limits(self, run_id, discord_id, cfg, *, max_per_day=None, max_per_run=None, drop_checkpoints=None):
        """Partial update on top of the current (or default) limits."""
        cur = self.get_limits(run_id, discord_id, cfg)
        new = (max_per_day if max_per_day is not None else cur["max_per_day"],
               max_per_run if max_per_run is not None else cur["max_per_run"],
               drop_checkpoints if drop_checkpoints is not None else cur["drop_checkpoints"])
        with self.conn:
            self.conn.execute(
                """INSERT INTO limits (run_id, discord_id, max_per_day, max_per_run, drop_checkpoints)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT (run_id, discord_id) DO UPDATE SET
                     max_per_day = excluded.max_per_day, max_per_run = excluded.max_per_run,
                     drop_checkpoints = excluded.drop_checkpoints""",
                (run_id, str(discord_id), *new))
        return self.get_limits(run_id, discord_id, cfg)

    # ------------------------------------------------------------ helpers

    def grant_helper(self, fight_id, discord_id, granted_by, now):
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO helper_grants (fight_id, discord_id, granted_by, granted_utc) "
                "VALUES (?, ?, ?, ?)", (fight_id, str(discord_id), str(granted_by), iso(now)))

    def revoke_helper(self, fight_id, discord_id):
        with self.conn:
            cur = self.conn.execute(
                "DELETE FROM helper_grants WHERE fight_id = ? AND discord_id = ?", (fight_id, str(discord_id)))
        return cur.rowcount > 0

    def helper_ids(self, fight_id):
        rows = self.conn.execute(
            "SELECT discord_id FROM helper_grants WHERE fight_id = ?", (fight_id,)).fetchall()
        return {r["discord_id"] for r in rows}

    # ------------------------------------------------------------ parties / drafts

    def previous_unmatched(self, run, day=None):
        """Players who ticked slots last time but got no party. Used for the solver's priority bonus.

        With `day`, "last time" is the latest earlier day of the same run that has a stored draft. Without
        one (or when no earlier day has a draft) it is the previous run of this fight.

        Derived from stored data only, so exporting the same day twice gives the same input
        (which keeps the draft fingerprint stable).
        """
        if day:
            prev = self.conn.execute(
                """SELECT MAX(substr(s.slot_key, 1, 10)) AS d FROM parties p JOIN slots s ON s.id = p.slot_id
                   WHERE p.run_id = ? AND substr(s.slot_key, 1, 10) < ?""", (run["id"], day)).fetchone()["d"]
            if prev:
                ticked = {r["discord_id"] for r in self.conn.execute(
                    """SELECT DISTINCT a.discord_id FROM availability a JOIN slots s ON s.id = a.slot_id
                       WHERE a.run_id = ? AND substr(s.slot_key, 1, 10) = ?""", (run["id"], prev))}
                seated = {r["discord_id"] for r in self.conn.execute(
                    """SELECT DISTINCT m.discord_id FROM party_members m JOIN parties p ON p.id = m.party_id
                       JOIN slots s ON s.id = p.slot_id
                       WHERE p.run_id = ? AND substr(s.slot_key, 1, 10) = ?""", (run["id"], prev))}
                return ticked - seated
        prev = self.conn.execute(
            """SELECT r.id FROM runs r
               WHERE r.guild_id = ? AND r.fight_id = ? AND r.id < ?
                 AND EXISTS (SELECT 1 FROM parties p WHERE p.run_id = r.id)
               ORDER BY r.id DESC LIMIT 1""", (run["guild_id"], run["fight_id"], run["id"])).fetchone()
        if not prev:
            return set()
        ticked = {r["discord_id"] for r in self.conn.execute(
            "SELECT DISTINCT discord_id FROM availability WHERE run_id = ?", (prev["id"],))}
        seated = {r["discord_id"] for r in self.conn.execute(
            """SELECT DISTINCT m.discord_id FROM party_members m JOIN parties p ON p.id = m.party_id
               WHERE p.run_id = ?""", (prev["id"],))}
        return ticked - seated

    def replace_draft(self, run_id, parties, notes, day=None):
        """Swap in a new draft. `parties`: [{slot_key, party_index, target_idx, members: [...]}].

        With `day`, only that day's draft parties (and notes) are replaced; other days are untouched.
        Refuses if any party already in scope is confirmed or done.
        """
        slot_ids = {r["slot_key"]: r["id"] for r in self.run_slots(run_id)}
        in_day = "AND slot_id IN (SELECT id FROM slots WHERE run_id = ? AND substr(slot_key, 1, 10) = ?)"
        scope = (run_id, run_id, day) if day else (run_id,)
        with self.conn:
            locked = self.conn.execute(
                f"SELECT COUNT(*) FROM parties WHERE run_id = ? AND status != 'draft' {in_day if day else ''}",
                scope).fetchone()[0]
            if locked:
                raise DraftConflict()
            self.conn.execute(
                f"DELETE FROM parties WHERE run_id = ? AND status = 'draft' {in_day if day else ''}", scope)
            if day:
                self.conn.execute("DELETE FROM draft_notes WHERE run_id = ? AND kind = 'slot' "
                                  "AND substr(ref, 1, 10) = ?", (run_id, day))
                self.conn.execute(
                    """DELETE FROM draft_notes WHERE run_id = ? AND kind = 'player' AND ref IN
                       (SELECT a.discord_id FROM availability a JOIN slots s ON s.id = a.slot_id
                        WHERE a.run_id = ? AND substr(s.slot_key, 1, 10) = ?)""", (run_id, run_id, day))
            else:
                self.conn.execute("DELETE FROM draft_notes WHERE run_id = ?", (run_id,))
            for pa in parties:
                cur = self.conn.execute(
                    "INSERT INTO parties (run_id, slot_id, party_index, status, target_idx) VALUES (?, ?, ?, 'draft', ?)",
                    (run_id, slot_ids[pa["slot_key"]], pa["party_index"], pa["target_idx"]))
                self.conn.executemany(
                    """INSERT INTO party_members (party_id, discord_id, role, is_helper, placed_down, distance)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    [(cur.lastrowid, m["discord_id"], m["role"], int(m["helper"]), int(m["placed_down"]),
                      int(m["distance"])) for m in pa["members"]])
            self.conn.executemany(
                "INSERT OR REPLACE INTO draft_notes (run_id, kind, ref, text) VALUES (?, ?, ?, ?)",
                [(run_id, kind, ref, text) for kind, ref, text in notes])

    def parties(self, run_id):
        return self.conn.execute(
            """SELECT p.*, s.slot_key, s.start_utc FROM parties p JOIN slots s ON s.id = p.slot_id
               WHERE p.run_id = ? ORDER BY s.start_utc, p.party_index""", (run_id,)).fetchall()

    def party_members(self, party_id):
        return self.conn.execute(
            "SELECT * FROM party_members WHERE party_id = ? ORDER BY role, discord_id", (party_id,)).fetchall()

    def player_parties(self, run_id, discord_id, statuses=("confirmed", "done")):
        marks = ",".join("?" for _ in statuses)
        return self.conn.execute(
            f"""SELECT s.slot_key, s.start_utc, s.minutes, p.party_index, p.status, m.role
                FROM party_members m JOIN parties p ON p.id = m.party_id JOIN slots s ON s.id = p.slot_id
                WHERE p.run_id = ? AND m.discord_id = ? AND p.status IN ({marks}) ORDER BY s.start_utc""",
            (run_id, str(discord_id), *statuses)).fetchall()

    def draft_notes(self, run_id):
        return self.conn.execute(
            "SELECT kind, ref, text FROM draft_notes WHERE run_id = ? ORDER BY kind, ref", (run_id,)).fetchall()
