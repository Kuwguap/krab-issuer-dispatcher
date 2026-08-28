-- The board's status ladder grows up: from 4 stops to the whole journey.
--
--   new  →  followup  →  tag_issued  →  tag_emailed  →  tag_printed
--        →  on_the_way  →  receipt_uploaded
--
-- The bot advances these automatically (tag sent, driver accepted, receipt
-- handed in); people can still set any of them by hand on /receipts. The old
-- values stay legal so existing rows never violate the constraint:
-- 'delivered' still reads as Delivered, and 'paid' displays as
-- Receipt uploaded.
--
-- Run this in the Supabase SQL editor (idempotent, safe to re-run).
-- Requires migration_lead_delivery_status.sql to have run first.

alter table leads drop constraint if exists leads_delivery_status_check;
alter table leads add constraint leads_delivery_status_check
    check (delivery_status is null
           or delivery_status in ('new', 'followup', 'tag_issued', 'tag_emailed',
                                  'tag_printed', 'on_the_way', 'delivered',
                                  'paid', 'receipt_uploaded'));
