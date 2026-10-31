-- Raid matchmaking bot: SQLite schema (V1 skeleton).
-- All timestamps are UTC ISO-8601 strings. Slot keys ("2026-11-02T17:00") are the
-- grid's local wall-clock time and double as the solver's slot ids.
-- Safe to run repeatedly: every statement is IF NOT EXISTS / INSERT OR IGNORE.

CREATE TABLE IF NOT EXISTS fights (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    short_name  TEXT
);

-- Ordered prog checkpoints per fight. idx is the position the solver measures distance on.
CREATE TABLE IF NOT EXISTS checkpoints (
    fight_id  INTEGER NOT NULL REFERENCES fights(id),
    idx       INTEGER NOT NULL,
    phase     TEXT NOT NULL,
    name      TEXT NOT NULL,
    PRIMARY KEY (fight_id, idx)
);

CREATE TABLE IF NOT EXISTS runs (
    id                   INTEGER PRIMARY KEY,
    guild_id             TEXT NOT NULL,
    name                 TEXT NOT NULL COLLATE NOCASE,   -- 'UWU week 1' and 'uwu week 1' are the same run
    fight_id             INTEGER NOT NULL REFERENCES fights(id),
    start_date           TEXT NOT NULL,          -- first day of the run (grid timezone)
    end_date             TEXT NOT NULL,          -- last day, inclusive
    earliest_start       TEXT,                   -- optional "HH:MM" filter on candidate slots
    latest_start         TEXT,                   -- optional "HH:MM" filter on candidate slots
    signup_deadline_utc  TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'open', 'closed')),
    created_by           TEXT,
    created_utc          TEXT NOT NULL,
    UNIQUE (guild_id, name)
);

-- One row per day of a run: that day's signup cutoff plus export and reminder bookkeeping.
-- `day` is the grid-timezone date ('2026-11-02'), the same prefix the slot keys use.
-- (runs.signup_deadline_utc is kept for old databases; the bot only reads it once, in the migration.)
CREATE TABLE IF NOT EXISTS run_days (
    run_id             INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    day                TEXT NOT NULL,
    cutoff_utc         TEXT NOT NULL,
    exported_utc       TEXT,                 -- set when /draft export ran after the cutoff; stops reminders
    last_reminded_utc  TEXT,
    snapshot_utc       TEXT,                 -- set once the day's signups were frozen (even with zero signups)
    snapshot_late      INTEGER NOT NULL DEFAULT 0,   -- 1 = taken well after the cutoff, so later edits may be in it
    PRIMARY KEY (run_id, day)
);

-- Each day's signups frozen at that day's cutoff: later edits to roles, prog or limits do not touch them.
-- Helper grants are deliberately not snapshotted.
CREATE TABLE IF NOT EXISTS day_snapshots (
    run_id            INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    day               TEXT NOT NULL,
    discord_id        TEXT NOT NULL,
    display_name      TEXT NOT NULL,
    roles             TEXT NOT NULL DEFAULT '',
    prog_index        INTEGER,
    max_per_day       INTEGER NOT NULL,
    max_per_run       INTEGER NOT NULL,
    drop_checkpoints  INTEGER NOT NULL,
    taken_utc         TEXT NOT NULL,
    late              INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, day, discord_id)
);

CREATE TABLE IF NOT EXISTS slots (
    id              INTEGER PRIMARY KEY,
    run_id          INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    slot_key        TEXT NOT NULL,               -- local wall-clock id, e.g. 2026-11-02T17:00
    start_utc       TEXT NOT NULL,
    minutes         INTEGER NOT NULL DEFAULT 90, -- scheduled length
    actual_minutes  INTEGER,                     -- filled in when a slot ends early
    thread_id       TEXT,                        -- Discord thread the approved parties were posted in
    rating_posted_utc TEXT,                      -- when the "Rate your party" buttons went into that thread
    UNIQUE (run_id, slot_key)
);

