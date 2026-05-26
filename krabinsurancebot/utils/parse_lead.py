"""Parse Phase 1 intake into a lead dict for insurance card issuance."""
from __future__ import annotations

import re
from typing import Any, Optional

from utils import ai_vision


def parse_phase1_structured(message_text: str) -> dict[str, Any]:
    """11-line layout (same as krableadsV2)."""
    lines = [line.strip() for line in message_text.splitlines() if line.strip()]

    def get_line(idx: int) -> str:
        return lines[idx] if idx < len(lines) else ""

    name = get_line(0)
    address = get_line(1)
    city_state_zip = get_line(2)
    delivery_address = get_line(3)
    delivery_city_state_zip = get_line(4)
    vin = get_line(5)
    car = get_line(6)
    color = ai_vision.normalize_phase1_color(get_line(7))
    insurance_company = get_line(8)
    insurance_policy_number = get_line(9)
    extra_info = get_line(10)

    vehicle_lines = [
        name,
        address,
        city_state_zip,
        vin,
        car,
        color,
        insurance_company,
        insurance_policy_number,
        extra_info,
    ]
    vehicle_details = "\n".join([l for l in vehicle_lines if l])
    delivery_details = "\n".join(
        [l for l in [delivery_address, delivery_city_state_zip] if l]
    )

    return {
        "name": name,
        "address": address,
        "city_state_zip": city_state_zip,
        "delivery_address": delivery_address,
        "delivery_city_state_zip": delivery_city_state_zip,
        "vin": vin,
        "car": car,
        "color": color,
        "insurance_company": insurance_company,
        "insurance_policy_number": insurance_policy_number,
        "extra_info": extra_info,
        "vehicle_details": vehicle_details,
        "delivery_details": delivery_details,
        "raw_text": message_text,
    }


def _extract_email_and_dl_from_text(text: str) -> tuple[Optional[str], Optional[str]]:
    if not text:
        return (None, None)
    email_match = re.search(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", text)
    email_val: Optional[str] = None
    if email_match:
        email_val = ai_vision.normalize_email(email_match.group(0)) or None
    dl_val: Optional[str] = None
    dl_label_pat = re.compile(
        r"^\s*(?:driver\s*license\s*id|driverlicenseid|driver\s*license|dl\s*id|dl|daq|dmv\s*id|license\s*id)\s*[:#-]\s*(.+?)\s*$",
        re.IGNORECASE | re.MULTILINE,
    )
    m = dl_label_pat.search(text)
    if m:
        dl_val = ai_vision.normalize_driver_license_id(m.group(1)) or None
    return (email_val, dl_val)


def _apply_single_address_as_both(state: dict) -> None:
    def _has(v: str) -> bool:
        return bool(v and str(v).strip() and str(v).strip() != "-")

    addr = (state.get("address") or "").strip()
    csz = (state.get("city_state_zip") or "").strip()
    daddr = (state.get("delivery_address") or "").strip()
    dcsz = (state.get("delivery_city_state_zip") or "").strip()
    has_reg = _has(addr) or _has(csz)
    has_del = _has(daddr) or _has(dcsz)
    if has_reg and not has_del:
        state["delivery_address"] = addr or "-"
        state["delivery_city_state_zip"] = csz or "-"
    elif has_del and not has_reg:
        state["address"] = daddr or "-"
        state["city_state_zip"] = dcsz or "-"


def structured_to_lead(state: dict[str, Any], *, email: Optional[str] = None, driver_license_id: Optional[str] = None) -> dict[str, Any]:
    """Build lead dict expected by build_and_send_insurance_card."""
    vd_lines = [
        state.get("name") or "",
        state.get("address") or "",
        state.get("city_state_zip") or "",
        state.get("delivery_address") or "",
        state.get("delivery_city_state_zip") or "",
        state.get("vin") or "",
        state.get("car") or "",
        state.get("color") or "-",
        state.get("insurance_company") or "-",
        state.get("insurance_policy_number") or "-",
        state.get("extra_info") or "",
    ]
    vehicle_details = "\n".join(vd_lines)
    return {
        "vehicle_details": vehicle_details,
        "delivery_details": state.get("delivery_details") or "",
        "extra_info": state.get("extra_info") or "",
        "email": (email or state.get("email") or "").strip() or None,
        "driver_license_id": driver_license_id or state.get("driver_license_id"),
        "phone_number": state.get("phone_number"),
        "price": state.get("price"),
    }


def parse_from_text(text: str) -> Optional[dict[str, Any]]:
    """Text -> structured state -> lead."""
    raw = (text or "").strip()
    if not raw:
        return None
    email, dl = _extract_email_and_dl_from_text(raw)
    structured = ai_vision.extract_structured_from_text(raw)
    if structured:
        state = parse_phase1_structured(structured)
    else:
        state = parse_phase1_structured(raw)
    _apply_single_address_as_both(state)
    if email:
        state["email"] = email
    if dl:
        state["driver_license_id"] = dl
    return structured_to_lead(state, email=email, driver_license_id=dl)


def parse_from_image(image_bytes: bytes, mime_type: str = "image/jpeg") -> Optional[dict[str, Any]]:
    structured = ai_vision.extract_structured_from_image(image_bytes, mime_type=mime_type)
    if not structured:
        return None
    email, dl = _extract_email_and_dl_from_text(structured)
    state = parse_phase1_structured(structured)
    _apply_single_address_as_both(state)
    return structured_to_lead(state, email=email, driver_license_id=dl)


def parse_from_pdf(pdf_bytes: bytes) -> Optional[dict[str, Any]]:
    structured = ai_vision.extract_structured_from_pdf(pdf_bytes)
    if not structured:
        return None
    email, dl = _extract_email_and_dl_from_text(structured)
    state = parse_phase1_structured(structured)
    _apply_single_address_as_both(state)
    return structured_to_lead(state, email=email, driver_license_id=dl)


def parse_from_media_parts(parts: list[tuple[bytes, str]]) -> Optional[dict[str, Any]]:
    structured = ai_vision.extract_structured_from_media_parts(parts)
    if not structured:
        return None
    email, dl = _extract_email_and_dl_from_text(structured)
    state = parse_phase1_structured(structured)
    _apply_single_address_as_both(state)
    return structured_to_lead(state, email=email, driver_license_id=dl)
