-- The sender's NAME ("JB", "Sensei") — what the leaderboard groups by and what
-- Skip Dispatch posts show, instead of the @handle. Captured from Telegram's
-- profile name at lead creation; older rows fall back to telegram_username in
-- every display, so nothing goes blank.
-- Run in the SAME Supabase project as krableadsV2 (the `leads` table).
-- Idempotent: safe to re-run.

ALTER TABLE IF EXISTS leads
    ADD COLUMN IF NOT EXISTS telegram_name TEXT;
