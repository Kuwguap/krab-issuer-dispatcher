-- Extra vehicles on ONE lead.
--
-- A client with two cars used to mean two leads: two reference ids, two prices,
-- two transactions and two receipts. The dispatcher and the driver had no way to
-- see that it was one job. This column lets a single lead carry any number of
-- additional vehicles, so the transaction stays single and only the TAGS multiply.
--
-- Car 1 is untouched: it stays in vehicle_details / plate / tag_control_number,
-- so every existing lead, query and PDF keeps working exactly as before.
--
-- Shape: a JSON array, one object per extra car, each carrying its OWN plate and
-- control number so there is no second array to keep index-aligned:
--   [{"name": "...", "address": "...", "city_state_zip": "...",
--     "vin": "...", "car": "...", "color": "...",
--     "insurance_company": "...", "insurance_policy_number": "...",
--     "plate": "477040V", "tag_control_number": "1234567890",
--     "insurance_card_sent_at": null}]
--
-- Safe to run more than once.

alter table leads
  add column if not exists extra_vehicles jsonb not null default '[]'::jsonb;

comment on column leads.extra_vehicles is
  'Additional vehicles on this lead (car 2, 3, ...). One temp tag is issued per '
  'entry; the reference id, price, phone, driver, dispatcher and receipt stay '
  'single. Car 1 lives in vehicle_details/plate/tag_control_number.';

-- Only a handful of leads ever have extra cars, so index just those.
create index if not exists idx_leads_extra_vehicles
  on leads using gin (extra_vehicles)
  where extra_vehicles <> '[]'::jsonb;
