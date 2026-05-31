"""Krab Insurance Bot — AI intake + NY FS-20 insurance card email only."""
from __future__ import annotations

import asyncio
import html
import logging
import re
import sys
from datetime import datetime
from typing import Any, Optional

import pytz
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from config import Config
from utils import ai_vision
from utils import parse_lead as pl
from utils import state_detection as sd
from utils import transactions as tx

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

STATE_PHASE1_INPUT = 1
STATE_REVIEW = 2
STATE_EDIT_FIELD_PICK = 3
STATE_EDIT_FIELD_VALUE = 4
STATE_AWAIT_EMAIL = 5
STATE_DELIVERY_CHOICE = 6

PHASE1_VISION_CANCEL_CB = "p1_cancel"
PHASE1_VISION_PHOTO_CB = "p1_photo"
PHASE1_VISION_DONE_CB = "p1_done"

PLAN_OPTIONS = (
    (1, "1 month"),
    (3, "3 months"),
    (6, "6 months"),
    (12, "12 months"),
)
DEFAULT_PLAN_MONTHS = 1
PLAN_MONTH_VALUES = tuple(m for m, _ in PLAN_OPTIONS)
CARD_STATE_OPTIONS = ("NY", "NJ")
DEFAULT_CARD_STATE = "NY"


def _next_plan_months(current: int) -> int:
    try:
        idx = PLAN_MONTH_VALUES.index(current)
    except ValueError:
        return PLAN_MONTH_VALUES[0]
    return PLAN_MONTH_VALUES[(idx + 1) % len(PLAN_MONTH_VALUES)]


def _plan_label(months: int) -> str:
    return next((label for m, label in PLAN_OPTIONS if m == months), f"{months} months")

EDITABLE_FIELDS = {
    "name": "Name",
    "address": "Address",
    "city_state_zip": "City, State, ZIP",
    "vin": "VIN",
    "car": "Car (year make model)",
    "color": "Color",
    "email": "Email",
    "driver_license_id": "Driver license ID",
}

PORTAL_DEFAULT_PASSWORD = "Temp#A9"

MOTIVATIONAL_QUOTES = (
    "“Coverage today is peace of mind tomorrow.”",
    "“Drive safe. Stay covered. Live easy.”",
    "“The best protection is the one already in place.”",
    "“Small step today, big protection tomorrow.”",
    "“Smooth roads start with smart coverage.”",
    "“Insured drivers sleep better.”",
    "“Confidence on the road begins with the right card.”",
    "“One card. Total peace of mind.”",
    "“Be road-ready, always.”",
    "“Protect the journey, enjoy the ride.”",
)


def _rotating_quote() -> str:
    from datetime import datetime as _dt, timezone as _tz
    idx = _dt.now(_tz.utc).minute % len(MOTIVATIONAL_QUOTES)
    return MOTIVATIONAL_QUOTES[idx]


def _intro_message() -> str:
    return (
        "🚗🪪 <b>Insurance Card Generator</b>\n\n"
        "Send <b>PHOTOS</b> or <b>TEXT</b>\n\n"
        "📸 Driver License\n"
        "📸 VIN/Title\n"
        "📸 Email\n"
        "_______________________\n"
        "👤 Name\n"
        "🏠 Address\n"
        "🚗 Year Make Model\n"
        "🎨 Color\n"
        "🔠 VIN\n"
        "🪪 DL Number\n"
        "📧 Email\n\n"
        "⚡️ <b>Ai Data Parse Enabled</b>✅\n"
        f"<i>{html.escape(_rotating_quote())}</i>"
    )

HELP_TEXT = (
    "🚗 <b>Insurance Card Generator</b> 🤖\n\n"
    "<b>Commands</b>\n"
    "/start — begin a new insurance card\n"
    "/transactions — view your past insurance cards\n"
    "/cancel — cancel the current flow\n"
    "/help — show this message\n\n"
    "<b>How it works</b>\n"
    "📸 Send a photo of your Driver's License and Title/Registration\n"
    "⌨️ Or type your Full Name, Address, VIN, and DL Number\n\n"
    "You can send multiple photos, then tap <b>✅ Done</b>.\n"
    "Include an <b>email</b> in the image or text so we can send the card."
)


async def _send_clean(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    text: str,
    *,
    parse_mode: str | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
):
    """Send a bot message and delete the previously tracked one for this chat.

    Keeps the bot side of the conversation to a single live message — every
    new reply replaces the prior one. The tracked id lives on ``chat_data``
    so it survives ``user_data.clear()`` between flows.
    """
    new_msg = await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=parse_mode,
        reply_markup=reply_markup,
    )
    chat_data = context.chat_data
    prev_id = chat_data.get("last_bot_msg_id") if chat_data is not None else None
    if prev_id and prev_id != new_msg.message_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=prev_id)
        except Exception as e:
            logger.debug("delete previous bot message %s failed: %s", prev_id, e)
    if chat_data is not None:
        chat_data["last_bot_msg_id"] = new_msg.message_id
    return new_msg


def _track_message(context: ContextTypes.DEFAULT_TYPE, message) -> None:
    """Mark ``message`` as the currently-visible bot message so the next
    ``_send_clean`` call deletes it.

    Used after ``edit_message_text`` so the edited callback message stays the
    one tracked for deletion.
    """
    if message is None:
        return
    chat_data = context.chat_data
    if chat_data is not None:
        chat_data["last_bot_msg_id"] = message.message_id


