-- OG OFFCL: Moolre payments, order tracking, refunds, email system
-- Run once in the Supabase SQL editor (project ztleknsyyxgtbiebjudq).

-- Order payment + tracking + refund fields
alter table orders add column if not exists momo_phone text;
alter table orders add column if not exists momo_channel text;          -- mtn | telecel | at
alter table orders add column if not exists payment_ref text;           -- moolre transaction id
alter table orders add column if not exists paid_at timestamptz;
alter table orders add column if not exists tracking_note text;         -- rider/courier info shown to you
alter table orders add column if not exists status_history jsonb default '[]'::jsonb;
alter table orders add column if not exists refund_status text;         -- requested | refunded | failed
alter table orders add column if not exists refund_amount numeric;
alter table orders add column if not exists refund_ref text;
alter table orders add column if not exists refunded_at timestamptz;

-- Email templates (admin email builder)
create table if not exists email_templates (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  subject text not null default '',
  html text not null default '',
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);
alter table email_templates enable row level security;
drop policy if exists "email_templates_all" on email_templates;
create policy "email_templates_all" on email_templates for all using (true) with check (true);

-- Campaign history
create table if not exists email_campaigns (
  id uuid primary key default gen_random_uuid(),
  subject text not null,
  html text not null,
  audience text not null default 'newsletter',   -- waitlist | newsletter | both
  sent_count integer default 0,
  failed_count integer default 0,
  created_at timestamptz default now()
);
alter table email_campaigns enable row level security;
drop policy if exists "email_campaigns_all" on email_campaigns;
create policy "email_campaigns_all" on email_campaigns for all using (true) with check (true);
