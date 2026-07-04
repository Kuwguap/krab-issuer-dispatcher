from dataclasses import dataclass
import os

from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class BotConfig:
    telegram_bot_token: str
    email_provider: str
    email_from_address: str
    email_to_address: str
    admin_password: str
    email_smtp_host: str
    email_smtp_port: int
    email_smtp_username: str
    email_smtp_password: str
    sendgrid_api_key: str
    api_base_url: str
    issuer_group_chat_id: str  # Optional: Telegram chat ID to notify when send is successful
    highkage_team_chat_id: str
    sensei_group_handles: str
    highkage_group_handles: str
    # Optional: same Supabase project as Krab Issuer — match PDF names to open leads and show receipts in admin.
    supabase_url: str
    supabase_service_role_key: str
    # NY FS-20 insurance card (Resend + TriStateCoverage portal)
    resend_api_key: str
    resend_from: str
    integrations_api_key: str
    tristatecoverage_api_base: str
    barcode_app_base_url: str

    @classmethod
    def from_env(cls) -> "BotConfig":
        """
        Load configuration from environment variables.

        This centralizes all config so later phases (DB, cron, dashboard)
        can extend this class without touching the bot logic.
        """
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        if not token:
            raise RuntimeError(
                "TELEGRAM_BOT_TOKEN is not set. "
                "Create a .env file (see config.example.env.txt) and set TELEGRAM_BOT_TOKEN."
            )

        return cls(
            telegram_bot_token=token,
            email_provider=os.getenv("EMAIL_PROVIDER", "stub"),
            email_from_address=os.getenv("EMAIL_FROM_ADDRESS", "krab-dispatch@example.com"),
            email_to_address=os.getenv("EMAIL_TO_ADDRESS", "destination@example.com"),
            admin_password=os.getenv("ADMIN_PASSWORD", "AdminPassword123!"),
            email_smtp_host=os.getenv("EMAIL_SMTP_HOST", "smtp.gmail.com"),
            email_smtp_port=int(os.getenv("EMAIL_SMTP_PORT", "587")),
            email_smtp_username=os.getenv("EMAIL_SMTP_USERNAME", ""),
            email_smtp_password=os.getenv("EMAIL_SMTP_PASSWORD", ""),
            sendgrid_api_key=(os.getenv("SENDGRID_API_KEY") or "").strip().lstrip("="),
            api_base_url=os.getenv("API_BASE_URL", "http://127.0.0.1:8000"),
            issuer_group_chat_id=(os.getenv("ISSUER_GROUP_CHAT_ID") or "").strip(),
            highkage_team_chat_id=(os.getenv("HIGHKAGE_TEAM_CHAT_ID") or "").strip(),
            sensei_group_handles=(os.getenv("SENSEI_GROUP_HANDLES") or "").strip(),
            highkage_group_handles=(os.getenv("HIGHKAGE_GROUP_HANDLES") or "").strip(),
            supabase_url=(os.getenv("SUPABASE_URL") or "").strip().rstrip("/"),
            supabase_service_role_key=(
                os.getenv("SUPABASE_SERVICE_ROLE_KEY")
                or os.getenv("SUPABASE_KEY")
                or ""
            ).strip(),
            resend_api_key=(os.getenv("RESEND_API_KEY") or "").strip().lstrip("="),
            resend_from=(os.getenv("RESEND_FROM") or "").strip().lstrip("="),
            integrations_api_key=(os.getenv("INTEGRATIONS_API_KEY") or "").strip().lstrip("="),
            tristatecoverage_api_base=(
                os.getenv("TRISTATECOVERAGE_API_BASE") or "https://tristatecoverage.com"
            ).strip().rstrip("/"),
            barcode_app_base_url=(os.getenv("BARCODE_APP_BASE_URL") or "").strip().rstrip("/"),
        )

    def supabase_configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_service_role_key)

    def is_resend_configured(self) -> bool:
        return bool(self.resend_api_key and self.resend_from)

    def is_portal_integration_configured(self) -> bool:
        return bool(self.integrations_api_key)

    def is_nj_configured(self) -> bool:
        return bool(self.barcode_app_base_url)


class Config:
    """Env-driven shim that mirrors ``krableadsV2/config.py::Config`` for the
    subset of attributes the insurance-card wrappers (``resend_client``,
    ``tristatecoverage_api``, ``insurance_card``) read directly.

    Kept intentionally minimal — Krab Dispatch's own configuration still lives
    on ``BotConfig``; this class only exists so the wrapper modules can be
    byte-for-byte copies of the Krab Issuer versions.
    """

    RESEND_API_KEY = (os.getenv("RESEND_API_KEY") or "").strip().lstrip("=") or None
    RESEND_FROM = (os.getenv("RESEND_FROM") or "").strip().lstrip("=") or None

    INSURANCE_ISSUER_NAME = (
        os.getenv("INSURANCE_ISSUER_NAME") or "Serviced by AIPSO-SAIP"
    ).strip()
    INSURANCE_ISSUER_PHONE = (os.getenv("INSURANCE_ISSUER_PHONE") or "").strip()
    INSURANCE_ISSUER_ADDRESS = (
        os.getenv("INSURANCE_ISSUER_ADDRESS")
        or "PO Box 6400|Providence, RI 02940-6200"
    ).strip()
    INSURANCE_CARRIER_NAME = (
        os.getenv("INSURANCE_CARRIER_NAME") or "169 National Specialty Insurance Company"
    ).strip()

    TRISTATECOVERAGE_API_BASE = (
        os.getenv("TRISTATECOVERAGE_API_BASE") or "https://tristatecoverage.com"
    ).strip().rstrip("/")
    INTEGRATIONS_API_KEY = (
        os.getenv("INTEGRATIONS_API_KEY") or ""
    ).strip().lstrip("=") or None

    BARCODE_APP_BASE_URL = (os.getenv("BARCODE_APP_BASE_URL") or "").strip().rstrip("/") or None

    @classmethod
    def is_portal_integration_configured(cls) -> bool:
        return bool(cls.INTEGRATIONS_API_KEY)

    @classmethod
    def is_nj_configured(cls) -> bool:
        return bool((cls.BARCODE_APP_BASE_URL or "").strip())

    @classmethod
    def is_resend_configured(cls) -> bool:
        return bool(
            (cls.RESEND_API_KEY or "").strip() and (cls.RESEND_FROM or "").strip()
        )
