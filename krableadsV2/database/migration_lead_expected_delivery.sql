-- When the tag is expected to reach the client.
--
-- The board could say when a lead was ENTERED and, sometimes, when it was
-- DELIVERED -- but never when it was EXPECTED, so there was no way to look at
-- five hundred rows and see which ones are late. The only place a promised time
-- could live was leads.extra_info: free text labelled "Delivery Date/Time &
-- Notes", which also carries pipe-joined public-form keys and goes to drivers
-- verbatim. It cannot be compared to anything.
--
-- NULL means nobody promised a time. The board shows an assumed one -- entry
-- plus twenty-four hours -- says out loud that it is assuming, and never counts
-- it as a broken promise. That assumption is computed at display time and is
-- never written here, so it can never later be mistaken for a commitment
-- somebody actually made, and the rule can change without a data migration.
--
-- Nothing is backfilled, for the same reason migration_lead_delivered_at.sql
-- refuses to guess the rows it cannot know: the only candidate source is
-- extra_info, and parsing it would mint promises nobody made.
--
-- Run this in the Supabase SQL editor. Safe to run more than once.

alter table public.leads
  add column if not exists expected_delivery_at     timestamptz,
  add column if not exists expected_delivery_set_by text;

comment on column public.leads.expected_delivery_at is
  'When the tag is expected to reach the client. Set from the bot as a lead is '
  'entered, or from the receipts board. NULL = nobody promised a time; the '
  'board assumes entry + 24h and says that it is assuming.';

comment on column public.leads.expected_delivery_set_by is
  'Who set that time, as the bot or the board knows them. A promise with no '
  'author is only half a record.';

-- The question the board asks: what is past its time and still not delivered.
create index if not exists leads_expected_delivery_idx
  on public.leads (expected_delivery_at)
  where expected_delivery_at is not null;
