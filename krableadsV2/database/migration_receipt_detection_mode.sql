-- Ensures admin panel / API can upsert receipt mode (default lax).
INSERT INTO settings (key, value) VALUES ('receipt_detection_mode', 'lax')
ON CONFLICT (key) DO NOTHING;
