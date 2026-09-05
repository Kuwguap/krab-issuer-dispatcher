-- Who approved a hire. Nothing recorded it before, which mattered less when only
-- supervisors could hire; now that anyone on the team can, an anonymous hire is
-- not good enough. Fail-soft in the bot: a missing column never blocks a hire.
ALTER TABLE interviews ADD COLUMN IF NOT EXISTS hired_by_telegram_id TEXT;
