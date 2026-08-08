-- Add column to track when we've sent driver timeout notification (10 min no-accept)
ALTER TABLE leads ADD COLUMN IF NOT EXISTS driver_timeout_notified_at TIMESTAMP WITH TIME ZONE;
