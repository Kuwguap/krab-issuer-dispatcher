"""
Email delivery abstraction.

In Phase 1 we implement a simple stub that logs the intended email payload.
In later phases this can be swapped for SendGrid, Mailgun, SES, etc.
"""

import logging
import re
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


# ─── Client-facing policy-issued email ───────────────────────────────────────
# Replicated from krableadsV2/utils/resend_client.py::build_purchase_welcome_email
# so both the Krab Issuer (insurance card generator) and Krab Sender bots
# deliver the exact same body/subject to the client.

# Matches "2021 Honda Civic", "2013 Ford F-150", etc. inside free-form text.
_VEHICLE_YEAR_RE = re.compile(
    r"\b((?:19|20)\d{2})\s+([A-Za-z][A-Za-z0-9\-]*(?:\s+[A-Za-z0-9\-]+){0,4})"
)


def _first_name_from_full(full: Optional[str]) -> str:
    parts = [p for p in (full or "").strip().split() if p]
    return parts[0] if parts else "there"


def _format_effective_date_ny(ts: datetime) -> str:
    """Format a UTC datetime as 'May 8, 2026' in America/New_York."""
    d = ts.astimezone(NY_TZ)
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def _extract_vehicle_line(client_details: Optional[str]) -> str:
    """Best-effort 'YYYY Make Model' extraction from free-form client details."""
    text = (client_details or "").strip()
    if not text:
        return "—"
    m = _VEHICLE_YEAR_RE.search(text)
    if not m:
        return "—"
    year = m.group(1)
    rest = re.split(r"[\n,;|]", m.group(2))[0].strip()
    rest = re.sub(r"\s+", " ", rest)
    line = f"{year} {rest}".strip()
    return line or "—"


def _build_email_subject(tx: Transaction) -> str:
    policy_number = (tx.reference_id or "").strip() or "—"
    return f"Your policy is active — {policy_number}"


def _build_email_body(tx: Transaction, *, mention_attached_card: bool = True) -> str:
    """Build the policy-issued email body.

    Byte-for-byte match (modulo the optional attachment note) with
    ``krableadsV2/utils/resend_client.py::build_purchase_welcome_email`` so both
    bots send the same message to the client.
    """
    first_name = _first_name_from_full(tx.recipient_name)
    policy_number = (tx.reference_id or "").strip() or "—"
    effective_date_label = _format_effective_date_ny(tx.timestamp)
    vehicle_line = _extract_vehicle_line(tx.client_details)

    attachment_note = (
        "\nYour proof of insurance (PDF) is attached to this email.\n\n"
        if mention_attached_card
        else "\n"
    )
    return (
        f"Hi {first_name},\n\n"
        "Thank you for choosing Tri State Coverage for your auto insurance needs.\n\n"
        "Your policy is now active and coverage has been successfully issued.\n"
        f"{attachment_note}"
        "Here's a quick summary of your policy:\n"
        f"• Policy Number: {policy_number}\n"
        f"• Effective Date: {effective_date_label}\n"
        f"• Vehicle Insured: {vehicle_line}\n\n"
        "What's Next?\n"
        "• Review your coverage online\n"
        "• Download proof of insurance\n"
        "• Set up automatic payments\n"
        "• Access your policy anytime through your online dashboard\n\n"
        "Log into your TRISTATECOVERAGE account anytime to manage your policy online.\n\n"
        "Thank you again for choosing Tri State Coverage.\n\n"
        "Sincerely,\n"
        "The Tri State Coverage Team\n\n"
        "Www.TriStateCoverage.com (http://www.tristatecoverage.com/)\n"
        "Tri State Coverage Inc\n"
        "1 N Central Rd 6th floor suite 629\n"
        "Fort Lee, NJ 07024\n"
    )


class EmailProvider(Protocol):
    async def send_transaction_email(
        self,
        tx: Transaction,
        attachment_bytes: Optional[bytes],
        attachment_filename: Optional[str],
        recipient_email: Optional[str] = None,
    ) -> None:
        ...

    async def send_plain_email(
        self,
        *,
        to_address: str,
        subject: str,
        body: str,
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
        subject = _build_email_subject(tx)
        body = _build_email_body(tx, mention_attached_card=attachment_bytes is not None)
        to_addr = recipient_email or self.to_address

        # For now we just log to stdout. Replace this with real email API calls later.
        print("=== Krab Dispatch Email Stub ===")
        print(f"From: {self.from_address}")
        print(f"To:   {to_addr}")
        print(f"Subj: {subject}")
        print("--- Body ---")
        print(body)
        print("=== End Email Stub ===")

    async def send_plain_email(
        self,
        *,
        to_address: str,
        subject: str,
        body: str,
    ) -> None:
        print("=== Krab Dispatch Plain Email Stub ===")
        print(f"From: {self.from_address}")
        print(f"To:   {to_address}")
        print(f"Subj: {subject}")
        print("--- Body ---")
        print(body)
        print("=== End Plain Email Stub ===")


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
        subject = _build_email_subject(tx)
        body = _build_email_body(tx, mention_attached_card=attachment_bytes is not None)
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

    async def send_plain_email(
        self,
        *,
        to_address: str,
        subject: str,
        body: str,
    ) -> None:
        """
        Send a simple text email without attachments.
        Used for insurance credential forwarding flows.
        """
        to_addr = (to_address or "").strip()
        if not to_addr:
            raise ValueError("to_address is required")
        msg = EmailMessage()
        msg["Subject"] = subject or ""
        msg["From"] = self.from_address
        msg["To"] = to_addr
        msg.set_content(body or "")

        connection_timeout = 20
        send_timeout = 60

        attempt_plan: list[tuple[int, str]] = []
        if self.port == 465:
            attempt_plan.append((465, "ssl"))
        else:
            attempt_plan.append((587, "starttls"))
        if attempt_plan[0] == (587, "starttls"):
            attempt_plan.append((465, "ssl"))
        else:
            attempt_plan.append((587, "starttls"))
        attempt_plan.append(attempt_plan[0])

        last_error: Exception | None = None
        for idx, (port, mode) in enumerate(attempt_plan, start=1):
            try:
                logger.info(
                    "Attempting SMTP %s on %s:%d (plain email attempt %d/%d)",
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
                    server.send_message(msg)
                    logger.info("✅ Plain email sent successfully to %s", to_addr)
                    return
            except Exception as e:
                last_error = e
                logger.warning("Plain email send failed (attempt %d): %s", idx, e)
                continue
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


