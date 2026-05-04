-- Split Phase 2 special request into issuer (group) vs driver-only notes.
ALTER TABLE leads ADD COLUMN IF NOT EXISTS special_request_issuers TEXT;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS special_request_drivers TEXT;

-- Backfill from legacy column
UPDATE leads
SET special_request_issuers = special_request_note
WHERE (special_request_issuers IS NULL OR special_request_issuers = '')
  AND special_request_note IS NOT NULL
  AND special_request_note != '';
