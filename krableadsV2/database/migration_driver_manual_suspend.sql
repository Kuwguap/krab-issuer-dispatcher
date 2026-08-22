-- Manual driver suspension, set by a supervisor from /settings.
--
-- Suspension was previously DERIVED only: a driver counted as suspended when they
-- had SUSPENSION_THRESHOLD (5) or more accepted leads with no receipt uploaded.
-- There was no way for a supervisor to suspend a driver who owes nothing, and
-- `is_active` could not double as the flag because it is the separate
-- enable/disable switch (an inactive driver disappears from the pickers entirely,
-- whereas a suspended one stays visible and is shown as suspended).
--
-- Safe to re-run. Additive only: no data is modified or removed.
--
-- Run in the Supabase SQL editor:
--   Supabase dashboard -> your project -> SQL Editor -> paste -> Run

ALTER TABLE drivers ADD COLUMN IF NOT EXISTS is_suspended BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN drivers.is_suspended IS
    'Supervisor-set suspension. Independent of receipt-debt suspension, which is '
    'derived from unpaid receipts. A suspended driver stays visible in the pickers '
    'but cannot be assigned new leads.';

-- Until this runs, the bot degrades gracefully: manual suspend reports that the
-- migration is pending, and receipt-debt suspension keeps working as before.
