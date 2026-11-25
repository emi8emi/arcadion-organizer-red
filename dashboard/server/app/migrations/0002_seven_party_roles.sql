-- Parties are seven roles instead of tank/healer/dps: main_tank, off_tank, regen, shield, melee (x2), ranged, caster.
-- There is no automatic conversion (a "tank" could be either tank, a "dps" any of four), so a database that already
-- holds parties with the old roles is refused here instead of being left half-converted. Clear those parties first
-- (for a test database: DELETE FROM parties;) and run the migration again.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM party_members WHERE role IN ('tank', 'healer', 'dps')) THEN
        RAISE EXCEPTION 'party_members still has the old tank/healer/dps roles; delete those parties (DELETE FROM parties) and migrate again';
    END IF;
END
$$;

ALTER TABLE party_members DROP CONSTRAINT party_members_role_check;
ALTER TABLE party_members ADD CONSTRAINT party_members_role_check
    CHECK (role IN ('main_tank', 'off_tank', 'regen', 'shield', 'melee', 'ranged', 'caster'));

COMMENT ON COLUMN players.roles IS 'comma list of roles they can play: main_tank,off_tank,regen,shield,melee,ranged,caster';
