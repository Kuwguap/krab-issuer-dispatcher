"""Configuration for Paper Investigator bot."""
import os
from pathlib import Path
from dotenv import load_dotenv

env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)


class Config:
    TELEGRAM_BOT_TOKEN = os.getenv("PAPER_BOT_TOKEN")
    SUPERVISOR_TELEGRAM_ID = os.getenv("PAPER_SUPERVISOR_TELEGRAM_ID")
    PAPER_GIRL_TELEGRAM_ID = os.getenv("PAPER_GIRL_TELEGRAM_ID")

    SUPABASE_URL = os.getenv("SUPABASE_URL")
    SUPABASE_KEY = os.getenv("SUPABASE_KEY")

    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip() or None
    OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o").strip()

    LOW_PAPER_THRESHOLD = int(os.getenv("LOW_PAPER_THRESHOLD", "5"))
    RECEIPT_REMINDER_HOURS = int(os.getenv("RECEIPT_REMINDER_HOURS", "2"))

    @classmethod
    def validate(cls):
        missing = []
        for var in ("TELEGRAM_BOT_TOKEN", "SUPABASE_URL", "SUPABASE_KEY"):
            if not getattr(cls, var):
                missing.append(var)
        if missing:
            raise ValueError(f"Missing required env vars: {', '.join(missing)}")
        if not cls.SUPERVISOR_TELEGRAM_ID:
            import warnings
            warnings.warn("PAPER_SUPERVISOR_TELEGRAM_ID not set — supervisor commands disabled.")
        if not cls.PAPER_GIRL_TELEGRAM_ID:
            import warnings
            warnings.warn("PAPER_GIRL_TELEGRAM_ID not set — delivery notifications disabled.")
        return True
