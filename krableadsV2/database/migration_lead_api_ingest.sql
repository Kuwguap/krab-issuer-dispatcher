`-- External lead ingest API: queue dispatch from bot worker + store source order id
-- Run in Supabase SQL Editor before enabling POST /api/v1/leads/ingest

ALTER TABLE leads ADD COLUMN IF NOT EXISTS ingest_dispatch_pending BOOLEAN DEFAULT FALSE;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS external_order_id TEXT;

CREATE INDEX IF NOT EXISTS idx_leads_ingest_dispatch_pending
  ON leads (ingest_dispatch_pending) WHERE ingest_dispatch_pending = TRUE;

COMMENT ON COLUMN leads.ingest_dispatch_pending IS
  'When true, bot worker posts Accept buttons to all groups then clears flag.';
COMMENT ON COLUMN leads.external_order_id IS
  'Order id from external website (e.g. bf6923ca from Order #bf6923ca).';
`