def _phase1_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("❌ Cancel", callback_data=PHASE1_VISION_CANCEL_CB),
            InlineKeyboardButton("➕ Photo", callback_data=PHASE1_VISION_PHOTO_CB),
            InlineKeyboardButton("✅ Done", callback_data=PHASE1_VISION_DONE_CB),
        ],
    ])


def _review_keyboard(
    selected_months: int = DEFAULT_PLAN_MONTHS,
    selected_state: str = DEFAULT_CARD_STATE,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton("✅ Generate insurance card", callback_data="review_ok")],
    ]
    if selected_state != "NJ":
        rows.append([
            InlineKeyboardButton(
                f"📅 Duration: {_plan_label(selected_months)} ▶",
                callback_data="review_plan_cycle",
            )
        ])
    rows.append([InlineKeyboardButton("✏️ Edit field", callback_data="review_edit")])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="review_cancel")])
    return InlineKeyboardMarkup(rows)


def _delivery_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📧 Send PDF only", callback_data="delivery_pdf_only")],
        [InlineKeyboardButton("📧 Send TriStateCoverage login", callback_data="delivery_portal")],
        [InlineKeyboardButton("❌ No email", callback_data="delivery_none")],
    ])


def _edit_fields_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for key, label in EDITABLE_FIELDS.items():
        rows.append([InlineKeyboardButton(label, callback_data=f"ef_{key}")])
    rows.append([InlineKeyboardButton("⬅️ Back to review", callback_data="ef_back")])
    return InlineKeyboardMarkup(rows)


_COLOR_FULL_NAME = {
    "BLK": "BLACK",
    "WHT": "WHITE",
    "RD": "RED",
    "BL": "BLUE", "BLU": "BLUE",
    "GRN": "GREEN",
    "YEL": "YELLOW", "YLW": "YELLOW",
    "ORG": "ORANGE",
    "PRP": "PURPLE", "PUR": "PURPLE",
    "BRN": "BROWN", "BR": "BROWN",
    "GRY": "GRAY", "GRA": "GRAY",
    "SIL": "SILVER", "SLV": "SILVER",
    "GLD": "GOLD",
}

_COLOR_EMOJI = {
    "BLACK": "⚫",
    "WHITE": "⚪",
    "SILVER": "⚪",
    "GRAY": "🔘", "GREY": "🔘",
    "RED": "🔴",
    "BLUE": "🔵",
    "GREEN": "🟢",
    "YELLOW": "🟡",
    "GOLD": "🟡",
    "ORANGE": "🟠",
    "PURPLE": "🟣",
    "BROWN": "🟤",
    "TAN": "🟤",
    "BEIGE": "🟤",
}


def _color_display(raw: str) -> tuple[str, str]:
    s = (raw or "").strip().upper()
    if not s or s == "-":
        return ("—", "🎨")
    name = _COLOR_FULL_NAME.get(s, s)
    emoji = _COLOR_EMOJI.get(name, "🎨")
    return (name, emoji)


def _format_city_state_zip(raw: str) -> str:
    """Render ``CITY STATE ZIP`` (parser output) as ``CITY, STATE ZIP``."""
    s = (raw or "").strip().upper()
    if not s:
        return "—"
    m = re.match(r"^(.+?)[\s,]+([A-Z]{2})\s+(\d{5}(?:-\d{4})?)\s*$", s)
    if m:
        return f"{m.group(1).strip()}, {m.group(2)} {m.group(3)}"
    return s


async def _ensure_vin_decoded(lead: dict) -> None:
    """Decode the lead VIN via NHTSA once and cache year/make/model on it.

    Used by the review formatter so the operator sees the real Year Make
    Model on screen instead of whatever raw text the parser captured.
    """
    if lead.get("decoded_year") or lead.get("decoded_make") or lead.get("decoded_model"):
        return
    from utils import insurance_card as ic

    vd = (lead.get("vehicle_details") or "").splitlines()
    vin_candidates = [
        ic.extract_vin_from_text(lead.get("vehicle_details") or ""),
        ic.normalize_vin(vd[5].strip()) if len(vd) > 5 else "",
    ]
    vin = next((v for v in vin_candidates if v and len(v) == 17), "")
    if not vin:
        return
    decoded = await asyncio.to_thread(ic.decode_vin_from_nhtsa, vin)
    if not decoded:
        return
    lead["decoded_year"] = (decoded.get("modelYear") or "").strip()
    lead["decoded_make"] = (decoded.get("vehicleMake") or "").strip()
    lead["decoded_model"] = (decoded.get("vehicleModel") or "").strip()


