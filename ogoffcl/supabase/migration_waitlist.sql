-- OG OFFCL: live site lock + mailing list (waitlist / newsletter)
-- Run once in Supabase SQL editor (project ztleknsyyxgtbiebjudq).

create table if not exists site_settings (
  key text primary key,
  value jsonb not null default '{}'::jsonb,
  updated_at timestamptz default now()
);
alter table site_settings enable row level security;
drop policy if exists "site_settings_read" on site_settings;
create policy "site_settings_read" on site_settings for select using (true);
drop policy if exists "site_settings_write" on site_settings;
create policy "site_settings_write" on site_settings for all using (true) with check (true);

insert into site_settings (key, value)
values ('site_locked', '{"locked": false}'::jsonb)
on conflict (key) do nothing;

create table if not exists subscribers (
  id uuid primary key default gen_random_uuid(),
  email text not null unique,
  source text default 'newsletter',      -- 'waitlist' | 'newsletter'
  created_at timestamptz default now(),
  unsubscribed_at timestamptz
);
alter table subscribers enable row level security;
drop policy if exists "subscribers_insert" on subscribers;
create policy "subscribers_insert" on subscribers for insert with check (true);
drop policy if exists "subscribers_read" on subscribers;
create policy "subscribers_read" on subscribers for select using (true);
drop policy if exists "subscribers_update" on subscribers;
create policy "subscribers_update" on subscribers for update using (true) with check (true);
drop policy if exists "subscribers_delete" on subscribers;
create policy "subscribers_delete" on subscribers for delete using (true);
