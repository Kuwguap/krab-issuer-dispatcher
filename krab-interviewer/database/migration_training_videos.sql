-- Multiple training videos for Krab Interviewer.
-- Run in Supabase SQL Editor after migration_paper_shipments.sql.
--
-- Existing single-entry row in bot_settings.training_video keeps working as a
-- fallback; once you upload via /training the new table takes over.

CREATE TABLE IF NOT EXISTS training_videos (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    media_kind TEXT NOT NULL CHECK (media_kind IN ('video','animation','document','photo')),
    media_file_id TEXT NOT NULL,
    caption TEXT,
    added_by_telegram_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_training_videos_created_at ON training_videos(created_at);
