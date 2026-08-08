"""Log each generated tag to tristatetags.com/backend (the krab-dispatch ledger)
so it's tracked by reference number and issuer — the same reference/pre-register
system krableadsV2 uses. Best-effort: a logging failure never blocks a tag.
"""
from __future__ import annotations

import logging
import re
import secrets
import string

import requests

from config import Config

logger = logging.getLogger(__name__)

_NAME_JUNK_RE = re.compile(r"[^A-Za-z0-9 .,'\-]")


def generate_reference_id() -> str:
    """8-char A-Z0-9 reference — same shape as krableadsV2 leads."""
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(8))


def _pdf_filename(client_name: str) -> str:
    name = " ".join(_NAME_JUNK_RE.sub("", str(client_name or "")).split()).upper().rstrip(" .,")
    return f"{name}.pdf" if name else ""


def log_tag(
    *,
    reference_id: str,
    client_name: str = "",
    issuer_name: str = "",
    issuer_handle: str = "",
    client_phone: str = "",
    client_email: str = "",
) -> bool:
    """Post a PENDING ledger row for a generated tag. Returns True on 2xx."""
    if not Config.ledger_configured() or not reference_id:
        return False
    client_name = " ".join(_NAME_JUNK_RE.sub("", str(client_name or "")).split())
    payload = {
        "reference_id": reference_id,
        "client_name": client_name or None,
        "client_phone": (str(client_phone or "").strip() or None),
        "client_email": (str(client_email or "").strip() or None),
        "issuer_name": (str(issuer_name or "").strip() or None),
        "issuer_handle": (str(issuer_handle or "").strip() or None),
        "filename": _pdf_filename(client_name) or None,
    }
    try:
        r = requests.post(
            f"{Config.KRAB_DISPATCH_API_URL}/transactions/pre-register",
            headers={"X-Admin-Password": Config.KRAB_DISPATCH_ADMIN_PASSWORD},
            json=payload,
            timeout=8,
        )
        if r.ok:
            logger.info("Logged tag %s (issuer=%s)", reference_id, issuer_handle or issuer_name)
            return True
        logger.warning("Tag log failed (%s): %s", r.status_code, r.text[:120])
    except Exception as e:
        logger.warning("Tag log error: %s", e)
    return False
