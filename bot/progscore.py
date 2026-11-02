"""Prog scoring glue: prog point files, stored scorer state per character, and the two cleanup modes.

No Discord imports. The scorer itself (prog_scorer.py, progpoints.py, fflogs_api.py) is a standalone,
stdlib-only package that is copied here unchanged.

Modes for points tagged `cleanup` (see `effective_highest`):
  normal       the tag is ignored: the highest point reached in any pull counts.
  consistent   a cleanup point only counts when the tag check passed for it (reached in at least `min` of the
               most recent `of` pulls, 3 of 5 unless the prog point file says otherwise), or when the next
               non-cleanup point was reached that consistently. A failed cleanup point blocks itself and everything
               after it, but only for a character whose current prog is below it.
"""
import json
import logging
from pathlib import Path
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

log = logging.getLogger("raidbot")
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


def checkpoint_remap(old, new):
    """{old idx: new idx} for moving stored checkpoint numbers from one list of (idx, phase, name) to another.

    A point is matched by (phase, name); failing that by (position of its phase, name) when both lists have the
    same number of phases (a renamed phase); failing that by a name that is unique in both. A point that
    matches nothing becomes the nearest earlier point that did (0 if none): a conservative prog, never a higher one."""
    def phases(rows):
        out = []
        for _, ph, _ in rows:
            if ph.lower() not in out:
                out.append(ph.lower())
        return out
    old_ph, new_ph = phases(old), phases(new)
    same_phases = len(old_ph) == len(new_ph)
    by_phase_name = {(ph.lower(), n.lower()): i for i, ph, n in new}
    by_pos_name = {(new_ph.index(ph.lower()), n.lower()): i for i, ph, n in new}
    names = [n.lower() for _, _, n in new]
    old_names = [n.lower() for _, _, n in old]
    out, last = {}, 0
    for idx, ph, n in sorted(old):
        hit = by_phase_name.get((ph.lower(), n.lower()))
        if hit is None and same_phases:
            hit = by_pos_name.get((old_ph.index(ph.lower()), n.lower()))
        if hit is None and names.count(n.lower()) == 1 and old_names.count(n.lower()) == 1:
            hit = next(i for i, _, nn in new if nn.lower() == n.lower())
        last = out[idx] = last if hit is None else hit
    return out


def _apply_remap(db, fight_id, encounter, remap):
    """Rewrite every stored checkpoint number that belongs to this fight. Returns how many rows changed."""
    changed = 0
    conn = db.conn

    def rewrite(select, update, params=()):
        nonlocal changed
        for row in conn.execute(select, params).fetchall():
            new = remap.get(row[1], row[1])
            if new != row[1]:
                conn.execute(update, (new, row[0]))
                changed += 1
    runs = "(SELECT id FROM runs WHERE fight_id = ?)"
    rewrite(f"SELECT rowid, prog_index FROM day_snapshots WHERE prog_index IS NOT NULL AND run_id IN {runs}",
            "UPDATE day_snapshots SET prog_index = ? WHERE rowid = ?", (fight_id,))
    rewrite(f"SELECT id, target_idx FROM parties WHERE target_idx IS NOT NULL AND run_id IN {runs}",
            "UPDATE parties SET target_idx = ? WHERE id = ?", (fight_id,))
    rewrite(f"SELECT party_id, target_idx FROM party_originals WHERE target_idx IS NOT NULL AND run_id IN {runs}",
            "UPDATE party_originals SET target_idx = ? WHERE party_id = ?", (fight_id,))
    if conn.execute("SELECT COUNT(DISTINCT fight_id) FROM checkpoints").fetchone()[0] <= 1:
        # players.prog_index is not per fight, so it can only be moved while there is a single fight
        rewrite("SELECT discord_id, prog_index FROM players WHERE prog_index IS NOT NULL",
                "UPDATE players SET prog_index = ? WHERE discord_id = ?")
    if encounter is not None:
        for col in ("confirmed_idx", "latest_idx"):
            rewrite(f"SELECT character_id, {col} FROM prog_scores WHERE encounter = {int(encounter)} AND {col} IS NOT NULL",
                    f"UPDATE prog_scores SET {col} = ? WHERE character_id = ? AND encounter = {int(encounter)}")
    return changed


