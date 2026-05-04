-- Supabase Storage: durable receipt images for the admin dashboard
-- Run in Supabase → SQL Editor after main schema.
-- Bot should use the service_role key (or a key allowed to INSERT into storage).

INSERT INTO storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
VALUES (
  'receipts',
  'receipts',
  true,
  5242880,
  ARRAY['image/jpeg', 'image/png', 'image/webp', 'image/gif']::text[]
)
ON CONFLICT (id) DO UPDATE
SET
  public = true,
  file_size_limit = EXCLUDED.file_size_limit,
  allowed_mime_types = EXCLUDED.allowed_mime_types;

-- Allow anonymous/public read so admin UI can use <img src="...public URL...">
DROP POLICY IF EXISTS "receipts_public_select" ON storage.objects;
CREATE POLICY "receipts_public_select"
ON storage.objects
FOR SELECT
TO public
USING (bucket_id = 'receipts');

-- Note: INSERT/UPDATE on storage.objects is allowed for the service_role JWT without RLS checks.
-- If you only have the anon key on the bot, add a restricted INSERT policy or switch to service_role.
