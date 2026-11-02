"""Prog points: definition format, validation and evaluation against a pull.

A prog point file is JSON:

    {"version": 1, "encounter": "UWU", "points": [
        {"idx": 0, "phase": "P1 Garuda", "name": "Wicked Wheel", "mode": "all",
         "conditions": [{"type": "cast", "ability": "Wicked Wheel", "kind": "cast", "nth": 1}]},
        {"idx": 1, "phase": "P1 Garuda", "name": "Garuda < 40%", "conditions": [{"type": "hp", "lte": 40}]}
    ]}

Condition types (times are seconds from pull start):
  cast   {ability | aid, source?, kind: "cast"|"begincast" (default cast), nth (default 1), phase?}
         with `phase`, only casts inside that phase count (and nth counts within it)
  hp     {enemy: name|null, lte}   first time that enemy's HP% <= lte (null = the main boss HP series)
  phase  {id}                      the pull entered that phase
  time   {gte, since_phase?}       the pull lasted at least gte seconds (from pull start or a phase start)
  kill   {}                        the pull was a kill (reached at the end of the pull)

A point with mode "all" (default) is reached when every condition fired; "any" when at least one did.
`idx` matches checkpoints(fight_id, idx, phase, name) / players.prog_index in the bot schema.

`encounter_id` / `encounter_name` (FFLogs encounter id and its name) are filled in automatically by the editor from the
clears it was built from; `encounter` is a free-form short label (e.g. "UWU"). The scorer uses `encounter_id`
when --encounter is not given, and refuses a mismatch.

`display_name` (optional) is the fight's name in the Discord bot when it creates the fight from this file.

Tags: a point may carry `"tags": ["cleanup"]`. The scorer reports, per tag, whether the player reached every tagged
point in at least `min` of their most recent `of` pulls (default 3 of 5). Per-tag overrides go in the file:
    "tag_rules": {"cleanup": {"min": 4, "of": 6}}
Tags and rules are metadata: changing them does not invalidate stored scores.

The editor page (progpoints_editor.html) carries a JS port of condition_time/point_time; tests keep the
two in lockstep.
"""
import hashlib
import json
import re
from pathlib import Path

VERSION = 1
KINDS = ("cast", "begincast")
MODES = ("all", "any")


class ProgPointError(Exception):
    pass


def normalize_condition(c):
    if not isinstance(c, dict) or "type" not in c:
        raise ProgPointError(f"Condition needs a 'type': {c!r}")
    t = c["type"]
    if t == "cast":
        if not c.get("ability") and c.get("aid") is None:
            raise ProgPointError("A cast condition needs 'ability' or 'aid'.")
        kind = c.get("kind", "cast")
        if kind not in KINDS:
            raise ProgPointError(f"Cast kind must be one of {KINDS}, not {kind!r}.")
        nth = int(c.get("nth", 1))
        if nth < 1:
            raise ProgPointError("Cast 'nth' must be >= 1.")
        return {"type": "cast", "ability": c.get("ability"), "aid": c.get("aid"),
                "source": c.get("source") or None, "kind": kind, "nth": nth,
                "phase": None if c.get("phase") is None else int(c["phase"])}
    if t == "hp":
        if "lte" not in c:
            raise ProgPointError("An hp condition needs 'lte'.")
        lte = float(c["lte"])
        if not 0 <= lte <= 100:
            raise ProgPointError("hp 'lte' must be between 0 and 100.")
        return {"type": "hp", "enemy": c.get("enemy") or None, "lte": lte}
    if t == "phase":
        if c.get("id") is None:
            raise ProgPointError("A phase condition needs 'id'.")
        return {"type": "phase", "id": int(c["id"])}
    if t == "time":
        if "gte" not in c:
            raise ProgPointError("A time condition needs 'gte'.")
        since = c.get("since_phase")
        return {"type": "time", "gte": float(c["gte"]), "since_phase": None if since is None else int(since)}
    if t == "kill":
        return {"type": "kill"}
    raise ProgPointError(f"Unknown condition type {t!r}.")