CREATE TABLE IF NOT EXISTS players (
    discord_id    TEXT PRIMARY KEY,
    display_name  TEXT NOT NULL,
    roles         TEXT NOT NULL DEFAULT '',      -- comma list of roles they can play: tank,healer,dps
    timezone      TEXT NOT NULL DEFAULT 'America/New_York',
    prog_index    INTEGER,                       -- checkpoint idx in the current fight; NULL = not set yet
    updated_utc   TEXT
);

-- characters, character_claims and signup_characters are created by bot/characters.py (ensure_schema).

CREATE TABLE IF NOT EXISTS availability (
    run_id      INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    discord_id  TEXT NOT NULL REFERENCES players(discord_id),
    slot_id     INTEGER NOT NULL REFERENCES slots(id) ON DELETE CASCADE,
    PRIMARY KEY (run_id, discord_id, slot_id)
);

-- Per-run limits. drop_checkpoints = how many checkpoints below their prog a player accepts (0 = not OK).
CREATE TABLE IF NOT EXISTS limits (
    run_id           INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    discord_id       TEXT NOT NULL REFERENCES players(discord_id),
    max_per_day      INTEGER NOT NULL,
    max_per_run      INTEGER NOT NULL,
    drop_checkpoints INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, discord_id)
);

-- Admin-granted helper status, per fight.
CREATE TABLE IF NOT EXISTS helper_grants (
    fight_id     INTEGER NOT NULL REFERENCES fights(id),
    discord_id   TEXT NOT NULL,
    granted_by   TEXT,
    granted_utc  TEXT NOT NULL,
    PRIMARY KEY (fight_id, discord_id)
);

-- Admin-picked "helper X should play with player Y" pairings (soft preference). Not used by the solver yet.
CREATE TABLE IF NOT EXISTS helper_seeds (
    run_id     INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    helper_id  TEXT NOT NULL,
    player_id  TEXT NOT NULL,
    role_lock  TEXT,                              -- optional: force the helper's role
    PRIMARY KEY (run_id, helper_id, player_id)
);

CREATE TABLE IF NOT EXISTS parties (
    id            INTEGER PRIMARY KEY,
    run_id        INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    slot_id       INTEGER NOT NULL REFERENCES slots(id) ON DELETE CASCADE,
    party_index   INTEGER NOT NULL,
    status        TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'confirmed', 'done')),
    target_idx    INTEGER,                        -- checkpoint the party is built around
    message_id    TEXT,                           -- the Discord message that announced the party
    ended_utc     TEXT,                           -- when the party was marked ended (button or automatically)
    ended_by      TEXT,                           -- discord id of whoever pressed it, or 'auto'
    rating_posted_utc TEXT,                       -- when this party's "Rate" button went into the thread
    UNIQUE (run_id, slot_id, party_index)
);

CREATE TABLE IF NOT EXISTS party_members (
    party_id     INTEGER NOT NULL REFERENCES parties(id) ON DELETE CASCADE,
    discord_id   TEXT NOT NULL,
    role         TEXT NOT NULL CHECK (role IN ('tank', 'healer', 'dps')),
    is_helper    INTEGER NOT NULL DEFAULT 0,
    placed_down  INTEGER NOT NULL DEFAULT 0,
    distance     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (party_id, discord_id)
);

-- People left over for a slot (filled by the standby flow later).
CREATE TABLE IF NOT EXISTS standby (
    slot_id     INTEGER NOT NULL REFERENCES slots(id) ON DELETE CASCADE,
    discord_id  TEXT NOT NULL,
    PRIMARY KEY (slot_id, discord_id)
);

-- Minutes actually played, for burnout tracking.
CREATE TABLE IF NOT EXISTS play_log (
    slot_id     INTEGER NOT NULL REFERENCES slots(id) ON DELETE CASCADE,
    discord_id  TEXT NOT NULL,
    minutes     INTEGER NOT NULL,
    PRIMARY KEY (slot_id, discord_id)
);

-- The solver's plain-language reasons for unfilled slots and unmatched players.
CREATE TABLE IF NOT EXISTS draft_notes (
    run_id  INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    kind    TEXT NOT NULL CHECK (kind IN ('slot', 'player')),
    ref     TEXT NOT NULL,                        -- slot key or discord id
    text    TEXT NOT NULL,
    PRIMARY KEY (run_id, kind, ref)
);

