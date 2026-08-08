-- Migration: driver tracking v2 — destination + ETA support
-- Adds the lead's delivery address to the tracking session (denormalized at
-- accept time) plus cached geocoded destination coordinates, so the admin map
-- can show From/To addresses, the route the driver is supposed to take, and a
-- live ETA. Run once on your Supabase database (after migration_driver_tracking.sql).

ALTER TABLE driver_tracking_sessions ADD COLUMN IF NOT EXISTS delivery_address TEXT;
ALTER TABLE driver_tracking_sessions ADD COLUMN IF NOT EXISTS dest_lat DOUBLE PRECISION;
ALTER TABLE driver_tracking_sessions ADD COLUMN IF NOT EXISTS dest_lng DOUBLE PRECISION;

-- Arrival geofence: set when the bot has told the driver to upload the receipt
-- (fired once, when a ping lands within TRACKING_ARRIVAL_RADIUS_M of the dest).
ALTER TABLE driver_tracking_sessions ADD COLUMN IF NOT EXISTS arrival_notified_at TIMESTAMP WITH TIME ZONE;