def normalize_tags(raw):
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    out = []
    for t in (x for item in raw for x in str(item).split(",")):
        t = re.sub(r"\s+", " ", str(t).strip().lower())
        if t and t not in out:
            out.append(t)
    return out


def normalize_rule(tag, r):
    try:
        need, of = int(r["min"]), int(r["of"])
    except (KeyError, TypeError, ValueError):
        raise ProgPointError(f"tag_rules[{tag!r}] needs integer 'min' and 'of'.") from None
    if not 1 <= need <= of:
        raise ProgPointError(f"tag_rules[{tag!r}]: need 1 <= min <= of (got {need} of {of}).")
    return {"min": need, "of": of}


def load_points(source):
    """Accept a path, a JSON string or a dict; return a validated, normalised document."""
    if isinstance(source, dict):
        doc = source
    else:
        text = str(source)
        if not text.lstrip().startswith("{"):
            text = Path(text).read_text(encoding="utf-8")
        try:
            doc = json.loads(text)
        except json.JSONDecodeError as e:
            raise ProgPointError(f"Prog point file is not valid JSON: {e}") from e
    if doc.get("version", VERSION) != VERSION:
        raise ProgPointError(f"Unsupported prog point version {doc.get('version')}.")
    pts, seen = [], set()
    for i, p in enumerate(doc.get("points", [])):
        idx = int(p.get("idx", i))
        if idx in seen:
            raise ProgPointError(f"Duplicate idx {idx}.")
        seen.add(idx)
        mode = p.get("mode", "all")
        if mode not in MODES:
            raise ProgPointError(f"Point {idx}: mode must be one of {MODES}.")
        conds = [normalize_condition(c) for c in p.get("conditions", [])]
        if not conds:
            raise ProgPointError(f"Point {idx} ({p.get('name', '?')}) has no conditions.")
        pts.append({"idx": idx, "phase": p.get("phase") or "", "name": p.get("name") or f"Point {idx}",
                    "mode": mode, "conditions": conds, "tags": normalize_tags(p.get("tags"))})
    pts.sort(key=lambda p: p["idx"])
    rules = {normalize_tags(k)[0]: normalize_rule(k, v) for k, v in (doc.get("tag_rules") or {}).items() if normalize_tags(k)}
    enc_id = doc.get("encounter_id")
    return {"version": VERSION, "encounter": doc.get("encounter", ""),
            "encounter_id": int(enc_id) if enc_id not in (None, "") else None,
            "encounter_name": doc.get("encounter_name") or "", "display_name": doc.get("display_name") or "",
            "points": pts, "tag_rules": rules}


def fingerprint(doc):
    """Stable hash of the scoring-relevant content; stored results are only valid for one hash."""
    body = [{k: p[k] for k in ("idx", "mode", "conditions")} for p in doc["points"]]
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]


def tag_report(doc, pulls, default_rule=(3, 5)):
    """Per tag: have all tagged points been reached in >= min of the most recent `of` pulls?

    pulls: stored pull records ({'reached': [idx...], 'at': epoch_ms}); records without 'reached' are ignored.
    -> {tag: {'consistent': bool, 'points': [{idx, name, phase, reached, of, need, ok}]}}"""
    recent = sorted((p for p in pulls if "reached" in p), key=lambda p: p.get("at") or 0, reverse=True)
    out = {}
    for pt in doc["points"]:
        for tag in pt.get("tags", []):
            rule = doc.get("tag_rules", {}).get(tag) or {"min": default_rule[0], "of": default_rule[1]}
            window = recent[:rule["of"]]
            n = sum(1 for p in window if pt["idx"] in p["reached"])
            ok = n >= rule["min"]
            e = out.setdefault(tag, {"consistent": True, "points": []})
            e["points"].append({"idx": pt["idx"], "name": pt["name"], "phase": pt["phase"], "reached": n,
                                "of": len(window), "need": rule["min"], "ok": ok})
            e["consistent"] = e["consistent"] and ok
    return out


