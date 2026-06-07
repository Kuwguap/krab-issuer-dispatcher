-- Usernames typed on the web form before Telegram ID is known.
-- When the user taps /start on the bot, we match and fill telegram_id on their draft.
-- Run after migration_interview_drafts.sql

CREATE TABLE IF NOT EXISTS telegram_username_pending (
    username_lower TEXT PRIMARY KEY,
    draft_id UUID REFERENCES interview_drafts(id) ON DELETE CASCADE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_telegram_username_pending_updated
    ON telegram_username_pending (updated_at DESC);
