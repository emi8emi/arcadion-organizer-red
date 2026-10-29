"""Config loading: config.json for tunables, .env / environment for secrets and overrides."""
import json
import os
from pathlib import Path

ROLES = ("tank", "healer", "dps")

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS = {
    "timezone": "America/New_York",
    "slot_minutes": 90,
    "day_start": "08:00",
    "last_start": "23:00",
    "default_max_per_day": 2,
    "default_max_per_run": 4,
    "max_run_days": 14,
    "party": {"tank": 2, "healer": 2, "dps": 4},
    "solver_phase_cross_cost": 3,   # must equal phase_cross_cost in raid-solver/config.json
    "cutoff_time": "23:59",          # each day's signups close at this local time ...
    "cutoff_days_before": 1,         # ... this many days before the day (1 = the evening before)
    "reminder_channel_id": None,     # where the bot nags organizers to run /draft export
    "reminder_interval_minutes": 30,
    "parties_channel_id": None,      # where /draft approve posts the parties (one thread per slot)
    "vote_window_hours": 72,         # how long after a slot ends its players may rate each other
    "vote_expiry_days": 90,          # older votes stop counting (export and /votes summary)
    "vote_floor": 3,                 # /votes summary hides players with fewer votes than this
    "organizer_role_id": None,
    "db_path": "raidbot.sqlite3",
    "export_dir": "exports",
}


def load_config(path=None, env=None):
    """Defaults, then config.json, then environment overrides (ORGANIZER_ROLE_ID, REMINDER_CHANNEL_ID, PARTIES_CHANNEL_ID, DB_PATH)."""
    env = os.environ if env is None else env
    cfg = dict(DEFAULTS)
    path = Path(path) if path else ROOT / "config.json"
    if path.exists():
        cfg.update(json.loads(path.read_text()))
    if env.get("ORGANIZER_ROLE_ID"):
        cfg["organizer_role_id"] = int(env["ORGANIZER_ROLE_ID"])
    if env.get("REMINDER_CHANNEL_ID"):
        cfg["reminder_channel_id"] = int(env["REMINDER_CHANNEL_ID"])
    if env.get("PARTIES_CHANNEL_ID"):
        cfg["parties_channel_id"] = int(env["PARTIES_CHANNEL_ID"])
    if env.get("DB_PATH"):
        cfg["db_path"] = env["DB_PATH"]
    return cfg
