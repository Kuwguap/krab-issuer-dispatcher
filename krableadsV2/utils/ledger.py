"""Immediate ledger registration: post new leads to the krab-dispatch backend.

The moment a lead is created here, a PENDING transaction row appears on
tristatetags.com/backend with whatever columns are known (reference, client
name, price, phone, email). When krab-sender later emails the tag, it adopts
that same row and fills the remaining columns — neither bot waits on the
other, and no naming format has to line up for the row to exist.
"""
from __future__ import annotations

import logging

import requests

from config import Config

logger = logging.getLogger(__name__)


def is_configured() -> bool:
    return bool(Config.KRAB_DISPATCH_API_URL and Config.KRAB_DISPATCH_ADMIN_PASSWORD)


def preregister_lead(lead: dict) -> bool:
    """POST a PENDING ledger row for a freshly created lead. Best-effort."""
    if not is_configured() or not lead:
        return False
    vd_lines = [l.strip() for l in str(lead.get("vehicle_details") or "").splitlines() if l.strip()]
    payload = {
        "reference_id": (lead.get("reference_id") or "").strip() or None,
        "client_name": vd_lines[0] if vd_lines else None,
        "price": (str(lead.get("price")).strip() or None) if lead.get("price") is not None else None,
        "client_phone": (str(lead.get("phone_number") or "").strip() or None),
        "client_email": (str(lead.get("email") or "").strip() or None),
        "issuer_handle": (str(lead.get("telegram_username") or "").strip() or None),
    }
    try:
        r = requests.post(
            f"{Config.KRAB_DISPATCH_API_URL}/transactions/pre-register",
            headers={"X-Admin-Password": Config.KRAB_DISPATCH_ADMIN_PASSWORD},
            json=payload,
            timeout=8,
        )
        if r.ok:
            logger.info("Ledger pre-registered lead %s", payload["reference_id"])
            return True
        logger.warning("Ledger pre-register failed (%s): %s", r.status_code, r.text[:120])
    except Exception as e:
        logger.warning("Ledger pre-register error: %s", e)
    return False
