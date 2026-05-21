-- Portal login credentials stored when NY FS-20 + TriStateCoverage account is issued.
-- Run in the SAME Supabase project as krableadsV2 (Issuer bot) — the `leads` table.
-- NOT the TriStateCoverage website database.
-- Idempotent: safe to re-run.

ALTER TABLE IF EXISTS leads
    ADD COLUMN IF NOT EXISTS portal_email TEXT,
    ADD COLUMN IF NOT EXISTS portal_password TEXT;
