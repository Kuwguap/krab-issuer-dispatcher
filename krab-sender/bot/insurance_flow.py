"""High-level NY FS-20 insurance card flow for the Krab Dispatch bot.

Mirrors ``krableadsV2/bot.py::_build_and_send_insurance_card``: FS-20 PDF,
TriStateCoverage portal account, Resend welcome email with Portal Login block.
Lead data is pulled from shared Issuer Supabase by ``reference_id``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from zoneinfo import ZoneInfo

from . import insurance_card as ic
from .config import BotConfig
from . import nj_card_api as nj
from . import resend_client as rc
from . import state_detection as sd
from . import tristatecoverage_api as tsc
from backend.issuer_supabase import fetch_lead_row_by_reference

logger = logging.getLogger(__name__)
NY_TZ = ZoneInfo("America/New_York")

PORTAL_DEFAULT_PASSWORD = "Temp#A9"


def _generate_portal_password() -> str:
    return PORTAL_DEFAULT_PASSWORD


def _parse_annual_premium(lead: dict) -> float:
    raw = (lead.get("price") or "").strip().replace(",", "").replace("$", "")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _format_effective_date_label(d) -> str:
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def _issuer_from_env() -> ic.CardIssuer:
    carrier_name = (os.getenv("INSURANCE_CARRIER_NAME") or "169 National Specialty Insurance Company").strip()
    agency_name = (os.getenv("INSURANCE_ISSUER_NAME") or "Serviced by AIPSO-SAIP").strip()
    agency_phone = (os.getenv("INSURANCE_ISSUER_PHONE") or "").strip()
    agency_address = (os.getenv("INSURANCE_ISSUER_ADDRESS") or "PO Box 6400|Providence, RI 02940-6200").strip()
    return ic.CardIssuer(
        carrier_name=carrier_name,
        agency_name=agency_name,
        agency_phone=agency_phone,
        agency_address_lines=[ln.strip() for ln in agency_address.split("|") if ln.strip()],
    )


@dataclass
class InsuranceCardResult:
    ok: bool
    policy_number: Optional[str]
    pdf_filename: Optional[str]
    error: Optional[str]
    portal_email: Optional[str] = None
    portal_password: Optional[str] = None


async def build_and_send_insurance_card(
    *,
    email_to: str,
    reference_id: Optional[str],
    bot_config: BotConfig,
) -> InsuranceCardResult:
    """Look up lead by reference, build FS-20 PDF, create portal account, email via Resend."""
    email_to = (email_to or "").strip()
    if not email_to:
        return InsuranceCardResult(False, None, None, "Recipient email is required.")

    ref = (reference_id or "").strip()
    if not ref:
        return InsuranceCardResult(
            False,
            None,
            None,
            "No reference ID is attached to this chat — cannot look up the lead. "
            "Send a tag PDF first so the bot can match it to the Krab Issuer lead.",
        )

    if not bot_config.supabase_configured():
        return InsuranceCardResult(
            False, None, None, "Supabase is not configured on this service — cannot fetch the lead."
        )

    lead = await asyncio.to_thread(
        fetch_lead_row_by_reference,
        bot_config.supabase_url,
        bot_config.supabase_service_role_key,
        ref,
    )
    if not lead:
        return InsuranceCardResult(
            False, None, None, f"No Krab Issuer lead found for reference ID {ref!r}."
        )

    raw_vehicle = (lead.get("vehicle_details") or "").splitlines()

    def _ln(idx: int) -> str:
        return raw_vehicle[idx].strip() if idx < len(raw_vehicle) else ""

    name = _ln(0) or "UNKNOWN"
    addr_line1 = _ln(1)
    addr_csz = _ln(2)

    vin_blob = "\n".join(
        s
        for s in (
            (lead.get("vehicle_details") or "").strip(),
            (lead.get("delivery_details") or "").strip(),
            (lead.get("extra_info") or "").strip(),
        )
        if s
    )
    vin_clean = ic.extract_vin_from_text(vin_blob) or ic.normalize_vin(_ln(5))
    if not vin_clean:
        return InsuranceCardResult(
            False,
            None,
            None,
            "No valid 17-character VIN found on this lead (searched vehicle details, "
            "delivery details, and notes). Update the lead with the correct VIN and try again.",
        )

    car_raw, color = ic.infer_car_and_color_from_vehicle_lines(raw_vehicle, vin_clean=vin_clean)

    decoded = await asyncio.to_thread(ic.decode_vin_from_nhtsa, vin_clean)
    if decoded:
        vehicle_year = (decoded.get("modelYear") or "").strip()
        vehicle_make_full = (decoded.get("vehicleMake") or "").strip()
        vehicle_model = (decoded.get("vehicleModel") or "").strip()
    else:
        parts = car_raw.split()
        vehicle_year = parts[0] if parts and parts[0].isdigit() else ""
        vehicle_make_full = parts[1] if len(parts) > 1 else ""
        vehicle_model = " ".join(parts[2:]) if len(parts) > 2 else ""

    vehicle_make_short = (
        re.sub(r"[^A-Za-z0-9]", "", vehicle_make_full).upper()[:5] or "MAKE"
    )
    if not (vehicle_year and vehicle_year.isdigit() and len(vehicle_year) == 4):
        vehicle_year = "0000"

    today = datetime.now(NY_TZ).date()
    expiration_date = ic.expiration_for_plan(today, months=1)
    effective_label = ic.date_to_mmddyyyy(today)
    expiration_label = ic.date_to_mmddyyyy(expiration_date)
    policy_number = ic.generate_policy_number()
    portal_password = _generate_portal_password()

    address_lines: list[str] = []
    if addr_line1:
        address_lines.append(addr_line1)
    if addr_csz:
        address_lines.append(addr_csz)
    if not address_lines:
        address_lines = ["UNKNOWN ADDRESS"]

    phone_raw = (lead.get("phone_number") or "").strip()
    card_state = sd.detect_card_state(lead)

    if card_state == "NJ":
        if not bot_config.is_nj_configured():
            return InsuranceCardResult(
                False,
                None,
                None,
                "NJ insurance card not configured (BARCODE_APP_BASE_URL).",
            )
        policy_number = nj.generate_nj_policy_number()
        nj_payload = nj.build_nj_email_payload(
            policy_number=policy_number,
            effective_mm_dd_yyyy=effective_label,
            expiration_mm_dd_yyyy=expiration_label,
            vehicle_year=vehicle_year if vehicle_year != "0000" else "",
            vehicle_make=vehicle_make_full or "UNKNOWN",
            vehicle_model=vehicle_model or "UNKNOWN",
            vin=vin_clean,
            insured_name_upper=name.upper(),
            insured_address_lines=address_lines,
            email=email_to,
            first_name=rc.first_name_from_full(name),
            phone=phone_raw or None,
            annual_premium=_parse_annual_premium(lead) or None,
        )
        nj_result = await asyncio.to_thread(nj.send_nj_insurance_email, nj_payload)
        if not nj_result.ok:
            err = nj_result.error or "NJ card API failed."
            if nj_result.status_code:
                err = f"{err} (HTTP {nj_result.status_code})"
            return InsuranceCardResult(False, policy_number, None, err)
        return InsuranceCardResult(
            True,
            nj_result.policy_number or policy_number,
            None,
            None,
            nj_result.email or email_to,
            None,
        )

    if not bot_config.is_portal_integration_configured():
        return InsuranceCardResult(
            False, None, None, "INTEGRATIONS_API_KEY missing on bot."
        )

    if not bot_config.is_resend_configured():
        return InsuranceCardResult(
            False, None, None, "RESEND_API_KEY/RESEND_FROM missing."
        )

    pdf_input = ic.InsuranceCardInput(
        policy_number=policy_number,
        effective_mm_dd_yyyy=effective_label,
        expiration_mm_dd_yyyy=expiration_label,
        vehicle_year_full=vehicle_year,
        vehicle_make_short=vehicle_make_short,
        vin=vin_clean,
        insured_name_upper=name.upper(),
        insured_fs20_name=ic.format_insured_fs20_name(name.upper()),
        insured_address_lines=address_lines,
        daq=(lead.get("driver_license_id") or "").strip() or None,
        issuer=_issuer_from_env(),
    )

    try:
        pdf_bytes = await asyncio.to_thread(ic.build_ny_insurance_id_card_pdf, pdf_input)
    except Exception as e:
        logger.exception("Failed to build FS-20 PDF for ref %s: %s", ref, e)
        return InsuranceCardResult(
            False, policy_number, None, f"Could not build insurance card PDF: {e}"
        )

    vehicle_label = ic.format_suggested_vehicle_name(vehicle_year, vehicle_make_full, vehicle_model)
    if color and color != "-":
        vehicle_label = f"{vehicle_label} — {color}".strip(" —")
    vehicle_name_api = vehicle_label or car_raw or "Vehicle on file"

    portal_payload = {
        "email": email_to,
        "password": portal_password,
        "name": name.upper(),
        "phone": phone_raw or "+1 000 000 0000",
        "vehicleName": vehicle_name_api,
        "vin": vin_clean,
        "policyNumber": policy_number,
        "policyEffectiveDate": today.isoformat(),
        "policyExpirationDate": expiration_date.isoformat(),
        "annualPremium": _parse_annual_premium(lead),
        "vehicleColor": color if color and color != "-" else None,
        "vehicleYear": vehicle_year if vehicle_year != "0000" else None,
        "vehicleMake": vehicle_make_full or None,
        "vehicleModel": vehicle_model or None,
        # Bot sends the welcome email (with portal login) via Resend after create succeeds.
        "skipWelcomeEmail": True,
    }
    portal_payload = {k: v for k, v in portal_payload.items() if v is not None}

    portal_result = await asyncio.to_thread(
        tsc.create_portal_client,
        portal_payload,
        pdf_bytes,
    )
    if not portal_result.ok:
        return InsuranceCardResult(
            False,
            policy_number,
            None,
            f"Portal create failed ({portal_result.status_code}): {portal_result.error}",
        )

    subject, body = rc.build_purchase_welcome_email(
        rc.PurchaseWelcomeEmailInput(
            first_name=rc.first_name_from_full(name),
            policy_number=policy_number,
            effective_date_label=_format_effective_date_label(today),
            vehicle_line=vehicle_label or "Vehicle on file",
            portal_email=email_to,
            portal_password=portal_password,
        )
    )

    pdf_filename = f"insurance-id-card-{policy_number}.pdf"
    send_result = await asyncio.to_thread(
        rc.send_insurance_card_email,
        to_address=email_to,
        subject=subject,
        body=body,
        pdf_bytes=pdf_bytes,
        pdf_filename=pdf_filename,
    )
    if not send_result.ok:
        return InsuranceCardResult(
            False,
            policy_number,
            pdf_filename,
            send_result.error or "Resend send failed.",
            email_to,
            portal_password,
        )

    return InsuranceCardResult(
        True,
        policy_number,
        pdf_filename,
        None,
        email_to,
        portal_password,
    )
