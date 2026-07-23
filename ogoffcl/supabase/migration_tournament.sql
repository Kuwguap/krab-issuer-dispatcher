-- ── OG OFFCL Tournament (FC26) ──────────────────────────────────────────────
-- Fresh tables only — no locks on existing tables, safe to run any time.
set lock_timeout = '5s';

create table if not exists public.tournament_players (
  id uuid primary key default gen_random_uuid(),
  full_name text not null,
  email text not null,
  phone text not null,
  gamertag text not null,           -- PSN / EA ID
  platform text not null default 'ps5',  -- ps5 | xbox | pc
  photo_path text,                  -- storage path (tournament/ folder)
  hub boolean not null default false,    -- plays from the OGOFFCL Hub (+GH₵20)
  fee numeric not null default 50,
  payment_status text not null default 'pending',  -- pending | paid
  payment_ref text,
  momo_phone text,
  momo_channel text,
  paid_at timestamptz,
  seed int,
  checked_in boolean not null default false,
  created_at timestamptz not null default now()
);

create index if not exists tournament_players_created_idx on public.tournament_players (created_at desc);
create index if not exists tournament_players_ref_idx on public.tournament_players (payment_ref);

create table if not exists public.tournament_matches (
  id uuid primary key default gen_random_uuid(),
  round int not null,               -- 0 = prelims, 1 = first full round, ...
  slot int not null,                -- position within the round
  player1 uuid references public.tournament_players(id) on delete set null,
  player2 uuid references public.tournament_players(id) on delete set null,
  score1 int,
  score2 int,
  winner uuid references public.tournament_players(id) on delete set null,
  created_at timestamptz not null default now(),
  unique (round, slot)
);

-- RLS on, NO anon policies: every read/write goes through the serverless API
-- (service-role key), so emails/phones never leak to the browser.
alter table public.tournament_players enable row level security;
alter table public.tournament_matches enable row level security;
