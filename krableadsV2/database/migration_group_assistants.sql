-- Assistants: Telegram user IDs that can send leads; leads go to their assigned group.
-- Run after schema_multi_group.sql

CREATE TABLE IF NOT EXISTS group_assistants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    group_id UUID NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    telegram_id VARCHAR(50) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(group_id, telegram_id)
);

CREATE INDEX IF NOT EXISTS idx_group_assistants_group_id ON group_assistants(group_id);
CREATE INDEX IF NOT EXISTS idx_group_assistants_telegram_id ON group_assistants(telegram_id);
