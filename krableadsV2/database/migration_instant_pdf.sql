-- $100 instant PDF: pay, and the tag goes straight to the chosen driver.
--
-- The normal route waits for a dispatch team to accept. This one skips that: the
-- money is the approval. Every step is written down so a payment can never leave a
-- transaction hanging — the bot delivers from `paid_at` and stamps `delivered_at`,
-- so a crash between the two only delays the tag, never loses it.
--
-- Run this in the Supabase SQL editor.

alter table leads
    add column if not exists instant_pdf_requested_at timestamptz,
    add column if not exists instant_pdf_session_id   text,
    add column if not exists instant_pdf_paid_at      timestamptz,
    add column if not exists instant_pdf_delivered_at timestamptz,
    add column if not exists instant_pdf_driver_id    uuid,
    add column if not exists instant_pdf_amount_cents integer;

-- The delivery poller asks exactly one question: what is paid and not yet
-- delivered? This index is that question.
create index if not exists leads_instant_pdf_pending_idx
    on leads (instant_pdf_paid_at)
    where instant_pdf_paid_at is not null and instant_pdf_delivered_at is null;

-- Stripe retries a webhook until it gets a 2xx, so the same session can arrive
-- several times. One row per session makes the second arrival a no-op.
create unique index if not exists leads_instant_pdf_session_uniq
    on leads (instant_pdf_session_id)
    where instant_pdf_session_id is not null;
