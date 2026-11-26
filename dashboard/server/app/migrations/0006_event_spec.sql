-- Events are described by one JSON spec (see bot/events.py and docs/event-spec.schema.json). The bot and the
-- dashboard both write it; `spec` keeps it on the run exactly as it was made, so an event can be shown,
-- exported and copied later. Runs made before this migration have spec NULL (the bot rebuilds one from days/slots).
-- open_at_utc: when the bot opens the draft by itself (NULL = an organizer opens it). announce_message_id: the
-- announcement the bot posted when it opened, so it is not posted twice. Additive and nullable.
ALTER TABLE runs ADD COLUMN spec JSONB;
ALTER TABLE runs ADD COLUMN open_at_utc TEXT;
ALTER TABLE runs ADD COLUMN announce_message_id TEXT;
