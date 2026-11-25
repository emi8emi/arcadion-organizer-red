-- A day of a run can be skipped: it stays in the run's date range but has no signups and no parties, and the
-- calendar shows it as skipped. Additive and defaulted, so an older bot keeps working (it just does not know
-- about skipped days yet: the bot has to ignore days with skipped = 1 when it takes signups and builds drafts).
ALTER TABLE run_days ADD COLUMN skipped INTEGER NOT NULL DEFAULT 0;
COMMENT ON COLUMN run_days.skipped IS '1 = this day is skipped for the run: no signups, no matchmaking';
