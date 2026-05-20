"""Configuration for Krab Interviewer bot."""
import os
from pathlib import Path

from dotenv import load_dotenv

env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)


class Config:
    TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip() or None
    SUPERVISORY_TELEGRAM_ID = os.getenv("SUPERVISORY_TELEGRAM_ID")
    SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").strip() or None
    # Prefer SUPABASE_KEY; fall back to SUPABASE_SERVICE_ROLE_KEY (name used on krab-dispatch).
    SUPABASE_KEY = (
        (os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip() or None
    )
    OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip() or None
    OPENAI_VISION_MODEL = (os.getenv("OPENAI_VISION_MODEL") or "gpt-4o").strip() or "gpt-4o"
    DRIVER_CHANNEL_ID = (os.getenv("DRIVER_CHANNEL_ID") or "").strip() or None
    KRAB_SENDER_DATABASE_URL = (os.getenv("KRAB_SENDER_DATABASE_URL") or "").strip() or None
    INTERVIEWER_TIMEZONE = (os.getenv("INTERVIEWER_TIMEZONE") or "America/New_York").strip()
    KRAB_DISPATCH_BOT_USERNAME = (os.getenv("KRAB_DISPATCH_BOT_USERNAME") or "KrabIssuerBot").strip()

    @classmethod
    def is_ai_configured(cls) -> bool:
        return bool(cls.OPENAI_API_KEY)

    @classmethod
    def validate(cls) -> bool:
        missing = []
        for var in ("TELEGRAM_BOT_TOKEN", "SUPABASE_URL", "SUPABASE_KEY"):
            if not getattr(cls, var):
                missing.append(var)
        if missing:
            hints = []
            if "SUPABASE_URL" in missing or "SUPABASE_KEY" in missing:
                hints.append(
                    "Set SUPABASE_URL + SUPABASE_KEY on Render (krab-interviewer-bot → Environment). "
                    "Copy from krableadsV2 / Issuer, or use SUPABASE_SERVICE_ROLE_KEY instead of SUPABASE_KEY."
                )
            msg = f"Missing required environment variables: {', '.join(missing)}"
            if hints:
                msg += ". " + " ".join(hints)
            raise ValueError(msg)
        return True
