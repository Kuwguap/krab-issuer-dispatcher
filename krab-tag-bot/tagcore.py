"""Shared tag-building logic for the HTTP route and the Telegram bot.

The canonical generator (``tag_pdf.py``) + templates + fonts are copied into
``taggen/`` and ``assets/`` at build time from the sibling krableadsV2 (see the
render.yaml buildCommand) so there is exactly ONE source of the PDF output. For
local dev / tests where that copy is absent, we load the generator straight from
``../krableadsV2/utils/tag_pdf.py`` (whose own ``_ASSETS`` then points at
krableadsV2/assets — identical bytes).
"""
from __future__ import annotations

import importlib.util
import os
from datetime import date, datetime, timedelta
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

_HERE = os.path.dirname(os.path.abspath(__file__))
NY_TZ = ZoneInfo("America/New_York")


def _load_tag_pdf():
    local = os.path.join(_HERE, "taggen", "tag_pdf.py")
    if os.path.exists(local):
        import sys
        if _HERE not in sys.path:
            sys.path.insert(0, _HERE)
        from taggen import tag_pdf  # type: ignore
        return tag_pdf
    src = os.path.abspath(os.path.join(_HERE, "..", "krableadsV2", "utils", "tag_pdf.py"))
    if not os.path.exists(src):
        raise RuntimeError(
            "tag_pdf.py not found — build must copy ../krableadsV2/utils/tag_pdf.py "
            "into taggen/, or run beside the krableadsV2 checkout."
        )
    spec = importlib.util.spec_from_file_location("krab_tag_pdf_src", src)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


tag_pdf = _load_tag_pdf()


def _parse_date(v: Any) -> Optional[date]:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Unparseable date: {v!r}")


def build_fields(payload: Dict[str, Any], db) -> Dict[str, Any]:
    """Normalize an incoming client/vehicle dict into the exact field set
    ``tag_pdf.build_tag_pdf`` expects. Fills gaps: derive is_nj from state,
    allocate plate/control when blank (shared RPC), VIN-decode when
    year/make/model blank, default issued=today ET and expires=issued+29."""
    p = dict(payload or {})

    # Split a combined name if first/last not given.
    first, last = (p.get("first") or "").strip(), (p.get("last") or "").strip()
    if not first and not last and p.get("name"):
        first, last = tag_pdf.split_name(p.get("name"))

    # City/state/zip. Seed from a combined city_state_zip if the separate
    # fields are blank, then normalize — this also re-splits a `city` field that
    # actually carries the whole "CITY STATE: XX ZIP: NNNNN" blob so the labels
    # and zip never leak into the printed City box.
    city = (p.get("city") or "").strip()
    zip_ = (p.get("zip") or "").strip()
    state = (p.get("state") or "").strip()
    csz = p.get("city_state_zip")
    if csz and (not city or not state or not zip_):
        st0 = tag_pdf.parse_state(csz)
        c0, z0 = tag_pdf.parse_city_zip(csz, st0)
        city = city or c0
        state = state or st0
        zip_ = zip_ or z0
    city, state, zip_ = tag_pdf.normalize_city_state_zip(city, state, zip_)

    is_nj = p.get("is_nj")
    if is_nj is None:
        is_nj = (state or "").upper() == "NJ"
    is_nj = bool(is_nj)

    year = (str(p.get("year") or "")).strip()
    make = (p.get("make") or "").strip()
    model = (p.get("model") or "").strip()
    body = (p.get("body") or "").strip()
    vin = (p.get("vin") or "").strip()
    # Decode the VIN whenever ANY of year/make/model/body is missing — a common
    # case is make/model supplied but body left blank, which would otherwise
    # print an empty Body Style.
    if vin and not (year and make and model and body):
        try:
            dec = tag_pdf.decode_vin_for_tag(vin)
            if dec:
                year = year or str(dec.get("year") or "")
                make = make or dec.get("make") or ""
                model = model or dec.get("model") or ""
                body = body or dec.get("body") or ""
        except Exception:
            pass

    plate = (p.get("plate") or "").strip()
    control = (str(p.get("control_number") or "")).strip()
    if not plate or not control:
        alloc = db.allocate_temp_plate(is_nj)
        plate = plate or alloc["plate"]
        control = control or alloc["control_number"]

    issued = _parse_date(p.get("issued")) or datetime.now(NY_TZ).date()
    expires = _parse_date(p.get("expires")) or (issued + timedelta(days=29))

    return {
        "is_nj": is_nj,
        "plate": plate,
        "control_number": control,
        "vin": vin,
        "year": year,
        "make": make,
        "model": model,
        "body": body,
        "color": p.get("color") or "",
        "first": first,
        "last": last,
        "address": p.get("address") or "",
        "city": city,
        "state": state,
        "zip": zip_,
        "insurance_company": p.get("insurance_company") or p.get("insuranceCompany") or "",
        "policy": p.get("policy") or p.get("policyNumber") or "",
        "issued": issued,
        "expires": expires,
    }


def generate_full(payload: Dict[str, Any], db) -> Tuple[bytes, Dict[str, Any]]:
    """Normalize once and render. Returns (pdf_bytes, resolved_fields).

    Use this when the caller also needs the resolved fields (e.g. to build a
    supervisory message) — calling build_fields twice would allocate a SECOND
    plate/control from the shared sequence and re-hit the VIN decoder.
    """
    fields = build_fields(payload, db)
    return tag_pdf.build_tag_pdf(fields), fields


def generate(payload: Dict[str, Any], db) -> Tuple[bytes, str, str]:
    """Return (pdf_bytes, plate, control_number)."""
    pdf, fields = generate_full(payload, db)
    return pdf, fields["plate"], fields["control_number"]
