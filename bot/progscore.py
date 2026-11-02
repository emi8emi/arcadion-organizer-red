"""Prog scoring glue: prog point files, stored scorer state per character, and the two cleanup modes.

No Discord imports. The scorer itself (prog_scorer.py, progpoints.py, fflogs_api.py) is a standalone,
stdlib-only package that is copied here unchanged.

Modes for points tagged `cleanup` (see `effective_highest`):
  normal       the tag is ignored: the highest point reached in any pull counts.
  consistent   a cleanup point only counts when the tag check passed for it (reached in at least `min` of the
               most recent `of` pulls, 3 of 5 unless the prog point file says otherwise).
"""
import json

from . import prog_scorer, progpoints
from .config import ROOT
from .db import iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS prog_scores (
    character_id INTEGER NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    encounter    INTEGER NOT NULL,
    state        TEXT NOT NULL,
    updated_utc  TEXT NOT NULL,
    PRIMARY KEY (character_id, encounter)
);
"""

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
    return progpoints.load_points(path)


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

def failed_cleanup_points(state, tag=CLEANUP_TAG):
    """idx of the tagged points whose consistency check failed."""
    return {p["idx"] for p in (state.get("tags") or {}).get(tag, {}).get("points", []) if not p["ok"]}


def effective_highest(doc, state, mode="normal"):
    """{'idx', 'phase', 'name', 'pull'} of the player's highest point under `mode`, or None if none was reached.

    'consistent' skips cleanup points that failed the consistency check; everything else counts as usual,
    including points after a failed cleanup point."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    if mode == "normal":
        return state.get("highest")
    skip = failed_cleanup_points(state)
    info = {p["idx"]: p for p in doc["points"]}
    best = None
    for key, pull in state["pulls"].items():
        for idx in pull.get("reached", []):
            if idx not in skip and (best is None or idx > best["idx"]):
                best = {"idx": idx, "phase": info[idx]["phase"], "name": info[idx]["name"], "pull": key}
    return best


def cleanup_lines(state):
    """Human-readable consistency of the cleanup points, one per line ('' list when none are tagged)."""
    pts = (state.get("tags") or {}).get(CLEANUP_TAG, {}).get("points", [])
    return [f"{'✅' if p['ok'] else '❌'} {p['phase']} {p['name']}: reached in {p['reached']} of the last "
            f"{p['of']} pulls (needs {p['need']})" for p in pts]


# ------------------------------------------------------------------ scoring

async def score_character(ctx, char, doc, *, count=10, reset=False, progress=None):
    """Score a character's recent logs, keeping the stored state up to date after every report.

    Returns (state, restarted_reason or None). Raises FFLogsError (FFLogs trouble) or ProgPointError."""
    enc = prog_scorer.resolve_encounter(doc, None)
    state, restarted = (None if reset else load_state(ctx.db, char["id"], enc)), None
    if state is not None:
        try:
            prog_scorer.check_state(state, doc, char["name"], enc)
        except ScorerError as e:                  # prog points edited, or the character was renamed
            state, restarted = None, str(e)
    if state is None:
        state = prog_scorer.new_state(doc, char["name"], enc)
    kw = {"server": char["world"].lower()}      # slug for the name lookup; also tells same-name players apart
    if char["fflogs_id"]:
        kw["fflogs_id"] = char["fflogs_id"]
    else:
        kw["region"] = ctx.cfg.get("lodestone_region", "na").upper()
    state = await prog_scorer.score_player_async(
        ctx.fflogs.query, doc, state, char["name"], enc, count=count, progress=progress,
        checkpoint=lambda st: save_state(ctx.db, char["id"], enc, st, ctx.now()), **kw)
    save_state(ctx.db, char["id"], enc, state, ctx.now())
    return state, restarted
