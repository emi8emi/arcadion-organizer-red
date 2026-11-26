"""The event spec: one JSON description of an event (a run), and the code that checks it and turns it into days.

Everything that creates an event goes through here: the guided form in Discord builds a spec, `/run create` with
an attached file reads one, and the dashboard can write the same JSON. `resolve` is pure (no Discord, it only
reads fights from the database) and reports every problem at once, so a form can show them live and a
dashboard can show them next to the fields. The spec is stored on the run (`runs.spec`), so an event can be
shown, exported and copied later exactly as it was made.

Spec (version 1), all times are wall-clock in the bot's grid timezone unless `cutoff_timezone` says otherwise:

    {
      "version": 1,
      "name": "UWU week 1",                       # 1-60 characters, unique per server (case-insensitive)
      "content": "Ultima Weapon (Ultimate)",       # a fight the bot knows (optional when mode is "availability")
      "mode": "roles",                             # "roles" (default): seven-role parties; "availability": only
      "party_size": 8,                             #   collect who can make which slot (like when2meet), looking for
                                                   #   groups of party_size (1-100, availability only)
      "first_start": "17:00",                      # defaults for every day: first and last slot start, slot length
      "last_start": "20:00",
      "slot_minutes": 90,
      "cutoff": {"time": "23:59", "days_before": 1},      # default signup cutoff for every day
      "cutoff_timezone": "America/Sao_Paulo",     # timezone the cutoff times and open_at are written in
      "days": [                                    # "2026-11-02" alone is shorthand for {"date": "2026-11-02"}
        "2026-11-02",
        {"date": "2026-11-04", "first_start": "19:00", "last_start": "21:00", "slot_minutes": 120,
         "cutoff_at": "2026-11-03 18:00"}          # a day can override the window, the slot length and its cutoff
      ],                                           # or {"from": "2026-11-02", "to": "2026-11-08"} for a whole range
      "announce": {"channel_id": "123", "ping_role_id": "456", "message": "Signups are open!",
                   "open_at": "2026-11-01 12:00"}   # every part optional; open_at opens the event by itself
    }

In "availability" mode players only tick slots: no roles, prog or limits, no solver, no parties. `/run status`
shows how many people ticked each slot and how many groups of `party_size` that makes. Custom role lists are not
supported yet.

Slot starts of a day are first_start, first_start + slot_minutes, ... up to and including last_start. Every
field except name, content and days is optional (defaults come from config.json).
"""
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, available_timezones

from .grid import GridConfig, day_cutoff, day_slots, parse_hm

SPEC_VERSION = 1
MODES = ("roles", "availability")
MAX_PARTY_SIZE = 100
TOP_KEYS = {"version", "name", "content", "mode", "party_size", "first_start", "last_start", "slot_minutes", "cutoff", "cutoff_timezone",
            "days", "announce"}
DAY_KEYS = {"date", "first_start", "last_start", "slot_minutes", "cutoff_at"}
CUTOFF_KEYS = {"time", "days_before"}
ANNOUNCE_KEYS = {"channel_id", "ping_role_id", "message", "open_at"}
MIN_SLOT_MINUTES, MAX_SLOT_MINUTES = 15, 480
MAX_MESSAGE = 500


@dataclass
class PlanDay:
    day: date
    slots: list
    cutoff_utc: datetime
    first_start: str
    last_start: str
    slot_minutes: int


@dataclass
class Plan:
    spec: dict                      # canonical: version set, days expanded, fight name as stored
    name: str
    fight: object
    cutoff_tz: str
    mode: str = "roles"
    party_size: int = None
    days: list = field(default_factory=list)
    open_at_utc: datetime = None
    announce: dict = field(default_factory=dict)

    @property
    def slots(self):
        return [s for d in self.days for s in d.slots]

    @property
    def cutoffs(self):
        return {d.day.isoformat(): d.cutoff_utc for d in self.days}


