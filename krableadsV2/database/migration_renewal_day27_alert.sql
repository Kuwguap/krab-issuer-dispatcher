-- Day-27 pre-due renewal alert: 🔔 sent once to the original group + original
-- driver ~24h before renewal_due_at (renewals dispatch on day 28 of the cycle).
-- Run once in the Supabase SQL editor BEFORE deploying the bot.

ALTER TABLE lead_renewals ADD COLUMN IF NOT EXISTS alert_27_sent_at TIMESTAMP WITH TIME ZONE;
