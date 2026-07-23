-- ── Tournament RLS policies ─────────────────────────────────────────────────
-- The Vercel functions run on the ANON key (no SUPABASE_SERVICE_ROLE_KEY is
-- configured), so the tournament tables need anon policies — same access
-- profile the rest of the site (orders, customers, subscribers) already uses.
-- Policies only, no table locks — deadlock-safe, run any time.
set lock_timeout = '5s';

-- players: register (only ever as pending — nobody can self-insert a paid row)
drop policy if exists tournament_players_insert on public.tournament_players;
create policy tournament_players_insert on public.tournament_players
  for insert to anon, authenticated
  with check (payment_status = 'pending');

drop policy if exists tournament_players_select on public.tournament_players;
create policy tournament_players_select on public.tournament_players
  for select to anon, authenticated using (true);

drop policy if exists tournament_players_update on public.tournament_players;
create policy tournament_players_update on public.tournament_players
  for update to anon, authenticated using (true) with check (true);

drop policy if exists tournament_players_delete on public.tournament_players;
create policy tournament_players_delete on public.tournament_players
  for delete to anon, authenticated using (true);

-- matches: bracket generation / results / public bracket reads
drop policy if exists tournament_matches_select on public.tournament_matches;
create policy tournament_matches_select on public.tournament_matches
  for select to anon, authenticated using (true);

drop policy if exists tournament_matches_insert on public.tournament_matches;
create policy tournament_matches_insert on public.tournament_matches
  for insert to anon, authenticated with check (true);

drop policy if exists tournament_matches_update on public.tournament_matches;
create policy tournament_matches_update on public.tournament_matches
  for update to anon, authenticated using (true) with check (true);

drop policy if exists tournament_matches_delete on public.tournament_matches;
create policy tournament_matches_delete on public.tournament_matches
  for delete to anon, authenticated using (true);

-- ── Hardening (recommended, later) ─────────────────────────────────────────
-- Add SUPABASE_SERVICE_ROLE_KEY on Vercel (Supabase → Settings → API →
-- service_role) — the API automatically prefers it. Once it's live you can
-- lock the tables back down by dropping every policy above:
--   drop policy tournament_players_insert  on public.tournament_players;
--   drop policy tournament_players_select  on public.tournament_players;
--   drop policy tournament_players_update  on public.tournament_players;
--   drop policy tournament_players_delete  on public.tournament_players;
--   drop policy tournament_matches_select  on public.tournament_matches;
--   drop policy tournament_matches_insert  on public.tournament_matches;
--   drop policy tournament_matches_update  on public.tournament_matches;
--   drop policy tournament_matches_delete  on public.tournament_matches;
