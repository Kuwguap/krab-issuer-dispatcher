-- Add full_name to interviews (first + last stored as one field, split at display time)
ALTER TABLE interviews ADD COLUMN IF NOT EXISTS full_name TEXT;
