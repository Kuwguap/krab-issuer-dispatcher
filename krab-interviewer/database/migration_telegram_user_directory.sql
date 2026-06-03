-- Cache Telegram username → numeric user id (filled when user /start's the interviewer bot)
-- Run in Issuer Supabase after migration_krab_interviewer.sql

CREATE TABLE IF NOT EXISTS telegram_user_directory (
    telegram_id TEXT PRIMARY KEY,
    telegram_username TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_telegram_user_directory_username
    ON telegram_user_directory (lower(telegram_username));
