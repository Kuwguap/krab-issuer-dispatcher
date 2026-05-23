-- Supervisor email addresses for appointment reminders.
CREATE TABLE IF NOT EXISTS supervisor_emails (
    telegram_id TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
