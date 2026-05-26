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

PHASE1_VISION_CANCEL_CB = "p1_cancel"
PHASE1_VISION_PHOTO_CB = "p1_photo"
PHASE1_VISION_DONE_CB = "p1_done"

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

PHASE1_INTRO = (
    "🛡️ <b>Krab Insurance Bot</b>\n\n"
    "Send client info to issue a NY FS-20 insurance card:\n"
    "• Photo or PDF of registration / insurance docs\n"
    "• Or paste the 11-line text block\n\n"
    "Include an <b>email</b> in the image or text so we can send the card.\n\n"
    "You can send multiple photos, then tap <b>Done</b>."
)

HELP_TEXT = (
    "🛡️ <b>Krab Insurance Bot</b>\n\n"
    "<b>Commands</b>\n"
    "/start — begin a new insurance card\n"
    "/cancel — cancel current flow\n"
    "/help — this message\n\n"
    "<b>11-line text format</b>\n"
    "1. Name\n"
    "2. Address\n"
    "3. City, State, ZIP\n"
    "4. Delivery address\n"
    "5. Delivery city, State, ZIP\n"
    "6. VIN\n"
    "7. Car\n"
    "8. Color\n"
    "9. Insurance company\n"
    "10. Insurance policy #\n"
    "11. Extra info\n\n"
    "Or send a photo/PDF — AI will read it."
)


def _phase1_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📷 Add photo", callback_data=PHASE1_VISION_PHOTO_CB),
            InlineKeyboardButton("✅ Done", callback_data=PHASE1_VISION_DONE_CB),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data=PHASE1_VISION_CANCEL_CB)],
    ])


def _review_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Send insurance card", callback_data="review_ok")],
        [InlineKeyboardButton("✏️ Edit field", callback_data="review_edit")],
        [InlineKeyboardButton("❌ Cancel", callback_data="review_cancel")],
    ])


def _edit_fields_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for key, label in EDITABLE_FIELDS.items():
        rows.append([InlineKeyboardButton(label, callback_data=f"ef_{key}")])
    rows.append([InlineKeyboardButton("⬅️ Back to review", callback_data="ef_back")])
    return InlineKeyboardMarkup(rows)


def _format_review(lead: dict[str, Any]) -> str:
    vd = (lead.get("vehicle_details") or "").splitlines()
    def ln(i: int) -> str:
        return vd[i].strip() if i < len(vd) else "—"
    em = (lead.get("email") or "").strip() or "— (will ask)"
    return (
        "📋 <b>Review client data</b>\n\n"
        f"Name: {html.escape(ln(0))}\n"
        f"Address: {html.escape(ln(1))}\n"
        f"City/State/ZIP: {html.escape(ln(2))}\n"
        f"VIN: {html.escape(ln(5))}\n"
        f"Car: {html.escape(ln(6))}\n"
        f"Color: {html.escape(ln(7))}\n"
        f"Email: {html.escape(em)}\n"
        f"DL ID: {html.escape((lead.get('driver_license_id') or '—'))}\n\n"
        "Tap <b>Send insurance card</b> when ready."
    )


def _parse_annual_premium(lead: dict) -> float:
    raw = (lead.get("price") or "").strip().replace(",", "").replace("$", "")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


