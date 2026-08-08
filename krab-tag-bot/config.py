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

    # AI parsing (same engine as krableadsV2 — build-copied ai_vision.py).
    OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip() or None
    OPENAI_VISION_MODEL = (os.getenv("OPENAI_VISION_MODEL") or "gpt-4o").strip() or "gpt-4o"

    @classmethod
    def is_ai_vision_configured(cls) -> bool:
        return bool(cls.OPENAI_API_KEY)

    KRAB_API_CORS_ALLOWED_ORIGINS = (os.getenv("KRAB_API_CORS_ALLOWED_ORIGINS") or "*").strip()

    # Telegram user IDs allowed to open /settings (comma-separated). Read at
    # call time (not import) and tolerant of surrounding quotes/whitespace.
    @classmethod
    def admin_ids(cls) -> set:
        raw = os.getenv("ADMIN_TELEGRAM_IDS") or ""
        return {s.strip().strip('"').strip("'") for s in raw.split(",") if s.strip()}

    # Ledger logging: post each generated tag to tristatetags.com/backend so it
    # shows which staff user made which tag (same reference system as krableadsV2).
    KRAB_DISPATCH_API_URL = (os.getenv("KRAB_DISPATCH_API_URL") or "https://krab-dispatch-api.onrender.com").strip().rstrip("/")
    KRAB_DISPATCH_ADMIN_PASSWORD = (os.getenv("KRAB_DISPATCH_ADMIN_PASSWORD") or "").strip()

    @classmethod
    def is_admin(cls, telegram_id) -> bool:
        return str(telegram_id).strip() in cls.admin_ids()

    @classmethod
    def ledger_configured(cls) -> bool:
        return bool(cls.KRAB_DISPATCH_API_URL and cls.KRAB_DISPATCH_ADMIN_PASSWORD)

    @classmethod
    def validate(cls) -> None:
        if not cls.TELEGRAM_BOT_TOKEN:
            raise ValueError("TELEGRAM_BOT_TOKEN is required")
