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
    DRIVER_CHANNEL_LINK = (os.getenv("DRIVER_CHANNEL_LINK") or "https://t.me/TriStateTags").strip()
    KRAB_SENDER_DATABASE_URL = (os.getenv("KRAB_SENDER_DATABASE_URL") or "").strip() or None
    INTERVIEWER_TIMEZONE = (os.getenv("INTERVIEWER_TIMEZONE") or "America/New_York").strip()
    KRAB_ISSUER_BOT_USERNAME = (os.getenv("KRAB_ISSUER_BOT_USERNAME") or "Krabissuerbot").strip()
    KRAB_DISPATCH_BOT_USERNAME = (os.getenv("KRAB_DISPATCH_BOT_USERNAME") or "Krabdispatchbot").strip()
    KRAB_INTERVIEWER_BOT_USERNAME = (
        os.getenv("KRAB_INTERVIEWER_BOT_USERNAME") or "krabinterviewerbot"
    ).strip()
    PAPER_GIRL_TELEGRAM_ID = (os.getenv("PAPER_GIRL_TELEGRAM_ID") or "").strip() or None
    PAPER_GIRL_USERNAME = (os.getenv("PAPER_GIRL_USERNAME") or "").strip() or None
    DEFAULT_PAPER_QTY = int(os.getenv("DEFAULT_PAPER_QTY") or "10")
    RESEND_API_KEY = (os.getenv("RESEND_API_KEY") or "").strip() or None
    RESEND_FROM = (os.getenv("RESEND_FROM") or "").strip() or None
    USPS_TRACK_URL_BASE = (
        os.getenv("USPS_TRACK_URL_BASE")
        or "https://tools.usps.com/go/TrackConfirmAction?tLabels="
    ).strip()

    # Web form + admin dashboard (FastAPI in same process as bot)
    ADMIN_PASSWORD = (os.getenv("ADMIN_PASSWORD") or "").strip() or None
    IP_HASH_SALT = (os.getenv("IP_HASH_SALT") or "krab-interviewer").strip()
    KRAB_API_CORS_ALLOWED_ORIGINS = (os.getenv("KRAB_API_CORS_ALLOWED_ORIGINS") or "*").strip()
    KRAB_PUBLIC_BASE_URL = (os.getenv("KRAB_PUBLIC_BASE_URL") or "").strip() or None

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
