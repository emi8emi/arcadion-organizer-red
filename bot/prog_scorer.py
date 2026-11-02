"""Score a player's pulls against prog points. This is the part the Discord bot reuses.

No command line, no printing, no files, no sys.exit: callers hand in a `client`, the prog point document and a
plain-dict `state`, and get the updated state back. Persisting the state (a JSON file, a SQLite row, ...) is the
caller's job; `checkpoint` is called after every report so a long run can be saved as it goes.

Script (blocking client)::

    doc = progpoints.load_points("UWU.json")
    state = prog_scorer.new_state(doc, "Emi Lune", doc["encounter_id"])
    prog_scorer.score_player(fflogs_api.Client(cid, secret), doc, state, "Emi Lune", doc["encounter_id"],
                             server="Cactuar", region="NA", count=20)
    state["highest"]   # {'idx': 12, 'phase': ..., 'name': ..., 'pull': 'CODE#3', 't': 412.5}
    state["tags"]      # per-tag consistency, see progpoints.tag_report

Discord bot (asyncio; FFLogs calls run in a worker thread so the event loop is never blocked)::

    state = await prog_scorer.score_player_async(fflogs.query, doc, state, char["name"], doc["encounter_id"],
                                                 fflogs_id=char["fflogs_id"], server=char["world"], count=20,
                                                 checkpoint=save_state)      # runs on the event loop thread

`fflogs.query` is any ``async def (graphql, variables) -> data`` (the bot's FFLogsClient._query). Standard library
only. Needs fflogs_api.py and progpoints.py next to it.
"""
import asyncio
import copy
import re

try:
    from . import progpoints as pp
    from .fflogs_api import FFLogsError, build_fight
except ImportError:                                   # flat folder / script
    import progpoints as pp
    from fflogs_api import FFLogsError, build_fight

STATE_VERSION = 1
ALL = {"casts": True, "hp": True, "phases": True}

PHASE_QUERY = """
query ($code: String!, $fights: [Int]) {
  reportData { report(code: $code) {
    phases { encounterID phases { id name } }
    fights(fightIDs: $fights) { id phaseTransitions { id startTime } }
  } }
}"""

CHAR_QUERY = """
query ($name: String!, $server: String!, $region: String!, $limit: Int, $page: Int) {
  characterData { character(name: $name, serverSlug: $server, serverRegion: $region) {
    recentReports(limit: $limit, page: $page) { data { code startTime } has_more_pages }
  } }
}"""

CHAR_ID_QUERY = """
query ($id: Int!, $limit: Int, $page: Int) {
  characterData { character(id: $id) {
    recentReports(limit: $limit, page: $page) { data { code startTime } has_more_pages }
  } }
}"""

PLAYER_META_QUERY = """
query ($code: String!) {
  reportData { report(code: $code) {
    startTime
    fights { id name startTime endTime kill fightPercentage bossPercentage encounterID friendlyPlayers }
    masterData { actors { id name type subType%s } abilities { gameID name } }
  } }
}"""


class ScorerError(Exception):
    """The stored state does not fit the prog points / player / encounter being scored."""


# ---------------- pulls ----------------

def fetch_phases_many(client, code, fights):
    """{fight_id: [{'id','name','t'}]} with t in seconds from pull start. Raises FFLogsError if the query fails."""
    rep = client.query(PHASE_QUERY, {"code": code, "fights": [f["id"] for f in fights]})["reportData"]["report"]
    by_id = {f["id"]: f for f in fights}
    names = {}
    for enc in rep.get("phases") or []:
        names.setdefault(enc.get("encounterID"), {}).update({p["id"]: p.get("name") for p in enc.get("phases") or []})
    out = {f["id"]: [] for f in fights}
    for f in rep.get("fights") or []:
        fight = by_id.get(f["id"])
        if not fight:
            continue
        nm = names.get(fight.get("encounterID"), {})
        out[f["id"]] = sorted(({"id": tr["id"], "name": nm.get(tr["id"]),
                                "t": round((tr["startTime"] - fight["startTime"]) / 1000, 2)}
                               for tr in f.get("phaseTransitions") or []), key=lambda p: p["t"])
    return out


