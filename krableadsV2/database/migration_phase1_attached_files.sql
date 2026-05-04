-- Phase 1 photos/documents (Telegram file_id payloads) held until single-target group approval Accept.
-- Cleared after forward to the winning group + supervisory, or after broadcast delivery.
ALTER TABLE leads ADD COLUMN IF NOT EXISTS phase1_attached_files JSONB DEFAULT '[]'::jsonb;
