-- Additional schema for multi-group support
-- Run this after the main schema.sql

-- Groups table: Manage multiple groups with their supervisory IDs
CREATE TABLE IF NOT EXISTS groups (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    group_name VARCHAR(255) NOT NULL UNIQUE,
    group_telegram_id VARCHAR(50) NOT NULL UNIQUE,
    supervisory_telegram_id VARCHAR(50) NOT NULL,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Drivers table: Manage all drivers
CREATE TABLE IF NOT EXISTS drivers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    driver_name VARCHAR(255) NOT NULL,
    driver_telegram_id VARCHAR(50) NOT NULL UNIQUE,
    phone_number VARCHAR(50),
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Group-Driver assignments: Which drivers belong to which groups
CREATE TABLE IF NOT EXISTS group_drivers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    group_id UUID NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    driver_id UUID NOT NULL REFERENCES drivers(id) ON DELETE CASCADE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(group_id, driver_id)
);

-- Lead assignments: Track which driver accepted which lead
CREATE TABLE IF NOT EXISTS lead_assignments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_id UUID NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    driver_id UUID NOT NULL REFERENCES drivers(id) ON DELETE CASCADE,
    group_id UUID NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    status VARCHAR(50) DEFAULT 'pending', -- pending, accepted, declined
    accepted_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(lead_id, driver_id)
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_groups_telegram_id ON groups(group_telegram_id);
CREATE INDEX IF NOT EXISTS idx_drivers_telegram_id ON drivers(driver_telegram_id);
CREATE INDEX IF NOT EXISTS idx_group_drivers_group_id ON group_drivers(group_id);
CREATE INDEX IF NOT EXISTS idx_group_drivers_driver_id ON group_drivers(driver_id);
CREATE INDEX IF NOT EXISTS idx_lead_assignments_lead_id ON lead_assignments(lead_id);
CREATE INDEX IF NOT EXISTS idx_lead_assignments_driver_id ON lead_assignments(driver_id);
CREATE INDEX IF NOT EXISTS idx_lead_assignments_status ON lead_assignments(status);

-- Update leads table to include group_id
ALTER TABLE leads ADD COLUMN IF NOT EXISTS group_id UUID REFERENCES groups(id);

-- Triggers for updated_at
CREATE TRIGGER update_groups_updated_at BEFORE UPDATE ON groups
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_drivers_updated_at BEFORE UPDATE ON drivers
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();


