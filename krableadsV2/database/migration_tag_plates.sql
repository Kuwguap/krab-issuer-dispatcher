-- ============================================================
-- NJ temp-tag plate + control-number allocation.
--   • Plate: NJ resident = H######, non-resident = ######V.
--   • Control number: the 10-digit number printed under the plate banner.
--   • Both counters jump by a random 100–300 per allocation so the sequence
--     looks organic (like real dealer plates), not +1.
--   • allocate_temp_plate() is atomic (SELECT … FOR UPDATE) so concurrent web
--     orders and manual leads never mint duplicate plates.
-- Ported from speedy-tags system/supabase/migrations/0003_plate_car_numbers.sql.
-- Run once in the krableadsV2 Supabase SQL editor.
-- ============================================================

-- Singleton counters row.
create table if not exists public.tag_plate_settings (
  id                       integer primary key default 1,
  nj_plate_prefix          text    not null default 'H',
  non_nj_plate_suffix      text    not null default 'V',
  plate_digits             integer not null default 6,
  nj_plate_next_number     bigint  not null default 150706,
  non_nj_plate_next_number bigint  not null default 150706,
  nj_car_next_number       bigint  not null default 6000000000,
  non_nj_car_next_number   bigint  not null default 6000000000,
  updated_at               timestamptz not null default now(),
  constraint tag_plate_settings_singleton check (id = 1)
);

insert into public.tag_plate_settings (id) values (1)
on conflict (id) do nothing;

-- Atomic allocation. Returns { "plate": "...", "car": "##########" }.
create or replace function public.allocate_temp_plate(p_is_nj boolean)
returns jsonb
language plpgsql
as $$
declare
  v_plate_n bigint;
  v_car_n   bigint;
  v_prefix  text;
  v_suffix  text;
  v_digits  integer;
  v_plate   text;
begin
  if p_is_nj then
    select nj_plate_next_number, nj_plate_prefix, plate_digits, nj_car_next_number
      into v_plate_n, v_prefix, v_digits, v_car_n
      from public.tag_plate_settings where id = 1 for update;
    v_plate := v_prefix || lpad(v_plate_n::text, v_digits, '0');
    update public.tag_plate_settings set
      nj_plate_next_number = nj_plate_next_number + floor(random() * 201 + 100)::int,
      nj_car_next_number   = nj_car_next_number   + floor(random() * 201 + 100)::int,
      updated_at = now()
    where id = 1;
  else
    select non_nj_plate_next_number, non_nj_plate_suffix, plate_digits, non_nj_car_next_number
      into v_plate_n, v_suffix, v_digits, v_car_n
      from public.tag_plate_settings where id = 1 for update;
    v_plate := lpad(v_plate_n::text, v_digits, '0') || v_suffix;
    update public.tag_plate_settings set
      non_nj_plate_next_number = non_nj_plate_next_number + floor(random() * 201 + 100)::int,
      non_nj_car_next_number   = non_nj_car_next_number   + floor(random() * 201 + 100)::int,
      updated_at = now()
    where id = 1;
  end if;

  return jsonb_build_object('plate', v_plate, 'car', lpad(v_car_n::text, 10, '0'));
end;
$$;

-- Persist the assigned plate + control number on the lead so re-sends reuse them.
alter table public.leads add column if not exists plate text;
alter table public.leads add column if not exists tag_control_number text;
