-- $100 insurance add-on payment gate: the FS-20 email to the client is HELD at
-- accept time and released by the dispatcher's "📧 Email insurance to client"
-- button once the receipt is in. insurance_emailed_at NULL = still held.
-- Run in the SAME Supabase project as krableadsV2 (Issuer bot) — the `leads` table.
-- Idempotent: safe to re-run.

ALTER TABLE IF EXISTS leads
    ADD COLUMN IF NOT EXISTS insurance_emailed_at TIMESTAMP WITH TIME ZONE,
    ADD COLUMN IF NOT EXISTS insurance_email_error TEXT;

-- Backfill: every card issued before this feature was emailed at issue time
-- (the old pipeline emailed immediately), so mark them done — otherwise the
-- new button would appear on historical leads and re-email old clients.
-- Re-run this UPDATE once more after the code deploy: leads issued during the
-- deploy window by the old code were also emailed immediately.
UPDATE leads
   SET insurance_emailed_at = insurance_card_sent_at
 WHERE insurance_card_sent_at IS NOT NULL
   AND insurance_emailed_at IS NULL;
