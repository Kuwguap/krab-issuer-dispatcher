-- Fixing a receipt from the /receipts board.
--
-- A driver photographs the wrong slip and the office is stuck with it. Nothing
-- in the schema could record that a receipt was swapped, or by whom -- and a
-- driver's proof of delivery must not be able to change anonymously.
--
-- Nothing here deletes anything. The old row is MARKED superseded and stepped
-- over; its bytes stay exactly where they are.
--
-- Run in the Supabase SQL editor. Idempotent.

alter table receipt_files
    add column if not exists superseded_at     timestamptz,
    add column if not exists superseded_by     text,
    add column if not exists superseded_reason text;

create index if not exists receipt_files_live_idx
    on receipt_files (lead_id, uploaded_at desc)
    where superseded_at is null;

-- The lead remembers the swap even when the file rows cannot be read.
alter table leads
    add column if not exists receipt_replaced_at  timestamptz,
    add column if not exists receipt_replaced_by  text,
    add column if not exists receipt_previous_url text;

-- SEPARATELY, and more important than the columns above: this service runs on
-- the Supabase ANON key, while migration_receipt_files.sql enabled row-level
-- security on receipt_files and defined NO policy -- it assumed service_role.
-- So every board read of that table returns empty and every write is refused,
-- silently, with a 200. Give krab-issuer-admin the SERVICE_ROLE key. That is
-- the fix; opening this table to anon would make every receipt readable by
-- anyone holding a key that ships in a browser.