def parse_local(text, tz_name):
    """'2026-11-03 18:00' (or with a T) in `tz_name` -> aware UTC datetime. Raises ValueError."""
    if not isinstance(text, str):
        raise ValueError("not a text")
    naive = datetime.strptime(text.strip().replace("T", " "), "%Y-%m-%d %H:%M")
    return naive.replace(tzinfo=ZoneInfo(tz_name)).astimezone(UTC)


def _hm(value, what, err):
    try:
        if not isinstance(value, str):
            raise ValueError
        return parse_hm(value)
    except ValueError:
        err(f"{what} must look like 17:00.")
        return None


def _minutes(value, what, err):
    if isinstance(value, bool) or not isinstance(value, int) or not MIN_SLOT_MINUTES <= value <= MAX_SLOT_MINUTES:
        err(f"{what} must be a whole number of minutes between {MIN_SLOT_MINUTES} and {MAX_SLOT_MINUTES}.")
        return None
    return value


def _date(value, what, err):
    try:
        if not isinstance(value, str):
            raise ValueError
        return date.fromisoformat(value.strip())
    except ValueError:
        err(f"{what} must be a date like 2026-11-02.")
        return None


def _unknown(obj, allowed, where, err):
    extra = sorted(str(k) for k in set(obj) - allowed)
    if extra:
        err(f"Unknown field(s) in {where}: {', '.join(extra)}.")


def expand_days(raw, max_days, err):
    """The `days` field as a list of day dicts, sorted by date. Accepts dates, day dicts or {"from", "to"}."""
    if isinstance(raw, dict):
        _unknown(raw, {"from", "to"}, "days", err)
        start, end = _date(raw.get("from"), "days.from", err), _date(raw.get("to"), "days.to", err)
        if start is None or end is None:
            return []
        if end < start:
            err("days.to is before days.from.")
            return []
        if (end - start).days + 1 > max_days:
            err(f"An event can be at most {max_days} days long.")
            return []
        return [{"date": (start + timedelta(days=n)).isoformat()} for n in range((end - start).days + 1)]
    if not isinstance(raw, list) or not raw:
        err("days must list at least one day.")
        return []
    out = []
    for n, item in enumerate(raw, 1):
        if isinstance(item, str):
            item = {"date": item}
        if not isinstance(item, dict):
            err(f"Day {n} must be a date or an object with a date.")
            continue
        _unknown(item, DAY_KEYS, f"day {n}", err)
        out.append(dict(item))
    if len(out) > max_days:
        err(f"An event can be at most {max_days} days long.")
    return out


