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

    @classmethod
    def validate(cls) -> None:
        if not cls.TELEGRAM_BOT_TOKEN:
            raise ValueError("TELEGRAM_BOT_TOKEN is required")
