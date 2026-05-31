"""Configuration for Krab Insurance Bot."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")


class Config:
    TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()

    OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip() or None
    OPENAI_VISION_MODEL = (os.getenv("OPENAI_VISION_MODEL") or "gpt-4o").strip() or "gpt-4o"

    RESEND_API_KEY = (os.getenv("RESEND_API_KEY") or "").strip().lstrip("=") or None
    RESEND_FROM = (os.getenv("RESEND_FROM") or "").strip().lstrip("=") or None

    TRISTATECOVERAGE_API_BASE = (
        (os.getenv("TRISTATECOVERAGE_API_BASE") or "https://tristatecoverage.com").strip().rstrip("/")
    )
    INTEGRATIONS_API_KEY = (os.getenv("INTEGRATIONS_API_KEY") or "").strip().lstrip("=") or None

    INSURANCE_ISSUER_NAME = (os.getenv("INSURANCE_ISSUER_NAME") or "Serviced by AIPSO-SAIP").strip()
    INSURANCE_ISSUER_PHONE = (os.getenv("INSURANCE_ISSUER_PHONE") or "").strip()
    INSURANCE_ISSUER_ADDRESS = (
        os.getenv("INSURANCE_ISSUER_ADDRESS") or "PO Box 6400|Providence, RI 02940-6200"
    ).strip()
    INSURANCE_CARRIER_NAME = (
        os.getenv("INSURANCE_CARRIER_NAME") or "169 National Specialty Insurance Company"
    ).strip()

    VIN_PROVIDER = (os.getenv("VIN_PROVIDER") or "nhtsa").strip().lower()
    API_NINJAS_API_KEY = (os.getenv("API_NINJAS_API_KEY") or "").strip() or None

    INTEGRATIONS_API_KEY = (os.getenv("INTEGRATIONS_API_KEY") or "").strip().lstrip("=") or None

    BARCODE_APP_BASE_URL = (os.getenv("BARCODE_APP_BASE_URL") or "").strip().rstrip("/") or None

    @classmethod
    def is_nj_configured(cls) -> bool:
        return bool((cls.BARCODE_APP_BASE_URL or "").strip())

    @classmethod
    def is_ai_vision_configured(cls) -> bool:
        return bool(cls.OPENAI_API_KEY)

    @classmethod
    def is_resend_configured(cls) -> bool:
        return bool(cls.RESEND_API_KEY and cls.RESEND_FROM)

    @classmethod
    def is_portal_integration_configured(cls) -> bool:
        return bool(cls.INTEGRATIONS_API_KEY)

    @classmethod
    def validate(cls) -> None:
        missing = []
        for var in (
            "TELEGRAM_BOT_TOKEN",
            "OPENAI_API_KEY",
            "RESEND_API_KEY",
            "RESEND_FROM",
            "INTEGRATIONS_API_KEY",
        ):
            if not getattr(cls, var):
                missing.append(var)
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")
