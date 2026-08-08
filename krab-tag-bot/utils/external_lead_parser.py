"""Parse external 'New Lead' labeled text into krableadsV2 phase-1 state."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from utils.lead_validation import normalize_phone

VIN_PATTERN = re.compile(r"\b[A-Za-z0-9]{17}\b")

# Longest keys first so "Registration address" wins over "address" substrings.
_LABEL_KEYS: Tuple[Tuple[str, str], ...] = (
    ("registration address", "registration_address"),
    ("delivery address", "delivery_address_full"),
    ("delivery email", "email"),
    ("delivery method", "delivery_method"),
    ("policy #", "insurance_policy_number"),
    ("policy number", "insurance_policy_number"),
    ("order #", "external_order_id"),
    ("order", "external_order_id"),
    ("customer", "name"),
    ("phone", "phone"),
    ("vehicle", "vehicle"),
    ("insurance", "insurance_company"),
    ("service", "service"),
    ("price", "price"),
    ("vin", "vin"),
)

SAMPLE_MESSAGE = """🆕 New Lead
Order #bf6923ca
Customer: Zebin Fang Fang
Phone: 2138622301
Delivery email: zebinfang1002@gmail.com
Delivery method: Email Delivery
Registration address: 28 brookside rd quincy MA 02169
Delivery address: 28 brookside rd quincy MA 02169
VIN: SCA665C56HUX86704
Vehicle: 2017 ROLLS-ROYCE Wraith, black
Insurance: AC Insurance
Policy #: 279-06071-913
Service: 30-Day NJ Temp Tag
Price: $150.00"""


def _split_us_address(full: str) -> Tuple[str, str]:
    """Split a one-line US address into (street, city_state_zip)."""
    raw = (full or "").strip()
    if not raw:
        return "-", "-"
    zip_match = re.search(r"\b(\d{5})(?:-\d{4})?\b\s*$", raw)
    if not zip_match:
        return raw, "-"
    zip_code = zip_match.group(1)
    before = raw[: zip_match.start()].strip().rstrip(",")
    parts = [p for p in before.split() if p]
    if len(parts) >= 2 and re.fullmatch(r"[A-Za-z]{2}", parts[-1]):
        state = parts[-1].upper()
        if len(parts) >= 3:
            city = parts[-2]
            street = " ".join(parts[:-2])
        else:
            city = "-"
            street = parts[0] if parts else before
        csz = f"{city}, {state} {zip_code}"
        return (street or "-"), csz
    if len(parts) >= 2:
        city = parts[-1]
        street = " ".join(parts[:-1])
        return (street or "-"), f"{city}, {zip_code}"
    return before or "-", zip_code


def _parse_labeled_fields(text: str) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("🆕"):
            continue
        lower = line.lower()
        if lower.startswith("order #") or lower.startswith("order#"):
            val = re.sub(r"^order\s*#?\s*", "", line, flags=re.I).strip().lstrip("#").strip()
            if val:
                fields["external_order_id"] = val
            continue
        for label, key in _LABEL_KEYS:
            prefix = f"{label}:"
            if lower.startswith(prefix):
                val = line[len(label) + 1 :].strip()
                if key == "external_order_id":
                    val = val.lstrip("#").strip()
                fields[key] = val
                break
    return fields


def _split_vehicle(vehicle: str) -> Tuple[str, str]:
    raw = (vehicle or "").strip()
    if not raw:
        return "-", "-"
    if "," in raw:
        car, color = raw.rsplit(",", 1)
        return car.strip() or "-", color.strip() or "-"
    return raw, "-"


def _apply_single_address_as_both(state: Dict[str, Any]) -> None:
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


def _build_extra_info(fields: Dict[str, str]) -> str:
    parts: List[str] = []
    if fields.get("external_order_id"):
        parts.append(f"Order #{fields['external_order_id']}")
    if fields.get("delivery_method"):
        parts.append(f"Delivery method: {fields['delivery_method']}")
    if fields.get("service"):
        parts.append(f"Service: {fields['service']}")
    return " | ".join(parts) if parts else "-"


def build_vehicle_details_11(state: Dict[str, Any]) -> str:
    return "\n".join([
        state.get("name", "-"),
        state.get("address", "-"),
        state.get("city_state_zip", "-"),
        state.get("delivery_address", "-"),
        state.get("delivery_city_state_zip", "-"),
        state.get("vin", "-"),
        state.get("car", "-"),
        state.get("color", "-"),
        state.get("insurance_company", "-"),
        state.get("insurance_policy_number", "-"),
        state.get("extra_info", "-"),
    ])


def build_delivery_details(state: Dict[str, Any]) -> str:
    lines = [
        state.get("delivery_address", "-"),
        state.get("delivery_city_state_zip", "-"),
    ]
    return "\n".join(l for l in lines if l and l != "-") or "-"


def parse_external_lead_message(text: str) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """Parse labeled New Lead text. Returns (state_dict, errors)."""
    fields = _parse_labeled_fields(text or "")
    return parse_external_lead_fields(fields)


def _normalize_field_key(key: str) -> str:
    k = (key or "").strip().lower().replace("_", " ")
    mapping = {
        "name": "name",
        "customer": "name",
        "phone": "phone",
        "price": "price",
        "vin": "vin",
        "vehicle": "vehicle",
        "email": "email",
        "delivery email": "email",
        "insurance": "insurance_company",
        "insurance company": "insurance_company",
        "policy #": "insurance_policy_number",
        "policy number": "insurance_policy_number",
        "service": "service",
        "delivery method": "delivery_method",
        "registration address": "registration_address",
        "delivery address": "delivery_address_full",
        "external order id": "external_order_id",
        "order id": "external_order_id",
    }
    return mapping.get(k, k.replace(" ", "_"))


def parse_external_lead_fields(fields: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """Build phase-1 state from labeled field dict. Returns (state_dict, errors)."""
    errors: List[str] = []
    normalized: Dict[str, str] = {}
    for raw_key, raw_val in (fields or {}).items():
        if raw_val is None or not str(raw_val).strip():
            continue
        nk = _normalize_field_key(str(raw_key))
        normalized[nk] = str(raw_val).strip()
    f = normalized

    name = f.get("name", "")
    if not name:
        errors.append("Customer is required")

    phone_raw = f.get("phone", "")
    phone = normalize_phone(phone_raw)
    if not phone:
        errors.append("Phone is required (9–10 digit US number)")

    price = f.get("price", "")
    if not price or "$" not in price or not re.search(r"\d", price):
        errors.append("Price is required (must include $ and a number)")

    vin_raw = f.get("vin", "")
    vin_match = VIN_PATTERN.search(vin_raw)
    vin = vin_match.group(0).upper() if vin_match else ""
    if not vin:
        errors.append("VIN is required (17 characters)")

    vehicle_raw = f.get("vehicle", "")
    if not vehicle_raw:
        errors.append("Vehicle is required")
    car, color = _split_vehicle(vehicle_raw)

    reg_full = f.get("registration_address", "")
    del_full = f.get("delivery_address_full", "")
    if not reg_full and not del_full:
        errors.append("Registration address or Delivery address is required")

    address, city_state_zip = _split_us_address(reg_full) if reg_full else ("-", "-")
    delivery_address, delivery_city_state_zip = (
        _split_us_address(del_full) if del_full else ("-", "-")
    )

    state: Dict[str, Any] = {
        "name": name or "-",
        "address": address,
        "city_state_zip": city_state_zip,
        "delivery_address": delivery_address,
        "delivery_city_state_zip": delivery_city_state_zip,
        "vin": vin,
        "car": car,
        "color": color,
        "insurance_company": f.get("insurance_company", "-") or "-",
        "insurance_policy_number": f.get("insurance_policy_number", "-") or "-",
        "extra_info": _build_extra_info(f),
        "pending_phone_number": phone,
        "pending_price": price,
        "email": f.get("email") or None,
        "external_order_id": f.get("external_order_id") or None,
    }
    _apply_single_address_as_both(state)
    state["vehicle_details"] = build_vehicle_details_11(state)
    state["delivery_details"] = build_delivery_details(state)

    if errors:
        return None, errors
    return state, []