def _format_review(
    lead: dict[str, Any],
    selected_months: int = DEFAULT_PLAN_MONTHS,
    selected_state: str = DEFAULT_CARD_STATE,
) -> str:
    vd = (lead.get("vehicle_details") or "").splitlines()
    def ln(i: int) -> str:
        return vd[i].strip() if i < len(vd) else ""

    name = (ln(0) or "—").upper()
    address = (ln(1) or "—").upper()
    csz = _format_city_state_zip(ln(2))
    vin = (ln(5) or "—").upper()

    decoded_year = (lead.get("decoded_year") or "").strip()
    decoded_make = (lead.get("decoded_make") or "").strip()
    decoded_model = (lead.get("decoded_model") or "").strip()
    if decoded_year or decoded_make or decoded_model:
        car = " ".join(p for p in [decoded_year, decoded_make, decoded_model] if p).upper()
    else:
        car = (ln(6) or "—").upper()

    color_name, _ = _color_display(ln(7))
    dl_id_raw = (lead.get("driver_license_id") or "").strip()
    dl_id = re.sub(r"\s+", "", dl_id_raw) or "—"
    email = (lead.get("email") or "").strip()

    if selected_state == "NJ":
        duration = "1 Month Policy"
        state_label = "🗽New Jersey (NJ TEI)"
    else:
        duration = f"{_plan_label(selected_months).replace('month', 'Month')} Policy"
        state_label = "🗽New York (NY FS-20)"

    parts = [
        "🚗🪪 <b>INSURANCE CARD READY</b>✅",
        "━━━━━━━━━━━━━━━",
        f"👤 {html.escape(name)}",
        f"🏠 {html.escape(address)}",
        f"🌆 {html.escape(csz)}",
        f"🚗 {html.escape(car)}",
        f"🎨{html.escape(color_name)}",
        f"🔠 {html.escape(vin)}",
        f"🪪 {html.escape(dl_id)}",
        f"📅{html.escape(duration)}",
        state_label,
    ]
    if email:
        parts.append(f"📧{html.escape(email)}")
    return "\n".join(parts)


def _parse_annual_premium(lead: dict) -> float:
    raw = (lead.get("price") or "").strip().replace(",", "").replace("$", "")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _generate_transaction_id() -> str:
    """``TXN_YYYYMMDD_HHMMSS_NNNN`` (NY time)."""
    import random as _rnd
    now = datetime.now(pytz.timezone("America/New_York"))
    return f"TXN_{now.strftime('%Y%m%d_%H%M%S')}_{_rnd.randint(1000, 9999)}"


async def _build_card_pdf(
    lead: dict,
    *,
    months: int,
    card_state: str,
) -> tuple[Optional[dict], Optional[str]]:
    """Common prep + PDF generation.

    Returns ``(card_data, error)``. ``card_data`` is a dict the bot caches in
    ``user_data`` so the email delivery handlers can finish the job without
    re-decoding the lead.
    """
    from utils import insurance_card as ic
    from utils import nj_card_api as nj
    from utils import resend_client as rc

    email = (lead.get("email") or "").strip()
    if not email:
        return None, "No email on file."

    state = card_state if card_state in CARD_STATE_OPTIONS else sd.detect_card_state(lead)

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
        return None, "No valid 17-character VIN found. Update the VIN and try again."

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

    vehicle_make_short = re.sub(r"[^A-Za-z0-9]", "", vehicle_make_full).upper()[:5] or "MAKE"
    if not (vehicle_year and vehicle_year.isdigit() and len(vehicle_year) == 4):
        vehicle_year = "0000"

    today = datetime.now(pytz.timezone("America/New_York")).date()
    plan_months = 1 if state == "NJ" else max(1, int(months or 1))
    expiration_date = ic.expiration_for_plan(today, months=plan_months)
    effective_label = ic.date_to_mmddyyyy(today)
    expiration_label = ic.date_to_mmddyyyy(expiration_date)

    address_lines: list[str] = []
    if addr_line1:
        address_lines.append(addr_line1)
    if addr_csz:
        address_lines.append(addr_csz)
    if not address_lines:
        address_lines = ["UNKNOWN ADDRESS"]

    phone_raw = (lead.get("phone_number") or "").strip()

    if state == "NJ":
        if not Config.is_nj_configured():
            return None, "NJ insurance card not configured (BARCODE_APP_BASE_URL)."
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
            email=email,
            first_name=rc.first_name_from_full(name),
            phone=phone_raw or None,
            annual_premium=_parse_annual_premium(lead) or None,
        )
        pdf_result = await asyncio.to_thread(nj.fetch_nj_pdf_preview, nj_payload)
        if not pdf_result.ok or not pdf_result.pdf_bytes:
            err = pdf_result.error or "NJ PDF preview failed."
            if pdf_result.status_code:
                err = f"{err} (HTTP {pdf_result.status_code})"
            return None, err
        pdf_bytes = pdf_result.pdf_bytes
    else:
        if not Config.is_resend_configured():
            return None, "Email not configured (RESEND_API_KEY and RESEND_FROM)."
        policy_number = ic.generate_policy_number()
        issuer = ic.CardIssuer(
            carrier_name=Config.INSURANCE_CARRIER_NAME,
            agency_phone=Config.INSURANCE_ISSUER_PHONE,
            agency_name=Config.INSURANCE_ISSUER_NAME,
            agency_address_lines=[
                ln.strip() for ln in (Config.INSURANCE_ISSUER_ADDRESS or "").split("|") if ln.strip()
            ],
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
            issuer=issuer,
        )
        try:
            pdf_bytes = await asyncio.to_thread(ic.build_ny_insurance_id_card_pdf, pdf_input)
        except Exception as e:
            logger.exception("PDF build failed: %s", e)
            return None, f"Could not build insurance card PDF: {e}"
        nj_payload = None

    vehicle_label = ic.format_suggested_vehicle_name(vehicle_year, vehicle_make_full, vehicle_model)
    if color and color != "-":
        vehicle_label = f"{vehicle_label} — {color}".strip(" —")

    return {
        "txn_id": _generate_transaction_id(),
        "policy_number": policy_number,
        "pdf_bytes": pdf_bytes,
        "card_state": state,
        "plan_months": plan_months,
        "name": name.upper(),
        "address_line": addr_line1.upper(),
        "csz": _format_city_state_zip(addr_csz),
        "vin": vin_clean,
        "vehicle_year": vehicle_year if vehicle_year != "0000" else "",
        "vehicle_make_full": vehicle_make_full,
        "vehicle_model": vehicle_model,
        "vehicle_label": vehicle_label or car_raw or "Vehicle on file",
        "color": color,
        "dl_id": (lead.get("driver_license_id") or "").strip() or None,
        "email": email,
        "phone": phone_raw or None,
        "annual_premium": _parse_annual_premium(lead),
        "today_iso": today.isoformat(),
        "today_ymd_slash": today.strftime("%Y/%m/%d"),
        "effective_label": effective_label,
        "expiration_label": expiration_label,
        "expiration_iso": expiration_date.isoformat(),
        "nj_payload": nj_payload,
        "vehicle_make_short": vehicle_make_short,
    }, None


