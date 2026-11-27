-- A Discord role per fight and server whose members all count as helpers for that fight (set in /helper).
-- Reading who holds the role needs the bot's Server Members Intent; helper_grants still work without it.
CREATE TABLE helper_roles (
    fight_id  INTEGER NOT NULL REFERENCES fights(id),
    guild_id  TEXT NOT NULL,
    role_id   TEXT NOT NULL,
    set_by    TEXT,
    set_utc   TEXT NOT NULL,
    PRIMARY KEY (fight_id, guild_id)
);
