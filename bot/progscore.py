"""Prog scoring glue: prog point files, stored scorer state per character, and the two cleanup modes.

No Discord imports. The scorer itself (prog_scorer.py, progpoints.py, fflogs_api.py) is a standalone,
stdlib-only package that is copied here unchanged.

Modes for points tagged `cleanup` (see `effective_highest`):
  normal       the tag is ignored: the highest point reached in any pull counts.
  consistent   a cleanup point only counts when the tag check passed for it (reached in at least `min` of the
               most recent `of` pulls, 3 of 5 unless the prog point file says otherwise). A pull that went on to
               the next non-cleanup point counts as having reached the cleanup point too.
"""
import json
from datetime import datetime, timedelta, timezone

from . import prog_scorer, progpoints
from .config import ROOT
from .db import iso
from .fflogs_api import FFLogsError

SCHEMA = """
CREATE TABLE IF NOT EXISTS prog_scores (
    character_id   INTEGER NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    encounter      INTEGER NOT NULL,
    state          TEXT NOT NULL,
    updated_utc    TEXT NOT NULL,
    confirmed_idx  INTEGER,          -- highest point ever confirmed; never goes down
    confirmed_mode TEXT,             -- the scoring mode that confirmed it
    confirmed_utc  TEXT,             -- when that pull happened
    latest_idx     INTEGER,          -- highest point of the latest scoring run (can be below confirmed)
    latest_mode    TEXT,
    latest_utc     TEXT,             -- when the latest run happened
    PRIMARY KEY (character_id, encounter)
);
"""
_NEW_COLUMNS = ("confirmed_idx INTEGER", "confirmed_mode TEXT", "confirmed_utc TEXT",
                "latest_idx INTEGER", "latest_mode TEXT", "latest_utc TEXT")


def ensure_schema(conn):
    conn.executescript(SCHEMA)
    have = {r[1] for r in conn.execute("PRAGMA table_info(prog_scores)")}
    for col in _NEW_COLUMNS:                      # tables made by the first version lack these
        if col.split()[0] not in have:
            conn.execute(f"ALTER TABLE prog_scores ADD COLUMN {col}")
    conn.commit()


MODES = ("normal", "consistent")
CLEANUP_TAG = "cleanup"

ProgPointError, ScorerError = progpoints.ProgPointError, prog_scorer.ScorerError


# ------------------------------------------------------------------ prog point files

def points_path(cfg, fight):
    """prog_points/<short name>.json next to config.json (folder = cfg['prog_points_dir'])."""
    folder = ROOT / cfg.get("prog_points_dir", "prog_points")
    return folder / f"{fight['short_name'] or fight['name']}.json"


def load_doc(cfg, fight):
    """The fight's prog point document. Raises ProgPointError with a readable message."""
    path = points_path(cfg, fight)
    if not path.exists():
        raise ProgPointError(f"There is no prog point file for {fight['name']} (expected {path.name}).")
    return with_clear(progpoints.load_points(path))


def with_clear(doc):
    """Every fight ends with a 'Clear' point that only checks for a kill. Added unless the file has one."""
    pts = doc["points"]
    if not pts or any(p["name"].lower() == "clear" for p in pts):
        return doc
    last = pts[-1]
    return progpoints.load_points({**doc, "points": pts + [
        {"idx": last["idx"] + 1, "phase": last["phase"], "name": "Clear", "mode": "all",
         "conditions": [{"type": "kill"}], "tags": []}]})


def _rows(doc):
    return [(p["idx"], p["phase"], p["name"]) for p in sorted(doc["points"], key=lambda p: p["idx"])]


def checkpoints_match(db, fight_id, doc):
    """Do the fight's checkpoints equal the prog point file? Scorer idx values are only meaningful if so."""
    return [(c["idx"], c["phase"], c["name"]) for c in db.checkpoints(fight_id)] == _rows(doc)