def resolve(spec, cfg, grid, db, now):
    """Check a spec. Returns (Plan, []) when it is fine, else (None, [every problem found])."""
    errors = []
    err = errors.append
    if not isinstance(spec, dict):
        return None, ["The event must be a JSON object."]
    _unknown(spec, TOP_KEYS, "the event", err)
    if spec.get("version", SPEC_VERSION) != SPEC_VERSION:
        err(f"This bot reads event spec version {SPEC_VERSION}.")

    name = spec.get("name")
    name = name.strip() if isinstance(name, str) else ""
    if not name or len(name) > 60:
        err("Give the event a name of 1 to 60 characters.")

    mode = spec.get("mode", "roles")
    if mode not in MODES:
        err(f"mode must be one of: {', '.join(MODES)}.")
        mode = "roles"
    party_size = spec.get("party_size")
    if mode == "roles":
        if party_size is not None:
            err("party_size only applies to availability events; a role party's size comes from the party "
                "composition in config.json.")
    elif party_size is None:
        party_size = 8
    if party_size is not None and mode == "availability" and (
            isinstance(party_size, bool) or not isinstance(party_size, int) or not 1 <= party_size <= MAX_PARTY_SIZE):
        err(f"party_size must be a whole number from 1 to {MAX_PARTY_SIZE}.")
        party_size = None

    content = spec.get("content")
    fight = db.find_fight(content) if isinstance(content, str) and content.strip() else None
    if fight is None and mode == "availability" and not content:
        fights = db.fights()                       # no fight is needed; runs keep a placeholder so old code works
        fight = fights[0] if fights else None
        if fight is None:
            err("No fights are known to the bot yet.")
    elif fight is None:
        known = ", ".join(f["name"] for f in db.fights())
        err(f"I don't know the content '{content}'. Known: {known}." if content else "Pick the fight (content).")

    cutoff_tz = spec.get("cutoff_timezone") or cfg["timezone"]
    if not isinstance(cutoff_tz, str) or cutoff_tz.strip() not in available_timezones():
        err(f"'{cutoff_tz}' is not a timezone name like America/Sao_Paulo.")
        cutoff_tz = cfg["timezone"]
    cutoff_tz = cutoff_tz.strip()

    first_default = spec.get("first_start", grid.day_start)
    last_default = spec.get("last_start", grid.last_start)
    minutes_default = spec.get("slot_minutes", grid.slot_minutes)
    for value, what in ((first_default, "first_start"), (last_default, "last_start")):
        _hm(value, what, err)
    _minutes(minutes_default, "slot_minutes", err)

    cutoff = spec.get("cutoff") or {}
    if not isinstance(cutoff, dict):
        err("cutoff must be an object like {\"time\": \"23:59\", \"days_before\": 1}.")
        cutoff = {}
    _unknown(cutoff, CUTOFF_KEYS, "cutoff", err)
    cutoff_hm = _hm(cutoff.get("time", cfg["cutoff_time"]), "cutoff.time", err)
    days_before = cutoff.get("days_before", cfg["cutoff_days_before"])
    if isinstance(days_before, bool) or not isinstance(days_before, int) or not 0 <= days_before <= 7:
        err("cutoff.days_before must be a whole number from 0 (the day itself) to 7.")
        days_before = None

    days, canonical_days, seen = [], [], set()
    for item in expand_days(spec.get("days"), cfg["max_run_days"], err):
        day = _date(item.get("date"), "A day's date", err)
        if day is None:
            continue
        label = day.isoformat()
        if day in seen:
            err(f"{label} is listed twice.")
            continue
        seen.add(day)
        first = item.get("first_start", first_default)
        last = item.get("last_start", last_default)
        minutes = item.get("slot_minutes", minutes_default)
        first_t, last_t = _hm(first, f"{label} first_start", err), _hm(last, f"{label} last_start", err)
        minutes = _minutes(minutes, f"{label} slot_minutes", err)
        if None in (first_t, last_t, minutes):
            continue
        if last_t < first_t:
            err(f"{label}: last_start is before first_start.")
            continue
        slots = day_slots(day, GridConfig(grid.tz, minutes, first, last))
        if not slots:
            err(f"{label} has no slots.")
            continue
        if "cutoff_at" in item:
            try:
                closes = parse_local(item["cutoff_at"], cutoff_tz)
            except ValueError:
                err(f"{label}: cutoff_at must look like 2026-11-01 23:59.")
                continue
        elif cutoff_hm is not None and days_before is not None:
            closes = day_cutoff(day, cutoff_hm, days_before, cutoff_tz)
        else:
            continue
        if closes >= slots[0].start_utc:
            err(f"The cutoff for {label} must be before that day's first slot ({first}). "
                "Use an earlier cutoff time, or more days before.")
            continue
        days.append(PlanDay(day, slots, closes, first, last, minutes))
        canonical_days.append(dict(item, date=label))

    announce = spec.get("announce") or {}
    open_at = None
    if not isinstance(announce, dict):
        err("announce must be an object.")
        announce = {}
    _unknown(announce, ANNOUNCE_KEYS, "announce", err)
    for key in ("channel_id", "ping_role_id"):
        v = announce.get(key)
        if v is not None and not (isinstance(v, (str, int)) and str(v).isdigit()):
            err(f"announce.{key} must be a Discord id.")
    message = announce.get("message")
    if message is not None and (not isinstance(message, str) or len(message) > MAX_MESSAGE):
        err(f"announce.message must be text of at most {MAX_MESSAGE} characters.")
    if announce.get("open_at") is not None:
        try:
            open_at = parse_local(announce["open_at"], cutoff_tz)
        except ValueError:
            err("announce.open_at must look like 2026-11-01 12:00.")
        else:
            if open_at <= now:
                err("announce.open_at is already in the past; leave it out to open the event yourself.")
            elif days and open_at >= min(d.cutoff_utc for d in days):
                err("announce.open_at must be before the first day's cutoff.")
    if announce.get("open_at") is not None and not announce.get("channel_id"):
        pass                                           # opening by itself without a channel is allowed (no post)

    if errors:
        return None, errors
    days.sort(key=lambda d: d.day)
    canonical = dict(spec, version=SPEC_VERSION, name=name, mode=mode,
                     days=sorted(canonical_days, key=lambda d: d["date"]))
    if mode == "roles" or content:
        canonical["content"] = fight["name"]
    else:
        canonical.pop("content", None)
    if mode == "availability":
        canonical["party_size"] = party_size
    for key in ("cutoff_timezone",):
        if key in canonical:
            canonical[key] = cutoff_tz
    return Plan(canonical, name, fight, cutoff_tz, mode, party_size, days, open_at,
                {k: v for k, v in announce.items() if v}), []


