-- Lead appeals: drivers/issuers can request delivery cancellation with proof; supervisors approve/decline.
-- Run in Supabase SQL editor.

CREATE TABLE IF NOT EXISTS lead_appeals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_id UUID NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    reference_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'accepted', 'declined')),
    proof_image_url TEXT,
    proof_file_id TEXT,
    submitted_by_telegram_id BIGINT NOT NULL,
    submitted_by_username TEXT,
    submitted_by_display_name TEXT,
    lead_client_name TEXT,
    reviewed_by_telegram_id BIGINT,
    reviewed_by_display_name TEXT,
    reviewed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_lead_appeals_lead_id ON lead_appeals(lead_id);
CREATE INDEX IF NOT EXISTS idx_lead_appeals_status ON lead_appeals(status);
CREATE INDEX IF NOT EXISTS idx_lead_appeals_reference_id ON lead_appeals(reference_id);

ALTER TABLE leads ADD COLUMN IF NOT EXISTS exclude_from_count BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS appeal_status TEXT;

COMMENT ON COLUMN leads.exclude_from_count IS 'When true, lead is excluded from stats and receipt debt counts (appeal accepted).';
COMMENT ON COLUMN leads.appeal_status IS 'pending | accepted | declined — for admin UI highlighting.';
