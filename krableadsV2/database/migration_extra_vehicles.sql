-- Extra vehicles on ONE lead.
--
-- A client with two cars used to mean two leads: two reference ids, two prices,
-- two transactions and two receipts, with nothing tying them together. This
-- column lets a single lead carry any number of additional cars, so the
-- transaction stays single and only the TAGS multiply.
--
-- Car 1 is untouched: it stays in vehicle_details / plate / tag_control_number,
-- so every existing lead, query, message and PDF keeps working exactly as before.
--
-- Shape: a JSON array, one object per extra car, each carrying its OWN plate and
-- control number so there is no second array to keep index-aligned:
--
--   [{"name": "CHARLES G JONES",
--     "address": "11530 Mango terrace drive apt.102",
--     "city_state_zip": "Seffner Florida 33584",
--     "vin": "4T1BF3EK6AU051219",
--     "car": "2010 Toyota Camry",
--     "color": "Grey",
--     "insurance_company": "Progressive",
--     "insurance_policy_number": "982658176",
--     "plate": "477040V",
--     "tag_control_number": "1234567890",
--     "insurance_card_sent_at": null}]
--
-- Additive and idempotent: safe to run more than once, and safe to run while the
-- bot is up. Existing rows backfill to '[]', which reads as "one car" everywhere.

alter table leads
  add column if not exists extra_vehicles jsonb not null default '[]'::jsonb;

comment on column leads.extra_vehicles is
  'Additional cars on this lead (car 2, 3, ...). One temp tag is issued per entry; the reference id, price, phone, driver, dispatcher and receipt stay single. Car 1 lives in vehicle_details/plate/tag_control_number.';

-- No index on purpose. Nothing queries INSIDE this column yet — the bot always
-- reads it via the lead row it already fetched by id. Add a GIN index here when
-- something genuinely searches by VIN or plate across leads, not before.