def describe(plan):
    """Plain-text summary of a plan, one line per day, for a preview or a confirmation."""
    tz = ZoneInfo(plan.cutoff_tz)
    kind = (f"availability only, groups of {plan.party_size}" if plan.mode == "availability"
            else plan.fight["name"])
    lines = [f"**{plan.name}** · {kind} · {len(plan.days)} days, {len(plan.slots)} candidate slots"]
    for d in plan.days:
        closes = d.cutoff_utc.astimezone(tz).strftime("%a %d %b %H:%M")
        lines.append(f"• {d.day.strftime('%a %d %b')}: {d.first_start}–{d.last_start}, {len(d.slots)} × "
                     f"{d.slot_minutes} min · signups close {closes}")
    lines.append(f"Cutoffs in {plan.cutoff_tz}.")
    a = plan.announce
    if a.get("channel_id"):
        extra = f", pinging <@&{a['ping_role_id']}>" if a.get("ping_role_id") else ""
        lines.append(f"Announced in <#{a['channel_id']}>{extra} when it opens.")
    if plan.open_at_utc:
        lines.append("Opens by itself at " + plan.open_at_utc.astimezone(tz).strftime("%a %d %b %H:%M") + ".")
    else:
        lines.append("Stays a draft until an organizer runs /run open.")
    return "\n".join(lines)


def spec_from_run(db, run, cfg, grid):
    """The spec of a run: the stored one, or one rebuilt from its days and slots for runs made before specs."""
    if run["spec"]:
        return run["spec"] if isinstance(run["spec"], dict) else __import__("json").loads(run["spec"])
    from .db import parse_iso
    tz = ZoneInfo(grid.tz)
    by_day = {}
    for s in db.run_slots(run["id"]):
        by_day.setdefault(s["slot_key"][:10], []).append(s)
    days = []
    for r in db.run_days(run["id"]):
        slots = sorted(by_day.get(r["day"], []), key=lambda s: s["start_utc"])
        item = {"date": r["day"], "cutoff_at": parse_iso(r["cutoff_utc"]).astimezone(tz).strftime("%Y-%m-%d %H:%M")}
        if slots:
            item.update(first_start=slots[0]["slot_key"][11:16], last_start=slots[-1]["slot_key"][11:16],
                        slot_minutes=slots[0]["minutes"])
        days.append(item)
    fight = db.fight(run["fight_id"])
    spec = {"version": SPEC_VERSION, "name": run["name"], "content": fight["name"] if fight else "",
            "cutoff_timezone": grid.tz, "days": days}
    if run["mode"] == "availability":
        spec.update(mode="availability", party_size=run["party_size"])
        spec.pop("content")
    return spec
