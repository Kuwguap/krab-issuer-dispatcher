"""Configuration for krab-tag-bot (web-only NJ temp-tag generator)."""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


class Config:
    # Shared secret for POST /api/tag/generate (Bearer). The tristatetags Vercel
    # proxy holds the matching value; the browser never sees it.
    TAG_API_KEY = (os.getenv("TAG_API_KEY") or "").strip()

    SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").strip()
    SUPABASE_KEY = (os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()

    KRAB_API_CORS_ALLOWED_ORIGINS = (os.getenv("KRAB_API_CORS_ALLOWED_ORIGINS") or "*").strip()

    # Log each generated tag to tristatetags.com/backend (same ledger krableadsV2 uses).
    KRAB_DISPATCH_API_URL = (os.getenv("KRAB_DISPATCH_API_URL") or "https://krab-dispatch-api.onrender.com").strip().rstrip("/")
    KRAB_DISPATCH_ADMIN_PASSWORD = (os.getenv("KRAB_DISPATCH_ADMIN_PASSWORD") or "").strip()

    @classmethod
    def ledger_configured(cls) -> bool:
        return bool(cls.KRAB_DISPATCH_API_URL and cls.KRAB_DISPATCH_ADMIN_PASSWORD)
