-- Discount codes: fix the "percentage column not found" error and add richer
-- discount types (percent OR fixed amount, audience targeting, minimum order,
-- usage caps). Safe to run more than once.
--
-- Run in the Supabase SQL editor for the OG OFFCL project.

create table if not exists discount_codes (
  id uuid primary key default gen_random_uuid(),
  code text unique not null,
  created_at timestamptz not null default now()
);

-- Core: percent vs fixed amount.
alter table discount_codes add column if not exists discount_type text not null default 'percent'; -- 'percent' | 'amount'
alter table discount_codes add column if not exists percentage numeric;   -- used when discount_type = 'percent' (e.g. 20 = 20% off)
alter table discount_codes add column if not exists amount_off numeric;    -- used when discount_type = 'amount'  (e.g. 50 = GH₵50 off)

-- Targeting / limits.
alter table discount_codes add column if not exists audience text not null default 'all';   -- 'all' | 'new' | 'returning'
alter table discount_codes add column if not exists min_subtotal numeric not null default 0; -- minimum order subtotal to qualify
alter table discount_codes add column if not exists max_uses integer;                        -- total redemption cap (null = unlimited)
alter table discount_codes add column if not exists used_count integer not null default 0;

-- Lifecycle (may already exist).
alter table discount_codes add column if not exists is_active boolean not null default true;
alter table discount_codes add column if not exists expires_at timestamptz;
alter table discount_codes add column if not exists created_at timestamptz not null default now();

-- An earlier schema shipped generic `type` / `value` columns. Keep them from
-- blocking inserts that don't set them (the app now uses the columns above).
do $$
begin
  begin alter table discount_codes alter column type drop not null; exception when others then null; end;
  begin alter table discount_codes alter column value drop not null; exception when others then null; end;
end $$;

-- Backfill: any legacy rows that used `value` as a percent become percent codes.
update discount_codes
   set percentage = coalesce(percentage, value)
 where percentage is null and value is not null;

create index if not exists discount_codes_code_idx on discount_codes (code);

-- Refresh PostgREST's schema cache so the new columns are usable immediately
-- (this is what the "could not find the 'percentage' column in the schema
-- cache" error was about).
notify pgrst, 'reload schema';