def needs(doc):
    """Which data a scoring run has to download: {'casts','hp','phases'} booleans."""
    types = {c["type"] for p in doc["points"] for c in p["conditions"]}
    scoped = any(c.get("since_phase") is not None or c.get("phase") is not None
                 for p in doc["points"] for c in p["conditions"] if c["type"] in ("time", "cast"))
    return {"casts": "cast" in types, "hp": "hp" in types, "phases": "phase" in types or scoped}


def _norm(s):
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _main_hp(pull):
    series = pull.get("hp") or []
    return max(series, key=lambda s: s.get("max", 0)) if series else None


def phase_windows(pull, pid):
    """[(start, end)] spans (seconds) the pull spent in phase `pid`; the last span runs to the end of the pull."""
    ph = pull.get("phases") or []
    return [(p["t"], ph[i + 1]["t"] if i + 1 < len(ph) else float("inf")) for i, p in enumerate(ph) if p["id"] == pid]


def condition_time(cond, pull):
    """Time (s) the condition first held in this pull, or None."""
    t = cond["type"]
    if t == "cast":
        n = 0
        wins = phase_windows(pull, cond["phase"]) if cond.get("phase") is not None else None
        if wins is not None and not wins:
            return None
        for c in sorted(pull.get("casts") or [], key=lambda c: c["t"]):
            if c["type"] != cond.get("kind", "cast"):
                continue
            if wins is not None and not any(a <= c["t"] < b for a, b in wins):
                continue
            if cond.get("aid") is not None:
                if c.get("aid") != cond["aid"]:
                    continue
            elif _norm(c["ability"]) != _norm(cond.get("ability")):
                continue
            if cond.get("source") and _norm(c["src"]) != _norm(cond["source"]):
                continue
            n += 1
            if n == cond.get("nth", 1):
                return c["t"]
        return None
    if t == "hp":
        if cond.get("enemy"):
            series = next((s for s in pull.get("hp") or [] if _norm(s["name"]) == _norm(cond["enemy"])), None)
        else:
            series = _main_hp(pull)
        if not series:
            return None
        for ts, pct in series["points"]:
            if pct <= cond["lte"]:
                return ts
        return None
    if t == "phase":
        return next((p["t"] for p in pull.get("phases") or [] if p["id"] == cond["id"]), None)
    if t == "time":
        base = 0.0
        if cond.get("since_phase") is not None:
            base = next((p["t"] for p in pull.get("phases") or [] if p["id"] == cond["since_phase"]), None)
            if base is None:
                return None
        t_hit = base + cond["gte"]
        return t_hit if t_hit <= pull.get("duration", 0) else None
    if t == "kill":
        return pull.get("duration", 0) if pull.get("kill") else None
    return None


def point_time(point, pull):
    """Time the point was reached, or None. 'all' = latest condition time, 'any' = earliest."""
    times = [condition_time(c, pull) for c in point["conditions"]]
    hit = [x for x in times if x is not None]
    if point.get("mode", "all") == "all":
        return max(hit) if len(hit) == len(times) else None
    return min(hit) if hit else None


def evaluate(doc, pull):
    """-> {'reached': [{idx, phase, name, t}, ...], 'highest': {...}|None} (highest = largest idx reached)."""
    reached = []
    for p in doc["points"]:
        t = point_time(p, pull)
        if t is not None:
            reached.append({"idx": p["idx"], "phase": p["phase"], "name": p["name"], "t": t})
    return {"reached": reached, "highest": max(reached, key=lambda r: r["idx"]) if reached else None}


def render_editor(clears, points=None):
    """Embed the clears (list of pull dicts) and optional existing points into the editor page."""
    html = (Path(__file__).with_name("progpoints_editor.html")).read_text(encoding="utf-8")
    data = {"__CLEARS__": clears, "__POINTS__": points or {"version": VERSION, "encounter": "", "points": []}}
    return re.sub(r"__(CLEARS|POINTS)__", lambda m: json.dumps(data[m.group(0)]).replace("</", "<\\/"), html)
