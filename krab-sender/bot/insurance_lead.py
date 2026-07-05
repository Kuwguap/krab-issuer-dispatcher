"""Resolve insured name/address/VIN for insurance cards from the tag PDF and notes.

The dispatch bot previously looked up only the Krab Issuer Supabase lead by
reference id. Fuzzy filename matching can attach the wrong lead (e.g. always
DENAO C). This module prefers data extracted from the PDF the operator just
sent, then pasted client details, then Supabase as a fallback.
"""
from __future__ import annotations

import io
import logging
import re
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_VIN_RE = re.compile(r"\b([A-HJ-NPR-Z0-9]{17})\b", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _norm(s: Any) -> str:
    return str(s or "").strip()


def parse_phase1_structured(message_text: str) -> dict[str, str]:
    """11-line Krab Issuer intake layout (same as krableadsV2 / krabinsurancebot)."""
    lines = [line.strip() for line in (message_text or "").splitlines() if line.strip()]

    def get_line(idx: int) -> str:
        return lines[idx] if idx < len(lines) else ""

    return {
        "name": get_line(0),
        "address": get_line(1),
        "city_state_zip": get_line(2),
        "delivery_address": get_line(3),
        "delivery_city_state_zip": get_line(4),
        "vin": get_line(5),
        "car": get_line(6),
        "color": get_line(7) or "-",
        "insurance_company": get_line(8),
        "insurance_policy_number": get_line(9),
        "extra_info": get_line(10),
    }


def structured_to_lead_dict(state: dict[str, str]) -> dict[str, Any]:
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
    delivery_lines = [
        ln
        for ln in [state.get("delivery_address"), state.get("delivery_city_state_zip")]
        if ln and str(ln).strip() and str(ln).strip() != "-"
    ]
    return {
        "vehicle_details": "\n".join(vd_lines),
        "delivery_details": "\n".join(delivery_lines),
        "extra_info": state.get("extra_info") or "",
        "driver_license_id": None,
        "phone_number": None,
        "price": None,
        "email": None,
        "_source": "client_details",
    }


def parse_client_details_text(text: str) -> Optional[dict[str, Any]]:
    raw = (text or "").strip()
    if not raw:
        return None
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if len(lines) < 3:
        return None
    state = parse_phase1_structured(raw)
    if not (state.get("name") or state.get("vin")):
        return None
    lead = structured_to_lead_dict(state)
    email_m = _EMAIL_RE.search(raw)
    if email_m:
        lead["email"] = email_m.group(0)
    return lead


def _lead_from_temp_tag_fields(fields: dict[str, str]) -> Optional[dict[str, Any]]:
    first = _norm(fields.get("first")).upper()
    last = _norm(fields.get("last")).upper()
    name = " ".join(p for p in [first, last] if p).strip()
    address = _norm(fields.get("address")).upper()
    city = _norm(fields.get("city")).upper()
    state_code = _norm(fields.get("state")).upper() or "NJ"
    zip_code = _norm(fields.get("zip"))
    csz = " ".join(p for p in [city, state_code, zip_code] if p).strip()
    vin = _norm(fields.get("vin1") or fields.get("vin3")).upper()
    year = _norm(fields.get("year"))
    make = _norm(fields.get("make1") or fields.get("make2"))
    model = _norm(fields.get("model1") or fields.get("model2"))
    color = _norm(fields.get("color")) or "-"
    car = " ".join(p for p in [year, make, model] if p).strip()

    if not name and not vin:
        return None

    state = {
        "name": name or "UNKNOWN",
        "address": address,
        "city_state_zip": csz,
        "delivery_address": address or "-",
        "delivery_city_state_zip": csz or "-",
        "vin": vin,
        "car": car,
        "color": color,
        "insurance_company": _norm(fields.get("ins")).upper() or "-",
        "insurance_policy_number": _norm(fields.get("policy")).upper() or "-",
        "extra_info": "",
    }
    lead = structured_to_lead_dict(state)
    lead["_source"] = "pdf_form"
    return lead


def _lead_from_pdf_text(text: str) -> Optional[dict[str, Any]]:
    if not (text or "").strip():
        return None
    vin_m = _VIN_RE.search(text)
    vin = vin_m.group(1).upper() if vin_m else ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    # Drop banner / boilerplate lines common on NJ temp tags.
    skip_prefixes = (
        "EXP ",
        "TEMPORARY",
        "NEW JERSEY",
        "STATE OF",
        "MOTOR VEHICLE",
        "THIS ",
        "KEEP ",
    )
    content = [
        ln
        for ln in lines
        if not any(ln.upper().startswith(p) for p in skip_prefixes)
        and not re.fullmatch(r"H\d{6}", ln, re.IGNORECASE)
        and not re.fullmatch(r"9\d{9}", ln)
    ]
    name = content[0].upper() if content else ""
    address = content[1].upper() if len(content) > 1 else ""
    csz = content[2].upper() if len(content) > 2 else ""
    if not name and not vin:
        return None
    state = {
        "name": name or "UNKNOWN",
        "address": address,
        "city_state_zip": csz,
        "delivery_address": address or "-",
        "delivery_city_state_zip": csz or "-",
        "vin": vin,
        "car": "",
        "color": "-",
        "insurance_company": "-",
        "insurance_policy_number": "-",
        "extra_info": "",
    }
    lead = structured_to_lead_dict(state)
    lead["_source"] = "pdf_text"
    return lead


def parse_temp_tag_pdf(pdf_bytes: bytes) -> Optional[dict[str, Any]]:
    """Extract insured fields from a Krab temp-tag PDF (AcroForm or flattened text)."""
    if not pdf_bytes:
        return None
    try:
        from pypdf import PdfReader
    except ImportError:
        logger.warning("pypdf not installed; cannot parse temp-tag PDF for insurance")
        return None

    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception as e:
        logger.warning("parse_temp_tag_pdf: could not open PDF: %s", e)
        return None

    try:
        form_fields = reader.get_form_text_fields() or {}
    except Exception:
        form_fields = {}

    if form_fields:
        normalized = {str(k): _norm(v) for k, v in form_fields.items() if _norm(v)}
        lead = _lead_from_temp_tag_fields(normalized)
        if lead:
            return lead

    text_parts: list[str] = []
    for page in reader.pages:
        try:
            t = page.extract_text() or ""
        except Exception:
            t = ""
        if t.strip():
            text_parts.append(t)
    combined = "\n".join(text_parts)
    return _lead_from_pdf_text(combined)


def _name_from_filename(file_name: str) -> str:
    stem = Path(file_name or "").stem.strip()
    if not stem:
        return ""
    # Drop trailing reference token if present: "JOHN DOE AB12CD34" -> "JOHN DOE"
    parts = stem.replace("_", " ").replace("-", " ").split()
    if len(parts) >= 2 and re.fullmatch(r"[A-Za-z0-9]{8}", parts[-1]):
        parts = parts[:-1]
    return " ".join(parts).strip().upper()


def merge_insurance_lead(
    *,
    supabase_lead: Optional[dict[str, Any]],
    pdf_bytes: Optional[bytes] = None,
    client_details: Optional[str] = None,
    file_name: Optional[str] = None,
) -> dict[str, Any]:
    """Pick the best lead fields for insurance card generation."""
    base: dict[str, Any] = dict(supabase_lead or {})
    sources: list[tuple[str, dict[str, Any]]] = []

    pdf_lead = parse_temp_tag_pdf(pdf_bytes) if pdf_bytes else None
    if pdf_lead:
        sources.append(("pdf", pdf_lead))

    cd_lead = parse_client_details_text(client_details or "")
    if cd_lead:
        sources.append(("client_details", cd_lead))

    fn_name = _name_from_filename(file_name or "")
    if fn_name:
        fn_state = parse_phase1_structured(f"{fn_name}\n\n")
        fn_lead = structured_to_lead_dict(fn_state)
        fn_lead["_source"] = "filename"
        sources.append(("filename", fn_lead))

    if base:
        base["_source"] = "supabase"
        sources.append(("supabase", base))

    if not sources:
        return base

    merged = dict(base)
    merged_sources: list[str] = []

    # Higher priority last so PDF / pasted notes beat Supabase name mismatches.
    for label, partial in reversed(sources):
        merged_sources.insert(0, label)
        if partial.get("vehicle_details"):
            merged["vehicle_details"] = partial["vehicle_details"]
        if partial.get("delivery_details"):
            merged["delivery_details"] = partial["delivery_details"]
        if partial.get("extra_info"):
            merged["extra_info"] = partial["extra_info"]
        for key in ("driver_license_id", "phone_number", "price", "email"):
            if partial.get(key) and not merged.get(key):
                merged[key] = partial[key]

    merged["_merge_sources"] = merged_sources
    sb_name = _norm((supabase_lead or {}).get("vehicle_details", "").splitlines()[0] if supabase_lead else "")
    pdf_name = _norm((pdf_lead or {}).get("vehicle_details", "").splitlines()[0] if pdf_lead else "")
    if sb_name and pdf_name and sb_name.upper() != pdf_name.upper():
        logger.warning(
            "Insurance lead name mismatch: supabase=%r pdf=%r — using pdf/client/filename over supabase",
            sb_name,
            pdf_name,
        )
    return merged
