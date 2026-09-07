-- When a delivery actually happened.
--
-- The board could only ever show `status_updated_at`, which is the time of the
-- LAST status change of any kind. A lead that is delivered and then has its
-- receipt uploaded overwrites it, so the delivery time -- the one the office
-- gets asked about -- was quietly lost the moment the paperwork came in.
--
-- Stamped once, by set_lead_status, the first time a lead reaches `delivered`.
-- Never cleared by a later status change: a delivery that happened stays
-- happened. Backing out to an earlier status and delivering again re-stamps it,
-- which is the honest reading of "when was this delivered".
--
-- Safe to run more than once.

alter table public.leads
  add column if not exists delivered_at timestamptz;

comment on column public.leads.delivered_at is
  'When the lead first reached delivery_status = delivered. Stamped by the receipts board; survives later status changes (e.g. receipt_uploaded).';

-- Backfill what can be known: rows sitting at a delivered-ish status right now
-- have not had another status change since, so status_updated_at IS the
-- delivery moment for them. Rows that moved on afterwards cannot be recovered
-- and are deliberately left null rather than given a plausible-looking guess.
update public.leads
   set delivered_at = status_updated_at
 where delivered_at is null
   and status_updated_at is not null
   and delivery_status = 'delivered';

create index if not exists leads_delivered_at_idx
  on public.leads (delivered_at desc)
  where delivered_at is not null;
