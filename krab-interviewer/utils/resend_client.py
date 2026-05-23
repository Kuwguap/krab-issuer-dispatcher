"""Resend email for driver tracking notifications."""
from __future__ import annotations

import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


def send_driver_tracking_email(
    *,
    to_address: str,
    driver_name: str,
    tracking_number: str,
    tracking_url: str,
    zip_code: Optional[str] = None,
    qty: int = 10,
) -> Tuple[bool, Optional[str]]:
    """Send tracking info to the new driver. Returns (success, error_message)."""
    from config import Config

    to = (to_address or "").strip()
    if not to:
        return False, "Recipient email is empty"

    api_key = (Config.RESEND_API_KEY or "").strip()
    from_addr = (Config.RESEND_FROM or "").strip()
    if not api_key or not from_addr:
        return False, "RESEND_API_KEY and RESEND_FROM are required"

    name = (driver_name or "Driver").strip()
    zip_line = f"Destination ZIP: {zip_code}\n" if zip_code else ""
    body = (
        f"Hi {name},\n\n"
        f"Your training papers ({qty} copies) have been shipped via USPS.\n\n"
        f"Tracking number: {tracking_number}\n"
        f"Track your package: {tracking_url}\n"
        f"{zip_line}\n"
        "Tips:\n"
        "• Do not open the package until your training day\n"
        "• Print materials on letter-size paper when instructed\n\n"
        "— Krab Team"
    )
    subject = f"Your training papers are on the way — tracking {tracking_number[:12]}…"

    try:
        import resend

        resend.api_key = api_key
        resend.Emails.send(
            {
                "from": from_addr,
                "to": [to],
                "subject": subject,
                "text": body,
            }
        )
        return True, None
    except ImportError:
        return False, "resend package not installed"
    except Exception as e:
        logger.warning("Resend send failed: %s", e)
        return False, str(e)


def send_appointment_reminder_email(
    *,
    to_address: str,
    driver_name: str,
    appointment_display: str,
    reference: str,
    notes: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    """Email a supervisor that an interview appointment is due."""
    from config import Config

    to = (to_address or "").strip()
    if not to:
        return False, "Recipient email is empty"

    api_key = (Config.RESEND_API_KEY or "").strip()
    from_addr = (Config.RESEND_FROM or "").strip()
    if not api_key or not from_addr:
        return False, "RESEND_API_KEY and RESEND_FROM are required"

    note_block = f"\nNotes: {notes}\n" if notes else ""
    body = (
        f"Interview appointment reminder\n\n"
        f"Driver: {driver_name or 'Driver'}\n"
        f"Time: {appointment_display}\n"
        f"Reference: {reference}{note_block}\n\n"
        "Open the bot to start the interview call.\n\n"
        "— Krab Interviewer"
    )
    subject = f"Interview reminder — {driver_name or 'driver'} at {appointment_display}"

    try:
        import resend

        resend.api_key = api_key
        resend.Emails.send(
            {
                "from": from_addr,
                "to": [to],
                "subject": subject,
                "text": body,
            }
        )
        return True, None
    except ImportError:
        return False, "resend package not installed"
    except Exception as e:
        logger.warning("Resend appointment reminder failed: %s", e)
        return False, str(e)
