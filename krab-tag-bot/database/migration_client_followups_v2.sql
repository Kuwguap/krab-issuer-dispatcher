-- Migration: client_followups v2 — bot chases the CLIENT, not just the agent
-- Adds: client email, "bot contacts client" flag, a stop date (end_at),
-- kind (followup vs monthly renewal after close), and client-contact tracking.
-- Run this once on your Supabase database (after migration_client_followups.sql).

ALTER TABLE client_followups ADD COLUMN IF NOT EXISTS email TEXT;

-- When TRUE, each reminder tick also texts/emails the client directly
-- (chasing them for the VIN) instead of only DMing the agent.
ALTER TABLE client_followups ADD COLUMN IF NOT EXISTS contact_client BOOLEAN DEFAULT FALSE;

-- Reminders stop automatically after this time (NULL = run until stopped/closed).
ALTER TABLE client_followups ADD COLUMN IF NOT EXISTS end_at TIMESTAMP WITH TIME ZONE;

-- 'followup' = chasing a prospect (e.g. for the VIN); 'renewal' = monthly
-- renewal reminder started when the deal was closed.
ALTER TABLE client_followups ADD COLUMN IF NOT EXISTS kind VARCHAR(20) DEFAULT 'followup';

-- Last time the bot texted/emailed the client (not the agent DM).
ALTER TABLE client_followups ADD COLUMN IF NOT EXISTS last_client_contact_at TIMESTAMP WITH TIME ZONE;
