"""Characters: verified-only links between a Discord player and FFXIV characters (alts allowed).

A player starts a *claim* (name + world + one-time code), puts the code in their Lodestone Character
Profile, and /character verify checks it. Only verified characters exist in `characters`; the Lodestone
id is the identity, so a rename or transfer never breaks the link. One pending claim per player.
FFLogs ids are looked up after verification and used to keep name/world current (see `refresh`).
No Discord imports, so services.py and the signup code can use it.
"""
import re
import secrets
from datetime import timedelta

from .db import iso, parse_iso

NAME_RE = re.compile(r"^[A-Za-z'-]{2,15} [A-Za-z'-]{2,15}$")
WORLD_RE = re.compile(r"^[A-Za-z]{3,20}$")
TOKEN_TTL = timedelta(hours=1)
VERIFY_COOLDOWN = timedelta(seconds=30)      # between Lodestone lookups per player
MAX_CHARACTERS = 8

SCHEMA = """
CREATE TABLE IF NOT EXISTS characters (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id   TEXT NOT NULL REFERENCES players(discord_id),
    lodestone_id TEXT NOT NULL UNIQUE,
    name         TEXT NOT NULL,
    world        TEXT NOT NULL,
    fflogs_id    INTEGER,
    verified_utc TEXT NOT NULL,
    synced_utc   TEXT
);
CREATE INDEX IF NOT EXISTS idx_characters_owner ON characters(discord_id);
-- Which character a player brings on a given day of a run. No row = their only character, if they have one.
CREATE TABLE IF NOT EXISTS signup_characters (
    run_id       INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    day          TEXT NOT NULL,
    discord_id   TEXT NOT NULL REFERENCES players(discord_id),
    character_id INTEGER REFERENCES characters(id) ON DELETE SET NULL,
    PRIMARY KEY (run_id, day, discord_id)
);
CREATE TABLE IF NOT EXISTS character_claims (
    discord_id   TEXT PRIMARY KEY REFERENCES players(discord_id),
    name         TEXT NOT NULL,
    world        TEXT NOT NULL,
    token        TEXT NOT NULL,
    token_utc    TEXT NOT NULL,
    last_try_utc TEXT
);
"""


def ensure_schema(conn):
    """Create the tables, migrating the first (soft-link) version of `characters` if it is there."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(characters)")]
    if cols and "id" not in cols:
        conn.execute("DROP INDEX IF EXISTS idx_characters_lodestone")
        conn.execute("DROP INDEX IF EXISTS idx_characters_name")
        conn.execute("ALTER TABLE characters RENAME TO characters_old")
        conn.executescript(SCHEMA)
        conn.execute("""INSERT INTO characters (discord_id, lodestone_id, name, world, verified_utc)
                        SELECT discord_id, lodestone_id, name, world, verified_utc
                        FROM characters_old WHERE lodestone_id IS NOT NULL""")
        conn.execute("DROP TABLE characters_old")
        conn.commit()
    else:
        conn.executescript(SCHEMA)


def parse_character(name, world):
    """Tidy what a player typed. Raises ValueError with a player-friendly message."""
    name = " ".join((name or "").split())
    world = (world or "").strip()
    if not NAME_RE.match(name):
        raise ValueError("A character name is a first and last name, like Y'shtola Rhul.")
    if not WORLD_RE.match(world):
        raise ValueError("The world is a single word, like Exodus.")
    return " ".join(w[:1].upper() + w[1:].lower() for w in name.split()), world.capitalize()


def new_token():
    return "raidbot-" + secrets.token_hex(3)


def label(char):
    return f"{char['name']} @ {char['world']}"


# ------------------------------------------------------------------ characters

def list_characters(db, discord_id):
    return db.conn.execute("SELECT * FROM characters WHERE discord_id = ? ORDER BY id",
                           (str(discord_id),)).fetchall()


def get_character(db, discord_id, char_id):
    """A character by row id, only if it belongs to this player (else None)."""
    return db.conn.execute("SELECT * FROM characters WHERE id = ? AND discord_id = ?",
                           (char_id, str(discord_id))).fetchone()


def owns(db, discord_id, name, world):
    return db.conn.execute(
        "SELECT 1 FROM characters WHERE discord_id = ? AND lower(name) = lower(?) AND lower(world) = lower(?)",
        (str(discord_id), name, world)).fetchone() is not None


def delete_character(db, discord_id, char_id):
    with db.conn:
        cur = db.conn.execute("DELETE FROM characters WHERE id = ? AND discord_id = ?",
                              (char_id, str(discord_id)))
    return cur.rowcount > 0


def characters_line(db, discord_id):
    chars = list_characters(db, discord_id)
    if not chars:
        return "Characters: none (use /character add)"
    return "Characters: " + ", ".join(label(c) for c in chars)


# ------------------------------------------------------------------ claims (one pending per player)

def get_claim(db, discord_id):
    return db.conn.execute("SELECT * FROM character_claims WHERE discord_id = ?", (str(discord_id),)).fetchone()


def start_claim(db, discord_id, name, world, now):
    """Begin (or replace) the player's single pending claim. Returns the new code."""
    token = new_token()
    with db.conn:
        db.conn.execute(
            """INSERT INTO character_claims (discord_id, name, world, token, token_utc) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (discord_id) DO UPDATE SET name = excluded.name, world = excluded.world,
                 token = excluded.token, token_utc = excluded.token_utc, last_try_utc = NULL""",
            (str(discord_id), name, world, token, iso(now)))
    return token


