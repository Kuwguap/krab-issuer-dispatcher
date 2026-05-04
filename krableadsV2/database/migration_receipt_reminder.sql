-- Track when we sent a receipt reminder so we only send once per assignment
ALTER TABLE lead_assignments ADD COLUMN IF NOT EXISTS receipt_reminder_sent_at TIMESTAMP WITH TIME ZONE;
