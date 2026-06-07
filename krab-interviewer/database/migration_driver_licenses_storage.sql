-- Supabase Storage bucket for driver license photos (web form + bot uploads)
-- Run in Supabase SQL Editor if uploads return 500 / "License upload failed".

INSERT INTO storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
VALUES (
    'driver_licenses',
    'driver_licenses',
    true,
    12582912,
    ARRAY['image/jpeg', 'image/png', 'image/webp', 'image/gif']::text[]
)
ON CONFLICT (id) DO UPDATE SET
    public = EXCLUDED.public,
    file_size_limit = EXCLUDED.file_size_limit,
    allowed_mime_types = EXCLUDED.allowed_mime_types;

-- Public read
DROP POLICY IF EXISTS "Public read driver licenses" ON storage.objects;
CREATE POLICY "Public read driver licenses"
    ON storage.objects FOR SELECT
    TO public
    USING (bucket_id = 'driver_licenses');

-- Backend uploads (anon JWT from API server)
DROP POLICY IF EXISTS "Public upload driver licenses" ON storage.objects;
CREATE POLICY "Public upload driver licenses"
    ON storage.objects FOR INSERT
    TO public
    WITH CHECK (bucket_id = 'driver_licenses');

DROP POLICY IF EXISTS "Public update driver licenses" ON storage.objects;
CREATE POLICY "Public update driver licenses"
    ON storage.objects FOR UPDATE
    TO public
    USING (bucket_id = 'driver_licenses');

DROP POLICY IF EXISTS "Public read driver licenses bucket" ON storage.buckets;
CREATE POLICY "Public read driver licenses bucket"
    ON storage.buckets FOR SELECT
    TO public
    USING (id = 'driver_licenses');

-- Legacy policy names (drop if re-running migration)
DROP POLICY IF EXISTS "Service upload driver licenses" ON storage.objects;
DROP POLICY IF EXISTS "Service update driver licenses" ON storage.objects;
DROP POLICY IF EXISTS "Service delete driver licenses" ON storage.objects;
DROP POLICY IF EXISTS "Anon upload driver licenses" ON storage.objects;
DROP POLICY IF EXISTS "Anon update driver licenses" ON storage.objects;
DROP POLICY IF EXISTS "Anon read driver licenses bucket" ON storage.buckets;
