"""High-level NY FS-20 insurance card flow for the Krab Dispatch bot.

Mirrors ``krableadsV2/bot.py::_build_and_send_insurance_card`` so both bots
produce the same PDF + email when a client asks for an insurance card. Lead
data is pulled from the shared Krab Issuer Supabase project by ``reference_id``
so the Dispatch bot does not need to re-collect it from the user.
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
from .email_client import EmailProvider
from backend.issuer_supabase import fetch_lead_row_by_reference


logger = logging.getLogger(__name__)
NY_TZ = ZoneInfo("America/New_York")


# ── Email body builder (mirror of krableadsV2/utils/resend_client.py) ────────


@dataclass
class _PurchaseWelcomeEmailInput:
    first_name: str
    policy_number: str
    effective_date_label: str
    vehicle_line: str


def _build_purchase_welcome_email(data: _PurchaseWelcomeEmailInput) -> tuple[str, str]:
    """Return (subject, body) identical to the Issuer bot's welcome email."""
    subject = f"Your policy is active — {data.policy_number}"
    body = (
        f"Hi {data.first_name},\n\n"
        "Thank you for choosing Tri State Coverage for your auto insurance needs.\n\n"
        "Your policy is now active and coverage has been successfully issued.\n\n"
        "Your proof of insurance (PDF) is attached to this email.\n\n"
        "Here's a quick summary of your policy:\n"
        f"• Policy Number: {data.policy_number}\n"
        f"• Effective Date: {data.effective_date_label}\n"
        f"• Vehicle Insured: {data.vehicle_line}\n\n"
        "What's Next?\n"
        "• Review your coverage online\n"
        "• Download proof of insurance\n"
        "• Set up automatic payments\n"
        "• Access your policy anytime through your online dashboard\n\n"
        "Log into your TRISTATECOVERAGE account anytime to manage your policy online.\n\n"
        "Thank you again for choosing Tri State Coverage.\n\n"
        "Sincerely,\n"
        "The Tri State Coverage Team\n\n"
        "Www.TriStateCoverage.com (http://www.tristatecoverage.com/)\n"
        "Tri State Coverage Inc\n"
        "1 N Central Rd 6th floor suite 629\n"
        "Fort Lee, NJ 07024\n"
    )
    return subject, body


def _first_name_from_full(full: Optional[str]) -> str:
    parts = [p for p in (full or "").strip().split() if p]
    return parts[0] if parts else "there"


def _format_effective_date_label(d) -> str:
    """e.g. 'May 8, 2026' (matches krableadsV2 today.strftime + str(day) + ', ' + str(year))."""
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def _issuer_from_env() -> ic.CardIssuer:
    """Build issuer block from env vars with the same fallbacks krableadsV2 uses."""
    carrier_name = (os.getenv("INSURANCE_CARRIER_NAME") or "746 AMERICAN ROAD INSURANCE COMPANY").strip()
    agency_name = (os.getenv("INSURANCE_ISSUER_NAME") or "AMERICAN ROAD INSURANCE CO").strip()
    agency_phone = (os.getenv("INSURANCE_ISSUER_PHONE") or "").strip()
    agency_address = (os.getenv("INSURANCE_ISSUER_ADDRESS") or "P.O. BOX 6027|DEARBORN, MI 48121-6027").strip()
    return ic.CardIssuer(
        carrier_name=carrier_name,
        agency_name=agency_name,
        agency_phone=agency_phone,
        agency_address_lines=[ln.strip() for ln in agency_address.split("|") if ln.strip()],
    )


# ── Main entrypoint ──────────────────────────────────────────────────────────


@dataclass
class InsuranceCardResult:
    ok: bool
    policy_number: Optional[str]
    pdf_filename: Optional[str]
    error: Optional[str]


async def build_and_send_insurance_card(
    *,
    email_to: str,
    reference_id: Optional[str],
    bot_config: BotConfig,
    email_provider: EmailProvider,
) -> InsuranceCardResult:
    """Look up the lead by reference, build the NY FS-20 PDF, and email it.

    Returns ``InsuranceCardResult`` so the caller can choose how to surface
    success vs. failure to the Telegram user.
    """
    email_to = (email_to or "").strip()
    if not email_to:
        return InsuranceCardResult(False, None, None, "Recipient email is required.")

    ref = (reference_id or "").strip()
    if not ref:
        return InsuranceCardResult(
            False, None, None,
            "No reference ID is attached to this chat — cannot look up the lead. "
            "Send a tag PDF first so the bot can match it to the Krab Issuer lead.",
        )

    if not bot_config.supabase_configured():
        return InsuranceCardResult(
            False, None, None,
            "Supabase is not configured on this service — cannot fetch the lead.",
        )

    lead = await asyncio.to_thread(
        fetch_lead_row_by_reference,
        bot_config.supabase_url,
        bot_config.supabase_service_role_key,
        ref,
    )
    if not lead:
        return InsuranceCardResult(
            False, None, None,
            f"No Krab Issuer lead found for reference ID {ref!r}.",
        )

    raw_vehicle = (lead.get("vehicle_details") or "").splitlines()

    def _ln(idx: int) -> str:
        return raw_vehicle[idx].strip() if idx < len(raw_vehicle) else ""

    name = _ln(0) or "UNKNOWN"
    addr_line1 = _ln(1)
    addr_csz = _ln(2)
    vin_raw = _ln(5)
    car_raw = _ln(6)
    color = _ln(7)

    vin_clean = ic.normalize_vin(vin_raw)
    if not vin_clean:
        return InsuranceCardResult(
            False, None, None,
            f"VIN '{vin_raw}' on the lead is invalid — cannot build insurance card.",
        )

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
    effective_label = ic.date_to_mmddyyyy(today)
    expiration_label = ic.date_to_mmddyyyy(ic.expiration_for_plan(today, months=1))
    policy_number = ic.generate_policy_number()

    address_lines: list[str] = []
    if addr_line1:
        address_lines.append(addr_line1)
    if addr_csz:
        address_lines.append(addr_csz)
    if not address_lines:
        address_lines = ["UNKNOWN ADDRESS"]

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
            False, policy_number, None,
            f"Could not build insurance card PDF: {e}",
        )

    vehicle_label = ic.format_suggested_vehicle_name(vehicle_year, vehicle_make_full, vehicle_model)
    if color and color != "-":
        vehicle_label = f"{vehicle_label} — {color}".strip(" —")

    subject, body = _build_purchase_welcome_email(
        _PurchaseWelcomeEmailInput(
            first_name=_first_name_from_full(name),
            policy_number=policy_number,
            effective_date_label=_format_effective_date_label(today),
            vehicle_line=vehicle_label or "Vehicle on file",
        )
    )

    pdf_filename = f"insurance-id-card-{policy_number}.pdf"
    try:
        await email_provider.send_email_with_attachment(
            to_address=email_to,
            subject=subject,
            body=body,
            attachment_bytes=pdf_bytes,
            attachment_filename=pdf_filename,
            attachment_mime=("application", "pdf"),
        )
    except Exception as e:
        logger.exception("Failed to send FS-20 PDF email to %s: %s", email_to, e)
        return InsuranceCardResult(
            False, policy_number, pdf_filename,
            f"Could not email insurance card: {e}",
        )

    return InsuranceCardResult(True, policy_number, pdf_filename, None)
