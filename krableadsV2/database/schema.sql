-- Supabase Database Schema for Krab Issuer
-- Run this SQL in your Supabase SQL Editor
--
-- NOTE: If you already have the leads table, use migration_add_receipt_fields.sql instead
-- to add the new columns without recreating the table.

-- States table: Tracks conversation state for each user
CREATE TABLE IF NOT EXISTS states (
    user_id BIGINT PRIMARY KEY,
    state VARCHAR(50) NOT NULL,
    data JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Leads table: Stores all lead information
CREATE TABLE IF NOT EXISTS leads (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id BIGINT NOT NULL,
    telegram_username VARCHAR(255),
    
    -- Phase 1: Vehicle and delivery details
    vehicle_details TEXT,
    delivery_details TEXT,
    
    -- Phase 2: Contact and pricing
    phone_number TEXT, -- Will be encrypted via OneTimeSecret
    price TEXT,

    -- Optional client identity (used by NY FS-20 insurance-card issuance)
    email TEXT,
    driver_license_id TEXT,

    -- Insurance-card issuance tracking (NY FS-20 PDF + Resend email)
    insurance_card_policy_number TEXT,
    insurance_card_sent_to_email TEXT,
    insurance_card_sent_at TIMESTAMP WITH TIME ZONE,
    insurance_card_error TEXT,

    -- OneTimeSecret integration
    onetimesecret_token VARCHAR(255),
    onetimesecret_secret_key VARCHAR(255),
    encrypted_link TEXT,
    
    -- Monday.com integration
    monday_item_id BIGINT,
    monday_status VARCHAR(50) DEFAULT 'Pending',
    
    -- Receipt submission
    reference_id VARCHAR(20) UNIQUE, -- Unique reference ID for driver receipt submission
    receipt_image_url TEXT, -- URL of uploaded receipt image
    
    -- Timestamps (NY Time)
    issue_date TIMESTAMP WITH TIME ZONE,
    expiration_date TIMESTAMP WITH TIME ZONE, -- issue_date + 30 days
    
    -- Extra info from Phase 1 (e.g., delivery time / notes)
    extra_info TEXT,
    -- Phase 2 notes after phone/price (legacy single column; issuers/drivers split below)
    special_request_note TEXT,
    special_request_issuers TEXT,  -- note for group / issuers (broadcast + group post)
    special_request_drivers TEXT,   -- note only for drivers (DM + accept message)
    
    -- Metadata
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Indexes for better query performance
CREATE INDEX IF NOT EXISTS idx_states_user_id ON states(user_id);
CREATE INDEX IF NOT EXISTS idx_leads_user_id ON leads(user_id);
CREATE INDEX IF NOT EXISTS idx_leads_monday_item_id ON leads(monday_item_id);
CREATE INDEX IF NOT EXISTS idx_leads_created_at ON leads(created_at);
CREATE INDEX IF NOT EXISTS idx_leads_reference_id ON leads(reference_id);
CREATE INDEX IF NOT EXISTS idx_leads_email ON leads(email);

-- Function to update updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Triggers to auto-update updated_at
-- Drop-then-create so re-running schema.sql against an existing DB never errors
-- with "trigger ... already exists" (Postgres < 14 lacks CREATE OR REPLACE TRIGGER).
DROP TRIGGER IF EXISTS update_states_updated_at ON states;
CREATE TRIGGER update_states_updated_at BEFORE UPDATE ON states
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS update_leads_updated_at ON leads;
CREATE TRIGGER update_leads_updated_at BEFORE UPDATE ON leads
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

