-- Payment handle / ID (Zelle phone, CashApp $tag, Venmo @user, PayPal email)
-- Run in Supabase SQL Editor after migration_krab_interviewer.sql.

ALTER TABLE interviews
    ADD COLUMN IF NOT EXISTS payment_id TEXT;