def _format_info_card(c: dict) -> str:
    year_make = " ".join(p for p in [c.get("vehicle_year"), c.get("vehicle_make_full")] if p).strip().upper() or "—"
    address_full = c["address_line"]
    if c.get("csz") and c["csz"] != "—":
        address_full = f"{c['address_line']}, {c['csz']}"
    return (
        "✅ <b>Insurance Card Ready!</b>\n\n"
        f"🆔 Transaction ID: <code>{html.escape(c['txn_id'])}</code>\n"
        f"🗽 State: <b>{html.escape(c['card_state'])}</b>\n"
        f"👤 Name: {html.escape(c['name'])}\n"
        f"🏠 Address: {html.escape(address_full)}\n"
        f"🔢 VIN: <code>{html.escape(c['vin'])}</code>\n"
        f"🚗 Vehicle: {html.escape(year_make)}\n"
        f"License: {html.escape(c.get('dl_id') or '—')}\n"
        f"📋 Policy: <code>{html.escape(c['policy_number'])}</code>\n"
        f"📅 Duration: {int(c['plan_months'])} month(s)\n"
        f"📆 Issue Date: {html.escape(c['today_ymd_slash'])}\n\n"
        "Would you like me to email?"
    )


async def _send_pdf_document(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    pdf_bytes: bytes,
    policy_number: str,
    card_state: str,
) -> None:
    """Send the PDF preview as a Telegram Document. Not tracked for deletion."""
    from io import BytesIO
    prefix = "nj-tei" if card_state == "NJ" else "ny-fs20"
    fname = f"{prefix}-{policy_number}.pdf"
    bio = BytesIO(pdf_bytes)
    bio.name = fname
    await context.bot.send_document(chat_id=chat_id, document=bio, filename=fname)


async def _email_pdf_only(card: dict) -> tuple[bool, Optional[str]]:
    """Email the PDF via the bot's own Resend wrapper — no portal account created."""
    from utils import resend_client as rc

    if not Config.is_resend_configured():
        return False, "Email not configured (RESEND_API_KEY and RESEND_FROM)."

    today = datetime.fromisoformat(card["today_iso"]).date()
    effective_date_label = f"{today.strftime('%B')} {today.day}, {today.year}"
    subject, body = rc.build_purchase_welcome_email(
        rc.PurchaseWelcomeEmailInput(
            first_name=rc.first_name_from_full(card["name"]),
            policy_number=card["policy_number"],
            effective_date_label=effective_date_label,
            vehicle_line=card.get("vehicle_label") or "Vehicle on file",
            portal_email=card["email"],
            portal_password="",
        )
    )
    pdf_filename = f"insurance-id-card-{card['policy_number']}.pdf"
    send_result = await asyncio.to_thread(
        rc.send_insurance_card_email,
        to_address=card["email"],
        subject=subject,
        body=body,
        pdf_bytes=card["pdf_bytes"],
        pdf_filename=pdf_filename,
    )
    if not send_result.ok:
        return False, send_result.error or "Resend send failed."
    return True, None


