-- Migration: lead_renewals table
-- Tracks 28-day renewal cycles for accepted leads.
-- Run this once on your Supabase database.

CREATE TABLE IF NOT EXISTS lead_renewals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_id UUID NOT NULL REFERENCES leads(id) ON DELETE CASCADE,

    -- Who originally handled this lead
    original_group_id UUID REFERENCES groups(id),
    original_driver_id UUID REFERENCES drivers(id),

    -- When renewal is due
    renewal_due_at TIMESTAMP WITH TIME ZONE NOT NULL,

    -- Group acceptance phase
    group_status VARCHAR(50) DEFAULT 'pending',       -- pending, sent, escalated, accepted
    group_accepted_by_id UUID REFERENCES groups(id),  -- which group accepted the renewal
    group_sent_at TIMESTAMP WITH TIME ZONE,
    group_escalated_at TIMESTAMP WITH TIME ZONE,

    -- Driver acceptance phase
    driver_status VARCHAR(50) DEFAULT 'pending',        -- pending, sent, escalated, accepted
    driver_accepted_by_id UUID REFERENCES drivers(id),  -- which driver accepted the renewal
    driver_sent_at TIMESTAMP WITH TIME ZONE,
    driver_escalated_at TIMESTAMP WITH TIME ZONE,

    -- Message tracking (so we can edit messages later)
    group_message_chat_id TEXT,
    group_message_id BIGINT,
    driver_message_chat_id TEXT,
    driver_message_id BIGINT,

    -- Overall status
    status VARCHAR(50) DEFAULT 'pending',  -- pending, group_phase, driver_phase, completed
    completed_at TIMESTAMP WITH TIME ZONE,

    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_lead_renewals_status ON lead_renewals(status);
CREATE INDEX IF NOT EXISTS idx_lead_renewals_due ON lead_renewals(renewal_due_at);
CREATE INDEX IF NOT EXISTS idx_lead_renewals_lead ON lead_renewals(lead_id);

-- Trigger to auto-update updated_at
CREATE TRIGGER update_lead_renewals_updated_at
    BEFORE UPDATE ON lead_renewals
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();
