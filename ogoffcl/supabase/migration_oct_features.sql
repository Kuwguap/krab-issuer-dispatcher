-- ── October features: order authenticity + tournament editions archive ──────
-- Additive only (new column + new table) — deadlock-safe, run any time.
set lock_timeout = '5s';

-- 1) Authenticity code on each order (assigned when the order is paid).
alter table public.orders add column if not exists authenticity_code text;
create index if not exists orders_authenticity_idx on public.orders (authenticity_code);

-- 2) Archive of finished tournaments — one row per edition.
create table if not exists public.tournament_editions (
  id uuid primary key default gen_random_uuid(),
  edition int,
  name text,
  event_date text,
  champion text,           -- winning gamertag
  champion_photo text,     -- public storage URL
  player_count int,
  players jsonb,           -- [{gamertag, platform, photo}]
  matches jsonb,           -- final bracket snapshot
  archived_at timestamptz not null default now()
);
create index if not exists tournament_editions_edition_idx on public.tournament_editions (edition desc);

alter table public.tournament_editions enable row level security;

drop policy if exists tournament_editions_select on public.tournament_editions;
create policy tournament_editions_select on public.tournament_editions
  for select to anon, authenticated using (true);

drop policy if exists tournament_editions_insert on public.tournament_editions;
create policy tournament_editions_insert on public.tournament_editions
  for insert to anon, authenticated with check (true);
