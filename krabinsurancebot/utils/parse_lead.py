"""Parse Phase 1 intake into a lead dict for insurance card issuance."""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from utils import ai_vision

logger = logging.getLogger(__name__)


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


def _normalize_ai_phase1_text(raw_text: str) -> str:
    """Normalize AI output into plain lines (strip numbering/bullets)."""
    text = (raw_text or "").replace("\r\n", "\n").strip()
    out: list[str] = []
    for ln in text.splitlines():
        s = ln.strip()
        if not s:
            continue
        s = re.sub(r"^\s*(?:\d{1,2}[.)]|[-*])\s*", "", s).strip()
        out.append(s)
    return "\n".join(out)


def _looks_like_refusal(text: str) -> bool:
    s = (text or "").strip().lower()
    if not s:
        return False
    refusal_markers = (
        "i'm sorry",
        "i am sorry",
        "i'm not able",
        "i am not able",
        "i cannot",
        "i can't",
        "can't assist",
        "cannot assist",
        "can't help",
        "cannot help",
        "unable to help",
        "unable to assist",
        "won't be able",
    )
    return any(m in s for m in refusal_markers)


def _structured_has_required_signal(state: dict) -> bool:
    """Reject obviously empty parses (refusals or non-data uploads)."""
    keys = ("name", "address", "city_state_zip", "vin", "car", "delivery_address")
    filled = 0
    for k in keys:
        v = (state.get(k) or "").strip()
        if v and v != "-":
            filled += 1
    return filled >= 2


_JSON_FIELDS = (
    "name",
    "address",
    "city_state_zip",
    "delivery_address",
    "delivery_city_state_zip",
    "vin",
    "car",
    "color",
    "insurance_company",
    "insurance_policy_number",
    "extra_info",
)


def _normalize_json_value(raw: Any) -> str:
    if raw is None:
        return ""
    s = str(raw).strip()
    if not s or s.lower() in ("none", "n/a", "na", "null", "unknown", "tbd", "pending"):
        return ""
    return s


def _state_from_json(data: dict[str, Any]) -> dict[str, Any]:
    state: dict[str, Any] = {}
    for k in _JSON_FIELDS:
        state[k] = _normalize_json_value(data.get(k))
    if state.get("color"):
        state["color"] = ai_vision.normalize_phase1_color(state["color"]) or state["color"]
    return state


def _lead_from_json(data: dict[str, Any]) -> Optional[dict[str, Any]]:
    if not isinstance(data, dict):
        return None
    state = _state_from_json(data)

    if _looks_like_refusal(" ".join(str(v) for v in state.values() if v)):
        logger.info("Rejected JSON output: refusal-style content")
        return None

    _apply_single_address_as_both(state)

    if not _structured_has_required_signal(state):
        logger.info("Rejected JSON output: not enough recognizable fields")
        return None

    email_raw = _normalize_json_value(data.get("email"))
    email = ai_vision.normalize_email(email_raw) if email_raw else ""

    dl_raw = _normalize_json_value(data.get("driver_license_id"))
    dl = ai_vision.normalize_driver_license_id(dl_raw) if dl_raw else ""

    if email:
        state["email"] = email
    if dl:
        state["driver_license_id"] = dl

    phone = _normalize_json_value(data.get("phone"))
    if phone:
        state["phone_number"] = phone

    return structured_to_lead(state, email=email or None, driver_license_id=dl or None)


def _parse_structured_output(structured: str) -> Optional[dict[str, Any]]:
    """Normalize + validate AI output before converting to a lead dict."""
    if _looks_like_refusal(structured):
        logger.info("Rejected AI output: refusal-style content (raw)")
        return None

    normalized = _normalize_ai_phase1_text(structured)
    lines = [ln.strip() for ln in normalized.splitlines() if ln.strip()]
    if len(lines) < ai_vision.PHASE1_LINE_COUNT:
        logger.info("Rejected AI output: expected >=11 lines, got %d", len(lines))
        return None

    normalized_11 = "\n".join(lines[: ai_vision.PHASE1_LINE_COUNT])
    state = parse_phase1_structured(normalized_11)
    _apply_single_address_as_both(state)
    valid, _errors = ai_vision.validate_phase1_extraction(normalized_11, state)
    if not valid:
        logger.info("Rejected AI output: validation failed")
        return None

    if _looks_like_refusal(state.get("name") or ""):
        logger.info("Rejected AI output: refusal-style content (name)")
        return None

    if not _structured_has_required_signal(state):
        logger.info("Rejected AI output: not enough recognizable fields")
        return None

    email, dl = _extract_email_and_dl_from_text(normalized)
    return structured_to_lead(state, email=email, driver_license_id=dl)


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

    json_data = ai_vision.extract_json_from_text(raw)
    lead = _lead_from_json(json_data) if json_data else None
    if lead:
        if email and not lead.get("email"):
            lead["email"] = email
        if dl and not lead.get("driver_license_id"):
            lead["driver_license_id"] = dl
        return lead

    structured = ai_vision.extract_structured_from_text(raw)
    if structured:
        parsed = _parse_structured_output(structured)
        if parsed:
            if email and not parsed.get("email"):
                parsed["email"] = email
            if dl and not parsed.get("driver_license_id"):
                parsed["driver_license_id"] = dl
            return parsed
        state = parse_phase1_structured(raw)
    else:
        state = parse_phase1_structured(raw)
    _apply_single_address_as_both(state)
    if email:
        state["email"] = email
    if dl:
        state["driver_license_id"] = dl
    return structured_to_lead(state, email=email, driver_license_id=dl)


def parse_from_image(image_bytes: bytes, mime_type: str = "image/jpeg") -> Optional[dict[str, Any]]:
    json_data = ai_vision.extract_json_from_image(image_bytes, mime_type=mime_type)
    lead = _lead_from_json(json_data) if json_data else None
    if lead:
        return lead
    structured = ai_vision.extract_structured_from_image(image_bytes, mime_type=mime_type)
    if not structured:
        return None
    return _parse_structured_output(structured)


def parse_from_pdf(pdf_bytes: bytes) -> Optional[dict[str, Any]]:
    json_data = ai_vision.extract_json_from_pdf(pdf_bytes)
    lead = _lead_from_json(json_data) if json_data else None
    if lead:
        return lead
    structured = ai_vision.extract_structured_from_pdf(pdf_bytes)
    if not structured:
        return None
    return _parse_structured_output(structured)


def parse_from_media_parts(parts: list[tuple[bytes, str]]) -> Optional[dict[str, Any]]:
    json_data = ai_vision.extract_json_from_media_parts(parts)
    lead = _lead_from_json(json_data) if json_data else None
    if lead:
        return lead
    structured = ai_vision.extract_structured_from_media_parts(parts)
    if not structured:
        return None
    return _parse_structured_output(structured)


def has_driver_license_id(lead: dict[str, Any]) -> bool:
    """True when the lead has a usable DMV / driver license ID."""
    raw = (lead.get("driver_license_id") or "").strip()
    return bool(ai_vision.normalize_driver_license_id(raw))


def ny_needs_driver_license_id(lead: dict[str, Any], card_state: str) -> bool:
    """NY FS-20 barcodes require a real DAQ / license ID."""
    return (card_state or "").strip().upper() == "NY" and not has_driver_license_id(lead)
