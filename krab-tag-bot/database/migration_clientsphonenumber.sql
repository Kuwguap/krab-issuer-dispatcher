-- clientsphonenumber: Supabase-backed "OneTimeSecret-compatible" secret storage
-- Tables:
--  1) clientsphonenumber_config: global settings (unlock passphrase, TTL enable, TTL days, one-time deletion)
--  2) clientsphonenumber_secrets: stored secrets (e.g., phone numbers) keyed by secret_key

-- Global config (single row with id=1)
CREATE TABLE IF NOT EXISTS clientsphonenumber_config (
  id INTEGER PRIMARY KEY DEFAULT 1,
  passphrase TEXT NOT NULL DEFAULT 'change-me',
  ttl_enabled BOOLEAN NOT NULL DEFAULT FALSE,
  ttl_days INTEGER NOT NULL DEFAULT 30,
  one_time_delete_enabled BOOLEAN NOT NULL DEFAULT FALSE,
  updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Stored secrets for unlock
CREATE TABLE IF NOT EXISTS clientsphonenumber_secrets (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  secret_key TEXT NOT NULL UNIQUE,
  metadata_key TEXT NOT NULL UNIQUE,
  secret_text TEXT NOT NULL,
  expires_at TIMESTAMP WITH TIME ZONE,
  created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
  deleted_at TIMESTAMP WITH TIME ZONE
);

CREATE INDEX IF NOT EXISTS idx_clientsphonenumber_secrets_expires_at
  ON clientsphonenumber_secrets(expires_at);

CREATE INDEX IF NOT EXISTS idx_clientsphonenumber_secrets_secret_key
  ON clientsphonenumber_secrets(secret_key);

