"""FFLogs v2 access shared by the timeline page, the editor/scorer CLI and the Discord bot.

Everything here takes a `client` that has one method, `client.query(graphql, variables) -> data dict`, and raises
FFLogsError for failures. `Client` below is a blocking urllib implementation for scripts; the bot wraps its own
async client instead (see prog_scorer.AsyncBridge). Standard library only; nothing here prints or exits.
"""
import base64
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

TOKEN_URL = "https://www.fflogs.com/oauth/token"
API_URL = "https://www.fflogs.com/api/v2/client"
USER_AGENT = "raid-matchmaking-fflogs-timeline/0.1"
AUTO_ATTACK_NAMES = {"attack", "auto-attack", "auto attack"}

META_QUERY = """
query ($code: String!) {
  reportData { report(code: $code) {
    startTime
    fights { id name startTime endTime kill fightPercentage bossPercentage encounterID }
    masterData { actors { id name type subType } abilities { gameID name } }
  } }
}"""

EVENTS_QUERY = """
query ($code: String!, $start: Float!, $end: Float!, $fights: [Int], $dt: EventDataType, $host: HostilityType) {
  reportData { report(code: $code) {
    events(startTime: $start, endTime: $end, fightIDs: $fights, dataType: $dt, hostilityType: $host, limit: 10000) {
      data nextPageTimestamp
    }
  } }
}"""


class FFLogsError(Exception):
    pass


