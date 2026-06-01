"""Configuration for Krab Telegram Forwarder."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")


class Config:
    TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    TELEGRAM_TARGET_CHAT_ID = (os.getenv("TELEGRAM_TARGET_CHAT_ID") or "").strip()
    FORWARDER_API_KEY = (os.getenv("FORWARDER_API_KEY") or "").strip()
    PORT = int(os.getenv("PORT", "8090"))
    DATABASE_PATH = (os.getenv("DATABASE_PATH") or "forwarder.sqlite").strip()

    @classmethod
    def validate(cls, *, require_telegram: bool = True) -> None:
        missing = []
        if not cls.FORWARDER_API_KEY:
            missing.append("FORWARDER_API_KEY")
        if require_telegram:
            if not cls.TELEGRAM_BOT_TOKEN:
                missing.append("TELEGRAM_BOT_TOKEN")
            if not cls.TELEGRAM_TARGET_CHAT_ID:
                missing.append("TELEGRAM_TARGET_CHAT_ID")
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")
