-- ── Two-legged ties ─────────────────────────────────────────────────────────
-- Every tie is now TWO games between the same opponents: aggregate goals
-- decide, penalties (decider game with ET+pens enabled) if level.
-- score1/score2 become LEG 1 scores; these columns add leg 2 + pens.
-- Additive nullable columns on a tiny table + lock_timeout: deadlock-safe.
set lock_timeout = '5s';

alter table public.tournament_matches
  add column if not exists leg2_score1 int,
  add column if not exists leg2_score2 int,
  add column if not exists pens1 int,
  add column if not exists pens2 int;