class Client:
    """Minimal FFLogs v2 GraphQL client (client-credentials OAuth)."""

    def __init__(self, client_id, client_secret):
        self.client_id, self.client_secret, self._token = client_id, client_secret, None

    def _request(self, url, body, headers):
        req = urllib.request.Request(url, data=body, headers={"User-Agent": USER_AGENT, **headers})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            hint = " (rate limited: wait for the hourly point budget to reset, or pull fewer reports)" \
                if e.code == 429 else ""
            raise FFLogsError(f"HTTP {e.code} from FFLogs{hint}: {e.read()[:300].decode(errors='replace')}") from e

    def _auth(self):
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        data = self._request(TOKEN_URL, b"grant_type=client_credentials",
                             {"Authorization": f"Basic {basic}",
                              "Content-Type": "application/x-www-form-urlencoded"})
        self._token = data["access_token"]

    def query(self, query, variables):
        if self._token is None:
            self._auth()
        data = self._request(API_URL, json.dumps({"query": query, "variables": variables}).encode(),
                             {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"})
        if data.get("errors"):
            raise FFLogsError("; ".join(e.get("message", "?") for e in data["errors"]))
        return data["data"]


def parse_report_ref(text):
    """'https://www.fflogs.com/reports/CODE#fight=12' or 'CODE' -> (code, fight or None)."""
    m = re.search(r"reports/([A-Za-z0-9]{16})", text) or re.fullmatch(r"([A-Za-z0-9]{16})", text.strip())
    if not m:
        raise FFLogsError("That does not look like an FFLogs report URL or 16-character code.")
    f = re.search(r"fight=(\w+)", text)
    return m.group(1), (f.group(1) if f else None)


def fetch_events(client, code, fight, data_type, hostility):
    """All events of one type for one fight, following pagination."""
    out, start = [], fight["startTime"]
    while start is not None:
        page = client.query(EVENTS_QUERY, {"code": code, "start": start, "end": fight["endTime"],
                                           "fights": [fight["id"]], "dt": data_type, "host": hostility})
        ev = page["reportData"]["report"]["events"]
        out += ev["data"]
        start = ev.get("nextPageTimestamp")
    return out


def fetch_windows(client, code, fight, data_type, hostility, windows=None):
    """fetch_events, optionally limited to absolute-ms [start, end] windows inside the fight."""
    if not windows:
        return fetch_events(client, code, fight, data_type, hostility)
    out = []
    for start, end in windows:
        out += fetch_events(client, code, {"id": fight["id"], "startTime": start, "endTime": end}, data_type, hostility)
    return out


def pick_fight(fights, wanted):
    pulls = [f for f in fights if f.get("encounterID")]
    if not pulls:
        raise FFLogsError("This report has no boss pulls.")
    if wanted in (None, "last"):
        return pulls[-1]
    try:
        n = int(wanted)
    except ValueError:
        raise FFLogsError(f"Fight must be a number or 'last', not '{wanted}'.") from None
    for f in fights:
        if f["id"] == n:
            return f
    raise FFLogsError(f"No fight {n} in this report. Use --list to see them.")


def _hp(ev):
    """(hitPoints, maxHitPoints) of the event's target, from whichever field FFLogs filled in."""
    res = ev.get("targetResources") or {}
    hp = ev.get("hitPoints", res.get("hitPoints"))
    mx = ev.get("maxHitPoints", res.get("maxHitPoints"))
    return hp, mx


def fetch_meta(client, code):
    return client.query(META_QUERY, {"code": code})["reportData"]["report"]


def build_fight(client, code, meta, fight, keep_autos=False, all_enemies=False, need_casts=True, need_hp=True,
                windows=None):
    """Fetch one fight's enemy casts and enemy HP series. `meta` comes from fetch_meta.
    `windows` ([(start_ms, end_ms)]) limits the event download; times stay relative to the pull start."""
    actors = {a["id"]: a for a in meta["masterData"]["actors"]}
    abilities = {a["gameID"]: a["name"] for a in meta["masterData"]["abilities"]}
    t0 = fight["startTime"]

    def name(aid):
        return actors.get(aid, {}).get("name") or f"#{aid}"

    casts = []
    for ev in (fetch_windows(client, code, fight, "Casts", "Enemies", windows) if need_casts else []):
        if ev.get("type") not in ("begincast", "cast"):
            continue
        ability = abilities.get(ev.get("abilityGameID")) or ev.get("ability", {}).get("name") \
            or f"Unknown {ev.get('abilityGameID')}"
        if not keep_autos and ability.lower() in AUTO_ATTACK_NAMES:
            continue
        casts.append({"t": round((ev["timestamp"] - t0) / 1000, 2), "src": name(ev.get("sourceID")),
                      "ability": ability, "aid": ev.get("abilityGameID"), "type": ev["type"],
                      "dur": round((ev.get("duration") or 0) / 1000, 2)})
    casts.sort(key=lambda c: c["t"])

    # Enemy HP: every player hit on an enemy carries the target's HP; keep the last sample per second.
    series, max_hp = {}, {}
    for ev in (fetch_windows(client, code, fight, "DamageDone", "Friendlies", windows) if need_hp else []):
        tid = ev.get("targetID")
        hp, mx = _hp(ev)
        if tid not in actors or not hp and hp != 0 or not mx:
            continue
        t = round((ev["timestamp"] - t0) / 1000, 2)
        pts = series.setdefault(tid, {})
        pts[int(t)] = [t, round(hp / mx * 100, 2)]
        max_hp[tid] = max(max_hp.get(tid, 0), mx)
    biggest = max(max_hp.values(), default=0)
    hp_series = []
    for tid, pts in series.items():
        if not all_enemies and max_hp[tid] < biggest * 0.05:      # skip small adds
            continue
        hp_series.append({"name": name(tid), "id": tid, "max": max_hp[tid],
                          "points": [pts[k] for k in sorted(pts)]})
    hp_series.sort(key=lambda s: s["points"][0][0])

    return {"code": code, "fight": fight["id"], "title": fight["name"], "kill": bool(fight.get("kill")),
            "bossPercentage": fight.get("bossPercentage"), "duration": (fight["endTime"] - t0) / 1000,
            "encounterID": fight.get("encounterID"), "reportStart": meta.get("startTime"),
            "casts": casts, "hp": hp_series}


def _load_env():
    """Load FFLOGS_* from a .env in the current folder, next to the script, or one level up.
    Plain KEY=VALUE parser (no python-dotenv needed); real environment variables win."""
    here = Path(__file__).resolve().parent
    for d in (Path.cwd(), here, here.parent):
        f = d / ".env"
        if not f.is_file():
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.removeprefix("export ").strip()
            os.environ.setdefault(k, v.strip().strip("'\""))
