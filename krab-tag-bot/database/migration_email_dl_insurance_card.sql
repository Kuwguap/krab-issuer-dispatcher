-- Add email + driver's license ID columns to leads, plus tracking columns
-- for the post-dispatch "send NY FS-20 insurance card" workflow.
--
-- Idempotent: safe to re-run.

ALTER TABLE IF EXISTS leads
    ADD COLUMN IF NOT EXISTS email TEXT,
    ADD COLUMN IF NOT EXISTS driver_license_id TEXT,
    ADD COLUMN IF NOT EXISTS insurance_card_policy_number TEXT,
    ADD COLUMN IF NOT EXISTS insurance_card_sent_to_email TEXT,
    ADD COLUMN IF NOT EXISTS insurance_card_sent_at TIMESTAMP WITH TIME ZONE,
    ADD COLUMN IF NOT EXISTS insurance_card_error TEXT;

-- Helpful for admin lookups by email
CREATE INDEX IF NOT EXISTS idx_leads_email ON leads(email);