def fetch_phases(client, code, fight):
    """Best effort for one pull: [] if FFLogs has no phase data."""
    try:
        return fetch_phases_many(client, code, [fight])[fight["id"]]
    except FFLogsError:
        return []


def build_pull(client, code, meta, fight, need=ALL, label=None, phases=None, windows=None):
    """Download one pull as the dict progpoints.evaluate() reads: casts, hp series, phases, duration."""
    d = build_fight(client, code, meta, fight, need_casts=need["casts"], need_hp=need["hp"], windows=windows)
    d["phases"] = phases if phases is not None else (fetch_phases(client, code, fight) if need["phases"] else [])
    d["label"] = label or f"{code} #{fight['id']}"
    return d


# ---------------- finding a player's pulls ----------------

def recent_reports(client, count, name=None, server=None, region=None, fflogs_id=None):
    """Codes of the character's most recent public reports, newest first.
    Identify the character by `fflogs_id`, or by `name` + `server` (slug) + `region`."""
    if fflogs_id is None and not (name and server and region):
        raise FFLogsError("Give fflogs_id, or name + server + region, to look a character up.")
    codes, page = [], 1
    while len(codes) < count:
        limit = min(count - len(codes), 100)
        if fflogs_id is not None:
            data = client.query(CHAR_ID_QUERY, {"id": int(fflogs_id), "limit": limit, "page": page})
        else:
            data = client.query(CHAR_QUERY, {"name": name, "server": server, "region": region,
                                             "limit": limit, "page": page})
        char = data["characterData"]["character"]
        if not char:
            raise FFLogsError(f"Character {name or fflogs_id} not found on FFLogs.")
        rr = char["recentReports"]
        codes += [r["code"] for r in rr["data"]]
        if not rr.get("has_more_pages") or not rr["data"]:
            break
        page += 1
    return codes[:count]