CREATE INDEX IF NOT EXISTS idx_slots_run ON slots(run_id);
CREATE INDEX IF NOT EXISTS idx_avail_slot ON availability(slot_id);
CREATE INDEX IF NOT EXISTS idx_parties_run ON parties(run_id);

-- Seed: Ultima Weapon (Ultimate), 19 positions (0 = P1 fresh ... 18 = P4 enrage).
INSERT OR IGNORE INTO fights (id, name, short_name) VALUES (1, 'Ultima Weapon (Ultimate)', 'UWU');

INSERT OR IGNORE INTO checkpoints (fight_id, idx, phase, name) VALUES
    (1,  0, 'P1', 'Fresh'),
    (1,  1, 'P1', 'Plumes'),
    (1,  2, 'P1', 'Cleanse'),
    (1,  3, 'P1', 'Twins bait'),
    (1,  4, 'P1', 'Enrage'),
    (1,  5, 'P2', 'Fresh'),
    (1,  6, 'P2', 'Nails'),
    (1,  7, 'P2', 'Nails cleanup'),
    (1,  8, 'P2', 'Enrage'),
    (1,  9, 'P3', 'Fresh'),
    (1, 10, 'P3', 'Gaols'),
    (1, 11, 'P3', 'Gaols cleanup'),
    (1, 12, 'P3', 'Mario kart'),
    (1, 13, 'P3', 'Enrage'),
    (1, 14, 'Intermission', 'Intermission'),
    (1, 15, 'P4', 'Predation'),
    (1, 16, 'P4', 'Annihilation'),
    (1, 17, 'P4', 'Suppression'),
    (1, 18, 'P4', 'Enrage');

-- Ratings between players who shared a confirmed party. Append-only: the latest row per (voter, target)
-- is the current vote, and 'neutral' is stored so a changed mind clears an earlier like or dislike.
-- Never shown to players; organizers only see aggregates (/votes summary).
CREATE TABLE IF NOT EXISTS votes (
    id           INTEGER PRIMARY KEY,
    run_id       INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    slot_id      INTEGER NOT NULL REFERENCES slots(id) ON DELETE CASCADE,
    voter_id     TEXT NOT NULL,
    target_id    TEXT NOT NULL,
    vote         TEXT NOT NULL CHECK (vote IN ('like', 'neutral', 'dislike')),
    created_utc  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS votes_pair ON votes (voter_id, target_id, created_utc);

-- The parties exactly as the solver made them, frozen when /draft approve confirms them. Never edited, so
-- manual changes can be compared against it later (party_changes holds the changes).
CREATE TABLE IF NOT EXISTS party_originals (
    party_id     INTEGER PRIMARY KEY REFERENCES parties(id) ON DELETE CASCADE,
    run_id       INTEGER NOT NULL,
    slot_id      INTEGER NOT NULL,
    taken_utc    TEXT NOT NULL,
    target_idx   INTEGER,
    members_json TEXT NOT NULL                    -- [{discord_id, role, is_helper, placed_down, distance}]
);

-- Everything done to a party after approval: swaps, ended / undone / overridden. Append-only.
-- reason_code: no_show, cant_make_it, wrong_role, prog_mismatch, other.
CREATE TABLE IF NOT EXISTS party_changes (
    id               INTEGER PRIMARY KEY,
    party_id         INTEGER NOT NULL REFERENCES parties(id) ON DELETE CASCADE,
    run_id           INTEGER NOT NULL,
    slot_id          INTEGER NOT NULL,
    at_utc           TEXT NOT NULL,
    actor_id         TEXT NOT NULL,
    kind             TEXT NOT NULL CHECK (kind IN ('swap', 'role', 'ended', 'end_override', 'end_undo')),
    player_id        TEXT,                        -- swap: the player taken out
    other_player_id  TEXT,                        -- swap: the player put in
    reason_code      TEXT,
    reason_text      TEXT,
    warnings_json    TEXT,                        -- overridden validation warnings
    detail_json      TEXT                         -- roles involved, source of the edit ('panel' or 'import')
);
