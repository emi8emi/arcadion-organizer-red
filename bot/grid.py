"""The fixed slot grid.

One grid for the whole server, defined in a single IANA timezone (America/New_York).
Each slot is `slot_minutes` of REAL time, so a DST change can never overlap or stretch
slots. Slots are stepped in UTC from the local day start; their local wall-clock time is
only used for the id and for the "stop after last_start" rule.

With the default window (08:00 to 23:00 starts) a US DST change (at 02:00) happens
outside the grid, so every day has the same 11 wall-clock slots and only the UTC offset
moves. Windows that cross 02:00 still work: real durations stay equal, and the repeated
hour on a fall-back day gets a "b" suffix on its second occurrence so ids stay unique.
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc


@dataclass(frozen=True)
class GridConfig:
    tz: str = "America/New_York"
    slot_minutes: int = 90
    day_start: str = "08:00"      # first slot of each local day starts here
    last_start: str = "23:00"     # no slot starts after this local time

    @classmethod
    def from_config(cls, cfg):
        return cls(cfg["timezone"], cfg["slot_minutes"], cfg["day_start"], cfg["last_start"])


@dataclass(frozen=True)
class Slot:
    key: str               # local wall-clock id, e.g. "2026-11-02T17:00" (also the solver's slot id)
    start_utc: datetime    # timezone-aware, UTC
    minutes: int

    @property
    def end_utc(self):
        return self.start_utc + timedelta(minutes=self.minutes)

    def local_start(self, tz):
        return self.start_utc.astimezone(ZoneInfo(tz))


def parse_hm(text):
    """'17:30' -> time(17, 30). Raises ValueError on anything else."""
    parts = text.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"'{text}' is not a time like 17:30")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"'{text}' is not a valid time")
    return time(h, m)



def parse_date(text):
    cleaned = text.strip().lower()
    
    if cleaned == "today":
        return date.today()
    if cleaned == "tomorrow":
        return date.today() + timedelta(days=1)
    if cleaned == "yesterday":
        return date.today() - timedelta(days=1)
        
    return date.fromisoformat(cleaned)



def parse_local_datetime(text: str, tz_name: str) -> datetime:
    """Parses absolute or relative local times into an aware UTC datetime.
    
    Examples:
        '2026-11-05 18:00' -> UTC datetime
        'today 14:30'      -> UTC datetime for today at 14:30 local time
        'tomorrow 09:00'   -> UTC datetime for tomorrow at 09:00 local time
    """
    cleaned = text.strip().lower()
    local_tz = ZoneInfo(tz_name)
    
    # 1. Handle relative date keywords
    if cleaned.startswith(("today", "tomorrow", "yesterday")):
        # Split keyword from the time portion (e.g., "today 18:00" -> "today", "18:00")
        parts = cleaned.split(maxsplit=1)
        keyword = parts[0]
        time_str = parts[1] if len(parts) > 1 else "00:00"
        
        # Get the current local date in the target timezone
        local_today = datetime.now(local_tz).date()
        
        if keyword == "today":
            target_date = local_today
        elif keyword == "tomorrow":
            target_date = local_today + timedelta(days=1)
        else: # yesterday
            target_date = local_today - timedelta(days=1)
            
        # Parse the remaining time component
        naive_time = datetime.strptime(time_str, "%H:%M").time()
        naive_dt = datetime.combine(target_date, naive_time)
        
    else:
        # 2. Fallback to standard absolute format
        naive_dt = datetime.strptime(cleaned, "%Y-%m-%d %H:%M")

    # 3. Attach timezone securely and convert to UTC
    # fold=0 ensures standard handling of ambiguous DST fallback hours
    aware_local = naive_dt.replace(tzinfo=local_tz, fold=0)
    return aware_local.astimezone(UTC)



def day_cutoff(day, cutoff_hm, days_before, tz_name):
    """When signups for `day` close: `days_before` days earlier, at the fixed local clock time `cutoff_hm`.

    The result is a UTC instant. The clock time is applied in `tz_name` on the target date, so a DST change
    between the cutoff and the day shifts the UTC value but never the wall-clock time players see.
    """
    local_day = day - timedelta(days=days_before)
    local = datetime.combine(local_day, cutoff_hm, tzinfo=ZoneInfo(tz_name))   # fold=0 for ambiguous hours
    return local.astimezone(UTC)


def day_slots(day, g):
    """All slots whose local start falls on `day`."""
    tz = ZoneInfo(g.tz)
    last = parse_hm(g.last_start)
    t = datetime.combine(day, parse_hm(g.day_start), tzinfo=tz).astimezone(UTC)
    step = timedelta(minutes=g.slot_minutes)
    out, seen = [], set()
    while True:
        local = t.astimezone(tz)
        if local.date() != day or local.time() > last:
            break
        key = local.strftime("%Y-%m-%dT%H:%M")
        if key in seen:                      # repeated wall-clock hour on a fall-back day
            key += "b"
        seen.add(key)
        out.append(Slot(key, t, g.slot_minutes))
        t += step
    return out


def run_slots(start, end, g, earliest=None, latest=None):
    """Slots for every day from `start` to `end` inclusive.

    earliest / latest ("HH:MM", local) optionally narrow which starts are candidates,
    e.g. only 17:00 to 20:00 to pack signups into fewer slots.
    """
    lo = parse_hm(earliest) if earliest else None
    hi = parse_hm(latest) if latest else None
    tz = ZoneInfo(g.tz)
    out, day = [], start
    while day <= end:
        for s in day_slots(day, g):
            lt = s.start_utc.astimezone(tz).time()
            if (lo and lt < lo) or (hi and lt > hi):
                continue
            out.append(s)
        day += timedelta(days=1)
    return out


def short_label(slot, tz):
    """'Mon 02 Nov 17:00'"""
    return slot.local_start(tz).strftime("%a %d %b %H:%M")
