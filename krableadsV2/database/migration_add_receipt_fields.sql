-- Migration: Add receipt submission fields to existing leads table
-- Run this SQL in your Supabase SQL Editor if you already have the leads table

-- Add reference_id column (will fail if column already exists, which is fine)
ALTER TABLE leads ADD COLUMN IF NOT EXISTS reference_id VARCHAR(20);

-- Add unique constraint on reference_id (only if column was just created)
-- Note: This may fail if there are existing NULL values, which is okay
-- You can manually add the constraint later if needed
-- ALTER TABLE leads ADD CONSTRAINT leads_reference_id_unique UNIQUE (reference_id);

-- Add receipt_image_url column
ALTER TABLE leads ADD COLUMN IF NOT EXISTS receipt_image_url TEXT;

-- Add extra_info column to store additional notes from Phase 1
ALTER TABLE leads ADD COLUMN IF NOT EXISTS extra_info TEXT;

-- Create index on reference_id
CREATE INDEX IF NOT EXISTS idx_leads_reference_id ON leads(reference_id);