def renew_claim(db, discord_id, now):
    token = new_token()
    with db.conn:
        db.conn.execute("UPDATE character_claims SET token = ?, token_utc = ? WHERE discord_id = ?",
                        (token, iso(now), str(discord_id)))
    return token


def claim_fresh(claim, now):
    return now - parse_iso(claim["token_utc"]) <= TOKEN_TTL


def cooldown_left(claim, now):
    """Seconds until the next Lodestone lookup is allowed (0 if now)."""
    if not claim["last_try_utc"]:
        return 0
    left = parse_iso(claim["last_try_utc"]) + VERIFY_COOLDOWN - now
    return max(0, int(left.total_seconds()) + 1) if left.total_seconds() > 0 else 0


def touch_claim(db, discord_id, now):
    with db.conn:
        db.conn.execute("UPDATE character_claims SET last_try_utc = ? WHERE discord_id = ?",
                        (iso(now), str(discord_id)))


def complete_claim(db, discord_id, profile, now):
    """The code was found: store the character and end the claim. Returns (character row, displaced owner id).

    If another player held this Lodestone character, their link is removed (taking over is allowed because
    only someone who controls the profile can get here). Their signups keep pointing at nothing (FK SET NULL).
    """
    did = str(discord_id)
    with db.conn:
        other = db.conn.execute("SELECT discord_id FROM characters WHERE lodestone_id = ? AND discord_id != ?",
                                (profile.lodestone_id, did)).fetchone()
        if other:
            db.conn.execute("DELETE FROM characters WHERE lodestone_id = ?", (profile.lodestone_id,))
        db.conn.execute(
            """INSERT INTO characters (discord_id, lodestone_id, name, world, verified_utc) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (lodestone_id) DO UPDATE SET name = excluded.name, world = excluded.world,
                 verified_utc = excluded.verified_utc""",
            (did, profile.lodestone_id, profile.name, profile.world, iso(now)))
        db.conn.execute("DELETE FROM character_claims WHERE discord_id = ?", (did,))
    row = db.conn.execute("SELECT * FROM characters WHERE lodestone_id = ?", (profile.lodestone_id,)).fetchone()
    return row, (other["discord_id"] if other else None)


# ------------------------------------------------------------------ FFLogs sync

async def refresh(db, fflogs, char, now):
    """Bring a character's name/world/FFLogs id up to date from FFLogs. Returns (row, changed).

    Call this before validating a player's progress. It never raises: with no FFLogs client, an FFLogs
    outage, or a character FFLogs does not know yet, the stored row is returned unchanged.
    """
    if fflogs is None:
        return char, False
    from .fflogs import FFLogsError
    try:
        ident = None
        if char["fflogs_id"]:
            ident = await fflogs.identity(fflogs_id=char["fflogs_id"])
        if ident is None or ident.lodestone_id != char["lodestone_id"]:
            ident = await fflogs.identity(lodestone_id=char["lodestone_id"])
    except FFLogsError:
        return char, False
    if ident is None or ident.lodestone_id != char["lodestone_id"]:
        return char, False
    changed = (ident.name, ident.world) != (char["name"], char["world"])
    with db.conn:
        db.conn.execute("UPDATE characters SET name = ?, world = ?, fflogs_id = ?, synced_utc = ? WHERE id = ?",
                        (ident.name, ident.world, ident.fflogs_id, iso(now), char["id"]))
    return db.conn.execute("SELECT * FROM characters WHERE id = ?", (char["id"],)).fetchone(), changed
