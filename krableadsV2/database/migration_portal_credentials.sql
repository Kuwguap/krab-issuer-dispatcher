-- Portal login credentials stored when NY FS-20 + TriStateCoverage account is issued.
-- Idempotent: safe to re-run.

ALTER TABLE IF EXISTS leads
    ADD COLUMN IF NOT EXISTS portal_email TEXT,
    ADD COLUMN IF NOT EXISTS portal_password TEXT;
