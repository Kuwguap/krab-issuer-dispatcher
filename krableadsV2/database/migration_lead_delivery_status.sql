-- Where a transmission has got to, so the whole team can see and move it.
--
-- The /receipts board works like a Monday column: anyone can set a lead to On the
-- way, Delivered or Paid, and everyone sees it. Kept on the lead itself rather than
-- in a side table because there is exactly one current status per lead and every
-- reader already has the lead row.
--
-- Run this in the Supabase SQL editor.

alter table leads
    add column if not exists delivery_status   text,
    add column if not exists status_updated_at timestamptz,
    add column if not exists status_updated_by text;

-- Only the four the board offers. NULL means "not started" so existing rows need
-- no backfill and read as New.
alter table leads drop constraint if exists leads_delivery_status_check;
alter table leads add constraint leads_delivery_status_check
    check (delivery_status is null
           or delivery_status in ('new', 'on_the_way', 'delivered', 'paid'));

-- The board sorts by most recently touched.
create index if not exists leads_status_updated_idx
    on leads (status_updated_at desc nulls last);
create index if not exists leads_delivery_status_idx
    on leads (delivery_status);
