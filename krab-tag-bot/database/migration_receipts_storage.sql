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

-- The bots upload with the ANON key, so INSERT must be allowed explicitly —
-- without this policy every upload 403s and the bot silently falls back to
-- expiring Telegram URLs (receipts break after ~1 hour).
DROP POLICY IF EXISTS "receipts_anon_insert" ON storage.objects;
CREATE POLICY "receipts_anon_insert"
ON storage.objects
FOR INSERT
TO public
WITH CHECK (bucket_id = 'receipts');
