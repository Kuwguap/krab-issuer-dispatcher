-- Migration: client_followups table
-- Lightweight tracker for prospective clients who aren't ready for a policy yet
-- (no VIN/car). Lets an agent store the client + a recurring follow-up reminder
-- that DMs the agent on a schedule until they close the sale or stop reminders.
-- Run this once on your Supabase database.

CREATE TABLE IF NOT EXISTS client_followups (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- The agent who owns this follow-up (Telegram id we DM the reminder to)
    user_id TEXT NOT NULL,
    telegram_username TEXT,

    -- Client details
    client_name TEXT,
    phone_number TEXT,
    notes TEXT,                     -- e.g. "no car yet", quote details, etc.

    -- Reminder schedule
    status VARCHAR(20) DEFAULT 'open',      -- open, closed
    frequency VARCHAR(20),                   -- daily, weekly, biweekly, monthly (null = no reminders)
    start_at TIMESTAMP WITH TIME ZONE,
    next_reminder_at TIMESTAMP WITH TIME ZONE,
    last_reminded_at TIMESTAMP WITH TIME ZONE,

    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Due-scan index: the reminder job filters status='open' AND next_reminder_at <= now
CREATE INDEX IF NOT EXISTS idx_client_followups_due ON client_followups(status, next_reminder_at);
CREATE INDEX IF NOT EXISTS idx_client_followups_user ON client_followups(user_id);

-- Trigger to auto-update updated_at (function already defined by the base schema)
CREATE TRIGGER update_client_followups_updated_at
    BEFORE UPDATE ON client_followups
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- The bot connects with the anon key and all base tables (leads, drivers, groups,
-- lead_renewals) run with RLS disabled. Keep this table consistent so the bot can
-- insert/update follow-ups. (New tables sometimes come up with RLS enabled.)
ALTER TABLE client_followups DISABLE ROW LEVEL SECURITY;
