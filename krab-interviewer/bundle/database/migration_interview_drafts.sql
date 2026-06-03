-- Krab Interviewer — web form draft tracking (one row per visitor IP hash)
-- Run in Supabase SQL Editor after migration_krab_interviewer.sql

CREATE TABLE IF NOT EXISTS interview_drafts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ip_hash TEXT NOT NULL UNIQUE,
    draft_cookie TEXT NOT NULL,
    user_agent TEXT,
    payload JSONB NOT NULL DEFAULT '{}',
    drivers_license_file_url TEXT,
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'submitted', 'abandoned')),
    submitted_interview_id UUID REFERENCES interviews(id) ON DELETE SET NULL,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    submitted_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_interview_drafts_status ON interview_drafts(status);
CREATE INDEX IF NOT EXISTS idx_interview_drafts_last_seen ON interview_drafts(last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_interview_drafts_submitted ON interview_drafts(submitted_interview_id);
