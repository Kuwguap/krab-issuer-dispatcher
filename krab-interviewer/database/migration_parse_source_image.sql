-- AI auto-fill screenshot kept with the application (separate from driver's license photo).
ALTER TABLE interviews ADD COLUMN IF NOT EXISTS parse_source_image_url TEXT;

COMMENT ON COLUMN interviews.parse_source_image_url IS
  'Screenshot/image uploaded for AI auto-fill on the web interview form.';
