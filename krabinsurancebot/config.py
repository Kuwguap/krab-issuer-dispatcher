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
    # Who may see EVERY entry, not just their own. Same variable name the dispatch
    # bot uses, so one list of people governs both.
    SUPERVISORY_TELEGRAM_ID = (os.getenv("SUPERVISORY_TELEGRAM_ID") or "").strip()

    # NY FS-20 issuer block — fixed (matches NJ TEI layout, carrier code 707).
    # Intentionally NOT read from env: stale Render dashboard values were still
    # printing 746 American Road / Dearborn after code defaults changed.
    NY_CARRIER_NAME = "707 National Specialty Insurance Company"
    NY_AGENCY_NAME = "Serviced by AIPSO-SAIP"
    NY_AGENCY_ADDRESS_LINES = ("PO Box 6400", "Providence, RI 02940-6200")
    INSURANCE_ISSUER_PHONE = (os.getenv("INSURANCE_ISSUER_PHONE") or "").strip()

    # Legacy aliases — always resolve to the fixed NY issuer above.
    INSURANCE_ISSUER_NAME = NY_AGENCY_NAME
    INSURANCE_ISSUER_ADDRESS = "|".join(NY_AGENCY_ADDRESS_LINES)
    INSURANCE_CARRIER_NAME = NY_CARRIER_NAME

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
