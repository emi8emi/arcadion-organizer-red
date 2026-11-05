"""Config loading: config.json for tunables, .env / environment for secrets and overrides."""
import json
import os
from pathlib import Path

# The seven seats of a party: two tanks (main and off), two healers (regen and shield), four DPS (two melee,
# one ranged, one caster). Players pick every role they can play; the solver fills exactly one seat per role below.
ROLES = ("main_tank", "off_tank", "regen", "shield", "melee", "ranged", "caster")
ROLE_LABELS = {"main_tank": "Main tank", "off_tank": "Off tank", "regen": "Regen healer", "shield": "Shield healer",
               "melee": "Melee", "ranged": "Ranged", "caster": "Caster"}
ROLE_SHORT = {"main_tank": "MT", "off_tank": "OT", "regen": "RG", "shield": "SH",
              "melee": "ML", "ranged": "RN", "caster": "CS"}


def role_label(role):
    return ROLE_LABELS.get(role, role)


def parse_role(text):
    """'Main tank', 'main-tank' or 'MAIN_TANK' -> 'main_tank'; None if it is not one of ROLES."""
    key = "_".join((text or "").strip().lower().replace("-", " ").replace("_", " ").split())
    return key if key in ROLES else None


ROOT = Path(__file__).resolve().parent.parent

DEFAULTS = {
    "timezone": "America/New_York",
    "slot_minutes": 90,
    "day_start": "08:00",
    "last_start": "23:00",
    "lodestone_region": "na",
    "default_max_per_day": 2,
    "default_max_per_run": 4,
    "max_run_days": 14,
    "party": {"main_tank": 1, "off_tank": 1, "regen": 1, "shield": 1, "melee": 2, "ranged": 1, "caster": 1},
    "solver_phase_cross_cost": 3,   # must equal phase_cross_cost in raid-solver/config.json
    "cutoff_time": "23:59",          # each day's signups close at this local time ...
    "cutoff_days_before": 1,         # ... this many days before the day (1 = the evening before)
    "reminder_channel_id": None,     # where the bot nags organizers to run /draft export
    "reminder_interval_minutes": 30,
    "parties_channel_id": None,      # where /draft approve posts the parties (one thread per slot)
    "vote_window_hours": 72,         # how long after a slot ends its players may rate each other
    "vote_expiry_days": 90,          # older votes stop counting (export and /votes summary)
    "vote_floor": 3,                 # /votes summary hides players with fewer votes than this
    "confirm_reminder_hours": 3,     # unconfirmed players in an approved party are pinged again this long before the slot
    "prog_fetch_hours": 48,          # /debug score reads FFLogs reports from this many hours back (0 = no limit)
    "organizer_role_id": None,
    "database_url": None,            # the Postgres database shared with the dashboard; set DATABASE_URL in .env
    "export_dir": "exports",
    "debug_commands": 0,
    "fflogs_client_id": None,
    "fflogs_client_secret": None
}


def load_config(path=None, env=None):
    """Defaults, then config.json, then environment overrides (ORGANIZER_ROLE_ID, REMINDER_CHANNEL_ID, PARTIES_CHANNEL_ID, DATABASE_URL)."""
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
    if env.get("DATABASE_URL"):
        cfg["database_url"] = env["DATABASE_URL"]
    if env.get("FFLOGS_CLIENT_ID"):
        cfg["fflogs_client_id"] = env["FFLOGS_CLIENT_ID"]
    if env.get("FFLOGS_CLIENT_SECRET"):
        cfg["fflogs_client_secret"] = env["FFLOGS_CLIENT_SECRET"]
    if env.get("DEBUG_COMMANDS"):
        cfg["debug_commands"] = env["DEBUG_COMMANDS"]
    return cfg
