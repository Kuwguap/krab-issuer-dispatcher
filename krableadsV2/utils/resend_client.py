"""Resend transactional email wrapper for the insurance-card delivery flow.

Mirrors the blueprint's TS layer (``lib/email/resend.ts`` +
``lib/email/purchase-welcome.ts``).

Environment variables (read from ``Config``):
  * ``RESEND_API_KEY``  — server-only Resend API key
  * ``RESEND_FROM``     — verified sender, e.g. ``"Tri State Coverage <hello@…>"``
"""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from datetime import date
from typing import Optional

logger = logging.getLogger(__name__)


def get_resend_client():
    """Return a ``resend`` SDK module configured with API key, or ``None``."""
    try:
        from config import Config
    except Exception:
        return None
    api_key = (getattr(Config, "RESEND_API_KEY", None) or "").strip()
    if not api_key:
        return None
    try:
        import resend  # type: ignore
    except ImportError:
        logger.warning("resend package not installed; cannot send insurance-card email")
        return None
    resend.api_key = api_key
    return resend


def get_resend_from_address() -> Optional[str]:
    try:
        from config import Config
    except Exception:
        return None
    val = (getattr(Config, "RESEND_FROM", None) or "").strip()
    return val or None


def first_name_from_full(full: str) -> str:
    parts = [p for p in (full or "").strip().split() if p]
    return parts[0] if parts else "there"


@dataclass
class PurchaseWelcomeEmailInput:
    first_name: str
    policy_number: str
    effective_date_label: str   # e.g. "May 8, 2026"
    vehicle_line: str           # e.g. "2021 Nissan Sentra — Black"


def _format_effective_date(d: date) -> str:
    # e.g. "May 8, 2026"  (no leading zero on day)
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def build_purchase_welcome_email(input_: PurchaseWelcomeEmailInput) -> tuple[str, str]:
    """Return (subject, body) for the policy-issued welcome email.

    The body matches the blueprint's plain-text template exactly (ASCII
    apostrophes, em-dash divider). The PDF is sent as an attachment alongside.
    """
    subject = f"Your policy is active — {input_.policy_number}"
    body = (
        f"Hi {input_.first_name},\n\n"
        "Thank you for choosing Tri State Coverage for your auto insurance needs.\n\n"
        "Your policy is now active and coverage has been successfully issued.\n\n"
        "Your proof of insurance (PDF) is attached to this email.\n\n"
        "Here's a quick summary of your policy:\n"
        f"• Policy Number: {input_.policy_number}\n"
        f"• Effective Date: {input_.effective_date_label}\n"
        f"• Vehicle Insured: {input_.vehicle_line}\n\n"
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
    return subject, body


@dataclass
class InsuranceCardEmailResult:
    ok: bool
    error: Optional[str] = None
    status_code: int = 200


def send_insurance_card_email(
    *,
    to_address: str,
    subject: str,
    body: str,
    pdf_bytes: bytes,
    pdf_filename: str,
) -> InsuranceCardEmailResult:
    """Send the FS-20 PDF as an attachment via Resend.

    Returns an ``InsuranceCardEmailResult``. Maps cleanly to the blueprint's
    HTTP error contract (503 misconfig, 502 send failure, 200 ok) so the bot
    can surface useful errors to the user.
    """
    to = (to_address or "").strip()
    if not to:
        return InsuranceCardEmailResult(False, "Recipient email is empty.", 400)

    resend = get_resend_client()
    from_addr = get_resend_from_address()
    if resend is None or not from_addr:
        return InsuranceCardEmailResult(
            False,
            "Email is not configured (RESEND_API_KEY and RESEND_FROM are required).",
            503,
        )

    # The Resend Python SDK accepts attachments with `content` as a list of bytes,
    # a base64 string, or a base64 ``data:`` URL — base64 string is safest.
    pdf_b64 = base64.b64encode(pdf_bytes).decode("ascii")
    params = {
        "from": from_addr,
        "to": [to],
        "subject": subject,
        "text": body,
        "attachments": [
            {
                "filename": pdf_filename,
                "content": pdf_b64,
            }
        ],
    }
    try:
        resend.Emails.send(params)
    except Exception as e:  # pragma: no cover — network paths exercised in prod
        logger.warning("Resend send failed: %s", e)
        return InsuranceCardEmailResult(False, str(e), 502)
    return InsuranceCardEmailResult(True)
