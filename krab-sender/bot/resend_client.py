"""Resend transactional email for NY FS-20 insurance card delivery."""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


def first_name_from_full(full: str) -> str:
    parts = [p for p in (full or "").strip().split() if p]
    return parts[0] if parts else "there"


@dataclass
class PurchaseWelcomeEmailInput:
    first_name: str
    policy_number: str
    effective_date_label: str
    vehicle_line: str
    portal_email: str
    portal_password: str
    portal_website: str = "TriStateCoverage.com/login"


def build_purchase_welcome_email(input_: PurchaseWelcomeEmailInput) -> tuple[str, str]:
    """Return (subject, body) with Portal Login block; PDF attached separately."""
    subject = f"Your policy is active — {input_.policy_number}"
    portal_site = (input_.portal_website or "TriStateCoverage.com/login").strip()
    body = (
        f"Your policy is active — {input_.policy_number}\n\n\n\n"
        "Tri State Coverage\n\n"
        f"Hi {input_.first_name},\n\n"
        "Good news — your auto insurance policy has been activated successfully ! "
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
        "Portal Login:\n"
        f"Website: {portal_site}\n"
        f"Email: {input_.portal_email}\n"
        f"Password: {input_.portal_password}\n\n"
        "Thank you again for choosing Tri State Coverage.\n\n"
        "Sincerely,\n"
        "Tri State Coverage Team\n"
        "Tri State Coverage (https://www.tristatecoverage.com/?utm_source=chatgpt.com)\n\n"
        "Tri State Coverage Inc\n"
        "Thank you again for choosing Tri State Coverage.\n"
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
    api_key: str,
    from_addr: str,
) -> InsuranceCardEmailResult:
    """Send the FS-20 PDF as an attachment via Resend."""
    to = (to_address or "").strip()
    if not to:
        return InsuranceCardEmailResult(False, "Recipient email is empty.", 400)

    key = (api_key or "").strip()
    sender = (from_addr or "").strip()
    if not key or not sender:
        return InsuranceCardEmailResult(
            False,
            "Email is not configured (RESEND_API_KEY and RESEND_FROM are required).",
            503,
        )

    try:
        import resend  # type: ignore
    except ImportError:
        logger.warning("resend package not installed")
        return InsuranceCardEmailResult(False, "resend package not installed.", 503)

    resend.api_key = key
    pdf_b64 = base64.b64encode(pdf_bytes).decode("ascii")
    params = {
        "from": sender,
        "to": [to],
        "subject": subject,
        "text": body,
        "attachments": [{"filename": pdf_filename, "content": pdf_b64}],
    }
    try:
        resend.Emails.send(params)
    except Exception as e:
        logger.warning("Resend send failed: %s", e)
        return InsuranceCardEmailResult(False, str(e), 502)
    return InsuranceCardEmailResult(True)
