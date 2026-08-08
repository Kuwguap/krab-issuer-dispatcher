-- Optional dispatcher note collected after phone + price; stored on leads and shown at bottom of group post.
-- Run in Supabase SQL Editor if the column is not already present.

ALTER TABLE leads ADD COLUMN IF NOT EXISTS special_request_note TEXT;
