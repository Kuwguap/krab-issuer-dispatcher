"""Configuration for krab-tag-bot (Telegram staff bot + HTTP tag generator)."""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


class Config:
    TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    # Shared secret for POST /api/tag/generate (Bearer). The tristatetags Vercel
    # proxy holds the matching value; the browser never sees it.
    TAG_API_KEY = (os.getenv("TAG_API_KEY") or "").strip()

    SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").strip()
    SUPABASE_KEY = (os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()

    KRAB_API_CORS_ALLOWED_ORIGINS = (os.getenv("KRAB_API_CORS_ALLOWED_ORIGINS") or "*").strip()

    # Telegram user IDs allowed to open /settings (comma-separated).
    ADMIN_TELEGRAM_IDS = {
        s.strip() for s in (os.getenv("ADMIN_TELEGRAM_IDS") or "").split(",") if s.strip()
    }

    # Ledger logging: post each generated tag to tristatetags.com/backend so it
    # shows which staff user made which tag (same reference system as krableadsV2).
    KRAB_DISPATCH_API_URL = (os.getenv("KRAB_DISPATCH_API_URL") or "https://krab-dispatch-api.onrender.com").strip().rstrip("/")
    KRAB_DISPATCH_ADMIN_PASSWORD = (os.getenv("KRAB_DISPATCH_ADMIN_PASSWORD") or "").strip()

    @classmethod
    def is_admin(cls, telegram_id) -> bool:
        return str(telegram_id) in cls.ADMIN_TELEGRAM_IDS

    @classmethod
    def ledger_configured(cls) -> bool:
        return bool(cls.KRAB_DISPATCH_API_URL and cls.KRAB_DISPATCH_ADMIN_PASSWORD)

    @classmethod
    def validate(cls) -> None:
        if not cls.TELEGRAM_BOT_TOKEN:
            raise ValueError("TELEGRAM_BOT_TOKEN is required")
