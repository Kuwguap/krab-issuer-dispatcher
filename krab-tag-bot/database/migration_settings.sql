-- Global settings (e.g. allow assistants to choose group).
-- Run after schema_multi_group.sql

CREATE TABLE IF NOT EXISTS settings (
    key VARCHAR(128) PRIMARY KEY,
    value TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

INSERT INTO settings (key, value) VALUES ('assistants_choose_group', 'false')
ON CONFLICT (key) DO NOTHING;
