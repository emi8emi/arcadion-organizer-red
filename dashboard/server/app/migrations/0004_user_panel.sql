-- The user panel: a pinned message with buttons (availability, characters) in a channel chosen with
-- /settings set user_panel_channel. The bot keeps the message id so it edits or replaces its own message
-- instead of posting a new one each time. Additive and nullable, so an older bot keeps working.
ALTER TABLE guild_settings ADD COLUMN user_panel_channel_id TEXT;
ALTER TABLE guild_settings ADD COLUMN user_panel_message_id TEXT;
