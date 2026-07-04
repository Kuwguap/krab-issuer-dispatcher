"""
Email delivery abstraction.

In Phase 1 we implement a simple stub that logs the intended email payload.
In later phases this can be swapped for SendGrid, Mailgun, SES, etc.
"""

import base64
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, Optional

from email.message import EmailMessage
import smtplib
from zoneinfo import ZoneInfo

import httpx

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

    async def send_email_with_attachment(
        self,
        *,
        to_address: str,
        subject: str,
        body: str,
        attachment_bytes: bytes,
        attachment_filename: str,
        attachment_mime: tuple[str, str] = ("application", "pdf"),
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

    async def send_email_with_attachment(
        self,
        *,
        to_address: str,
        subject: str,
        body: str,
        attachment_bytes: bytes,
        attachment_filename: str,
        attachment_mime: tuple[str, str] = ("application", "pdf"),
    ) -> None:
        print("=== Krab Dispatch Attachment Email Stub ===")
        print(f"From: {self.from_address}")
        print(f"To:   {to_address}")
        print(f"Subj: {subject}")
        print(f"Attachment: {attachment_filename} ({len(attachment_bytes)} bytes, {attachment_mime[0]}/{attachment_mime[1]})")
        print("--- Body ---")
        print(body)
        print("=== End Attachment Email Stub ===")


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

    async def send_email_with_attachment(
        self,
        *,
        to_address: str,
        subject: str,
        body: str,
        attachment_bytes: bytes,
        attachment_filename: str,
        attachment_mime: tuple[str, str] = ("application", "pdf"),
    ) -> None:
        """Send a single-recipient email with one binary attachment.

        Used by the insurance flow to deliver the NY FS-20 PDF alongside the
        same welcome body the krableadsV2 bot uses.
        """
        to_addr = (to_address or "").strip()
        if not to_addr:
            raise ValueError("to_address is required")
        if not attachment_bytes:
            raise ValueError("attachment_bytes is required")
        if not attachment_filename:
            raise ValueError("attachment_filename is required")

        msg = EmailMessage()
        msg["Subject"] = subject or ""
        msg["From"] = self.from_address
        msg["To"] = to_addr
        msg.set_content(body or "")
        maintype, subtype = attachment_mime
        msg.add_attachment(
            attachment_bytes,
            maintype=maintype,
            subtype=subtype,
            filename=attachment_filename,
        )

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
                    "Attempting SMTP %s on %s:%d (attachment email attempt %d/%d)",
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
                    logger.info(
                        "✅ Attachment email sent successfully to %s (%s, %d bytes)",
                        to_addr,
                        attachment_filename,
                        len(attachment_bytes),
                    )
                    return
            except smtplib.SMTPAuthenticationError as e:
                logger.error("❌ SMTP authentication failed: %s", e)
                raise
            except Exception as e:
                last_error = e
                logger.warning("Attachment email send failed (attempt %d): %s", idx, e)
                if idx < len(attempt_plan):
                    time.sleep(min(2 * idx, 6))
                    continue
                raise
        if last_error:
            raise last_error


_FROM_WITH_NAME_RE = re.compile(r"^\s*(.*?)\s*<\s*([^>]+)\s*>\s*$")


def _parse_from_address(raw: str) -> dict[str, str]:
    """Parse ``From`` into SendGrid's ``{email, name?}`` shape.

    Accepts ``"user@example.com"`` or ``"Display Name <user@example.com>"``.
    """
    s = (raw or "").strip()
    if not s:
        return {"email": ""}
    m = _FROM_WITH_NAME_RE.match(s)
    if m:
        name, email = m.group(1).strip().strip('"'), m.group(2).strip()
        out: dict[str, str] = {"email": email}
        if name:
            out["name"] = name
        return out
    return {"email": s}


@dataclass
class SendGridEmailProvider:
    """
    SendGrid v3 Mail Send provider.

    Uses the HTTP API (``POST https://api.sendgrid.com/v3/mail/send``) instead of
    SMTP so we sidestep Gmail-style daily quotas and get proper delivery telemetry
    from SendGrid itself.

    Env requirements:
      - ``SENDGRID_API_KEY``      — API key with ``mail.send`` scope
      - ``EMAIL_FROM_ADDRESS``    — verified sender, e.g. ``"Krab Dispatch <dispatch@yourdomain.com>"``
      - ``EMAIL_TO_ADDRESS``      — default recipient when a per-send address isn't supplied
    """

    api_key: str
    from_address: str
    to_address: str
    api_base: str = "https://api.sendgrid.com"
    timeout: float = 30.0
    max_attempts: int = 3

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _post(self, payload: dict) -> None:
        """Blocking POST to SendGrid with light retry on transient errors."""
        url = f"{self.api_base.rstrip('/')}/v3/mail/send"
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.post(url, headers=self._headers(), json=payload)
                if resp.status_code in (200, 202):
                    return
                # SendGrid returns 4xx for auth/config errors; don't retry those.
                if 400 <= resp.status_code < 500 and resp.status_code not in (408, 429):
                    logger.error(
                        "❌ SendGrid rejected send: %d %s",
                        resp.status_code,
                        resp.text[:500],
                    )
                    raise RuntimeError(
                        f"SendGrid returned {resp.status_code}: {resp.text[:500]}"
                    )
                last_error = RuntimeError(
                    f"SendGrid transient {resp.status_code}: {resp.text[:200]}"
                )
                logger.warning(
                    "SendGrid transient error on attempt %d/%d: %s",
                    attempt,
                    self.max_attempts,
                    last_error,
                )
            except httpx.HTTPError as e:
                last_error = e
                logger.warning(
                    "SendGrid HTTP error on attempt %d/%d: %s",
                    attempt,
                    self.max_attempts,
                    e,
                )
            if attempt < self.max_attempts:
                time.sleep(min(2 * attempt, 6))
        if last_error is not None:
            raise last_error
        raise RuntimeError("SendGrid send failed with no recorded error")

    def _build_payload(
        self,
        *,
        to_address: str,
        subject: str,
        body: str,
        attachment_bytes: Optional[bytes] = None,
        attachment_filename: Optional[str] = None,
        attachment_mime: tuple[str, str] = ("application", "octet-stream"),
    ) -> dict:
        to_addr = (to_address or "").strip()
        if not to_addr:
            raise ValueError("to_address is required")
        payload: dict = {
            "personalizations": [{"to": [{"email": to_addr}]}],
            "from": _parse_from_address(self.from_address),
            "subject": subject or "",
            "content": [{"type": "text/plain", "value": body or ""}],
        }
        if attachment_bytes and attachment_filename:
            maintype, subtype = attachment_mime
            payload["attachments"] = [
                {
                    "content": base64.b64encode(attachment_bytes).decode("ascii"),
                    "filename": attachment_filename,
                    "type": f"{maintype}/{subtype}",
                    "disposition": "attachment",
                }
            ]
        return payload

    async def send_transaction_email(
        self,
        tx: Transaction,
        attachment_bytes: Optional[bytes],
        attachment_filename: Optional[str],
        recipient_email: Optional[str] = None,
    ) -> None:
        import asyncio

        subject = _build_email_subject(tx)
        body = _build_email_body(tx, mention_attached_card=attachment_bytes is not None)
        to_addr = recipient_email or self.to_address

        if not body or not body.strip():
            logger.error("❌ Email body is empty!")
            raise ValueError("Email body is empty - cannot send email without content")

        payload = self._build_payload(
            to_address=to_addr,
            subject=subject,
            body=body,
            attachment_bytes=attachment_bytes,
            attachment_filename=attachment_filename,
            attachment_mime=("application", "pdf")
            if (attachment_filename or "").lower().endswith(".pdf")
            else ("application", "octet-stream"),
        )
        logger.info(
            "Sending SendGrid email to %s (body=%d chars, attach=%s)",
            to_addr,
            len(body),
            attachment_filename or "-",
        )
        await asyncio.to_thread(self._post, payload)
        logger.info("✅ SendGrid email sent successfully to %s", to_addr)

    async def send_plain_email(
        self,
        *,
        to_address: str,
        subject: str,
        body: str,
    ) -> None:
        import asyncio

        payload = self._build_payload(
            to_address=to_address, subject=subject, body=body
        )
        await asyncio.to_thread(self._post, payload)
        logger.info("✅ SendGrid plain email sent successfully to %s", to_address)

    async def send_email_with_attachment(
        self,
        *,
        to_address: str,
        subject: str,
        body: str,
        attachment_bytes: bytes,
        attachment_filename: str,
        attachment_mime: tuple[str, str] = ("application", "pdf"),
    ) -> None:
        import asyncio

        if not attachment_bytes:
            raise ValueError("attachment_bytes is required")
        if not attachment_filename:
            raise ValueError("attachment_filename is required")
        payload = self._build_payload(
            to_address=to_address,
            subject=subject,
            body=body,
            attachment_bytes=attachment_bytes,
            attachment_filename=attachment_filename,
            attachment_mime=attachment_mime,
        )
        await asyncio.to_thread(self._post, payload)
        logger.info(
            "✅ SendGrid attachment email sent successfully to %s (%s, %d bytes)",
            to_address,
            attachment_filename,
            len(attachment_bytes),
        )


def create_email_provider(
    provider_name: str,
    from_address: str,
    to_address: str,
    smtp_host: str,
    smtp_port: int,
    smtp_username: str,
    smtp_password: str,
    sendgrid_api_key: str = "",
) -> EmailProvider:
    """
    Factory for email providers.

    Supported ``provider_name`` values (case-insensitive):
        - ``stub`` / ``local`` / ``""``  → :class:`StubEmailProvider`
        - ``sendgrid``                   → :class:`SendGridEmailProvider` (requires ``sendgrid_api_key``)
        - ``gmail_smtp`` / ``smtp``      → :class:`SmtpEmailProvider`
    """
    normalized = provider_name.lower().strip()
    if normalized in ("stub", "", "local"):
        return StubEmailProvider(from_address=from_address, to_address=to_address)

    if normalized == "sendgrid":
        key = (sendgrid_api_key or "").strip()
        if not key:
            logger.error(
                "EMAIL_PROVIDER=sendgrid but SENDGRID_API_KEY is empty — "
                "falling back to StubEmailProvider so the bot doesn't crash."
            )
            return StubEmailProvider(from_address=from_address, to_address=to_address)
        return SendGridEmailProvider(
            api_key=key,
            from_address=from_address,
            to_address=to_address,
        )

    if normalized in ("gmail_smtp", "smtp"):
        return SmtpEmailProvider(
            host=smtp_host,
            port=smtp_port,
            username=smtp_username,
            password=smtp_password,
            from_address=from_address,
            to_address=to_address,
        )

    return StubEmailProvider(from_address=from_address, to_address=to_address)


