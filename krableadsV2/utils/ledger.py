"""Immediate ledger registration: post new leads to the krab-dispatch backend.

The moment a lead is created here, a PENDING transaction row appears on
tristatetags.com/backend with whatever columns are known (reference, client
name, price, phone, email). When krab-sender later emails the tag, it adopts
that same row and fills the remaining columns — neither bot waits on the
other, and no naming format has to line up for the row to exist.
"""
from __future__ import annotations

import logging
import re

import requests

from config import Config

logger = logging.getLogger(__name__)

_NAME_JUNK_RE = re.compile(r"[^A-Za-z0-9 .,'\-]")


def is_configured() -> bool:
    return bool(Config.KRAB_DISPATCH_API_URL and Config.KRAB_DISPATCH_ADMIN_PASSWORD)


def _client_name_from_lead(lead: dict) -> str:
    """First line of vehicle_details = the client's name, sanitized."""
    for line in str(lead.get("vehicle_details") or "").splitlines():
        line = line.strip()
        if line:
            name = " ".join(_NAME_JUNK_RE.sub("", line).split())
            return name if len(name) >= 2 and name != "-" else ""
    return ""


def _pdf_filename(client_name: str) -> str:
    """CAPITAL letters ending in .pdf — matches the krab-sender ledger rows,
    where the Client PDF Name column is the actual sent-file name."""
    # Trailing punctuation would double up against the extension ("JR..pdf").
    name = " ".join(str(client_name or "").split()).upper().rstrip(" .,")
    return f"{name}.pdf" if name else ""


def _lead_payload(lead: dict) -> dict:
    client_name = _client_name_from_lead(lead)
    issuer = (str(lead.get("telegram_username") or "").strip() or None)
    return {
        "reference_id": (lead.get("reference_id") or "").strip() or None,
        "client_name": client_name or None,
        "price": (str(lead.get("price")).strip() or None) if lead.get("price") is not None else None,
        "client_phone": (str(lead.get("phone_number") or "").strip() or None),
        "client_email": (str(lead.get("email") or "").strip() or None),
        "issuer_name": issuer,
        "issuer_handle": issuer,
        "filename": _pdf_filename(client_name) or None,
    }


def _post_pre_register(payload: dict, action: str) -> bool:
    try:
        r = requests.post(
            f"{Config.KRAB_DISPATCH_API_URL}/transactions/pre-register",
            headers={"X-Admin-Password": Config.KRAB_DISPATCH_ADMIN_PASSWORD},
            json=payload,
            timeout=8,
        )
        if r.ok:
            logger.info("Ledger %s for lead %s", action, payload.get("reference_id"))
            return True
        logger.warning("Ledger %s failed (%s): %s", action, r.status_code, r.text[:120])
    except Exception as e:
        logger.warning("Ledger %s error: %s", action, e)
    return False


def preregister_lead(lead: dict) -> bool:
    """POST a PENDING ledger row for a freshly created lead. Best-effort."""
    if not is_configured() or not lead:
        return False
    return _post_pre_register(_lead_payload(lead), "pre-registered")


def record_driver_for_lead(lead: dict, driver_name: str, driver_email: str | None = None) -> bool:
    """Post the accepting driver onto the lead's PENDING ledger row.

    The endpoint upserts per reference: the same call also (re)fills any
    client/issuer/filename blanks, so a row first created without them heals
    here. Reassignments overwrite the previous driver. Best-effort.
    """
    if not is_configured() or not lead:
        return False
    if not str(driver_name or "").strip():
        return False
    payload = _lead_payload(lead)
    payload["driver_name"] = str(driver_name).strip()
    if str(driver_email or "").strip():
        payload["driver_email"] = str(driver_email).strip()
    # Never mint a fresh PENDING row for a job whose ledger row the send
    # already closed (late accept / reassignment after delivery).
    payload["update_only"] = True
    return _post_pre_register(payload, "driver recorded")
