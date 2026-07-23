"""Client-facing follow-up outreach: SMS (Twilio) + email (Resend).

Used by the /followup flow: when the agent enables "bot chases client", every
reminder tick also texts/emails the client directly (e.g. chasing them for a
VIN) so the client follows up with the agent — not the other way around.

Environment variables (read from ``Config``):
  * ``TWILIO_ACCOUNT_SID`` / ``TWILIO_AUTH_TOKEN`` / ``TWILIO_FROM_NUMBER``
    — optional; when unset, SMS is silently skipped and only email is sent.
  * ``RESEND_API_KEY`` / ``RESEND_FROM`` — reused from the insurance-card flow.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

import requests

from utils.resend_client import get_resend_client, get_resend_from_address, first_name_from_full

logger = logging.getLogger(__name__)

_TWILIO_SMS_URL = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"


def _twilio_credentials() -> Optional[tuple[str, str, str]]:
    try:
        from config import Config
    except Exception:
        return None
    sid = (getattr(Config, "TWILIO_ACCOUNT_SID", None) or "").strip()
    token = (getattr(Config, "TWILIO_AUTH_TOKEN", None) or "").strip()
    from_num = (getattr(Config, "TWILIO_FROM_NUMBER", None) or "").strip()
    if not (sid and token and from_num):
        return None
    return sid, token, from_num


def normalize_us_phone(raw: str | None) -> Optional[str]:
    """Best-effort E.164 for US/tri-state numbers; None if not phone-shaped."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if len(digits) == 10:
        return f"+1{digits}"
    if (raw or "").strip().startswith("+") and len(digits) >= 10:
        return f"+{digits}"
    return None


def _contact_block(agency_name: str, website: str | None, phone: str | None) -> str:
    lines = [agency_name]
    if phone:
        lines.append(f"📞 {phone}")
    if website:
        lines.append(f"🌐 {website}")
    return "\n".join(lines)


def build_followup_sms(
    client_name: str | None, agency_name: str,
    website: str | None = None, phone: str | None = None,
) -> str:
    first = first_name_from_full(client_name or "")
    contact = ""
    if phone:
        contact += f" Call/text us: {phone}."
    if website:
        contact += f" {website}"
    return (
        f"Hi {first}, it's {agency_name}. This is a friendly reminder to send or provide "
        "the remaining information needed to complete your temporary tag. "
        f"Reply here and we'll get it processed same day.{contact}"
    )


def build_followup_email(
    client_name: str | None, agency_name: str,
    website: str | None = None, phone: str | None = None,
) -> tuple[str, str]:
    first = first_name_from_full(client_name or "")
    subject = f"Reminder: information needed to complete your temporary tag — {agency_name}"
    body = (
        f"Hi {first},\n\n"
        f"This is a friendly reminder from {agency_name} that we're still waiting on some "
        "information to complete your temporary tag.\n\n"
        "Please send or provide the remaining details (for example your VIN — found on the "
        "driver's-side dashboard, the door jamb sticker, or your registration/title) so we "
        "can finish processing it for you.\n\n"
        "How to reach us:\n"
        "• Reply directly to this email\n"
        + (f"• Call or text: {phone}\n" if phone else "")
        + (f"• Visit: {website}\n" if website else "")
        + "\nWe'll take care of it right away.\n\n"
        "Thank you,\n"
        f"{_contact_block(agency_name, website, phone)}\n"
    )
    return subject, body


def build_renewal_sms(
    client_name: str | None, agency_name: str,
    website: str | None = None, phone: str | None = None,
) -> str:
    first = first_name_from_full(client_name or "")
    contact = ""
    if phone:
        contact += f" Call/text us: {phone}."
    if website:
        contact += f" {website}"
    return (
        f"Hi {first}, it's {agency_name}. Your monthly renewal is coming up — "
        f"reply here to renew and stay covered. Thank you!{contact}"
    )


def build_renewal_email(
    client_name: str | None, agency_name: str,
    website: str | None = None, phone: str | None = None,
) -> tuple[str, str]:
    first = first_name_from_full(client_name or "")
    subject = f"Your monthly renewal is due — {agency_name}"
    body = (
        f"Hi {first},\n\n"
        f"This is a friendly reminder from {agency_name} that your monthly renewal is due.\n\n"
        "How to reach us:\n"
        "• Reply directly to this email\n"
        + (f"• Call or text: {phone}\n" if phone else "")
        + (f"• Visit: {website}\n" if website else "")
        + "\nWe'll process your renewal right away so you stay covered.\n\n"
        "Thank you,\n"
        f"{_contact_block(agency_name, website, phone)}\n"
    )
    return subject, body


def send_client_sms(to_phone: str | None, body: str) -> tuple[bool, Optional[str]]:
    """Send an SMS via Twilio. Returns (ok, error). Skips cleanly when unconfigured."""
    creds = _twilio_credentials()
    if creds is None:
        return False, "SMS not configured (TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN/TWILIO_FROM_NUMBER)."
    to = normalize_us_phone(to_phone)
    if not to:
        return False, f"Phone number not SMS-able: {to_phone!r}"
    sid, token, from_num = creds
    try:
        resp = requests.post(
            _TWILIO_SMS_URL.format(sid=sid),
            auth=(sid, token),
            data={"From": from_num, "To": to, "Body": body},
            timeout=15,
        )
        if resp.status_code >= 400:
            msg = resp.json().get("message") if resp.headers.get("content-type", "").startswith("application/json") else resp.text
            return False, f"Twilio {resp.status_code}: {msg}"
        return True, None
    except Exception as e:  # pragma: no cover — network paths exercised in prod
        logger.warning("Twilio SMS send failed: %s", e)
        return False, str(e)


def send_client_email(
    to_address: str | None, subject: str, body: str,
    copy_to: str | None = None,
) -> tuple[bool, Optional[str]]:
    """Send a plain-text follow-up email via Resend. Returns (ok, error).

    ``copy_to`` (e.g. SendReceiptToday@gmail.com) gets a BCC copy of every send.
    """
    to = (to_address or "").strip()
    if not to or "@" not in to:
        return False, f"Email address not usable: {to_address!r}"
    resend = get_resend_client()
    from_addr = get_resend_from_address()
    if resend is None or not from_addr:
        return False, "Email not configured (RESEND_API_KEY and RESEND_FROM are required)."
    params = {"from": from_addr, "to": [to], "subject": subject, "text": body}
    cc = (copy_to or "").strip()
    if cc and "@" in cc and cc.lower() != to.lower():
        params["bcc"] = [cc]
    try:
        resend.Emails.send(params)
        return True, None
    except Exception as e:  # pragma: no cover
        logger.warning("Resend follow-up email failed: %s", e)
        return False, str(e)


def sms_configured() -> bool:
    return _twilio_credentials() is not None
