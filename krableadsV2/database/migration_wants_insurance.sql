-- Adds the per-lead "issuer opted into insurance" flag used by the
-- insurance-rides-with-the-tag feature. Additive and reversible.
--
-- The bot code is deploy-safe without this column: writing/reading it is wrapped
-- so the insurance ride-along simply stays dormant until this runs.

ALTER TABLE leads
    ADD COLUMN IF NOT EXISTS wants_insurance boolean NOT NULL DEFAULT false;

-- (Optional) speed up any future "which leads wanted insurance" lookups.
CREATE INDEX IF NOT EXISTS idx_leads_wants_insurance
    ON leads (wants_insurance)
    WHERE wants_insurance = true;