async def _email_with_portal(card: dict) -> tuple[bool, Optional[str], Optional[str]]:
    """Email + create TriStateCoverage portal account. Returns (ok, error, portal_warning)."""
    from utils import nj_card_api as nj
    from utils import resend_client as rc
    from utils import tristatecoverage_api as tsc

    if card["card_state"] == "NJ":
        nj_payload = card.get("nj_payload")
        if not nj_payload:
            return False, "Internal: missing NJ payload.", None
        nj_result = await asyncio.to_thread(nj.send_nj_insurance_email, nj_payload)
        if not nj_result.ok:
            err = nj_result.error or "NJ card API failed."
            if nj_result.status_code:
                err = f"{err} (HTTP {nj_result.status_code})"
            return False, err, None
        return True, None, nj_result.portal_warning

    if not Config.is_portal_integration_configured():
        return False, "Portal integration not configured (INTEGRATIONS_API_KEY).", None
    if not Config.is_resend_configured():
        return False, "Email not configured (RESEND_API_KEY and RESEND_FROM).", None

    portal_password = PORTAL_DEFAULT_PASSWORD
    portal_payload = {
        "email": card["email"],
        "password": portal_password,
        "name": card["name"],
        "phone": card.get("phone") or "+1 000 000 0000",
        "vehicleName": card.get("vehicle_label") or "Vehicle on file",
        "vin": card["vin"],
        "policyNumber": card["policy_number"],
        "policyEffectiveDate": card["today_iso"],
        "policyExpirationDate": card["expiration_iso"],
        "annualPremium": card.get("annual_premium") or 0.0,
        "vehicleColor": card.get("color") if card.get("color") and card["color"] != "-" else None,
        "vehicleYear": card["vehicle_year"] or None,
        "vehicleMake": card.get("vehicle_make_full") or None,
        "vehicleModel": card.get("vehicle_model") or None,
    }
    portal_payload = {k: v for k, v in portal_payload.items() if v is not None}
    portal_result = await asyncio.to_thread(tsc.create_portal_client, portal_payload, card["pdf_bytes"])
    if not portal_result.ok:
        err = portal_result.error or "Portal create failed."
        return False, f"Portal create failed ({portal_result.status_code}): {err}", None

    today = datetime.fromisoformat(card["today_iso"]).date()
    effective_date_label = f"{today.strftime('%B')} {today.day}, {today.year}"
    subject, body = rc.build_purchase_welcome_email(
        rc.PurchaseWelcomeEmailInput(
            first_name=rc.first_name_from_full(card["name"]),
            policy_number=card["policy_number"],
            effective_date_label=effective_date_label,
            vehicle_line=card.get("vehicle_label") or "Vehicle on file",
            portal_email=card["email"],
            portal_password=portal_password,
        )
    )
    pdf_filename = f"insurance-id-card-{card['policy_number']}.pdf"
    send_result = await asyncio.to_thread(
        rc.send_insurance_card_email,
        to_address=card["email"],
        subject=subject,
        body=body,
        pdf_bytes=card["pdf_bytes"],
        pdf_filename=pdf_filename,
    )
    if not send_result.ok:
        return False, send_result.error or "Resend send failed.", None
    return True, None, None


def _current_plan_months(context: ContextTypes.DEFAULT_TYPE) -> int:
    months = int(context.user_data.get("plan_months") or DEFAULT_PLAN_MONTHS)
    if months not in {m for m, _ in PLAN_OPTIONS}:
        return DEFAULT_PLAN_MONTHS
    return months


def _current_card_state(context: ContextTypes.DEFAULT_TYPE, lead: dict | None = None) -> str:
    state = (context.user_data.get("card_state") or "").strip().upper()
    if state in CARD_STATE_OPTIONS:
        return state
    if lead:
        return sd.detect_card_state(lead)
    return DEFAULT_CARD_STATE


async def _go_to_review(update: Update, context: ContextTypes.DEFAULT_TYPE, lead: dict) -> int:
    context.user_data["lead"] = lead
    context.user_data.pop("pending_media", None)
    context.user_data.setdefault("plan_months", DEFAULT_PLAN_MONTHS)
    context.user_data["card_state"] = sd.detect_card_state(lead)
    chat = update.effective_chat
    if chat is None:
        return STATE_REVIEW
    if not (lead.get("email") or "").strip():
        await _send_clean(
            context,
            chat.id,
            "📧 Send the client's email address now:",
        )
        return STATE_AWAIT_EMAIL
    await _ensure_vin_decoded(lead)
    months = _current_plan_months(context)
    card_state = _current_card_state(context, lead)
    await _send_clean(
        context,
        chat.id,
        _format_review(lead, months, card_state),
        parse_mode="HTML",
        reply_markup=_review_keyboard(months, card_state),
    )
    return STATE_REVIEW


def _remember_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is not None:
        context.user_data["tg_user_id"] = user.id
        context.user_data["tg_username"] = user.username or user.full_name


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    _remember_user(update, context)
    context.user_data["pending_media"] = []
    await _send_clean(context, update.effective_chat.id, _intro_message(), parse_mode="HTML")
    return STATE_PHASE1_INPUT


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _send_clean(context, update.effective_chat.id, HELP_TEXT, parse_mode="HTML")
    return ConversationHandler.END


