-- Strike a lead out of every count.
--
-- The leaderboard, the /settings > Recent Leads browser, the usage stats and the
-- receipt-debt waiver all read leads.exclude_from_count. The column was never
-- created, so every one of those queries answered 42703 and returned nothing --
-- which is why /leaderboard said "No leads counted yet" and Recent Leads said
-- "0 total" on a database full of leads.
--
-- The readers degrade gracefully now (they retry without the column and treat
-- every lead as counting), so running this is what turns STRIKING back on --
-- listing and counting work either way.
--
-- Safe to run more than once.

ALTER TABLE leads
    ADD COLUMN IF NOT EXISTS exclude_from_count BOOLEAN NOT NULL DEFAULT FALSE;

-- The browser pages by created_at and the counts scan the whole table; this
-- keeps both off a sequential scan once the strike flag is actually in use.
CREATE INDEX IF NOT EXISTS idx_leads_exclude_from_count
    ON leads (exclude_from_count)
    WHERE exclude_from_count = TRUE;

COMMENT ON COLUMN leads.exclude_from_count IS
    'TRUE = struck: the lead stops counting on the leaderboard, in usage stats '
    'and towards receipt debt. Set by a supervisor from /settings > Recent Leads '
    'or by an accepted delivery appeal. Reversible.';