def sync_checkpoints(db, fight_id, doc):
    """Make the fight's checkpoints equal the prog point file. Returns how many players had a prog set.

    Old prog_index values mean something else under the new numbering, so they are cleared (the player
    picks again in /availability, or the scorer sets it)."""
    with db.conn:
        db.conn.execute("DELETE FROM checkpoints WHERE fight_id = ?", (fight_id,))
        db.conn.executemany("INSERT INTO checkpoints (fight_id, idx, phase, name) VALUES (?, ?, ?, ?)",
                            [(fight_id, *r) for r in _rows(doc)])
        n = db.conn.execute("SELECT COUNT(*) FROM players WHERE prog_index IS NOT NULL").fetchone()[0]
        db.conn.execute("UPDATE players SET prog_index = NULL")
    return n


def players_with_prog(db):
    return db.conn.execute("SELECT COUNT(*) FROM players WHERE prog_index IS NOT NULL").fetchone()[0]


# ------------------------------------------------------------------ stored state

def load_state(db, char_id, encounter):
    row = db.conn.execute("SELECT state FROM prog_scores WHERE character_id = ? AND encounter = ?",
                          (char_id, encounter)).fetchone()
    return json.loads(row["state"]) if row else None


def save_state(db, char_id, encounter, state, now):
    with db.conn:
        db.conn.execute("""INSERT INTO prog_scores (character_id, encounter, state, updated_utc) VALUES (?, ?, ?, ?)
                           ON CONFLICT (character_id, encounter)
                           DO UPDATE SET state = excluded.state, updated_utc = excluded.updated_utc""",
                        (char_id, encounter, json.dumps(state), iso(now)))


# ------------------------------------------------------------------ the two modes

def adjusted_reached(doc, reached, tag=CLEANUP_TAG):
    """A pull that got to the next point without the cleanup tag also counts as having reached the cleanup
    point before it (you only move on once the cleanup is done)."""
    out, top = set(reached), max(reached, default=-1)
    for pt in doc["points"]:
        if tag in pt["tags"]:
            nxt = min((q["idx"] for q in doc["points"] if q["idx"] > pt["idx"] and tag not in q["tags"]), default=None)
            if nxt is not None and top >= nxt:
                out.add(pt["idx"])
    return sorted(out)


def cleanup_report(doc, state, tag=CLEANUP_TAG):
    """The tag report for `tag`, recomputed from the stored pulls with the next-point rule above.
    -> {'consistent': bool, 'points': [{idx, name, phase, reached, of, need, ok}]} or None if nothing is tagged."""
    pulls = [{**p, "reached": adjusted_reached(doc, p["reached"], tag)} for p in state["pulls"].values() if "reached" in p]
    return progpoints.tag_report(doc, pulls).get(tag)


def failed_cleanup_points(doc, state, tag=CLEANUP_TAG):
    """idx of the tagged points whose consistency check failed."""
    rep = cleanup_report(doc, state, tag)
    return {p["idx"] for p in rep["points"] if not p["ok"]} if rep else set()


