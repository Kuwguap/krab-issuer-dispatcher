-- Contact info sources (managed in admin), ST Telegram ID, and bot usage tracking
-- Run after schema_multi_group.sql and migration_settings.sql

-- Contact info sources: options for "Select the Contact info source for this client"
CREATE TABLE IF NOT EXISTS contact_info_sources (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    label VARCHAR(255) NOT NULL,
    sort_order INT NOT NULL DEFAULT 0,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Lead contact source (set after driver selection)
ALTER TABLE leads ADD COLUMN IF NOT EXISTS contact_info_source TEXT;

-- Settings: ST Telegram ID (notify on every successful lead send)
INSERT INTO settings (key, value) VALUES ('st_telegram_id', '')
ON CONFLICT (key) DO NOTHING;

-- Bot usage: who used the bot and who they sent to (for admin view)
CREATE TABLE IF NOT EXISTS bot_usage (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_telegram_id BIGINT NOT NULL,
    telegram_username VARCHAR(255),
    lead_id UUID REFERENCES leads(id) ON DELETE SET NULL,
    group_name VARCHAR(255),
    driver_names TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_bot_usage_user ON bot_usage(user_telegram_id);
CREATE INDEX IF NOT EXISTS idx_bot_usage_created ON bot_usage(created_at DESC);
