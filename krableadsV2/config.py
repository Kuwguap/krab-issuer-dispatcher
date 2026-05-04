"""Configuration module for environment variables."""
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# Explicitly load .env file from the project root
env_path = Path(__file__).parent / '.env'
load_dotenv(dotenv_path=env_path)


class Config:
    """Application configuration from environment variables."""
    
    # Telegram
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    DRIVER_TELEGRAM_ID = os.getenv("DRIVER_TELEGRAM_ID")
    GROUP_TELEGRAM_ID = os.getenv("GROUP_TELEGRAM_ID")
    # Comma-separated Telegram chat IDs — all receive supervisory copies (same bot must be able to DM each)
    SUPERVISORY_TELEGRAM_ID = os.getenv("SUPERVISORY_TELEGRAM_ID")
    
    # Monday.com
    MONDAY_API_KEY = os.getenv("MONDAY_API_KEY")
    MONDAY_BOARD_ID = os.getenv("MONDAY_BOARD_ID")
    MONDAY_API_URL = "https://api.monday.com/v2"
    
    # OneTimeSecret (strip whitespace and stray '=' from copy-paste in Render dashboard)
    ONETIMESECRET_USERNAME = (os.getenv("ONETIMESECRET_USERNAME") or "").strip().lstrip("=") or None
    ONETIMESECRET_API_KEY = (os.getenv("ONETIMESECRET_API_KEY") or "").strip().lstrip("=") or None
    ONETIMESECRET_URL = (os.getenv("ONETIMESECRET_URL") or "https://clientsphonenumber.com/api/v1/share").strip().lstrip("=")
    ONETIMESECRET_LINK_BASE = (os.getenv("ONETIMESECRET_LINK_BASE") or "https://clientsphonenumber.com/secret/").strip().lstrip("=")
    ONETIMESECRET_PASSPHRASE = (os.getenv("ONETIMESECRET_PASSPHRASE") or "DispatchPassword").strip().lstrip("=")
    
    # Supabase
    SUPABASE_URL = os.getenv("SUPABASE_URL")
    SUPABASE_KEY = os.getenv("SUPABASE_KEY")

    # Paper Investigator (shared Supabase tables; optional — used by main bot when drivers accept leads)
    LOW_PAPER_THRESHOLD = int(os.getenv("LOW_PAPER_THRESHOLD", "5"))
    PAPER_SUPERVISOR_TELEGRAM_ID = (os.getenv("PAPER_SUPERVISOR_TELEGRAM_ID") or "").strip() or None

    # AI / Vision (optional – for image → structured Phase 1)
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip() or None
    OPENAI_VISION_MODEL = os.getenv("OPENAI_VISION_MODEL", "gpt-4o").strip() or "gpt-4o"
    # Receipt check: fallback when DB has no valid receipt_detection_mode. Admin Supabase setting wins over this.
    RECEIPT_DETECTION_MODE = (os.getenv("RECEIPT_DETECTION_MODE") or "").strip().lower() or None

    # VIN decode: choose provider in .env (nhtsa = free, api_ninjas = premium)
    VIN_PROVIDER = (os.getenv("VIN_PROVIDER") or "nhtsa").strip().lower()
    API_NINJAS_API_KEY = (os.getenv("API_NINJAS_API_KEY") or "").strip() or None

    # Shown in the Telegram message when a driver accepts a lead (override in .env)
    DRIVER_PAYMENT_CASHAPP = (os.getenv("DRIVER_PAYMENT_CASHAPP") or "$TriStateTags").strip()
    DRIVER_PAYMENT_VENMO = (os.getenv("DRIVER_PAYMENT_VENMO") or "@TriStateTags").strip()
    DRIVER_PAYMENT_ZELLE = (os.getenv("DRIVER_PAYMENT_ZELLE") or "OrganizeDataOnline@gmail.com").strip()
    DRIVER_PAYMENT_PAYPAL = (os.getenv("DRIVER_PAYMENT_PAYPAL") or "privatedealership@gmail.com").strip()
    DRIVER_PAYMENT_PAGE_URL = (os.getenv("DRIVER_PAYMENT_PAGE_URL") or "www.TriStateTags.com/Payments").strip()

    # Renewal cycle
    RENEWAL_DAYS = int(os.getenv("RENEWAL_DAYS", "28"))
    RENEWAL_ESCALATION_MINUTES = int(os.getenv("RENEWAL_ESCALATION_MINUTES", "5"))

    @classmethod
    def is_vin_lookup_configured(cls) -> bool:
        """True if VIN lookup is available (nhtsa always, or api_ninjas when key set)."""
        if cls.VIN_PROVIDER == "api_ninjas":
            return bool(cls.API_NINJAS_API_KEY)
        return True  # nhtsa or any other → assume available

    @classmethod
    def is_ai_vision_configured(cls) -> bool:
        """Whether image upload in Phase 1 can use AI to extract details."""
        return bool(cls.OPENAI_API_KEY)

    @classmethod
    def receipt_detection_mode_from_env(cls) -> Optional[str]:
        """``strict`` | ``lax`` from env when DB setting is absent; bot prefers Supabase when set."""
        v = (cls.RECEIPT_DETECTION_MODE or "").strip().lower()
        if v in ("strict", "lax"):
            return v
        return None

    @classmethod
    def validate(cls):
        """Validate that all required environment variables are set."""
        required_vars = [
            "TELEGRAM_BOT_TOKEN",
            "ONETIMESECRET_USERNAME",
            "ONETIMESECRET_API_KEY",
            "SUPABASE_URL",
            "SUPABASE_KEY",
        ]
        
        # Monday.com is optional - warn if not set but don't fail
        optional_vars = [
            "MONDAY_API_KEY",
            "MONDAY_BOARD_ID",
        ]
        
        missing = []
        for var in required_vars:
            value = getattr(cls, var)
            # Check if value is None or empty string
            if not value or (isinstance(value, str) and value.strip() == ""):
                missing.append(var)
        
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")
        
        # Warn about optional variables
        missing_optional = []
        for var in optional_vars:
            value = getattr(cls, var)
            if not value or (isinstance(value, str) and value.strip() == ""):
                missing_optional.append(var)
        
        if missing_optional:
            import warnings
            warnings.warn(f"Optional Monday.com variables not set: {', '.join(missing_optional)}. Monday.com integration will be disabled.")
        
        return True
    
    @classmethod
    def is_monday_configured(cls) -> bool:
        """Check if Monday.com is properly configured."""
        return bool(cls.MONDAY_API_KEY and cls.MONDAY_BOARD_ID)

