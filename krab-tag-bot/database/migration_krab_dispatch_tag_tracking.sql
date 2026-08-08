-- Krab Dispatch: mark when tag PDF email was sent for this lead (enables name-based matching without duplicate picks).
-- Run in Supabase SQL Editor after deploy.

ALTER TABLE leads ADD COLUMN IF NOT EXISTS krab_tag_pdf_sent_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_leads_krab_tag_pending
  ON leads (created_at DESC)
  WHERE reference_id IS NOT NULL AND krab_tag_pdf_sent_at IS NULL;
