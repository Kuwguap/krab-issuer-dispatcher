-- Cross-checking a transaction, on the board.
--
-- A check-in says: "I have been through this lead and it matches." It is an
-- accountability record, so it carries WHO and WHEN and nothing about the lead
-- itself -- the lead is already on the row beside it.
--
-- Its own table rather than a column on leads: several people cross-check the
-- same transaction, and a column would either hold one of them or turn into a
-- blob every writer has to read, rewrite and race over.
--
-- Run this in the Supabase SQL editor. Safe to run more than once.

create table if not exists lead_checkins (
    id         uuid primary key default gen_random_uuid(),
    lead_id    uuid not null references leads(id) on delete cascade,
    who        text not null,
    checked_at timestamptz not null default now(),
    note       text
);

comment on table lead_checkins is
    'One row per person per lead: that person cross-checked that transaction.';
comment on column lead_checkins.who is
    'The name the board knows them by. Self-declared -- the board has one '
    'shared password, so this is an accountability trail, not an identity.';

-- The board reads these by lead, newest first, for every lead on screen.
create index if not exists idx_lead_checkins_lead
    on lead_checkins (lead_id, checked_at desc);

-- One check per person per lead. Checking twice is not two cross-checks, and
-- without this a double-tap would say two people had looked.
create unique index if not exists uq_lead_checkins_lead_who
    on lead_checkins (lead_id, lower(who));

-- Reachable with the key the board actually holds.
--
-- receipt_files says in its own migration that "the bot and the dashboard both
-- use the service_role key, which bypasses RLS", and enables RLS with no
-- policy on the strength of it. That is not true of this deployment: both run
-- on the ANON key, which is why receipts written from the portal were silently
-- refused for months. So this table does not repeat it. RLS is left off — the
-- gate on this data is the board's own password, and a table nobody can write
-- to is a feature that quietly does nothing.
alter table lead_checkins disable row level security;
grant select, insert, update, delete on lead_checkins to anon, authenticated, service_role;