async def cmd_transactions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    if user is None:
        return ConversationHandler.END

    entries = tx.list_for_user(user.id, limit=20)
    if not entries:
        await _send_clean(
            context,
            update.effective_chat.id,
            "📭 No transactions yet.\n\nUse /start to issue your first insurance card.",
        )
        return ConversationHandler.END

    lines: list[str] = [f"📒 <b>Your last {len(entries)} transaction(s)</b>\n"]
    for e in entries:
        ts_raw = (e.get("ts") or "").rstrip("Z")
        try:
            ts_display = datetime.fromisoformat(ts_raw).strftime("%Y-%m-%d %H:%M UTC")
        except ValueError:
            ts_display = ts_raw or "—"
        status = "✅" if e.get("success") else "❌"
        policy = e.get("policy_number") or "—"
        email_to = e.get("email") or "—"
        name = e.get("name") or "—"
        vehicle = e.get("vehicle") or "—"
        plan_months_raw = e.get("plan_months")
        if isinstance(plan_months_raw, int) and plan_months_raw > 0:
            plan_label = f"{plan_months_raw} month{'s' if plan_months_raw != 1 else ''}"
        else:
            plan_label = "—"
        card_state = (e.get("state") or "NY").upper()
        block = (
            f"{status} <b>{html.escape(ts_display)}</b>\n"
            f"📋 Policy: <code>{html.escape(policy)}</code>\n"
            f"📅 Duration: {html.escape(plan_label)}\n"
            f"🗺 State: {html.escape(card_state)}\n"
            f"👤 {html.escape(name)}\n"
            f"🚗 {html.escape(vehicle)}\n"
            f"📧 {html.escape(email_to)}"
        )
        if not e.get("success") and e.get("error"):
            block += f"\n⚠️ {html.escape(str(e.get('error')))}"
        lines.append(block)

    await _send_clean(
        context, update.effective_chat.id, "\n\n".join(lines), parse_mode="HTML"
    )
    return ConversationHandler.END


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await _send_clean(context, update.effective_chat.id, "Cancelled.")
    return ConversationHandler.END


async def handle_phase1_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    if not Config.is_ai_vision_configured():
        await _send_clean(context, chat_id, "OPENAI_API_KEY is not configured.")
        return ConversationHandler.END
    text = (update.effective_message.text or "").strip()
    lead = pl.parse_from_text(text)
    if not lead:
        await _send_clean(
            context,
            chat_id,
            "Could not parse that text. Send the 11-line block or a clearer photo.",
        )
        return STATE_PHASE1_INPUT
    return await _go_to_review(update, context, lead)


async def handle_phase1_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    if not Config.is_ai_vision_configured():
        await _send_clean(context, chat_id, "OPENAI_API_KEY is not configured.")
        return ConversationHandler.END
    photos = update.effective_message.photo
    if not photos:
        return STATE_PHASE1_INPUT
    file = await context.bot.get_file(photos[-1].file_id)
    data = await file.download_as_bytearray()
    pending: list = context.user_data.setdefault("pending_media", [])
    pending.append((bytes(data), "image/jpeg"))
    await _send_clean(
        context,
        chat_id,
        f"📸 Received {len(pending)} photo(s).",
        reply_markup=_phase1_keyboard(),
    )
    return STATE_PHASE1_INPUT


async def handle_phase1_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    doc = update.effective_message.document
    if not doc:
        return STATE_PHASE1_INPUT
    mime = (doc.mime_type or "").lower()
    file = await context.bot.get_file(doc.file_id)
    data = await file.download_as_bytearray()
    raw = bytes(data)

    if mime == "application/pdf" or (doc.file_name or "").lower().endswith(".pdf"):
        lead = pl.parse_from_pdf(raw)
        if not lead:
            await _send_clean(context, chat_id, "Could not read that PDF.")
            return STATE_PHASE1_INPUT
        return await _go_to_review(update, context, lead)

    if mime.startswith("image/"):
        pending: list = context.user_data.setdefault("pending_media", [])
        pending.append((raw, mime))
        await _send_clean(
            context,
            chat_id,
            f"📸 Received {len(pending)} photo(s).",
            reply_markup=_phase1_keyboard(),
        )
        return STATE_PHASE1_INPUT

    await _send_clean(context, chat_id, "Send a photo or PDF.")
    return STATE_PHASE1_INPUT


async def handle_phase1_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    chat_id = q.message.chat.id

    if data == PHASE1_VISION_CANCEL_CB:
        context.user_data.clear()
        await q.edit_message_text("Cancelled.")
        _track_message(context, q.message)
        return ConversationHandler.END

    if data == PHASE1_VISION_PHOTO_CB:
        await _send_clean(context, chat_id, "📷 Send your photo(s) now, then tap Done.")
        return STATE_PHASE1_INPUT

    if data == PHASE1_VISION_DONE_CB:
        pending = context.user_data.get("pending_media") or []
        if not pending:
            await _send_clean(context, chat_id, "No photos yet — send an image or paste text.")
            return STATE_PHASE1_INPUT
        lead = pl.parse_from_media_parts(pending)
        if not lead:
            await _send_clean(
                context,
                chat_id,
                "⚠️ AI could not extract client info from those images.\n\n"
                "Try a clearer photo, or paste the 11-line text block.",
            )
            return STATE_PHASE1_INPUT
        context.user_data.pop("pending_media", None)
        context.user_data["lead"] = lead
        context.user_data.setdefault("plan_months", DEFAULT_PLAN_MONTHS)
        context.user_data["card_state"] = sd.detect_card_state(lead)
        if not (lead.get("email") or "").strip():
            await _send_clean(
                context,
                chat_id,
                "📧 Send the client's email address now:",
            )
            return STATE_AWAIT_EMAIL
        await _ensure_vin_decoded(lead)
        months = _current_plan_months(context)
        card_state = _current_card_state(context, lead)
        await _send_clean(
            context,
            chat_id,
            _format_review(lead, months, card_state),
            parse_mode="HTML",
            reply_markup=_review_keyboard(months, card_state),
        )
        return STATE_REVIEW

    return STATE_PHASE1_INPUT