def _flat(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def player_pulls(client, code, name, encounter, server=None):
    """-> (meta, [fights]): the pulls of `encounter` in this report that the player was in.
    With `server` (name or slug), same-name players from other worlds are told apart when FFLogs reports a world."""
    meta = None
    if server:
        try:
            meta = client.query(PLAYER_META_QUERY % " server", {"code": code})["reportData"]["report"]
        except FFLogsError:
            meta = None                               # field not available: fall back to matching by name only
    if meta is None:
        meta = client.query(PLAYER_META_QUERY % "", {"code": code})["reportData"]["report"]
    ids = {a["id"] for a in meta["masterData"]["actors"]
           if a.get("type") == "Player" and a["name"].lower() == name.lower()
           and (not server or not a.get("server") or _flat(a["server"]) == _flat(server))}
    fights = [f for f in meta["fights"] if f.get("encounterID") == encounter and ids & set(f.get("friendlyPlayers") or [])]
    return meta, fights


def score_pull(client, code, meta, fight, doc):
    return pp.evaluate(doc, build_pull(client, code, meta, fight, pp.needs(doc)))


# ---------------- state ----------------

def resolve_encounter(doc, given_id):
    """Encounter id from the caller and/or the prog point file; they must agree when both are given."""
    file_id = doc.get("encounter_id") if doc else None
    if given_id is not None and file_id is not None and given_id != file_id:
        raise pp.ProgPointError(f"Encounter {given_id} does not match the prog point file "
                                f"(encounter_id {file_id}, {doc.get('encounter_name') or doc.get('encounter') or '?'}).")
    return given_id if given_id is not None else file_id


def new_state(doc, name, encounter):
    return {"version": STATE_VERSION, "points_hash": pp.fingerprint(doc), "player": name, "encounter": encounter,
            "encounter_name": doc.get("encounter_name") or "", "pulls": {}, "highest": None, "tags": {}}


def check_state(state, doc, name, encounter):
    """Raise ScorerError unless `state` was made for these prog points, player and encounter."""
    if state.get("points_hash") != pp.fingerprint(doc):
        raise ScorerError("The stored scores were made with different prog points. Rescore from scratch "
                          "(reset=True, or --reset on the command line); this discards the stored results.")
    if state.get("encounter") != encounter or state.get("player", "").lower() != name.lower():
        raise ScorerError(f"These scores belong to {state.get('player')} / encounter {state.get('encounter')}; "
                          "use a separate state per player and encounter (command line: a separate --store file).")
    return state


def refresh_highest(state):
    best = None
    for key, r in state["pulls"].items():
        if r["highest"] is not None and (best is None or r["highest"] > best["idx"]):
            best = {"idx": r["highest"], "phase": r["phase"], "name": r["name"], "pull": key, "t": r["t"]}
    state["highest"] = best


def record_pull(state, code, fight, result, at=0):
    """Store one scored pull. `at` = absolute start of the pull (epoch ms), used to find the most recent pulls."""
    key = f"{code}#{fight['id']}"
    hi = result["highest"]
    state["pulls"][key] = {"highest": hi["idx"] if hi else None, "phase": hi["phase"] if hi else None,
                           "name": hi["name"] if hi else None, "t": hi["t"] if hi else None,
                           "kill": bool(fight.get("kill")), "at": at,
                           "reached": [r["idx"] for r in result["reached"]]}
    refresh_highest(state)
    return key


# ---------------- scoring ----------------

def score_player(client, doc, state, name, encounter, reports=None, count=10, server=None, region=None,
                 fflogs_id=None, rule=(3, 5), progress=None, checkpoint=None):
    """Score up to `count` of the player's recent reports (or the explicit `reports` codes) into `state`.

    Pulls already in `state` are skipped, so repeat calls only download what is new. `rule` = default
    (min, of) consistency for tags without their own tag_rules entry. `progress(event_dict)` is told about each
    report and pull; `checkpoint(state)` is called after every report. Returns `state` (also updated in place).
    """
    check_state(state, doc, name, encounter)
    codes = reports or recent_reports(client, count, name, server, region, fflogs_id)
    for code in codes:
        meta, fights = player_pulls(client, code, name, encounter, server)
        # pulls stored by an older version lack 'reached'/'at' and are scored again
        new = [f for f in fights if "reached" not in state["pulls"].get(f"{code}#{f['id']}", {})]
        if progress:
            progress({"event": "report", "code": code, "pulls": len(fights), "new": len(new)})
        for f in new:
            key = record_pull(state, code, f, score_pull(client, code, meta, f, doc),
                              (meta.get("startTime") or 0) + f["startTime"])
            state["encounter_name"] = state.get("encounter_name") or f.get("name") or ""
            if progress:
                progress({"event": "pull", "code": code, "fight": f["id"], "key": key, **state["pulls"][key]})
        state["tags"] = pp.tag_report(doc, state["pulls"].values(), rule)
        if checkpoint:
            checkpoint(state)
    state["tags"] = pp.tag_report(doc, state["pulls"].values(), rule)
    return state


# ---------------- asyncio (the bot) ----------------

class AsyncBridge:
    """Gives a coroutine function a blocking `.query`, for use from a worker thread while the event loop
    that owns the real (aiohttp) client keeps running. Any failure surfaces as FFLogsError."""

    def __init__(self, async_query, loop):
        self._aquery, self._loop = async_query, loop

    def query(self, graphql, variables):
        try:
            return asyncio.run_coroutine_threadsafe(self._aquery(graphql, variables), self._loop).result()
        except FFLogsError:
            raise
        except Exception as e:
            raise FFLogsError(str(e) or type(e).__name__) from e


async def score_player_async(async_query, doc, state, name, encounter, progress=None, checkpoint=None, **kw):
    """score_player for asyncio callers. The downloading happens in a worker thread; `progress` and `checkpoint`
    are still called on the event loop's thread (with a snapshot of the state), so they may touch the database."""
    loop = asyncio.get_running_loop()
    on_progress = (lambda ev: loop.call_soon_threadsafe(progress, dict(ev))) if progress else None
    on_checkpoint = (lambda st: loop.call_soon_threadsafe(checkpoint, copy.deepcopy(st))) if checkpoint else None
    return await asyncio.to_thread(score_player, AsyncBridge(async_query, loop), doc, state, name, encounter,
                                   progress=on_progress, checkpoint=on_checkpoint, **kw)
