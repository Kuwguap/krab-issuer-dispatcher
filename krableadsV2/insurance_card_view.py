"""Rebuild an issued FS-20 card so the receipts board can show it.

The card is emailed to the client and never stored — there is no file to serve.
Everything printed on it does survive on the lead, though, so it can be rebuilt
exactly: the policy number, the issue date, the VIN, the insured's name and
address, and the licence number.

This is deliberately VIEW ONLY. It sends no email, provisions no portal account,
and above all mints no policy number: a lead without a stored
``insurance_card_policy_number`` has not been issued a card, and inventing one
here would put a number on a document that exists nowhere else.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Optional

from utils import insurance_card as ic

logger = logging.getLogger(__name__)


def _issuer() -> "ic.CardIssuer":
    """Same issuer block the bot prints, read from the same config."""
    try:
        from config import Config
        return ic.CardIssuer(
            carrier_name=Config.INSURANCE_CARRIER_NAME,
            agency_phone=Config.INSURANCE_ISSUER_PHONE,
            agency_name=Config.INSURANCE_ISSUER_NAME,
            agency_address_lines=[
                ln.strip()
                for ln in (Config.INSURANCE_ISSUER_ADDRESS or "").split("|")
                if ln.strip()
            ],
        )
    except Exception:
        return ic.CardIssuer()


def _issued_on(lead: dict) -> date:
    """The date the card was issued — never today, or a reprint would lie.

    Prefers when the card actually went out, then the tag's issue date, and
    only falls back to today when the lead records neither.
    """
    for key in ("insurance_card_sent_at", "insurance_emailed_at", "issue_date", "created_at"):
        raw = str(lead.get(key) or "").strip()
        if not raw:
            continue
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
        except Exception:
            try:
                return date.fromisoformat(raw[:10])
            except Exception:
                continue
    return date.today()


def build_card_pdf_for_lead(lead: dict) -> tuple[Optional[bytes], Optional[str]]:
    """Return (pdf_bytes, error). Exactly one of the two is set."""
    policy = str(lead.get("insurance_card_policy_number") or "").strip()
    if not policy:
        return (None, "No insurance card has been issued for this lead.")

    raw_vehicle = (lead.get("vehicle_details") or "").splitlines()

    def _ln(idx: int) -> str:
        return raw_vehicle[idx].strip() if idx < len(raw_vehicle) else ""

    name = _ln(0) or "UNKNOWN"
    addr_line1 = _ln(1)
    addr_csz = _ln(2)

    # Same VIN resolution the issuer uses — older leads keep it in other fields.
    vin_blob = "\n".join(
        s for s in (
            (lead.get("vehicle_details") or "").strip(),
            (lead.get("delivery_details") or "").strip(),
            (lead.get("extra_info") or "").strip(),
        ) if s
    )
    vin_clean = ic.extract_vin_from_text(vin_blob) or ic.normalize_vin(_ln(5))
    if not vin_clean:
        return (None, "This lead has no valid 17-character VIN to rebuild the card from.")

    car_raw, _color = ic.infer_car_and_color_from_vehicle_lines(
        raw_vehicle, vin_clean=vin_clean
    )

    # Year + make come from the stored car line ("2006 TOYOTA Scion xB").
    # Deliberately NOT a VIN decode: viewing a card should not depend on a
    # third-party API being up, and the card was printed from this text anyway.
    parts = str(car_raw or "").split()
    vehicle_year = parts[0] if parts and parts[0].isdigit() and len(parts[0]) == 4 else "0000"
    make_src = parts[1] if len(parts) > 1 else ""
    vehicle_make_short = "".join(ch for ch in make_src.upper() if ch.isalnum())[:5] or "MAKE"

    issued = _issued_on(lead)
    expires = ic.expiration_for_plan(issued, months=1)

    address_lines = [ln for ln in (addr_line1, addr_csz) if ln] or ["UNKNOWN ADDRESS"]
    upper = name.upper()

    try:
        pdf = ic.build_ny_insurance_id_card_pdf(
            ic.InsuranceCardInput(
                policy_number=policy,
                effective_mm_dd_yyyy=ic.date_to_mmddyyyy(issued),
                expiration_mm_dd_yyyy=ic.date_to_mmddyyyy(expires),
                vehicle_year_full=vehicle_year,
                vehicle_make_short=vehicle_make_short,
                vin=vin_clean,
                insured_name_upper=upper,
                insured_fs20_name=ic.format_insured_fs20_name(upper),
                insured_address_lines=[ln.upper() for ln in address_lines],
                daq=(str(lead.get("driver_license_id") or "").strip() or None),
                issuer=_issuer(),
            )
        )
    except Exception as e:
        logger.error("insurance card rebuild failed for lead %s: %s", lead.get("id"), e)
        return (None, f"Could not rebuild the card: {e}")

    return (pdf, None)
