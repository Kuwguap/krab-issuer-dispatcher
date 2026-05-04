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
    api_base_url: str
    issuer_group_chat_id: str  # Optional: Telegram chat ID to notify when send is successful
    highkage_team_chat_id: str
    sensei_group_handles: str
    highkage_group_handles: str
    # Optional: same Supabase project as Krab Issuer — match PDF names to open leads and show receipts in admin.
    supabase_url: str
    supabase_service_role_key: str

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
        )

    def supabase_configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_service_role_key)


