-- Group must accept before drivers are notified (single-group issuer flow) + timeout notification
-- Run in Supabase SQL Editor after other lead migrations.

ALTER TABLE leads ADD COLUMN IF NOT EXISTS awaiting_group_accept BOOLEAN DEFAULT FALSE;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS group_accept_timeout_notified_at TIMESTAMP WITH TIME ZONE;
