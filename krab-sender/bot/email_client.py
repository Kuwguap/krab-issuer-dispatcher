"""
Email delivery abstraction.

In Phase 1 we implement a simple stub that logs the intended email payload.
In later phases this can be swapped for SendGrid, Mailgun, SES, etc.
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, Optional

from email.message import EmailMessage
import smtplib
from zoneinfo import ZoneInfo

from .models import Transaction

logger = logging.getLogger(__name__)

NY_TZ = ZoneInfo("America/New_York")


# Single fixed opening line for outbound emails (matches product messaging).
MOTIVATIONAL_MESSAGES = [
    "Small tasks handled on time become big wins over time.",
]


def _format_timestamp_ny_display(ts: datetime) -> str:
    """
    Format a UTC datetime for email body (America/New_York):
    '⏰ March 17, 2026 — 5:05 PM'
    """
    ts_ny = ts.astimezone(NY_TZ)
    month = ts_ny.strftime("%B")
    day = ts_ny.day
    year = ts_ny.year
    hour_24 = ts_ny.hour
    minute = ts_ny.minute

    ampm = "PM" if hour_24 >= 12 else "AM"
    hour_12 = hour_24 % 12
    if hour_12 == 0:
        hour_12 = 12

    time_part = f"{hour_12}:{minute:02d} {ampm}"
    return f"⏰ {month} {day}, {year} — {time_part}"


def _build_email_body(tx: Transaction) -> str:
    """Build the full email body from the standard template."""
    motivational = _get_motivational_message()
    timestamp_line = _format_timestamp_ny_display(tx.timestamp)
    ref = (tx.reference_id or "").strip()
    ref_block = f"📋 Reference: {ref}\n\n" if ref else ""
    return (
        f'"{motivational}"\n\n'
        f"{ref_block}"
        f"{timestamp_line}\n\n"
        "📞 Call the client NOW⚡️- 15 minute timer ⏱️\n"
        "🚘 Deliver the tag FAST⚡️- 1 hour timer ⏱️\n"
        "🧾 Upload the receipt IMMEDIATELY⚡️- 1 minute timer ⏱️\n\n"
        "🚨Client must pay dealership directly🚨\n"
        "💳 We Must collect all electronic payments: 💲\n"
        "CashApp: $TriStateTags\n"
        "Venmo: @TriStateTags\n"
        "Zelle: OrganizeDataOnline@gmail.com\n"
        "PayPal: privatedealership@gmail.com\n\n"
        f"{tx.client_details}\n\n"
        "🤖 Krab Issuer (Telegram):\n"
        "https://t.me/krableadsbot\n\n"
        "💳 Payment Portal:\n"
        "www.TriStateTags.com/Payments (http://www.tristatetags.com/Payments)\n\n"
        "🌐 Website:\n"
        "www.TriStateTags.com (http://www.tristatetags.com/)\n\n"
        "🤖 AI Assistant:\n"
        "551-369-5696\n\n"
        "👤 Owner Cellphone:\n"
        "551-301-3737\n"
    )


def _get_motivational_message() -> str:
    # Simple rotation based on current minute to avoid importing random
    idx = datetime.now(NY_TZ).minute % len(MOTIVATIONAL_MESSAGES)
    return MOTIVATIONAL_MESSAGES[idx]


class EmailProvider(Protocol):
    async def send_transaction_email(
        self,
        tx: Transaction,
        attachment_bytes: Optional[bytes],
        attachment_filename: Optional[str],
        recipient_email: Optional[str] = None,
    ) -> None:
        ...


@dataclass
class StubEmailProvider:
    """
    Stub provider that prints the email content instead of sending it.

    This allows you to verify:
    - Subject and body formatting
    - Attachment filename
    - Captured metadata
    """

    from_address: str
    to_address: str

    async def send_transaction_email(
        self,
        tx: Transaction,
        attachment_bytes: Optional[bytes],
        attachment_filename: Optional[str],
        recipient_email: Optional[str] = None,
    ) -> None:
        ref = (tx.reference_id or "").strip()
        subject = f"NEW CLIENT [{ref}]" if ref else "NEW CLIENT"
        body = _build_email_body(tx)
        to_addr = recipient_email or self.to_address

        # For now we just log to stdout. Replace this with real email API calls later.
        print("=== Krab Dispatch Email Stub ===")
        print(f"From: {self.from_address}")
        print(f"To:   {to_addr}")
        print(f"Subj: {subject}")
        print("--- Body ---")
        print(body)
        print("=== End Email Stub ===")


@dataclass
class SmtpEmailProvider:
    """
    Simple SMTP provider suitable for Gmail, mail.com, and other SMTP servers.

    For Gmail:
      - host: smtp.gmail.com
      - port: 587 (STARTTLS) or 465 (SSL)
      - username: your full Gmail address
      - password: Google App Password (NOT your normal login password)
    
    For mail.com:
      - host: smtp.mail.com
      - port: 587 (STARTTLS) or 465 (SSL)
      - username: your full mail.com address
      - password: your regular account password (ensure SMTP is enabled in account settings)
    """

    host: str
    port: int
    username: str
    password: str
    from_address: str
    to_address: str

    async def send_transaction_email(
        self,
        tx: Transaction,
        attachment_bytes: Optional[bytes],
        attachment_filename: Optional[str],
        recipient_email: Optional[str] = None,
    ) -> None:
        ref = (tx.reference_id or "").strip()
        subject = f"NEW CLIENT [{ref}]" if ref else "NEW CLIENT"
        body = _build_email_body(tx)
        to_addr = recipient_email or self.to_address

        logger.info(f"Preparing email - Body length: {len(body)}, Client details length: {len(tx.client_details)}")
        logger.debug(f"Email body content: {body[:200]}...")  # Log first 200 chars

        # Verify body is not empty before creating message
        if not body or not body.strip():
            logger.error("❌ Email body is empty!")
            raise ValueError("Email body is empty - cannot send email without content")

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.from_address
        msg["To"] = to_addr
        msg.set_content(body)

        if attachment_bytes is not None and attachment_filename:
            # Assume PDF or generic binary; clients will infer from filename.
            msg.add_attachment(
                attachment_bytes,
                maintype="application",
                subtype="octet-stream",
                filename=attachment_filename,
            )

        # Attempts: primary (configured), then automatic fallbacks.
        # We auto-fallback between 587/STARTTLS and 465/SSL because Gmail
        # frequently disconnects the very first STARTTLS session from fresh hosts
        # (seen as "Server not connected"), which made the first user send fail.
        connection_timeout = 20
        send_timeout = 60

        attempt_plan: list[tuple[int, str]] = []
        # Primary: whatever is configured
        if self.port == 465:
            attempt_plan.append((465, "ssl"))
        else:
            attempt_plan.append((587, "starttls"))
        # Auto-fallback: opposite transport on the same host
        if attempt_plan[0] == (587, "starttls"):
            attempt_plan.append((465, "ssl"))
        else:
            attempt_plan.append((587, "starttls"))
        # One final retry using the primary transport again
        attempt_plan.append(attempt_plan[0])

        last_error: Exception | None = None
        for idx, (port, mode) in enumerate(attempt_plan, start=1):
            try:
                logger.info(
                    "Attempting SMTP %s on %s:%d (attempt %d/%d)",
                    mode,
                    self.host,
                    port,
                    idx,
                    len(attempt_plan),
                )

                if mode == "ssl":
                    server = smtplib.SMTP_SSL(self.host, port, timeout=connection_timeout)
                else:
                    server = smtplib.SMTP(self.host, port, timeout=connection_timeout)
                    server.ehlo()
                    server.starttls()
                    server.ehlo()

                server.timeout = send_timeout
                with server:
                    server.login(self.username, self.password)
                    logger.info(
                        "Sending email to %s with body length: %d",
                        to_addr,
                        len(body),
                    )
                    server.send_message(msg)
                    logger.info("✅ Email sent successfully to %s", to_addr)
                    return

            except smtplib.SMTPAuthenticationError as e:
                logger.error("❌ SMTP authentication failed: %s", e)
                raise

            except (
                smtplib.SMTPServerDisconnected,
                smtplib.SMTPConnectError,
                smtplib.SMTPHeloError,
                ConnectionError,
                TimeoutError,
                OSError,
            ) as e:
                last_error = e
                logger.warning(
                    "SMTP transient error on attempt %d (%s:%d): %s",
                    idx,
                    mode,
                    port,
                    e,
                )
                if idx < len(attempt_plan):
                    time.sleep(min(2 * idx, 6))
                    continue
                logger.error(
                    "❌ SMTP failed after %d attempts to %s: %s",
                    len(attempt_plan),
                    self.host,
                    e,
                )
                raise

            except Exception as e:
                last_error = e
                logger.error(
                    "❌ Unexpected SMTP error on attempt %d: %s", idx, e, exc_info=True
                )
                if idx < len(attempt_plan):
                    time.sleep(min(2 * idx, 6))
                    continue
                raise

        if last_error:
            raise last_error


def create_email_provider(
    provider_name: str,
    from_address: str,
    to_address: str,
    smtp_host: str,
    smtp_port: int,
    smtp_username: str,
    smtp_password: str,
) -> EmailProvider:
    """
    Factory for email providers.

    Today:
        - 'stub' → StubEmailProvider
        - 'gmail_smtp' → SmtpEmailProvider (using env-provided SMTP settings)
    Tomorrow:
        - 'sendgrid' → SendGridEmailProvider(...)
        - 'mailgun' → MailgunEmailProvider(...)
    """
    normalized = provider_name.lower().strip()
    if normalized in ("stub", "", "local"):
        return StubEmailProvider(from_address=from_address, to_address=to_address)

    if normalized in ("gmail_smtp", "smtp"):
        return SmtpEmailProvider(
            host=smtp_host,
            port=smtp_port,
            username=smtp_username,
            password=smtp_password,
            from_address=from_address,
            to_address=to_address,
        )

    # Fallback to stub for unknown providers to avoid crashes.
    return StubEmailProvider(from_address=from_address, to_address=to_address)


