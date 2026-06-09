-- Krab Interviewer bot tables (shared Issuer Supabase project)
-- Run in Supabase SQL Editor after deploying krab-interviewer.

-- ---------------------------------------------------------------------------
-- interviews
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS interviews (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'scheduled', 'hired', 'cancelled')),
    created_by_telegram_id TEXT NOT NULL,
    is_supervisor_created BOOLEAN NOT NULL DEFAULT FALSE,
    work_commitment TEXT,
    phone_number TEXT,
    email TEXT,
    mailing_address TEXT,
    drivers_license_id TEXT,
    telegram_username TEXT,
    emergency_contact TEXT,
    referral TEXT,
    payment_method TEXT,
    payment_id TEXT,
    profession_skill TEXT,
    telegram_id TEXT,
    first_name TEXT,
    drivers_license_file_url TEXT,
    understanding_chat_id BIGINT,
    understanding_message_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_interviews_status ON interviews(status);
CREATE INDEX IF NOT EXISTS idx_interviews_created_at ON interviews(created_at DESC);

-- ---------------------------------------------------------------------------
-- appointments (interview call scheduling + reminders)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS appointments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    interview_id UUID NOT NULL REFERENCES interviews(id) ON DELETE CASCADE,
    scheduled_at TIMESTAMPTZ NOT NULL,
    reminder_sent_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'reminded', 'done', 'cancelled')),
    created_by_telegram_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_appointments_scheduled ON appointments(scheduled_at);
CREATE INDEX IF NOT EXISTS idx_appointments_status ON appointments(status);

-- ---------------------------------------------------------------------------
-- announcement_jobs (channel posts + scheduled cron via bot JobQueue)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS announcement_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind TEXT NOT NULL DEFAULT 'text'
        CHECK (kind IN ('text', 'photo', 'video', 'document')),
    body TEXT,
    media_file_id TEXT,
    scheduled_at TIMESTAMPTZ,
    sent_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'sent', 'cancelled')),
    created_by_telegram_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_announcement_jobs_scheduled ON announcement_jobs(scheduled_at);
CREATE INDEX IF NOT EXISTS idx_announcement_jobs_status ON announcement_jobs(status);

-- ---------------------------------------------------------------------------
-- Supabase Storage: create bucket `driver_licenses` (public read) in Dashboard:
-- Storage -> New bucket -> name: driver_licenses -> Public bucket ON
-- ---------------------------------------------------------------------------
