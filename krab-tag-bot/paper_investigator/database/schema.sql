-- Paper Investigator schema
-- Uses the SAME Supabase database as Krab Issuer (shares drivers, lead_assignments tables).
-- Run this AFTER Krab Issuer schema_multi_group.sql has been applied.

-- Driver addresses (set by supervisor, used for receipt verification)
CREATE TABLE IF NOT EXISTS driver_addresses (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    driver_id UUID NOT NULL REFERENCES drivers(id) ON DELETE CASCADE UNIQUE,
    address_line TEXT NOT NULL,
    city VARCHAR(255),
    state VARCHAR(50),
    zip_code VARCHAR(20),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Paper inventory: current paper count per driver
CREATE TABLE IF NOT EXISTS paper_inventory (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    driver_id UUID NOT NULL REFERENCES drivers(id) ON DELETE CASCADE UNIQUE,
    current_count INT DEFAULT 0,
    low_alert_sent BOOLEAN DEFAULT FALSE,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Paper transactions: full audit log of every add/subtract
CREATE TABLE IF NOT EXISTS paper_transactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    driver_id UUID NOT NULL REFERENCES drivers(id) ON DELETE CASCADE,
    type VARCHAR(50) NOT NULL,       -- add, subtract_order, adjustment
    amount INT NOT NULL,             -- positive = add, negative = subtract
    balance_after INT NOT NULL,
    reference_id VARCHAR(50),        -- Krab Issuer lead reference_id (if from an order)
    note TEXT,
    created_by BIGINT,               -- telegram user_id who initiated
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Paper delivery orders: supervisor requests paper girl to deliver
CREATE TABLE IF NOT EXISTS paper_delivery_orders (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    driver_id UUID NOT NULL REFERENCES drivers(id) ON DELETE CASCADE,
    quantity INT NOT NULL,
    status VARCHAR(50) DEFAULT 'pending_approval',
    -- pending_approval → approved → delivered / declined
    approved_at TIMESTAMP WITH TIME ZONE,
    delivered_at TIMESTAMP WITH TIME ZONE,
    receipt_image_url TEXT,
    receipt_verified BOOLEAN DEFAULT FALSE,
    receipt_verification_notes TEXT,
    last_reminder_sent_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Track which lead_assignments have already been counted for paper
CREATE TABLE IF NOT EXISTS paper_processed_assignments (
    assignment_id UUID PRIMARY KEY REFERENCES lead_assignments(id) ON DELETE CASCADE,
    driver_id UUID NOT NULL REFERENCES drivers(id) ON DELETE CASCADE,
    processed_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Paper bot settings (key-value)
CREATE TABLE IF NOT EXISTS paper_settings (
    key VARCHAR(100) PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_paper_transactions_driver ON paper_transactions(driver_id);
CREATE INDEX IF NOT EXISTS idx_paper_transactions_created ON paper_transactions(created_at);
CREATE INDEX IF NOT EXISTS idx_paper_delivery_orders_status ON paper_delivery_orders(status);
CREATE INDEX IF NOT EXISTS idx_paper_delivery_orders_driver ON paper_delivery_orders(driver_id);
