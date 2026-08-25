-- Receipts live in the DATABASE, not on Telegram.
--
-- Telegram file URLs expire about an hour after upload and file_ids are scoped to
-- the bot that received them, so every receipt eventually became a dead link and no
-- amount of re-signing brought it back. The bytes are stored here instead: a row
-- cannot expire, and serving one is a single read.
--
-- Run this in the Supabase SQL editor.

create table if not exists receipt_files (
    id             uuid primary key default gen_random_uuid(),
    lead_id        uuid not null,
    reference_id   text,
    driver_id      uuid,
    content_type   text not null default 'image/jpeg',
    size_bytes     integer not null default 0,
    -- base64 rather than bytea: it survives PostgREST/JSON round trips unchanged,
    -- which is how both the bot and the dashboard reach this table.
    data_base64    text not null,
    source         text not null default 'portal',   -- 'portal' | 'telegram'
    uploaded_at    timestamptz not null default now()
);

-- One receipt per lead is the norm; the newest wins when a driver re-uploads.
create index if not exists receipt_files_lead_idx
    on receipt_files (lead_id, uploaded_at desc);
create index if not exists receipt_files_driver_idx
    on receipt_files (driver_id, uploaded_at desc);

-- The bot and the dashboard both use the service_role key, which bypasses RLS.
-- Enable RLS with no public policy so nothing else can read receipts.
alter table receipt_files enable row level security;
