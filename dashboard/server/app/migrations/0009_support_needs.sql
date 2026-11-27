-- Players who should be seated with other players who can carry them: "needs N helpers", per fight.
-- Set by organizers (/helper support). The export sends it to the solver as players[].needs_support
-- {"helpers": N, "required": bool}; required = hard rule, otherwise a soft preference. Absent row = no need.
CREATE TABLE support_needs (
    fight_id    INTEGER NOT NULL REFERENCES fights(id),
    discord_id  TEXT NOT NULL,
    helpers     INTEGER NOT NULL CHECK (helpers BETWEEN 1 AND 7),
    required    BOOLEAN NOT NULL DEFAULT FALSE,
    set_by      TEXT,
    set_utc     TEXT NOT NULL,
    PRIMARY KEY (fight_id, discord_id)
);
