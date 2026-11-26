-- What kind of event a run is. 'roles' (the default, and every existing run) is the seven-role party matchmaking.
-- 'availability' only collects who can make which slot (like when2meet): no roles, prog or limits for players, no
-- solver, no parties. party_size is the group size an availability event is looking for (NULL for 'roles').
-- Additive; existing rows keep working.
ALTER TABLE runs ADD COLUMN mode TEXT NOT NULL DEFAULT 'roles' CHECK (mode IN ('roles', 'availability'));
ALTER TABLE runs ADD COLUMN party_size INTEGER CHECK (party_size IS NULL OR party_size BETWEEN 1 AND 100);