def effective_highest(doc, state, mode="normal"):
    """{'idx', 'phase', 'name', 'pull'} of the player's highest point under `mode`, or None if none was reached.

    'consistent' skips cleanup points that failed the consistency check; everything else counts as usual,
    including points after a failed cleanup point."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    if mode == "normal":
        return state.get("highest")
    skip = failed_cleanup_points(doc, state)
    info = {p["idx"]: p for p in doc["points"]}
    best = None
    for key, pull in state["pulls"].items():
        for idx in pull.get("reached", []):
            if idx not in skip and (best is None or idx > best["idx"]):
                best = {"idx": idx, "phase": info[idx]["phase"], "name": info[idx]["name"], "pull": key}
    return best


def cleanup_lines(doc, state):
    """Human-readable consistency of the cleanup points, one per line (empty when none are tagged)."""
    rep = cleanup_report(doc, state)
    return [f"{'✅' if p['ok'] else '❌'} {p['phase']} {p['name']}: reached in {p['reached']} of the last "
            f"{p['of']} pulls (needs {p['need']})" for p in rep["points"]] if rep else []


# ------------------------------------------------------------------ confirmed prog (never goes down)

def get_scores(db, char_id, encounter):
    return db.conn.execute("SELECT * FROM prog_scores WHERE character_id = ? AND encounter = ?",
                           (char_id, encounter)).fetchone()


def record_result(db, char_id, encounter, chosen, mode, state, now):
    """Store this run's highest as `latest`, and raise `confirmed` if it beats it. Returns (row, raised)."""
    row = get_scores(db, char_id, encounter)
    raised = bool(chosen) and (row["confirmed_idx"] is None or chosen["idx"] > row["confirmed_idx"])
    with db.conn:
        db.conn.execute("UPDATE prog_scores SET latest_idx = ?, latest_mode = ?, latest_utc = ? "
                        "WHERE character_id = ? AND encounter = ?",
                        (chosen["idx"] if chosen else None, mode, iso(now), char_id, encounter))
        if raised:
            at_ms = (state["pulls"].get(chosen.get("pull")) or {}).get("at")
            when = datetime.fromtimestamp(at_ms / 1000, timezone.utc) if at_ms else now
            db.conn.execute("UPDATE prog_scores SET confirmed_idx = ?, confirmed_mode = ?, confirmed_utc = ? "
                            "WHERE character_id = ? AND encounter = ?",
                            (chosen["idx"], mode, iso(when), char_id, encounter))
    return get_scores(db, char_id, encounter), raised


# ------------------------------------------------------------------ scoring

async def recent_codes(ctx, char, count, hours):
    """Codes of the character's newest reports, at most `count`, and only those that started within `hours`
    (hours 0/None = no time limit)."""
    since = (ctx.now() - timedelta(hours=hours)).timestamp() * 1000 if hours else None
    codes, page = [], 1
    while len(codes) < count:
        limit = min(count - len(codes), 100)
        if char["fflogs_id"]:
            data = await ctx.fflogs.query(prog_scorer.CHAR_ID_QUERY, {"id": int(char["fflogs_id"]), "limit": limit, "page": page})
        else:
            data = await ctx.fflogs.query(prog_scorer.CHAR_QUERY, {
                "name": char["name"], "server": char["world"].lower(),
                "region": ctx.cfg.get("lodestone_region", "na").upper(), "limit": limit, "page": page})
        found = ((data or {}).get("characterData") or {}).get("character")
        if not found:
            raise FFLogsError(f"Character {char['name']} not found on FFLogs.")
        reports = found["recentReports"]
        old = False
        for r in reports["data"]:                       # newest first
            if since is not None and r["startTime"] < since:
                old = True
                break
            codes.append(r["code"])
        if old or not reports.get("has_more_pages") or not reports["data"]:
            break
        page += 1
    return codes[:count]


async def score_character(ctx, char, doc, *, count=10, hours=24, reset=False, progress=None):
    """Score a character's recent logs, keeping the stored state up to date after every report.

    Only reports from the last `hours` hours are read (0 = no limit; `count` always applies). Returns
    (state, restarted_reason or None). Raises FFLogsError (FFLogs trouble) or ProgPointError."""
    enc = prog_scorer.resolve_encounter(doc, None)
    state, restarted = (None if reset else load_state(ctx.db, char["id"], enc)), None
    if state is not None:
        try:
            prog_scorer.check_state(state, doc, char["name"], enc)
        except ScorerError as e:                  # prog points edited, or the character was renamed
            state, restarted = None, str(e)
    if state is None:
        state = prog_scorer.new_state(doc, char["name"], enc)
    codes = await recent_codes(ctx, char, count, hours)
    if codes:                                     # (an empty list would make the scorer fetch its own)
        state = await prog_scorer.score_player_async(
            ctx.fflogs.query, doc, state, char["name"], enc, reports=codes, progress=progress,
            server=char["world"].lower(),
            checkpoint=lambda st: save_state(ctx.db, char["id"], enc, st, ctx.now()))
    save_state(ctx.db, char["id"], enc, state, ctx.now())
    return state, restarted