def sync_fight(db, doc, fallback_short=None):
    """Create the fight for this prog point document if needed and make its checkpoints equal the document's.

    Stored checkpoint numbers (players, signup snapshots, party targets, scorer results) are moved to the new
    numbering with `checkpoint_remap`. Returns {'fight_id', 'created', 'changed', 'remapped'}."""
    short = doc.get("encounter") or fallback_short
    if not short:
        raise ProgPointError("The prog point file needs an 'encounter' short name (like UWU).")
    row = db.conn.execute("SELECT * FROM fights WHERE lower(short_name) = lower(?)", (short,)).fetchone()
    created = row is None
    with db.conn:
        if created:
            name = doc.get("display_name") or doc.get("encounter_name") or short
            if db.conn.execute("SELECT 1 FROM fights WHERE name = ?", (name,)).fetchone():
                name = f"{name} ({short})"
            cur = db.conn.execute("INSERT INTO fights (name, short_name) VALUES (?, ?)", (name, short))
            fight_id = cur.lastrowid
        else:
            fight_id = row["id"]
        old = [(c["idx"], c["phase"], c["name"]) for c in db.checkpoints(fight_id)]
        new = _rows(doc)
        changed = old != new
        remapped = 0
        if changed:
            remap = checkpoint_remap(old, new) if old else {}
            db.conn.execute("DELETE FROM checkpoints WHERE fight_id = ?", (fight_id,))
            db.conn.executemany("INSERT INTO checkpoints (fight_id, idx, phase, name) VALUES (?, ?, ?, ?)",
                                [(fight_id, *r) for r in new])
            if remap:
                remapped = _apply_remap(db, fight_id, doc.get("encounter_id"), remap)
    return {"fight_id": fight_id, "created": created, "changed": changed, "remapped": remapped}


def sync_fights(db, folder):
    """Sync every prog point file in `folder` (prog_points/*.json). A broken file is logged and skipped."""
    reports = []
    folder = Path(folder)
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            doc = with_clear(progpoints.load_points(path))
            rep = sync_fight(db, doc, path.stem)
        except (ProgPointError, OSError) as e:
            log.warning("prog point file %s skipped: %s", path.name, e)
            continue
        if rep["created"] or rep["changed"]:
            log.info("fight %s synced from %s (%d stored number(s) moved)", rep["fight_id"], path.name, rep["remapped"])
        reports.append(rep)
    return reports


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

def _next_point(doc, pt, tag):
    """idx of the first point after `pt` that does not carry `tag`, or None."""
    return min((q["idx"] for q in doc["points"] if q["idx"] > pt["idx"] and tag not in q["tags"]), default=None)


def cleanup_report(doc, state, tag=CLEANUP_TAG, default_rule=(3, 5)):
    """Per tagged point: is it consistent? -> {'consistent', 'points': [{idx, name, phase, reached, of, need, ok, via_next}]}
    or None when nothing carries the tag.

    A point is ok when it was reached in at least `min` of the most recent `of` pulls, OR when the next
    non-tagged point was reached that consistently (moving on for good means the cleanup was done). Passing to
    the next point only once or twice does not count: the player may have been lucky, or everyone was dead."""
    pulls = sorted((p for p in state["pulls"].values() if "reached" in p), key=lambda p: p.get("at") or 0, reverse=True)
    out = {"consistent": True, "points": []}
    for pt in doc["points"]:
        if tag not in pt["tags"]:
            continue
        rule = doc.get("tag_rules", {}).get(tag) or {"min": default_rule[0], "of": default_rule[1]}
        window = pulls[:rule["of"]]
        own = sum(1 for p in window if pt["idx"] in p["reached"])
        nxt = _next_point(doc, pt, tag)
        via = sum(1 for p in window if nxt is not None and max(p["reached"], default=-1) >= nxt)
        ok = own >= rule["min"] or via >= rule["min"]
        out["points"].append({"idx": pt["idx"], "name": pt["name"], "phase": pt["phase"], "reached": own,
                              "of": len(window), "need": rule["min"], "ok": ok, "via_next": via})
        out["consistent"] = out["consistent"] and ok
    return out if out["points"] else None


def failed_cleanup_points(doc, state, tag=CLEANUP_TAG):
    """idx of the tagged points whose consistency check failed."""
    rep = cleanup_report(doc, state, tag)
    return {p["idx"] for p in rep["points"] if not p["ok"]} if rep else set()


def effective_highest(doc, state, mode="normal", current=None):
    """{'idx', 'phase', 'name', 'pull'} of the player's highest point under `mode`, or None if none was reached.

    'consistent': a cleanup point that failed its check blocks itself and everything after it, so the result
    is the highest point reached before it. This only applies while the character's current prog (`current`,
    a checkpoint idx or None) is below that cleanup point; someone already past it has proven it before."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    if mode == "normal":
        return state.get("highest")
    blocking = [i for i in failed_cleanup_points(doc, state) if current is None or current < i]
    cap = min(blocking, default=None)
    info = {p["idx"]: p for p in doc["points"]}
    best = None
    for key, pull in state["pulls"].items():
        for idx in pull.get("reached", []):
            if (cap is None or idx < cap) and (best is None or idx > best["idx"]):
                best = {"idx": idx, "phase": info[idx]["phase"], "name": info[idx]["name"], "pull": key}
    return best


def cleanup_lines(doc, state):
    """Human-readable consistency of the cleanup points, one per line (empty when none are tagged)."""
    rep = cleanup_report(doc, state)
    return [f"{'✅' if p['ok'] else '❌'} {p['phase']} {p['name']}: reached in {p['reached']} of the last "
            f"{p['of']} pulls (needs {p['need']}), went on to the next point in {p['via_next']}"
            for p in rep["points"]] if rep else []


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
