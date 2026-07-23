-- ── Page-view tracking (SEO / traffic analytics) ────────────────────────────
-- Fresh table only: takes no locks on existing tables, safe to run any time.
-- lock_timeout aborts politely instead of deadlocking if the DB is busy.
set lock_timeout = '5s';

create table if not exists public.page_views (
  id uuid primary key default gen_random_uuid(),
  path text not null,
  query text,
  referrer text,
  source text,          -- utm_source
  medium text,          -- utm_medium
  campaign text,        -- utm_campaign
  session_id text,      -- random per browser-session id (no cookies, no PII)
  screen_w int,
  lang text,
  created_at timestamptz not null default now()
);

create index if not exists page_views_created_at_idx on public.page_views (created_at desc);
create index if not exists page_views_path_idx on public.page_views (path);

alter table public.page_views enable row level security;

drop policy if exists page_views_insert on public.page_views;
create policy page_views_insert on public.page_views
  for insert to anon, authenticated with check (true);

drop policy if exists page_views_select on public.page_views;
create policy page_views_select on public.page_views
  for select to anon, authenticated using (true);