async def handle_review_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    lead = context.user_data.get("lead") or {}
    chat_id = q.message.chat.id

    if data == "review_cancel":
        context.user_data.clear()
        await q.edit_message_text("Cancelled.")
        _track_message(context, q.message)
        return ConversationHandler.END

    if data == "review_edit":
        await _send_clean(
            context,
            chat_id,
            "Pick a field to edit:",
            reply_markup=_edit_fields_keyboard(),
        )
        return STATE_EDIT_FIELD_PICK

    if data == "review_plan_cycle":
        card_state = _current_card_state(context, lead)
        if card_state == "NJ":
            return STATE_REVIEW
        current = _current_plan_months(context)
        new_months = _next_plan_months(current)
        context.user_data["plan_months"] = new_months
        try:
            await q.edit_message_text(
                _format_review(lead, new_months, card_state),
                parse_mode="HTML",
                reply_markup=_review_keyboard(new_months, card_state),
            )
            _track_message(context, q.message)
        except Exception:
            logger.exception("Failed to refresh duration cycle")
        return STATE_REVIEW

    if data == "review_ok":
        return await _send_preview_flow(q.message, context, lead)

    return STATE_REVIEW


async def handle_edit_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    chat_id = q.message.chat.id

    if data == "ef_back":
        lead = context.user_data.get("lead") or {}
        months = _current_plan_months(context)
        card_state = _current_card_state(context, lead)
        await _send_clean(
            context,
            chat_id,
            _format_review(lead, months, card_state),
            parse_mode="HTML",
            reply_markup=_review_keyboard(months, card_state),
        )
        return STATE_REVIEW

    if data.startswith("ef_"):
        key = data[3:]
        if key not in EDITABLE_FIELDS:
            return STATE_EDIT_FIELD_PICK
        context.user_data["edit_field"] = key
        await _send_clean(
            context,
            chat_id,
            f"✍️ Send new value for: {EDITABLE_FIELDS[key]}\n(Type - to clear)",
        )
        return STATE_EDIT_FIELD_VALUE

    return STATE_EDIT_FIELD_PICK


async def handle_edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    key = context.user_data.get("edit_field")
    if not key:
        return STATE_REVIEW
    text = (update.effective_message.text or "").strip()
    lead = context.user_data.get("lead") or {}
    vd = (lead.get("vehicle_details") or "").splitlines()
    while len(vd) < 11:
        vd.append("")

    if key == "email":
        lead["email"] = None if text == "-" else (ai_vision.normalize_email(text) or text)
    elif key == "driver_license_id":
        lead["driver_license_id"] = None if text == "-" else ai_vision.normalize_driver_license_id(text)
    elif key == "name":
        vd[0] = text if text != "-" else ""
    elif key == "address":
        vd[1] = text if text != "-" else ""
    elif key == "city_state_zip":
        vd[2] = text if text != "-" else ""
        context.user_data["card_state"] = sd.detect_card_state({"vehicle_details": "\n".join(vd)})
    elif key == "vin":
        vd[5] = text if text != "-" else ""
    elif key == "car":
        vd[6] = text if text != "-" else ""
    elif key == "color":
        vd[7] = ai_vision.normalize_phase1_color(text) if text != "-" else "-"

    lead["vehicle_details"] = "\n".join(vd)
    if key == "vin":
        for k in ("decoded_year", "decoded_make", "decoded_model"):
            lead.pop(k, None)
    context.user_data["lead"] = lead
    context.user_data.pop("edit_field", None)
    await _ensure_vin_decoded(lead)
    months = _current_plan_months(context)
    card_state = _current_card_state(context, lead)
    await _send_clean(
        context,
        update.effective_chat.id,
        _format_review(lead, months, card_state),
        parse_mode="HTML",
        reply_markup=_review_keyboard(months, card_state),
    )
    return STATE_REVIEW


async def handle_await_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    text = (update.effective_message.text or "").strip()
    em = ai_vision.normalize_email(text)
    if not em:
        await _send_clean(context, chat_id, "Invalid email. Try again or /cancel.")
        return STATE_AWAIT_EMAIL
    lead = context.user_data.get("lead") or {}
    lead["email"] = em
    context.user_data["lead"] = lead
    await _ensure_vin_decoded(lead)
    months = _current_plan_months(context)
    card_state = _current_card_state(context, lead)
    await _send_clean(
        context,
        chat_id,
        _format_review(lead, months, card_state),
        parse_mode="HTML",
        reply_markup=_review_keyboard(months, card_state),
    )
    return STATE_REVIEW


async def _send_preview_flow(msg, context: ContextTypes.DEFAULT_TYPE, lead: dict) -> int:
    chat_id = msg.chat.id
    card_state = _current_card_state(context, lead)
    plan_months = 1 if card_state == "NJ" else _current_plan_months(context)
    card_label = "NJ Temporary Evidence of Insurance" if card_state == "NJ" else "NY FS-20 insurance card"
    await _send_clean(
        context,
        chat_id,
        f"⏳ Generating {html.escape(card_label)}…",
        parse_mode="HTML",
    )
    card, err = await _build_card_pdf(lead, months=plan_months, card_state=card_state)
    if not card:
        await _send_clean(
            context,
            chat_id,
            "❌ <b>Could not generate insurance card</b>\n\n"
            f"{html.escape(err or 'Unknown error')}",
            parse_mode="HTML",
        )
        context.user_data.clear()
        return ConversationHandler.END

    context.user_data["card"] = card
    await _send_pdf_document(context, chat_id, card["pdf_bytes"], card["policy_number"], card["card_state"])
    await _send_clean(
        context,
        chat_id,
        _format_info_card(card),
        parse_mode="HTML",
        reply_markup=_delivery_keyboard(),
    )
    return STATE_DELIVERY_CHOICE


