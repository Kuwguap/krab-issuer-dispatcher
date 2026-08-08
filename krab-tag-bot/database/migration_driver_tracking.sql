-- Migration: driver GPS tracking (hard location gate before delivery details)
-- When a driver accepts a lead/renewal, the bot creates a tracking session and
-- sends a link to the tracking site (driver-track on Vercel). The site posts
-- location pings here; the bot only sends the full delivery details after the
-- session goes pending -> located (or a supervisor overrides).
-- Run this once on your Supabase database.

CREATE TABLE IF NOT EXISTS driver_tracking_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Opaque URL token (secrets.token_urlsafe(24) from the bot)
    token VARCHAR(64) UNIQUE NOT NULL,

    kind VARCHAR(10) NOT NULL DEFAULT 'lead',        -- 'lead' | 'renewal'
    lead_id UUID REFERENCES leads(id),
    renewal_id UUID,                                  -- lead_renewals.id when kind='renewal'
    driver_id UUID REFERENCES drivers(id),
    driver_name TEXT,                                 -- denormalized: site never reads drivers/leads
    reference_id TEXT,                                -- denormalized: admin display only
    chat_id TEXT NOT NULL,                            -- Telegram chat that gets the deferred details

    -- pending  -> waiting for the driver's first location ping
    -- located  -> ping arrived; bot job will send details
    -- details_sent -> details DM delivered
    -- overridden   -> supervisor sent details without a location (manual unblock)
    status VARCHAR(20) NOT NULL DEFAULT 'pending',

    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    located_at TIMESTAMP WITH TIME ZONE,
    details_sent_at TIMESTAMP WITH TIME ZONE,
    reminder_sent_at TIMESTAMP WITH TIME ZONE         -- driver reminder + supervisor alert sent
);

CREATE INDEX IF NOT EXISTS idx_dts_status ON driver_tracking_sessions(status, created_at);
CREATE INDEX IF NOT EXISTS idx_dts_driver ON driver_tracking_sessions(driver_id, created_at DESC);

CREATE TABLE IF NOT EXISTS driver_location_pings (
    id BIGSERIAL PRIMARY KEY,
    session_token VARCHAR(64) NOT NULL REFERENCES driver_tracking_sessions(token) ON DELETE CASCADE,
    driver_id UUID,
    lat DOUBLE PRECISION NOT NULL,
    lng DOUBLE PRECISION NOT NULL,
    accuracy DOUBLE PRECISION,
    speed DOUBLE PRECISION,
    heading DOUBLE PRECISION,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_dlp_session ON driver_location_pings(session_token, created_at);
CREATE INDEX IF NOT EXISTS idx_dlp_driver ON driver_location_pings(driver_id, created_at DESC);

-- Bot connects with the anon key; keep consistent with the other tables.
ALTER TABLE driver_tracking_sessions DISABLE ROW LEVEL SECURITY;
ALTER TABLE driver_location_pings DISABLE ROW LEVEL SECURITY;
