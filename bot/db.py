"""SQLite access. Plain sqlite3 (synchronous): fine for a 20-60 person community bot.

Every method that changes data commits before returning. Times are UTC ISO strings.
"""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import ROLES

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"


GUILD_SETTING_KEYS = ("organizer_role_id", "reminder_channel_id", "parties_channel_id")


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


def _member_dict(m):
    return {"discord_id": m["discord_id"], "role": m["role"], "is_helper": int(m["is_helper"]),
            "placed_down": int(m["placed_down"]), "distance": int(m["distance"])}


class DB:
    def __init__(self, path=":memory:"):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._park_old_votes()
        self.conn.executescript(SCHEMA_PATH.read_text())
        self._migrate()
        self.conn.commit()

    def _park_old_votes(self):
        """Early skeletons had an unused `votes` table (yes/neutral/no, one row per slot and pair).
        Move it aside so schema.sql can create the append-only table; _migrate copies any rows across."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(votes)")}
        if cols and "vote" not in cols:
            with self.conn:
                self.conn.execute("ALTER TABLE votes RENAME TO votes_old")

    def _migrate(self):
        """Bring a database made before per-day cutoffs up to date. Safe to run on every start.

        Runs without run_days rows get one row per day (taken from their slots) using the run's old
        signup_deadline_utc as every day's cutoff. A day that already has parties in the database counts
        as exported, so old runs do not trigger export reminders.
        """
        if self.conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'votes_old'").fetchone():
            with self.conn:                               # yes -> like, no -> dislike
                self.conn.execute(
                    """INSERT INTO votes (id, run_id, slot_id, voter_id, target_id, vote, created_utc)
                       SELECT o.id, s.run_id, o.slot_id, o.voter_id, o.target_id,
                              CASE o.answer WHEN 'yes' THEN 'like' WHEN 'no' THEN 'dislike' ELSE 'neutral' END,
                              o.created_utc FROM votes_old o JOIN slots s ON s.id = o.slot_id""")
                self.conn.execute("DROP TABLE votes_old")
        from .characters import ensure_schema          # characters tables, and the move off the soft-link version
        ensure_schema(self.conn)
        with self.conn:
            self.conn.execute(
                """CREATE TABLE IF NOT EXISTS guild_settings (
                       guild_id TEXT PRIMARY KEY, organizer_role_id TEXT,
                       reminder_channel_id TEXT, parties_channel_id TEXT)""")
            for table, col, ddl in (("run_days", "snapshot_utc", "TEXT"),
                                    ("run_days", "snapshot_late", "INTEGER NOT NULL DEFAULT 0"),
                                    ("day_snapshots", "character_id", "INTEGER REFERENCES characters(id) ON DELETE SET NULL"),
                                    ("slots", "thread_id", "TEXT"), ("slots", "rating_posted_utc", "TEXT"),
                                    ("parties", "message_id", "TEXT"), ("parties", "ended_utc", "TEXT"),
                                    ("parties", "ended_by", "TEXT"), ("parties", "rating_posted_utc", "TEXT")):
                cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
                if col not in cols:                       # database made before this column existed
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
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
        """Parties each player has in this run on days strictly before `before_day`.

        Only approved parties count (confirmed or done); an unapproved draft is not counted.
        """
        rows = self.conn.execute(
            """SELECT m.discord_id, COUNT(*) AS n
               FROM party_members m JOIN parties p ON p.id = m.party_id JOIN slots s ON s.id = p.slot_id
               WHERE p.run_id = ? AND p.status IN ('confirmed', 'done') AND substr(s.slot_key, 1, 10) < ?
               GROUP BY m.discord_id""",
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

    def tick_rows(self, run_id, snapshots=True):
        """One row per (slot, player) tick, with the player's roles and prog.

        For a day that has a snapshot, name, roles and prog come from the snapshot taken at that day's
        cutoff, not from the player's current profile. `snapshots=False` always reads the live profile.
        """
        use = 1 if snapshots else 0
        return self.conn.execute(
            """SELECT s.slot_key, p.discord_id,
                      CASE WHEN ds.discord_id IS NOT NULL THEN ds.display_name ELSE p.display_name END AS display_name,
                      CASE WHEN ds.discord_id IS NOT NULL THEN ds.roles ELSE p.roles END AS roles,
                      CASE WHEN ds.discord_id IS NOT NULL THEN ds.prog_index ELSE p.prog_index END AS prog_index
               FROM availability a
               JOIN slots s ON s.id = a.slot_id
               JOIN players p ON p.discord_id = a.discord_id
               LEFT JOIN day_snapshots ds ON ds.run_id = a.run_id AND ds.discord_id = a.discord_id
                    AND ds.day = substr(s.slot_key, 1, 10) AND ? = 1
               WHERE a.run_id = ? ORDER BY s.start_utc, p.discord_id""", (use, run_id)).fetchall()

    # ------------------------------------------------------------ day snapshots

    def snapshot_rows(self, run_id, day):
        """{discord_id: row} of the day's frozen signups (empty when there is no snapshot)."""
        rows = self.conn.execute("SELECT * FROM day_snapshots WHERE run_id = ? AND day = ?", (run_id, day))
        return {r["discord_id"]: r for r in rows}

    def take_snapshot(self, run_id, day, cfg, now, late=False):
        """Freeze the day's signups: every player with a tick that day, with name, roles, prog and the
        effective limits (defaults included). Never overwrites: returns False if one exists already.
        A day nobody signed up for still gets the marker, so taking it again is a no-op.
        """
        with self.conn:
            if self.conn.execute("SELECT snapshot_utc FROM run_days WHERE run_id = ? AND day = ?",
                                 (run_id, day)).fetchone()["snapshot_utc"]:
                return False
            ids = [r["discord_id"] for r in self.conn.execute(
                """SELECT DISTINCT a.discord_id FROM availability a JOIN slots s ON s.id = a.slot_id
                   WHERE a.run_id = ? AND substr(s.slot_key, 1, 10) = ? ORDER BY a.discord_id""", (run_id, day))]
            for did in ids:
                p = self.get_player(did)
                lim = self.get_limits(run_id, did, cfg)
                self.conn.execute(
                    """INSERT INTO day_snapshots (run_id, day, discord_id, display_name, roles, prog_index,
                                                  max_per_day, max_per_run, drop_checkpoints, taken_utc, late,
                                                  character_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (run_id, day, did, p["display_name"], p["roles"], p["prog_index"], lim["max_per_day"],
                     lim["max_per_run"], lim["drop_checkpoints"], iso(now), int(late),
                     self._live_character_id(run_id, day, did)))
            self.conn.execute("UPDATE run_days SET snapshot_utc = ?, snapshot_late = ? WHERE run_id = ? AND day = ?",
                              (iso(now), int(late), run_id, day))
        return True

    def clear_snapshot(self, run_id, day):
        with self.conn:
            self.conn.execute("DELETE FROM day_snapshots WHERE run_id = ? AND day = ?", (run_id, day))
            self.conn.execute("UPDATE run_days SET snapshot_utc = NULL, snapshot_late = 0 "
                              "WHERE run_id = ? AND day = ?", (run_id, day))

    def snapshot_candidates(self, now):
        """Days whose cutoff has passed, that were never exported, and have no snapshot yet."""
        return self.conn.execute(
            """SELECT d.*, r.guild_id, r.name AS run_name FROM run_days d JOIN runs r ON r.id = d.run_id
               WHERE r.status IN ('open', 'closed') AND d.snapshot_utc IS NULL AND d.exported_utc IS NULL
                 AND d.cutoff_utc <= ? ORDER BY d.cutoff_utc, r.id, d.day""", (iso(now),)).fetchall()

    # ------------------------------------------------------------ characters on signups

    def _live_character_id(self, run_id, day, discord_id):
        """The character a player would bring on `day` right now: their pick, else their only character."""
        row = self.conn.execute(
            "SELECT character_id FROM signup_characters WHERE run_id = ? AND day = ? AND discord_id = ?",
            (run_id, day, str(discord_id))).fetchone()
        if row and row["character_id"]:
            return row["character_id"]
        own = self.conn.execute("SELECT id FROM characters WHERE discord_id = ?", (str(discord_id),)).fetchall()
        return own[0]["id"] if len(own) == 1 else None

    def chosen_character(self, run_id, day, discord_id):
        """The `characters` row a player brings on a day, or None. Once the day's snapshot exists the choice
        is the one frozen in it (so adding an alt later cannot change a day that already closed)."""
        snap = self.conn.execute(
            "SELECT character_id FROM day_snapshots WHERE run_id = ? AND day = ? AND discord_id = ?",
            (run_id, day, str(discord_id))).fetchone()
        char_id = snap["character_id"] if snap else self._live_character_id(run_id, day, discord_id)
        if not char_id:
            return None
        return self.conn.execute("SELECT * FROM characters WHERE id = ?", (char_id,)).fetchone()

    def set_signup_character(self, run_id, days, discord_id, character_id):
        with self.conn:
            self.conn.executemany(
                """INSERT INTO signup_characters (run_id, day, discord_id, character_id) VALUES (?, ?, ?, ?)
                   ON CONFLICT (run_id, day, discord_id) DO UPDATE SET character_id = excluded.character_id""",
                [(run_id, day, str(discord_id), character_id) for day in days])

    def party_member_characters(self, party_id):
        """{discord_id: characters row or None} for a party, using the character each member brought that day."""
        party = self.party(party_id)
        day = party["slot_key"][:10]
        return {m["discord_id"]: self.chosen_character(party["run_id"], day, m["discord_id"])
                for m in self.party_members(party_id)}

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

        With `day`, "last time" is the latest earlier day of the same run that has approved (confirmed) parties.
        Without one (or when no earlier day has any) it is the previous run of this fight that has some.

        Derived from stored data only, so exporting the same day twice gives the same input
        (which keeps the draft fingerprint stable).
        """
        if day:
            prev = self.conn.execute(
                """SELECT MAX(substr(s.slot_key, 1, 10)) AS d FROM parties p JOIN slots s ON s.id = p.slot_id
                   WHERE p.run_id = ? AND p.status IN ('confirmed', 'done')
                     AND substr(s.slot_key, 1, 10) < ?""", (run["id"], day)).fetchone()["d"]
            if prev:
                ticked = {r["discord_id"] for r in self.conn.execute(
                    """SELECT DISTINCT a.discord_id FROM availability a JOIN slots s ON s.id = a.slot_id
                       WHERE a.run_id = ? AND substr(s.slot_key, 1, 10) = ?""", (run["id"], prev))}
                seated = {r["discord_id"] for r in self.conn.execute(
                    """SELECT DISTINCT m.discord_id FROM party_members m JOIN parties p ON p.id = m.party_id
                       JOIN slots s ON s.id = p.slot_id
                       WHERE p.run_id = ? AND p.status IN ('confirmed', 'done')
                         AND substr(s.slot_key, 1, 10) = ?""", (run["id"], prev))}
                return ticked - seated
        prev = self.conn.execute(
            """SELECT r.id FROM runs r
               WHERE r.guild_id = ? AND r.fight_id = ? AND r.id < ?
                 AND EXISTS (SELECT 1 FROM parties p WHERE p.run_id = r.id AND p.status IN ('confirmed', 'done'))
               ORDER BY r.id DESC LIMIT 1""", (run["guild_id"], run["fight_id"], run["id"])).fetchone()
        if not prev:
            return set()
        ticked = {r["discord_id"] for r in self.conn.execute(
            "SELECT DISTINCT discord_id FROM availability WHERE run_id = ?", (prev["id"],))}
        seated = {r["discord_id"] for r in self.conn.execute(
            """SELECT DISTINCT m.discord_id FROM party_members m JOIN parties p ON p.id = m.party_id
               WHERE p.run_id = ? AND p.status IN ('confirmed', 'done')""", (prev["id"],))}
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

    # ------------------------------------------------------------ approving and posting parties

    def day_party_counts(self, run_id, day):
        """(draft parties, approved parties) on one day."""
        row = self.conn.execute(
            """SELECT COALESCE(SUM(p.status = 'draft'), 0) AS d, COALESCE(SUM(p.status != 'draft'), 0) AS c
               FROM parties p JOIN slots s ON s.id = p.slot_id
               WHERE p.run_id = ? AND substr(s.slot_key, 1, 10) = ?""", (run_id, day)).fetchone()
        return row["d"], row["c"]

    def days_with_draft(self, run_id):
        """Days (sorted) that have at least one party still in draft."""
        rows = self.conn.execute(
            """SELECT DISTINCT substr(s.slot_key, 1, 10) AS day FROM parties p JOIN slots s ON s.id = p.slot_id
               WHERE p.run_id = ? AND p.status = 'draft' ORDER BY day""", (run_id,))
        return [r["day"] for r in rows]

    def confirm_day(self, run_id, day):
        """Draft -> confirmed for one day's parties. Returns how many parties changed."""
        with self.conn:
            drafts = self.conn.execute(
                """SELECT * FROM parties WHERE run_id = ? AND status = 'draft'
                   AND slot_id IN (SELECT id FROM slots WHERE run_id = ? AND substr(slot_key, 1, 10) = ?)""",
                (run_id, run_id, day)).fetchall()
            for p in drafts:                       # freeze the solver's version before anyone edits it
                self.conn.execute(
                    """INSERT OR IGNORE INTO party_originals (party_id, run_id, slot_id, taken_utc, target_idx, members_json)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (p["id"], run_id, p["slot_id"], iso(datetime.now(timezone.utc)), p["target_idx"],
                     json.dumps([_member_dict(m) for m in self.party_members(p["id"])])))
            cur = self.conn.execute(
                """UPDATE parties SET status = 'confirmed' WHERE run_id = ? AND status = 'draft'
                   AND slot_id IN (SELECT id FROM slots WHERE run_id = ? AND substr(slot_key, 1, 10) = ?)""",
                (run_id, run_id, day))
        return cur.rowcount

    def approved_slots(self, run_id, day):
        """Slots of the day that have approved parties, each with its parties and members, in time order."""
        out = []
        for s in self.slots_on(run_id, day):
            parties = self.conn.execute(
                "SELECT * FROM parties WHERE slot_id = ? AND status IN ('confirmed', 'done') ORDER BY party_index",
                (s["id"],)).fetchall()
            if parties:
                out.append((s, [(p, self.party_members(p["id"])) for p in parties]))
        return out

    def approved_parties_of_slot(self, slot_id):
        return self.conn.execute(
            "SELECT * FROM parties WHERE slot_id = ? AND status IN ('confirmed', 'done') ORDER BY party_index",
            (slot_id,)).fetchall()

    def set_slot_thread(self, slot_id, thread_id):
        with self.conn:
            self.conn.execute("UPDATE slots SET thread_id = ? WHERE id = ?", (str(thread_id), slot_id))

    def set_party_message(self, party_id, message_id):
        with self.conn:
            self.conn.execute("UPDATE parties SET message_id = ? WHERE id = ?", (str(message_id), party_id))

    def mark_rating_posted(self, slot_id, now):
        with self.conn:
            self.conn.execute("UPDATE slots SET rating_posted_utc = ? WHERE id = ?", (iso(now), slot_id))

    def guild_setting(self, guild_id, key, default=None):
        """A per-server setting set with /settings, or `default` (the .env / config.json value) when unset."""
        if key not in GUILD_SETTING_KEYS:
            raise KeyError(key)
        row = self.conn.execute(f"SELECT {key} FROM guild_settings WHERE guild_id = ?", (str(guild_id),)).fetchone()
        return row[0] if row and row[0] else default

    def set_guild_setting(self, guild_id, key, value):
        """Set one per-server setting; `None` clears it so the default applies again."""
        if key not in GUILD_SETTING_KEYS:
            raise KeyError(key)
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (str(guild_id),))
            self.conn.execute(f"UPDATE guild_settings SET {key} = ? WHERE guild_id = ?",
                              (None if value is None else str(value), str(guild_id)))

    def move_slot_start(self, slot_id, start_utc):
        """Debug only: move a slot's start (used by /debug complete to make a slot count as ended)."""
        with self.conn:
            self.conn.execute("UPDATE slots SET start_utc = ? WHERE id = ?", (iso(start_utc), slot_id))

    def slots_awaiting_rating(self):
        """Slots with a thread, approved parties and no "Rate your party" message yet (the caller checks the end)."""
        return self.conn.execute(
            """SELECT s.* FROM slots s WHERE s.thread_id IS NOT NULL AND s.rating_posted_utc IS NULL
                 AND EXISTS (SELECT 1 FROM parties p WHERE p.slot_id = s.id AND p.status IN ('confirmed', 'done'))
               ORDER BY s.start_utc, s.id""").fetchall()

    def party(self, party_id):
        """A party row joined with its slot (start, minutes, actual_minutes, key)."""
        return self.conn.execute(
            """SELECT p.*, s.slot_key, s.start_utc, s.minutes, s.actual_minutes, s.thread_id
               FROM parties p JOIN slots s ON s.id = p.slot_id WHERE p.id = ?""", (party_id,)).fetchone()

    def member_parties(self, discord_id):
        """Every approved party the player is in, newest first (all runs), joined with its slot."""
        return self.conn.execute(
            """SELECT p.id AS party_id, p.run_id, p.party_index, p.status, s.id AS slot_id, s.slot_key,
                      s.start_utc, s.minutes, s.actual_minutes, p.ended_utc
               FROM party_members m JOIN parties p ON p.id = m.party_id JOIN slots s ON s.id = p.slot_id
               WHERE m.discord_id = ? AND p.status IN ('confirmed', 'done') ORDER BY s.start_utc DESC, p.id""",
            (str(discord_id),)).fetchall()

    # ------------------------------------------------------------ party ended, edits, standby

    def log_change(self, party, actor_id, kind, now, *, player_id=None, other_player_id=None, reason_code=None,
                   reason_text=None, warnings=None, detail=None):
        """Append one row to party_changes (call inside `with self.conn`). Returns its id."""
        cur = self.conn.execute(
            """INSERT INTO party_changes (party_id, run_id, slot_id, at_utc, actor_id, kind, player_id,
                                          other_player_id, reason_code, reason_text, warnings_json, detail_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (party["id"], party["run_id"], party["slot_id"], iso(now), str(actor_id), kind,
             None if player_id is None else str(player_id), None if other_player_id is None else str(other_player_id),
             reason_code, reason_text, json.dumps(warnings) if warnings else None,
             json.dumps(detail) if detail else None))
        return cur.lastrowid

    def party_changes(self, party_id):
        return self.conn.execute("SELECT * FROM party_changes WHERE party_id = ? ORDER BY id", (party_id,)).fetchall()

    def change(self, change_id):
        return self.conn.execute("SELECT * FROM party_changes WHERE id = ?", (change_id,)).fetchone()

    def party_original(self, party_id):
        row = self.conn.execute("SELECT * FROM party_originals WHERE party_id = ?", (party_id,)).fetchone()
        return None if row is None else {"target_idx": row["target_idx"], "members": json.loads(row["members_json"])}

    def end_party(self, party_id, now, by, minutes, actor_id=None, override=False):
        """Mark a party done and log `minutes` of play for each member. `by` is a discord id or 'auto'.

        Returns False when it was already ended. `minutes` is the time played (see parties.minutes_played).
        """
        party = self.party(party_id)
        if party is None or party["ended_utc"] or party["status"] not in ("confirmed", "done"):
            return False
        with self.conn:
            self.conn.execute("UPDATE parties SET status = 'done', ended_utc = ?, ended_by = ? WHERE id = ?",
                              (iso(now), str(by), party_id))
            self.conn.executemany(
                "INSERT OR REPLACE INTO play_log (slot_id, discord_id, minutes) VALUES (?, ?, ?)",
                [(party["slot_id"], m["discord_id"], minutes) for m in self.party_members(party_id)])
            if by != "auto":
                self.log_change(party, actor_id or by, "end_override" if override else "ended", now,
                                detail={"minutes": minutes})
        return True

    def undo_end_party(self, party_id, now, actor_id):
        """Reopen an ended party: back to confirmed, its members' play_log rows removed, rating button reset."""
        party = self.party(party_id)
        if party is None or not party["ended_utc"]:
            return False
        with self.conn:
            self.conn.execute(
                "UPDATE parties SET status = 'confirmed', ended_utc = NULL, ended_by = NULL, rating_posted_utc = NULL "
                "WHERE id = ?", (party_id,))
            self.conn.execute(
                "DELETE FROM play_log WHERE slot_id = ? AND discord_id IN "
                "(SELECT discord_id FROM party_members WHERE party_id = ?)", (party["slot_id"], party_id))
            self.log_change(party, actor_id, "end_undo", now)
        return True

    def mark_party_rating_posted(self, party_id, now):
        with self.conn:
            self.conn.execute("UPDATE parties SET rating_posted_utc = ? WHERE id = ?", (iso(now), party_id))

    def parties_to_auto_end(self):
        """Confirmed parties with their slot's timing; the caller checks whether the slot has run out."""
        return self.conn.execute(
            """SELECT p.id, s.start_utc, s.minutes, s.actual_minutes FROM parties p JOIN slots s ON s.id = p.slot_id
               WHERE p.status = 'confirmed' ORDER BY s.start_utc, p.id""").fetchall()

    def standby_pool(self, slot_id):
        """Players who could fill in for a slot: ticked it (or were put back on standby) and sit in no approved party then."""
        rows = self.conn.execute(
            """SELECT discord_id FROM availability WHERE slot_id = ?
               UNION SELECT discord_id FROM standby WHERE slot_id = ?""", (slot_id, slot_id)).fetchall()
        seated = {r[0] for r in self.conn.execute(
            """SELECT m.discord_id FROM party_members m JOIN parties p ON p.id = m.party_id
               WHERE p.slot_id = ? AND p.status IN ('confirmed', 'done')""", (slot_id,))}
        return sorted(r[0] for r in rows if r[0] not in seated)

    def swap_member(self, party_id, out_id, in_id, in_role, actor_id, now, *, reason_code, reason_text,
                    warnings=None, is_helper=False, source="panel"):
        """Replace one member of an approved party. Returns the party_changes id.

        The player taken out goes back to standby, loses their play_log row (and any rating they gave or got
        for that slot, since they were not there); the player put in leaves standby and, if the party already
        ended, gets the same minutes as the others.
        """
        party = self.party(party_id)
        members = {m["discord_id"]: m for m in self.party_members(party_id)}
        out_id, in_id = str(out_id), str(in_id)
        if party is None or out_id not in members or in_id in members:
            raise ValueError("swap does not apply to this party")
        with self.conn:
            minutes = self.conn.execute(
                "SELECT MAX(minutes) FROM play_log WHERE slot_id = ? AND discord_id IN "
                "(SELECT discord_id FROM party_members WHERE party_id = ?)", (party["slot_id"], party_id)).fetchone()[0]
            self.conn.execute("DELETE FROM party_members WHERE party_id = ? AND discord_id = ?", (party_id, out_id))
            self.conn.execute(
                "INSERT INTO party_members (party_id, discord_id, role, is_helper, placed_down, distance) "
                "VALUES (?, ?, ?, ?, 0, 0)", (party_id, in_id, in_role, int(is_helper)))
            self.conn.execute("DELETE FROM standby WHERE slot_id = ? AND discord_id = ?", (party["slot_id"], in_id))
            self.conn.execute("INSERT OR IGNORE INTO standby (slot_id, discord_id) VALUES (?, ?)",
                              (party["slot_id"], out_id))
            self.conn.execute("DELETE FROM play_log WHERE slot_id = ? AND discord_id = ?", (party["slot_id"], out_id))
            self.conn.execute("DELETE FROM votes WHERE slot_id = ? AND (voter_id = ? OR target_id = ?)",
                              (party["slot_id"], out_id, out_id))
            if party["ended_utc"] and minutes is not None:
                self.conn.execute("INSERT OR REPLACE INTO play_log (slot_id, discord_id, minutes) VALUES (?, ?, ?)",
                                  (party["slot_id"], in_id, minutes))
            return self.log_change(party, actor_id, "swap", now, player_id=out_id, other_player_id=in_id,
                                   reason_code=reason_code, reason_text=reason_text, warnings=warnings,
                                   detail={"out_role": members[out_id]["role"], "in_role": in_role, "source": source})

    def set_member_role(self, party_id, discord_id, role, actor_id, now, *, reason_code="other", reason_text="",
                        warnings=None, source="panel"):
        party = self.party(party_id)
        old = {m["discord_id"]: m["role"] for m in self.party_members(party_id)}.get(str(discord_id))
        if party is None or old is None:
            raise ValueError("that player is not in the party")
        with self.conn:
            self.conn.execute("UPDATE party_members SET role = ? WHERE party_id = ? AND discord_id = ?",
                              (role, party_id, str(discord_id)))
            return self.log_change(party, actor_id, "role", now, player_id=discord_id, reason_code=reason_code,
                                   reason_text=reason_text, warnings=warnings,
                                   detail={"old_role": old, "new_role": role, "source": source})

    # ------------------------------------------------------------ votes (append-only)

    def add_vote(self, run_id, slot_id, voter_id, target_id, vote, now):
        with self.conn:
            self.conn.execute(
                "INSERT INTO votes (run_id, slot_id, voter_id, target_id, vote, created_utc) VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, slot_id, str(voter_id), str(target_id), vote, iso(now)))

    def latest_votes(self, as_of, since=None):
        """The current vote of every (voter, target) pair as of `as_of`: the latest row created at or before
        it (ties broken by id), neutral included. `since` drops pairs whose latest vote is older than it."""
        rows = self.conn.execute(
            """SELECT v.* FROM votes v
               WHERE v.created_utc <= ? AND v.id = (
                   SELECT v2.id FROM votes v2 WHERE v2.voter_id = v.voter_id AND v2.target_id = v.target_id
                     AND v2.created_utc <= ? ORDER BY v2.created_utc DESC, v2.id DESC LIMIT 1)
               ORDER BY v.voter_id, v.target_id""", (iso(as_of), iso(as_of))).fetchall()
        return [r for r in rows if since is None or r["created_utc"] >= iso(since)]

    def latest_vote(self, voter_id, target_id, as_of):
        row = self.conn.execute(
            """SELECT vote FROM votes WHERE voter_id = ? AND target_id = ? AND created_utc <= ?
               ORDER BY created_utc DESC, id DESC LIMIT 1""", (str(voter_id), str(target_id), iso(as_of))).fetchone()
        return row["vote"] if row else None
