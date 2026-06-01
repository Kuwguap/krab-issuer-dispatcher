"""Configuration for Krab Facebook Sales Bot."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

DEFAULT_SYSTEM_PROMPT = """You are a friendly, professional sales representative for Tri State Coverage (auto insurance).
You chat with Facebook Messenger leads to collect information for a temporary NY insurance card.

Collect these fields (one or two missing items per message — do not overwhelm):
- Full name
- Registration address and city/state/ZIP
- Delivery address and city/state/ZIP (if different; otherwise same as registration)
- 17-character VIN
- Year, make, model
- Vehicle color
- Current insurance company and policy number (if known)
- When they need delivery / any extra notes
- Phone number
- Email address

Be warm, concise, and helpful. Never invent data. If the user gives multiple fields at once, acknowledge them.
When all fields are collected, thank them and say a team member will follow up shortly with their documents.
"""


REQUIRED_SLOTS = (
    "name",
    "address",
    "city_state_zip",
    "delivery_address",
    "delivery_city_state_zip",
    "vin",
    "year_make_model",
    "color",
    "current_insurance",
    "policy_number",
    "delivery_time",
    "phone",
    "email",
)


class Config:
    OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip() or None
    OPENAI_MODEL = (os.getenv("OPENAI_MODEL") or "gpt-4o-mini").strip()

    META_VERIFY_TOKEN = (os.getenv("META_VERIFY_TOKEN") or "").strip()
    META_PAGE_ACCESS_TOKEN = (os.getenv("META_PAGE_ACCESS_TOKEN") or "").strip()
    META_APP_SECRET = (os.getenv("META_APP_SECRET") or "").strip()

    _fwd = (os.getenv("FORWARDER_BASE_URL") or "http://localhost:8090").strip().rstrip("/")
    if _fwd and not _fwd.startswith(("http://", "https://")):
        _fwd = f"https://{_fwd}"
    FORWARDER_BASE_URL = _fwd
    FORWARDER_API_KEY = (os.getenv("FORWARDER_API_KEY") or "").strip()

    FB_BOT_SYSTEM_PROMPT = (os.getenv("FB_BOT_SYSTEM_PROMPT") or DEFAULT_SYSTEM_PROMPT).strip()
    PORT = int(os.getenv("PORT", "8080"))
    DATABASE_PATH = (os.getenv("DATABASE_PATH") or "krabfbbot.sqlite").strip()
    RETRY_INTERVAL_SECONDS = int(os.getenv("RETRY_INTERVAL_SECONDS", "60"))

    @classmethod
    def validate(cls) -> None:
        missing = []
        if not cls.META_VERIFY_TOKEN:
            missing.append("META_VERIFY_TOKEN")
        if not cls.META_PAGE_ACCESS_TOKEN:
            missing.append("META_PAGE_ACCESS_TOKEN")
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")
