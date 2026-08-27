"""Minimal GoHighLevel (LeadConnector) SMS client for the receipts board.

There was no existing GoHighLevel integration anywhere in these repos — searched
``gohighlevel`` / ``highlevel`` / ``leadconnector`` across every project and every
``.env`` — so this is the minimal client, written for the board's "Send SMS"
buttons. When configured it takes precedence over the Twilio sender in
``utils/client_outreach.py``; when not, the board falls back to Twilio cleanly.

Environment (read at call time, like the other senders):
  * ``GHL_API_KEY``      — Private Integration token (``pit-…``) or an API key
                           with contacts.write + conversations/message.write.
  * ``GHL_LOCATION_ID``  — the sub-account (location) the contacts live in.
  * ``GHL_API_BASE``     — optional; defaults to the LeadConnector API host.

GoHighLevel cannot text a bare number: every SMS belongs to a contact, so a send
is upsert-contact-by-phone, then message to that contact id.
"""
from __future__ import annotations

import logging
import os
from typing import Optional, Tuple

import requests

from utils.client_outreach import normalize_us_phone

logger = logging.getLogger(__name__)

_DEFAULT_BASE = "https://services.leadconnectorhq.com"
# GHL API 2.0 pins each endpoint family to its own date header.
_CONTACTS_VERSION = "2021-07-28"
_MESSAGES_VERSION = "2021-04-15"


def _config() -> Optional[Tuple[str, str, str]]:
    key = (os.getenv("GHL_API_KEY") or "").strip()
    location = (os.getenv("GHL_LOCATION_ID") or "").strip()
    if not (key and location):
        return None
    base = (os.getenv("GHL_API_BASE") or _DEFAULT_BASE).strip().rstrip("/")
    return key, location, base


def ghl_configured() -> bool:
    return _config() is not None


def send_ghl_sms(to_phone: str | None, message: str) -> tuple[bool, Optional[str]]:
    """Send one SMS via GoHighLevel. Returns (ok, error)."""
    cfg = _config()
    if cfg is None:
        return False, "GoHighLevel not configured (set GHL_API_KEY + GHL_LOCATION_ID)."
    to = normalize_us_phone(to_phone)
    if not to:
        return False, f"Phone number not SMS-able: {to_phone!r}"
    if not (message or "").strip():
        return False, "Empty message."
    key, location, base = cfg
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    try:
        r = requests.post(
            f"{base}/contacts/upsert",
            headers={**headers, "Version": _CONTACTS_VERSION},
            json={"locationId": location, "phone": to},
            timeout=15,
        )
        if r.status_code >= 400:
            return False, f"GoHighLevel contact upsert {r.status_code}: {r.text[:200]}"
        try:
            contact_id = ((r.json() or {}).get("contact") or {}).get("id")
        except ValueError:
            contact_id = None
        if not contact_id:
            return False, "GoHighLevel upsert returned no contact id."
        r = requests.post(
            f"{base}/conversations/messages",
            headers={**headers, "Version": _MESSAGES_VERSION},
            json={"type": "SMS", "contactId": contact_id, "message": message},
            timeout=15,
        )
        if r.status_code >= 400:
            return False, f"GoHighLevel send {r.status_code}: {r.text[:200]}"
        return True, None
    except Exception as e:  # pragma: no cover — network paths exercised in prod
        logger.warning("GoHighLevel SMS failed: %s", e)
        return False, str(e)
