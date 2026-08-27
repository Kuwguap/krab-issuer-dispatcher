-- 🤖 Instant Tag: the toggle and the amount the DRIVER pays by card.
-- driver_amount is price minus $50 by default (auto-refreshed on every price
-- write, hand-editable in ✏️ Edit → 💵 Amount) and is what the Stripe checkout
-- charges before the tag sends itself.
-- Run in the SAME Supabase project as krableadsV2 (the `leads` table).
-- Idempotent: safe to re-run.

ALTER TABLE IF EXISTS leads
    ADD COLUMN IF NOT EXISTS instant_tag BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS driver_amount TEXT;
