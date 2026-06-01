"""Format leads as 11-line insurance intake block for Telegram."""
from __future__ import annotations

from typing import Any


def _line(payload: dict[str, Any], key: str, default: str = "-") -> str:
    v = (payload.get(key) or "").strip()
    return v if v else default


def format_lead_message(payload: dict[str, Any], *, reference_id: str) -> str:
    """Render lead for human paste into krabinsurancebot."""
    lines = [
        _line(payload, "name"),
        _line(payload, "address"),
        _line(payload, "city_state_zip"),
        _line(payload, "delivery_address"),
        _line(payload, "delivery_city_state_zip"),
        _line(payload, "vin"),
        _line(payload, "year_make_model"),
        _line(payload, "color"),
        _line(payload, "current_insurance"),
        _line(payload, "policy_number"),
        _line(payload, "delivery_time"),
    ]
    block = "\n".join(lines)
    phone = _line(payload, "phone", "")
    email = _line(payload, "email", "")
    psid = _line(payload, "fb_psid", "")
    extras = []
    if phone and phone != "-":
        extras.append(f"Phone: {phone}")
    if email and email != "-":
        extras.append(f"Email: {email}")
    if psid and psid != "-":
        extras.append(f"FB PSID: {psid}")
    extra_block = "\n".join(extras)
    header = f"New FB lead — ref {reference_id}\n\n"
    if extra_block:
        return header + block + "\n\n" + extra_block
    return header + block
