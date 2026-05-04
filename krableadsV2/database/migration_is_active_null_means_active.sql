-- NULL is_active was treated as inactive in the bot (dict.get returned None).
-- Normalize to TRUE so DB and app agree.
UPDATE drivers SET is_active = TRUE WHERE is_active IS NULL;
UPDATE groups SET is_active = TRUE WHERE is_active IS NULL;
