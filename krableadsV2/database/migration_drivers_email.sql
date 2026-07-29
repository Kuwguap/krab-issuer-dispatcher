-- Migration: drivers.email — combined driver add (Krab Issuer + Krab Dispatch)
-- stores the driver's email on the issuer side too. Run once on Supabase.

ALTER TABLE drivers ADD COLUMN IF NOT EXISTS email TEXT;
