-- Group lead offers: allow broadcasting a lead to multiple groups
-- Run this in Supabase SQL editor.

CREATE TABLE IF NOT EXISTS group_lead_offers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_id UUID NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    group_id UUID NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    status VARCHAR(50) DEFAULT 'pending', -- pending, accepted, declined
    accepted_by_telegram_id VARCHAR(50),
    accepted_at TIMESTAMP WITH TIME ZONE,
    group_chat_id VARCHAR(50),
    group_message_id BIGINT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(lead_id, group_id)
);

CREATE INDEX IF NOT EXISTS idx_group_lead_offers_lead_id ON group_lead_offers(lead_id);
CREATE INDEX IF NOT EXISTS idx_group_lead_offers_group_id ON group_lead_offers(group_id);
CREATE INDEX IF NOT EXISTS idx_group_lead_offers_status ON group_lead_offers(status);

