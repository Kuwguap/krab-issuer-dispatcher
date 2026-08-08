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
    DRIVER_PAYMENT_CASHAPP = (os.getenv("DRIVER_PAYMENT_CASHAPP") or "$tristatetag").strip()
    DRIVER_PAYMENT_VENMO = (os.getenv("DRIVER_PAYMENT_VENMO") or "@TriStateTags").strip()
    DRIVER_PAYMENT_ZELLE = (os.getenv("DRIVER_PAYMENT_ZELLE") or "OrganizeDataOnline@gmail.com").strip()
    DRIVER_PAYMENT_PAYPAL = (os.getenv("DRIVER_PAYMENT_PAYPAL") or "privatedealership@gmail.com").strip()
    DRIVER_PAYMENT_PAGE_URL = (os.getenv("DRIVER_PAYMENT_PAGE_URL") or "www.TriStateTags.com/Payments").strip()

    # Renewal cycle
    RENEWAL_DAYS = int(os.getenv("RENEWAL_DAYS", "28"))
    RENEWAL_ESCALATION_MINUTES = int(os.getenv("RENEWAL_ESCALATION_MINUTES", "5"))

    # NY FS-20 insurance-card issuance (Resend transactional email).
    # When both are set, the bot offers to email the client a FS-20 PDF after dispatch.
    RESEND_API_KEY = (os.getenv("RESEND_API_KEY") or "").strip().lstrip("=") or None
    RESEND_FROM = (os.getenv("RESEND_FROM") or "").strip().lstrip("=") or None

    # Issuer block printed on the NY FS-20 card.
    # INSURANCE_ISSUER_NAME → agency name (top "Name & Address of Issuer" block)
    # INSURANCE_ISSUER_ADDRESS → pipe-separated multi-line agency address
    # INSURANCE_ISSUER_PHONE → contact phone printed under the carrier line
    # INSURANCE_CARRIER_NAME → underwriting carrier line (e.g. "484 NEW SOUTH INS.CO.")
    INSURANCE_ISSUER_NAME = (os.getenv("INSURANCE_ISSUER_NAME") or "Serviced by AIPSO-SAIP").strip()
    INSURANCE_ISSUER_PHONE = (os.getenv("INSURANCE_ISSUER_PHONE") or "").strip()
    INSURANCE_ISSUER_ADDRESS = (os.getenv("INSURANCE_ISSUER_ADDRESS") or "PO Box 6400|Providence, RI 02940-6200").strip()
    INSURANCE_CARRIER_NAME = (os.getenv("INSURANCE_CARRIER_NAME") or "169 National Specialty Insurance Company").strip()

    # TriStateCoverage portal (POST /api/integrations/clients) — creates dashboard account.
    TRISTATECOVERAGE_API_BASE = (
        os.getenv("TRISTATECOVERAGE_API_BASE") or "https://tristatecoverage.com"
    ).strip().rstrip("/")
    INTEGRATIONS_API_KEY = (os.getenv("INTEGRATIONS_API_KEY") or "").strip().lstrip("=") or None

    # Immediate ledger registration: every new lead posts a PENDING row to the
    # krab-dispatch backend the moment it's created (no waiting for the send).
    KRAB_DISPATCH_API_URL = (os.getenv("KRAB_DISPATCH_API_URL") or "https://krab-dispatch-api.onrender.com").strip().rstrip("/")
    KRAB_DISPATCH_ADMIN_PASSWORD = (os.getenv("KRAB_DISPATCH_ADMIN_PASSWORD") or "").strip() or None

    # Driver GPS tracking site (driver-track on Vercel). Feature fully OFF when
    # base URL unset: accepts send delivery details immediately (old behavior).
    TRACKING_SITE_BASE_URL = (os.getenv("TRACKING_SITE_BASE_URL") or "").strip().lstrip("=").rstrip("/") or None
    # Minutes before a still-pending session triggers a driver reminder +
    # supervisor alert (hard block — details are never auto-sent on timeout).
    TRACKING_TIMEOUT_MINUTES = int(os.getenv("TRACKING_TIMEOUT_MINUTES", "5"))
    # Arrival geofence: when a driver ping lands within this many meters of the
    # delivery destination, the bot DMs them to upload the receipt.
    TRACKING_ARRIVAL_RADIUS_M = int(os.getenv("TRACKING_ARRIVAL_RADIUS_M", "200"))

    # Client follow-up outreach (bot texts the client chasing the VIN).
    # Optional — when unset, follow-ups fall back to email-only client contact.
    TWILIO_ACCOUNT_SID = (os.getenv("TWILIO_ACCOUNT_SID") or "").strip().lstrip("=") or None
    TWILIO_AUTH_TOKEN = (os.getenv("TWILIO_AUTH_TOKEN") or "").strip().lstrip("=") or None
    TWILIO_FROM_NUMBER = (os.getenv("TWILIO_FROM_NUMBER") or "").strip().lstrip("=") or None
    # Agency name + contact block used in client-facing follow-up texts/emails.
    FOLLOWUP_AGENCY_NAME = (os.getenv("FOLLOWUP_AGENCY_NAME") or "Tri State Coverage").strip()
    FOLLOWUP_WEBSITE = (os.getenv("FOLLOWUP_WEBSITE") or "Www.tristatetags.com").strip()
    FOLLOWUP_PHONE = (os.getenv("FOLLOWUP_PHONE") or "551-301-3737").strip()
    # Every client follow-up email is BCC-copied here (empty string disables).
    FOLLOWUP_EMAIL_COPY = (os.getenv("FOLLOWUP_EMAIL_COPY") or "SendReceiptToday@gmail.com").strip()
    # Follow-up email fallback provider: SendGrid (reuse the krab-sender key).
    SENDGRID_API_KEY = (os.getenv("SENDGRID_API_KEY") or "").strip().lstrip("=") or None
    SENDGRID_FROM = (os.getenv("SENDGRID_FROM") or "").strip().lstrip("=") or None

    # NJ Temporary Evidence of Insurance (upstream barcode-app HTTP endpoint).
    BARCODE_APP_BASE_URL = (os.getenv("BARCODE_APP_BASE_URL") or "").strip().rstrip("/") or None

    # External lead ingest API (POST /api/v1/leads/ingest on admin web service)
    LEAD_INGEST_API_KEY = (os.getenv("LEAD_INGEST_API_KEY") or "").strip() or None
    API_LEAD_USER_ID = (os.getenv("API_LEAD_USER_ID") or "tristatetag").strip() or "tristatetag"
    LEAD_INGEST_SOURCE_LABEL = (os.getenv("LEAD_INGEST_SOURCE_LABEL") or "External API").strip()

    @classmethod
    def is_lead_ingest_configured(cls) -> bool:
        return bool(cls.LEAD_INGEST_API_KEY and cls.API_LEAD_USER_ID)

    @classmethod
    def is_tracking_configured(cls) -> bool:
        """True if the driver GPS tracking gate is enabled (site URL set)."""
        return bool(cls.TRACKING_SITE_BASE_URL)

    @classmethod
    def is_twilio_configured(cls) -> bool:
        """True if the bot can text clients directly (follow-up chase SMS)."""
        return bool(cls.TWILIO_ACCOUNT_SID and cls.TWILIO_AUTH_TOKEN and cls.TWILIO_FROM_NUMBER)

    @classmethod
    def is_nj_configured(cls) -> bool:
        """True if the upstream NJ insurance-card endpoint base URL is set."""
        return bool((cls.BARCODE_APP_BASE_URL or "").strip())

    @classmethod
    def is_portal_integration_configured(cls) -> bool:
        """True if TriStateCoverage integrations API key is set."""
        return bool(cls.INTEGRATIONS_API_KEY)

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
    def is_resend_configured(cls) -> bool:
        """True if Resend (NY FS-20 insurance card email delivery) is set up."""
        return bool((cls.RESEND_API_KEY or "").strip() and (cls.RESEND_FROM or "").strip())

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

    # ── krab-tag-bot additions: /api/tag/generate web endpoint + admin ────────
    TAG_API_KEY = (os.getenv("TAG_API_KEY") or "").strip()
    KRAB_API_CORS_ALLOWED_ORIGINS = (os.getenv("KRAB_API_CORS_ALLOWED_ORIGINS") or "*").strip()

    @classmethod
    def admin_ids(cls) -> set:
        raw = os.getenv("ADMIN_TELEGRAM_IDS") or ""
        return {s.strip().strip('"').strip("'") for s in raw.split(",") if s.strip()}

    @classmethod
    def is_admin(cls, telegram_id) -> bool:
        return str(telegram_id).strip() in cls.admin_ids()

    @classmethod
    def ledger_configured(cls) -> bool:
        return bool(cls.KRAB_DISPATCH_API_URL and cls.KRAB_DISPATCH_ADMIN_PASSWORD)

