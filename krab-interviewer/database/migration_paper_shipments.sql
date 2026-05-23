-- Paper shipment flow for Krab Interviewer (hire -> paper girl -> tracking -> driver notify)
-- Run in Supabase SQL Editor after migration_krab_interviewer.sql.

-- ---------------------------------------------------------------------------
-- paper_shipments
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS paper_shipments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    interview_id UUID REFERENCES interviews(id) ON DELETE SET NULL,
    driver_name TEXT NOT NULL,
    driver_address TEXT,
    driver_phone TEXT,
    driver_email TEXT,
    driver_telegram_id TEXT,
    quantity INT NOT NULL DEFAULT 10,
    tracking_number TEXT,
    tracking_zip TEXT,
    receipt_chat_id BIGINT,
    receipt_message_id BIGINT,
    receipt_file_id TEXT,
    status TEXT NOT NULL DEFAULT 'awaiting_tracking'
        CHECK (status IN ('awaiting_tracking', 'tracking_received', 'driver_notified', 'cancelled')),
    created_by_telegram_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_paper_shipments_status ON paper_shipments(status);
CREATE INDEX IF NOT EXISTS idx_paper_shipments_created_at ON paper_shipments(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_paper_shipments_interview_id ON paper_shipments(interview_id);

-- ---------------------------------------------------------------------------
-- bot_settings (training video file_id, etc.)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bot_settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    media_kind TEXT,
    media_file_id TEXT,
    caption TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
