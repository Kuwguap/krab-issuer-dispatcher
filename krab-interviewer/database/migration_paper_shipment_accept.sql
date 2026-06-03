-- Paper shipment accept flow: multiple paper girls, first accept wins.
-- Run in Supabase SQL Editor after migration_paper_shipments.sql.

ALTER TABLE paper_shipments
    ADD COLUMN IF NOT EXISTS driver_city TEXT,
    ADD COLUMN IF NOT EXISTS driver_zip TEXT,
    ADD COLUMN IF NOT EXISTS accepted_by_telegram_id TEXT,
    ADD COLUMN IF NOT EXISTS accepted_by_name TEXT,
    ADD COLUMN IF NOT EXISTS accepted_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS offer_messages JSONB NOT NULL DEFAULT '[]'::jsonb;

-- Extend status values (drop old check, re-add)
ALTER TABLE paper_shipments DROP CONSTRAINT IF EXISTS paper_shipments_status_check;

ALTER TABLE paper_shipments
    ADD CONSTRAINT paper_shipments_status_check
    CHECK (status IN (
        'pending_accept',
        'awaiting_tracking',
        'tracking_received',
        'driver_notified',
        'cancelled'
    ));

CREATE INDEX IF NOT EXISTS idx_paper_shipments_accepted_by
    ON paper_shipments(accepted_by_telegram_id)
    WHERE accepted_by_telegram_id IS NOT NULL;