async def build_and_send_insurance_card(
    lead: dict,
) -> tuple[bool, Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Returns (ok, policy_number, error, portal_email, portal_password)."""
    from utils import insurance_card as ic
    from utils import resend_client as rc
    from utils import tristatecoverage_api as tsc

    if not Config.is_portal_integration_configured():
        return (False, None, "Portal integration not configured (INTEGRATIONS_API_KEY).", None, None)
    if not Config.is_resend_configured():
        return (False, None, "Email not configured (RESEND_API_KEY and RESEND_FROM).", None, None)

    email = (lead.get("email") or "").strip()
    if not email:
        return (False, None, "No email on file.", None, None)

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
        return (
            False,
            None,
            "No valid 17-character VIN found. Update the VIN and try again.",
            None,
            None,
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

    vehicle_make_short = re.sub(r"[^A-Za-z0-9]", "", vehicle_make_full).upper()[:5] or "MAKE"
    if not (vehicle_year and vehicle_year.isdigit() and len(vehicle_year) == 4):
        vehicle_year = "0000"

    today = datetime.now(pytz.timezone("America/New_York")).date()
    expiration_date = ic.expiration_for_plan(today, months=1)
    effective_label = ic.date_to_mmddyyyy(today)
    expiration_label = ic.date_to_mmddyyyy(expiration_date)
    policy_number = ic.generate_policy_number()
    portal_password = PORTAL_DEFAULT_PASSWORD

    address_lines: list[str] = []
    if addr_line1:
        address_lines.append(addr_line1)
    if addr_csz:
        address_lines.append(addr_csz)
    if not address_lines:
        address_lines = ["UNKNOWN ADDRESS"]

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
        return (False, policy_number, f"Could not build insurance card PDF: {e}", None, None)

    vehicle_label = ic.format_suggested_vehicle_name(vehicle_year, vehicle_make_full, vehicle_model)
    if color and color != "-":
        vehicle_label = f"{vehicle_label} — {color}".strip(" —")
    vehicle_name_api = vehicle_label or car_raw or "Vehicle on file"

    phone_raw = (lead.get("phone_number") or "").strip()
    portal_payload = {
        "email": email,
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
    }
    portal_payload = {k: v for k, v in portal_payload.items() if v is not None}

    portal_result = await asyncio.to_thread(tsc.create_portal_client, portal_payload, pdf_bytes)
    if not portal_result.ok:
        err = portal_result.error or "Portal create failed."
        return (
            False,
            policy_number,
            f"Portal create failed ({portal_result.status_code}): {err}",
            None,
            None,
        )

    effective_date_label = f"{today.strftime('%B')} {today.day}, {today.year}"
    subject, body = rc.build_purchase_welcome_email(
        rc.PurchaseWelcomeEmailInput(
            first_name=rc.first_name_from_full(name),
            policy_number=policy_number,
            effective_date_label=effective_date_label,
            vehicle_line=vehicle_label or "Vehicle on file",
            portal_email=email,
            portal_password=portal_password,
        )
    )

    send_result = await asyncio.to_thread(
        rc.send_insurance_card_email,
        to_address=email,
        subject=subject,
        body=body,
        pdf_bytes=pdf_bytes,
        pdf_filename=f"insurance-id-card-{policy_number}.pdf",
    )
    if not send_result.ok:
        return (False, policy_number, send_result.error or "Resend send failed.", email, portal_password)
    return (True, policy_number, None, email, portal_password)


async def _go_to_review(update: Update, context: ContextTypes.DEFAULT_TYPE, lead: dict) -> int:
    context.user_data["lead"] = lead
    context.user_data.pop("pending_media", None)
    msg = update.effective_message
    if msg:
        await msg.reply_text(_format_review(lead), parse_mode="HTML", reply_markup=_review_keyboard())
    return STATE_REVIEW


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    context.user_data["pending_media"] = []
    await update.effective_message.reply_text(PHASE1_INTRO, parse_mode="HTML", reply_markup=_phase1_keyboard())
    return STATE_PHASE1_INPUT


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text(HELP_TEXT, parse_mode="HTML")
    return ConversationHandler.END


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.effective_message.reply_text("Cancelled.")
    return ConversationHandler.END


async def handle_phase1_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not Config.is_ai_vision_configured():
        await update.effective_message.reply_text("OPENAI_API_KEY is not configured.")
        return ConversationHandler.END
    text = (update.effective_message.text or "").strip()
    lead = pl.parse_from_text(text)
    if not lead:
        await update.effective_message.reply_text(
            "Could not parse that text. Send the 11-line block or a clearer photo."
        )
        return STATE_PHASE1_INPUT
    return await _go_to_review(update, context, lead)


async def handle_phase1_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not Config.is_ai_vision_configured():
        await update.effective_message.reply_text("OPENAI_API_KEY is not configured.")
        return ConversationHandler.END
    photos = update.effective_message.photo
    if not photos:
        return STATE_PHASE1_INPUT
    file = await context.bot.get_file(photos[-1].file_id)
    data = await file.download_as_bytearray()
    pending: list = context.user_data.setdefault("pending_media", [])
    pending.append((bytes(data), "image/jpeg"))
    await update.effective_message.reply_text(
        f"📷 Photo saved ({len(pending)} file(s)). Send more or tap Done.",
        reply_markup=_phase1_keyboard(),
    )
    return STATE_PHASE1_INPUT


async def handle_phase1_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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
            await update.effective_message.reply_text("Could not read that PDF.")
            return STATE_PHASE1_INPUT
        return await _go_to_review(update, context, lead)

    if mime.startswith("image/"):
        pending: list = context.user_data.setdefault("pending_media", [])
        pending.append((raw, mime))
        await update.effective_message.reply_text(
            f"📷 Image saved ({len(pending)}). Tap Done when ready.",
            reply_markup=_phase1_keyboard(),
        )
        return STATE_PHASE1_INPUT

    await update.effective_message.reply_text("Send a photo or PDF.")
    return STATE_PHASE1_INPUT


async def handle_phase1_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    data = q.data or ""

    if data == PHASE1_VISION_CANCEL_CB:
        context.user_data.clear()
        await q.edit_message_text("Cancelled.")
        return ConversationHandler.END

    if data == PHASE1_VISION_PHOTO_CB:
        await q.message.reply_text("📷 Send your photo(s) now, then tap Done.")
        return STATE_PHASE1_INPUT

    if data == PHASE1_VISION_DONE_CB:
        pending = context.user_data.get("pending_media") or []
        if not pending:
            await q.message.reply_text("No photos yet — send an image or paste text.")
            return STATE_PHASE1_INPUT
        lead = pl.parse_from_media_parts(pending)
        if not lead:
            await q.message.reply_text("AI could not read those images. Try clearer photos or paste text.")
            return STATE_PHASE1_INPUT
        context.user_data.pop("pending_media", None)
        await q.message.reply_text(_format_review(lead), parse_mode="HTML", reply_markup=_review_keyboard())
        context.user_data["lead"] = lead
        return STATE_REVIEW

    return STATE_PHASE1_INPUT


async def handle_review_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    lead = context.user_data.get("lead") or {}

    if data == "review_cancel":
        context.user_data.clear()
        await q.edit_message_text("Cancelled.")
        return ConversationHandler.END

    if data == "review_edit":
        await q.message.reply_text("Pick a field to edit:", reply_markup=_edit_fields_keyboard())
        return STATE_EDIT_FIELD_PICK

    if data == "review_ok":
        em = (lead.get("email") or "").strip()
        if not em:
            await q.message.reply_text("📧 What email should we send the insurance card to?")
            return STATE_AWAIT_EMAIL
        return await _send_card_flow(q.message, context, lead)

    return STATE_REVIEW


async def handle_edit_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    data = q.data or ""

    if data == "ef_back":
        lead = context.user_data.get("lead") or {}
        await q.message.reply_text(_format_review(lead), parse_mode="HTML", reply_markup=_review_keyboard())
        return STATE_REVIEW

    if data.startswith("ef_"):
        key = data[3:]
        if key not in EDITABLE_FIELDS:
            return STATE_EDIT_FIELD_PICK
        context.user_data["edit_field"] = key
        await q.message.reply_text(
            f"✍️ Send new value for: {EDITABLE_FIELDS[key]}\n(Type - to clear)"
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
    elif key == "vin":
        vd[5] = text if text != "-" else ""
    elif key == "car":
        vd[6] = text if text != "-" else ""
    elif key == "color":
        vd[7] = ai_vision.normalize_phase1_color(text) if text != "-" else "-"

    lead["vehicle_details"] = "\n".join(vd)
    context.user_data["lead"] = lead
    context.user_data.pop("edit_field", None)
    await update.effective_message.reply_text(_format_review(lead), parse_mode="HTML", reply_markup=_review_keyboard())
    return STATE_REVIEW


async def handle_await_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.effective_message.text or "").strip()
    em = ai_vision.normalize_email(text)
    if not em:
        await update.effective_message.reply_text("Invalid email. Try again or /cancel.")
        return STATE_AWAIT_EMAIL
    lead = context.user_data.get("lead") or {}
    lead["email"] = em
    context.user_data["lead"] = lead
    return await _send_card_flow(update.effective_message, context, lead)


async def _send_card_flow(msg, context: ContextTypes.DEFAULT_TYPE, lead: dict) -> int:
    email = (lead.get("email") or "").strip()
    safe_email = html.escape(email, quote=False)
    await msg.reply_text(
        f"⏳ Building NY FS-20 insurance card and emailing <code>{safe_email}</code>…",
        parse_mode="HTML",
    )
    ok, policy_number, err, portal_email, portal_password = await build_and_send_insurance_card(lead)
    if ok:
        await msg.reply_text(
            "✅ <b>Insurance card sent</b>\n\n"
            f"📋 Policy: <code>{html.escape(policy_number or '—')}</code>\n"
            f"📧 Delivered to <code>{safe_email}</code>\n\n"
            f"Portal email: <code>{html.escape(portal_email or email)}</code>\n"
            f"Portal password: <code>{html.escape(portal_password or '—')}</code>",
            parse_mode="HTML",
        )
    else:
        await msg.reply_text(
            "❌ <b>Could not send insurance card</b>\n\n"
            f"{html.escape(err or 'Unknown error')}",
            parse_mode="HTML",
        )
    context.user_data.clear()
    return ConversationHandler.END


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
        },
        fallbacks=[
            CommandHandler("cancel", cancel_cmd),
            CommandHandler("help", cmd_help),
        ],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("help", cmd_help))

    logger.info("Krab Insurance Bot polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