async def handle_delivery_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    chat_id = q.message.chat.id
    card = context.user_data.get("card") or {}
    if not card:
        await q.edit_message_text("Session expired. /start to begin again.")
        _track_message(context, q.message)
        return ConversationHandler.END

    user_id = context.user_data.get("tg_user_id")
    username = context.user_data.get("tg_username")
    safe_email = html.escape(card.get("email") or "", quote=False)
    safe_policy = html.escape(card.get("policy_number") or "—", quote=False)

    async def _record(success: bool, error: str | None) -> None:
        try:
            tx.record(
                user_id=user_id,
                username=username,
                policy_number=card.get("policy_number"),
                email=card.get("email"),
                vehicle=card.get("vehicle_label"),
                name=card.get("name"),
                success=success,
                error=error,
                plan_months=int(card.get("plan_months") or 1),
                state=card.get("card_state"),
            )
        except Exception:
            logger.exception("Failed to record transaction")

    if data == "delivery_none":
        await _send_clean(
            context,
            chat_id,
            "✅ <b>PDF delivered above.</b>\n\n"
            f"📋 Policy: <code>{safe_policy}</code>\n"
            "ℹ️ No email sent.",
            parse_mode="HTML",
        )
        await _record(True, None)
        context.user_data.clear()
        return ConversationHandler.END

    if data == "delivery_pdf_only":
        await _send_clean(
            context,
            chat_id,
            f"⏳ Emailing PDF to <code>{safe_email}</code>…",
            parse_mode="HTML",
        )
        ok, err = await _email_pdf_only(card)
        if ok:
            await _send_clean(
                context,
                chat_id,
                "✅ <b>Insurance card emailed</b>\n\n"
                f"📋 Policy: <code>{safe_policy}</code>\n"
                f"📧 Delivered to <code>{safe_email}</code>\n"
                "ℹ️ PDF only — no portal account created.",
                parse_mode="HTML",
            )
            await _record(True, None)
        else:
            await _send_clean(
                context,
                chat_id,
                "❌ <b>Could not email the insurance card</b>\n\n"
                f"{html.escape(err or 'Unknown error')}",
                parse_mode="HTML",
            )
            await _record(False, err)
        context.user_data.clear()
        return ConversationHandler.END

    if data == "delivery_portal":
        await _send_clean(
            context,
            chat_id,
            f"⏳ Emailing PDF + creating TriStateCoverage account for "
            f"<code>{safe_email}</code>…",
            parse_mode="HTML",
        )
        ok, err, portal_warning = await _email_with_portal(card)
        if ok:
            text = (
                "✅ <b>Insurance card emailed</b>\n\n"
                f"📋 Policy: <code>{safe_policy}</code>\n"
                f"📧 Delivered to <code>{safe_email}</code>\n"
                "🔐 Portal: TriStateCoverage.com/login"
            )
            if portal_warning:
                text += f"\n\nℹ️ {html.escape(portal_warning)}"
            await _send_clean(context, chat_id, text, parse_mode="HTML")
            await _record(True, None)
        else:
            await _send_clean(
                context,
                chat_id,
                "❌ <b>Could not email the insurance card</b>\n\n"
                f"{html.escape(err or 'Unknown error')}",
                parse_mode="HTML",
            )
            await _record(False, err)
        context.user_data.clear()
        return ConversationHandler.END

    return STATE_DELIVERY_CHOICE


def main() -> None:
    try:
        Config.validate()
    except ValueError as e:
        logger.error("%s", e)
        sys.exit(1)

    app = Application.builder().token(Config.TELEGRAM_BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", cmd_start),
        ],
        states={
            STATE_PHASE1_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_phase1_text),
                MessageHandler(filters.PHOTO, handle_phase1_photo),
                MessageHandler(filters.Document.ALL, handle_phase1_document),
                CallbackQueryHandler(handle_phase1_callbacks, pattern=r"^p1_"),
            ],
            STATE_REVIEW: [
                CallbackQueryHandler(handle_review_callbacks, pattern=r"^review_"),
                CallbackQueryHandler(handle_edit_pick, pattern=r"^ef_"),
            ],
            STATE_EDIT_FIELD_PICK: [
                CallbackQueryHandler(handle_edit_pick, pattern=r"^ef_"),
            ],
            STATE_EDIT_FIELD_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_edit_value),
            ],
            STATE_AWAIT_EMAIL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_await_email),
            ],
            STATE_DELIVERY_CHOICE: [
                CallbackQueryHandler(handle_delivery_callbacks, pattern=r"^delivery_"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_cmd),
            CommandHandler("help", cmd_help),
            CommandHandler("transactions", cmd_transactions),
        ],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("transactions", cmd_transactions))
    app.add_handler(CommandHandler("cancel", cancel_cmd))

    logger.info("Krab Insurance Bot polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
