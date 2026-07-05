from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from enum import IntEnum, auto
from io import BytesIO
from pathlib import Path
from typing import List, Dict

import httpx
import sys
import time
from telegram import Update, Document, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Conflict
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    filters,
)
from telegram.ext import Job

from .config import BotConfig
from .email_client import create_email_provider
from .models import Transaction
from .reference_id import collect_reference_candidates
from backend.db import init_db
from backend.repository import save_transaction
from backend.issuer_supabase import (
    get_lead_id_by_reference,
    get_pending_tag_lead_id_by_reference,
    mark_krab_tag_pdf_sent,
    resolve_reference_from_filename as resolve_issuer_ref_from_supabase,
)


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


MOTIVATION_FILE = Path(__file__).with_name("motivation.json")
FALLBACK_MOTIVATIONS = [
    "Paperwork handled. You just made dispatch smoother for everyone.",
    "Every clean upload keeps your record sharp. Nice work.",
    "You are running your route like a business. Keep stacking wins.",
    "The best drivers stay ahead of paperwork. You are one of them.",
    "Another document locked in. Stay consistent and unstoppable.",
]


def _load_motivational_messages() -> List[str]:
    try:
        raw = json.loads(MOTIVATION_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            cleaned = [str(x).strip() for x in raw if str(x).strip()]
            if cleaned:
                return cleaned
    except Exception as e:
        logger.warning("Failed to load motivation file: %s", e)
    return FALLBACK_MOTIVATIONS


BOT_MOTIVATIONAL_MESSAGES = _load_motivational_messages()


def _get_bot_motivational() -> str:
    # Rotate deterministically based on current minute
    now_minute = datetime.now(timezone.utc).minute
    idx = now_minute % len(BOT_MOTIVATIONAL_MESSAGES)
    return BOT_MOTIVATIONAL_MESSAGES[idx]


def _format_dt_ny_pretty(utc_dt: datetime) -> str:
    """e.g. April 26 2026 12:28pm (America/New_York)."""
    from zoneinfo import ZoneInfo

    ny_tz = ZoneInfo("America/New_York")
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=timezone.utc)
    ts_ny = utc_dt.astimezone(ny_tz)
    month = ts_ny.strftime("%B")
    day = ts_ny.day
    year = ts_ny.year
    hour_24 = ts_ny.hour
    minute = ts_ny.minute
    ampm = "pm" if hour_24 >= 12 else "am"
    hour_12 = hour_24 % 12
    if hour_12 == 0:
        hour_12 = 12
    return f"{month} {day} {year} {hour_12}:{minute:02d}{ampm}"


def _format_send_complete_message(driver_name: str, reference_id: str | None = None) -> str:
    """Success DM after email send; includes Krab Issuer reference when matched from filename / Supabase."""
    quote = _get_bot_motivational()
    ref = (reference_id or "").strip()
    ref_block = f"📋 Reference ID: {ref}\n\n" if ref else ""
    return (
        f"🚘Email📧sent to {driver_name}✅\n\n"
        "✅Completed🏷Successfully❗️\n\n"
        f"{ref_block}"
        f"{quote}\n\n"
        "📈Thank you, keep up the great work⭐️ ! \n"
        "👑🤖🦀!"
    )


def _display_reference_for_tx(tx: Transaction) -> str | None:
    """Prefer stored reference_id; otherwise rescan filename + client notes (handles late-parse edge cases)."""
    r = (tx.reference_id or "").strip()
    if r:
        return r
    alt = collect_reference_candidates(tx.filename or "", tx.client_details or "")
    return alt[0] if alt else None


class State(IntEnum):
    WAITING_FOR_CLIENT_DETAILS = auto()
    WAITING_FOR_RECIPIENT = auto()
    WAITING_FOR_CONFIRMATION = auto()
    WAITING_FOR_INSURANCE = auto()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /start command handler.
    """
    user = update.effective_user
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🛡️INSURANCE🛡️", callback_data="insurance_only"),
            InlineKeyboardButton("📋TRANSACTIONS📋", callback_data="view_transactions"),
        ]
    ])
    await update.message.reply_text(
        "🦀 Welcome to Krab Issuer!\n\n"
        "🏷Please upload PDF Document.\n\n"
        f"{_get_bot_motivational()}\n\n"
        "👑🤖🦀.\n\n",
        reply_markup=keyboard,
    )
    logger.info("User %s (%s) started the bot", user.full_name, user.username)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Entry point: user sends a document.

    State 1 (from roadmap):
    - Receive File -> Log User/Filename/Time.
    - Prompt for 'Client Details'.
    """
    message = update.message
    user = update.effective_user
    document: Document = message.document

    file_name = document.file_name or "unnamed_file"

    # Store minimal context for next step
    context.user_data["pending_document"] = {
        "file_id": document.file_id,
        "file_name": file_name,
    }

    logger.info(
        "Received document from %s (@%s): %s",
        user.full_name,
        user.username,
        file_name,
    )

    prompt_msg = await message.reply_text(
        "🏷PDF Complete✅ Thank you❗️\n\n"
        "👤Now TYPE notes📝 :\n\n"
        f"{_get_bot_motivational()}"
    )
    # Remember this prompt's message id so we can delete it after the user
    # types their notes — keeps the chat clean.
    if prompt_msg is not None:
        context.user_data["notes_prompt_message_id"] = prompt_msg.message_id

    return State.WAITING_FOR_CLIENT_DETAILS


async def handle_client_details(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    State 2: After client details, fetch recipients and show selection.
    """
    message = update.message
    user = update.effective_user

    pending_doc = context.user_data.get("pending_document")
    if not pending_doc:
        await message.reply_text(
            "I couldn't find an associated document. "
            "Please send the PDF again, then provide the client details."
        )
        return ConversationHandler.END

    client_details_text = (message.text or "").strip()
    if not client_details_text:
        await message.reply_text("Please provide some client details as text.")
        return State.WAITING_FOR_CLIENT_DETAILS

    # Once the user replied with notes, remove the "TYPE notes" prompt so the
    # chat stays clean.
    prompt_mid = context.user_data.pop("notes_prompt_message_id", None)
    if prompt_mid:
        try:
            await context.bot.delete_message(
                chat_id=message.chat_id,
                message_id=int(prompt_mid),
            )
        except Exception as del_err:
            logger.debug("Could not delete notes prompt message: %s", del_err)

    # Store client details for later
    context.user_data["client_details"] = client_details_text

    # Fetch recipients from API
    application = context.application
    bot_config: BotConfig = application.bot_data["config"]  # type: ignore[assignment]

    logger.info("Fetching recipients from API: %s/recipients", bot_config.api_base_url)
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{bot_config.api_base_url}/recipients")
            if response.status_code == 200:
                recipients: List[Dict] = response.json()
                logger.info("Successfully fetched %d recipients", len(recipients))
            else:
                logger.warning("Failed to fetch recipients: HTTP %d", response.status_code)
                recipients = []
    except httpx.TimeoutException:
        logger.error("Timeout while fetching recipients from API")
        await message.reply_text(
            "⏱️ The request timed out while fetching recipients. Please try again."
        )
        return State.WAITING_FOR_CLIENT_DETAILS
    except Exception as e:
        logger.error("Failed to fetch recipients: %s", e, exc_info=True)
        recipients = []

    if not recipients:
        await message.reply_text(
            "❌ No recipients configured. Please contact the admin to add recipients."
        )
        context.user_data.pop("pending_document", None)
        context.user_data.pop("client_details", None)
        return ConversationHandler.END

    # Build inline keyboard with recipient names (2 buttons per row for better spacing)
    keyboard_buttons = []
    for i in range(0, len(recipients), 2):
        row = [
            InlineKeyboardButton(recipients[i]["name"], callback_data=f"recipient_{recipients[i]['id']}")
        ]
        # Add second button if there's another recipient
        if i + 1 < len(recipients):
            row.append(
                InlineKeyboardButton(recipients[i + 1]["name"], callback_data=f"recipient_{recipients[i + 1]['id']}")
            )
        keyboard_buttons.append(row)

    keyboard = InlineKeyboardMarkup(keyboard_buttons)

    logger.info("Sending recipient selection keyboard to user %s", user.full_name)
    try:
        await message.reply_text(
            "👤Client info Received✅ Thank you❗️\n\n"
            "🚘 Please Select a Driver:\n\n"
            f"{_get_bot_motivational()}",
            reply_markup=keyboard,
        )
        logger.info("Successfully sent recipient selection to user %s", user.full_name)
    except Exception as e:
        logger.error("Failed to send recipient selection message: %s", e, exc_info=True)
        await message.reply_text(
            "❌ An error occurred while preparing the recipient list. Please try again."
        )
        return ConversationHandler.END

    return State.WAITING_FOR_RECIPIENT


async def handle_recipient_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    State 3: User selected a recipient, show confirmation.
    """
    query = update.callback_query
    await query.answer()

    if not query.data or not query.data.startswith("recipient_"):
        await query.edit_message_text("❌ Invalid selection. Please try again.")
        return ConversationHandler.END

    recipient_id = query.data.replace("recipient_", "")
    pending_doc = context.user_data.get("pending_document")
    client_details_text = context.user_data.get("client_details")

    if not pending_doc or not client_details_text:
        await query.edit_message_text("❌ Session expired. Please start over.")
        context.user_data.pop("pending_document", None)
        context.user_data.pop("client_details", None)
        return ConversationHandler.END

    # Fetch recipient email from API
    application = context.application
    bot_config: BotConfig = application.bot_data["config"]  # type: ignore[assignment]

    recipient_email = None
    recipient_name = None

    try:
        async with httpx.AsyncClient() as client:
            # Fetch recipient email by ID
            response = await client.get(f"{bot_config.api_base_url}/recipients/{recipient_id}/email")
            if response.status_code == 200:
                recipient_data = response.json()
                recipient_email = recipient_data["email"]
                recipient_name = recipient_data["name"]
            else:
                logger.error("Failed to fetch recipient: HTTP %d", response.status_code)
    except Exception as e:
        logger.error("Failed to fetch recipient details: %s", e)

    if not recipient_email:
        await query.edit_message_text("❌ Recipient not found. Please try again.")
        context.user_data.pop("pending_document", None)
        context.user_data.pop("client_details", None)
        return ConversationHandler.END

    # Store recipient info for confirmation
    context.user_data["selected_recipient_id"] = recipient_id
    context.user_data["selected_recipient_email"] = recipient_email
    context.user_data["selected_recipient_name"] = recipient_name

    # Show confirmation message
    filename = pending_doc["file_name"]
    confirmation_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Yes, Send", callback_data="confirm_yes"),
            InlineKeyboardButton("❌ No, Cancel", callback_data="confirm_no")
        ]
    ])

    await query.edit_message_text(
        f"⚠️ **Confirmation Required**\n\n"
        f"Are you sure you want to send:\n"
        f"📄 **{filename}**\n\n"
        f"To: **{recipient_name}**\n\n"
        f"Please confirm:",
        reply_markup=confirmation_keyboard,
        parse_mode="Markdown"
    )

    return State.WAITING_FOR_CONFIRMATION


async def handle_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    State 4: Handle confirmation (Yes or No).
    """
    query = update.callback_query
    await query.answer()

    if not query.data:
        await query.edit_message_text("❌ Invalid selection. Please try again.")
        return ConversationHandler.END

    if query.data == "confirm_no":
        # User cancelled
        await query.edit_message_text(
            "❌ **Cancelled**\n\n"
            "The email was not sent. You can start over by sending a new document.",
            parse_mode="Markdown"
        )
        # Clear the context
        context.user_data.pop("pending_document", None)
        context.user_data.pop("client_details", None)
        context.user_data.pop("selected_recipient_id", None)
        context.user_data.pop("selected_recipient_email", None)
        context.user_data.pop("selected_recipient_name", None)
        return ConversationHandler.END

    if query.data != "confirm_yes":
        await query.edit_message_text("❌ Invalid selection. Please try again.")
        return ConversationHandler.END

    # User confirmed - proceed with sending
    pending_doc = context.user_data.get("pending_document")
    client_details_text = context.user_data.get("client_details")
    recipient_id = context.user_data.get("selected_recipient_id")
    recipient_email = context.user_data.get("selected_recipient_email")
    recipient_name = context.user_data.get("selected_recipient_name")

    # Forward-step validation: if recipient metadata is incomplete in session,
    # refresh it from API before sending so DB records always include driver lead.
    if recipient_id and (not recipient_email or not recipient_name):
        application = context.application
        bot_config: BotConfig = application.bot_data["config"]  # type: ignore[assignment]
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    f"{bot_config.api_base_url}/recipients/{recipient_id}/email"
                )
                if response.status_code == 200:
                    recipient_data = response.json()
                    recipient_email = recipient_email or recipient_data.get("email")
                    recipient_name = recipient_name or recipient_data.get("name")
        except Exception as refresh_err:
            logger.warning("Failed to refresh recipient details at confirm step: %s", refresh_err)

    # Diagnostic: log exactly what's missing so we can tell if only a specific
    # field was lost (common when bot restarts between recipient pick and confirm).
    if not pending_doc or not client_details_text or not recipient_email or not recipient_name:
        logger.warning(
            "Session incomplete at confirm: have_doc=%s have_details=%s have_recipient_id=%s "
            "have_email=%s have_name=%s",
            bool(pending_doc),
            bool(client_details_text),
            bool(recipient_id),
            bool(recipient_email),
            bool(recipient_name),
        )
        missing = []
        if not pending_doc:
            missing.append("document")
        if not client_details_text:
            missing.append("client details")
        if not recipient_email or not recipient_name:
            missing.append("recipient")
        reason = ", ".join(missing) or "session state"
        try:
            await query.edit_message_text(
                "❌ Session expired (lost: "
                + reason
                + "). Please /start and send the document again."
            )
        except Exception:
            pass
        context.user_data.pop("pending_document", None)
        context.user_data.pop("client_details", None)
        context.user_data.pop("selected_recipient_id", None)
        context.user_data.pop("selected_recipient_email", None)
        context.user_data.pop("selected_recipient_name", None)
        return ConversationHandler.END

    application = context.application
    bot_config: BotConfig = application.bot_data["config"]  # type: ignore[assignment]
    user = update.effective_user

    file_name = pending_doc["file_name"]
    candidates = collect_reference_candidates(file_name, client_details_text)
    ref: str | None = None
    matched_lead_id: str | None = None

    if bot_config.supabase_configured():
        # Try explicit 8-char tokens in file/client text first (fast, exact match).
        for cand in candidates:
            lid = await asyncio.to_thread(
                get_pending_tag_lead_id_by_reference,
                bot_config.supabase_url,
                bot_config.supabase_service_role_key,
                cand,
            )
            if lid:
                ref = cand
                matched_lead_id = lid
                logger.info(
                    "Krab Issuer ref (pending tag): ref=%s lead_id=%s file=%s",
                    ref,
                    matched_lead_id,
                    file_name,
                )
                break

        # Always try name-based fuzzy/AI match so we can cross-link by client name.
        # We still keep explicit-token match if it succeeded, so fuzzy only fills gaps.
        fuzzy_ref, fuzzy_lid = await asyncio.to_thread(
            resolve_issuer_ref_from_supabase,
            bot_config.supabase_url,
            bot_config.supabase_service_role_key,
            file_name,
            context_text=client_details_text,
        )
        if fuzzy_lid:
            logger.info(
                "Krab Issuer fuzzy name match: ref=%s lead_id=%s file=%s",
                fuzzy_ref,
                fuzzy_lid,
                file_name,
            )
            if not matched_lead_id:
                ref = fuzzy_ref
                matched_lead_id = fuzzy_lid

        # Non-DB fallback to any 8-char token if still nothing resolved.
        if not ref and candidates:
            ref = candidates[0]
    else:
        ref = candidates[0] if candidates else None
        logger.info("Supabase not configured: skipping Issuer lead match.")

    # Final pass: filename/client extraction only (no DB). Fixes lowercase filenames & pasted Issuer lines.
    if not (ref or "").strip():
        fb = collect_reference_candidates(file_name, client_details_text)
        if fb:
            ref = fb[0]

    logger.info(
        "Dispatch send confirm: file=%r candidates=%s resolved_ref=%s matched_lead=%s client_chars=%s",
        file_name,
        candidates,
        ref,
        matched_lead_id,
        len(client_details_text or ""),
    )

    tx = Transaction.new(
        id=str(uuid.uuid4()),
        telegram_name=user.full_name,
        telegram_handle=user.username,
        filename=file_name,
        client_details=client_details_text,
        recipient_name=recipient_name,
        recipient_email=recipient_email,
        issuer_group=_resolve_issuer_group(
            user.username,
            update.effective_chat.id if update.effective_chat else None,
            bot_config,
        ),
        reference_id=ref,
    )
    if not (tx.reference_id or "").strip():
        z = _display_reference_for_tx(tx)
        if z:
            tx.reference_id = z

    # Download the file bytes from Telegram so we can attach it to the email.
    bot = context.bot
    telegram_file = await bot.get_file(pending_doc["file_id"])
    file_bytes = await telegram_file.download_as_bytearray()

    email_provider = create_email_provider(
        provider_name=bot_config.email_provider,
        from_address=bot_config.email_from_address,
        to_address=bot_config.email_to_address,
        smtp_host=bot_config.email_smtp_host,
        smtp_port=bot_config.email_smtp_port,
        smtp_username=bot_config.email_smtp_username,
        smtp_password=bot_config.email_smtp_password,
        sendgrid_api_key=bot_config.sendgrid_api_key,
    )

    logger.info(
        "Processing transaction %s for user %s (@%s) with file %s to recipient %s",
        tx.id,
        tx.telegram_name,
        tx.telegram_handle,
        tx.filename,
        recipient_name,
    )

    email_sent = False
    try:
        await email_provider.send_transaction_email(
            tx=tx,
            attachment_bytes=bytes(file_bytes),
            attachment_filename=pending_doc["file_name"],
            recipient_email=recipient_email,
        )
        email_sent = True

        # Notify issuer group that send was successful (if configured)
        if bot_config.issuer_group_chat_id:
            try:
                gref = (tx.reference_id or "").strip() or _display_reference_for_tx(tx)
                await bot.send_message(
                    chat_id=bot_config.issuer_group_chat_id,
                    text=(
                        f"✅ Send successful\n\n"
                        f"📄 {pending_doc['file_name']}\n"
                        + (f"📋 Reference: {gref}\n" if gref else "")
                        + f"🚘 Driver: {recipient_name}\n"
                        f"👤 Issuer: {user.full_name}"
                    ),
                    parse_mode=None,
                )
            except Exception as group_err:
                logger.warning("Failed to notify issuer group: %s", group_err)

        # Mark as delivered and persist to DB.
        tx.delivery_status = "DELIVERED"
        try:
            save_transaction(tx)
            ref_for_lead = (tx.reference_id or "").strip() or (_display_reference_for_tx(tx) or "").strip()
            if bot_config.supabase_configured() and ref_for_lead:
                lid = matched_lead_id
                if not lid:
                    lid = await asyncio.to_thread(
                        get_lead_id_by_reference,
                        bot_config.supabase_url,
                        bot_config.supabase_service_role_key,
                        ref_for_lead,
                    )
                if lid:
                    ok = await asyncio.to_thread(
                        mark_krab_tag_pdf_sent,
                        bot_config.supabase_url,
                        bot_config.supabase_service_role_key,
                        lid,
                    )
                    if ok:
                        logger.info("Krab Issuer lead %s marked krab_tag_pdf_sent", lid)
        except Exception as db_error:
            logger.error("Email sent successfully but failed to save to database: %s", db_error, exc_info=True)
            # Email was sent, so we still show success but warn about DB issue
            await query.edit_message_text(
                _format_send_complete_message(
                    recipient_name, _display_reference_for_tx(tx)
                )
                + "\n\n"
                "⚠️ Note: There was an issue saving the record to the database, "
                "but your email was delivered successfully.",
                parse_mode=None,
            )
            # Clear context
            context.user_data.pop("pending_document", None)
            context.user_data.pop("client_details", None)
            context.user_data.pop("selected_recipient_id", None)
            context.user_data.pop("selected_recipient_email", None)
            context.user_data.pop("selected_recipient_name", None)
            return ConversationHandler.END

        # Insurance side-flow prompt (does NOT block the tag email; it already sent).
        insurance_prompt = (
            "✅Success ! PDF🏷️ Successfully Sent\n\n"
            "To 📧email🛡️insurance: type the email now\n"
            "(Otherwise ignore this message)"
        )
        await query.edit_message_text(insurance_prompt, parse_mode=None)

        # Store pending insurance context for this chat.
        ref_for_msg = (tx.reference_id or "").strip() or (_display_reference_for_tx(tx) or "").strip()
        context.user_data["insurance_pending"] = {
            "recipient_name": recipient_name,
            "reference_id": ref_for_msg,
        }
        context.user_data["insurance_received"] = False

        # Clear document/session context for next transaction right away.
        context.user_data.pop("pending_document", None)
        context.user_data.pop("client_details", None)
        context.user_data.pop("selected_recipient_id", None)
        context.user_data.pop("selected_recipient_email", None)
        context.user_data.pop("selected_recipient_name", None)

        # Schedule "no insurance detected" follow-up in 10 minutes.
        if context.application.job_queue:
            prev: Job | None = context.user_data.get("insurance_timeout_job")
            try:
                if prev:
                    prev.schedule_removal()
            except Exception:
                pass
            context.user_data["insurance_timeout_job"] = context.application.job_queue.run_once(
                _insurance_timeout_job,
                when=10 * 60,
                chat_id=update.effective_chat.id if update.effective_chat else None,
                user_id=update.effective_user.id if update.effective_user else None,
                name=f"insurance_timeout_{update.effective_chat.id if update.effective_chat else 'chat'}",
            )
        return State.WAITING_FOR_INSURANCE
    except Exception as e:
        logger.error("Failed to send email: %s", e, exc_info=True)
        if email_sent:
            # Email was sent but something else failed
            disp_ref_err = _display_reference_for_tx(tx)
            await query.edit_message_text(
                _format_send_complete_message(recipient_name, disp_ref_err)
                + "\n\n"
                "⚠️ There was an issue recording the transaction, "
                "but your email was delivered successfully.",
                parse_mode=None,
            )
        else:
            # Email sending failed
            tx.delivery_status = "FAILED"
            try:
                save_transaction(tx)
            except Exception as db_error:
                logger.error("Also failed to save failed transaction to DB: %s", db_error)
            await query.edit_message_text(
                "❌ Failed to send email. Please try again or contact support.",
                parse_mode="Markdown"
            )

    # Clear the context for next transaction
    context.user_data.pop("pending_document", None)
    context.user_data.pop("client_details", None)
    context.user_data.pop("selected_recipient_id", None)
    context.user_data.pop("selected_recipient_email", None)
    context.user_data.pop("selected_recipient_name", None)

    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Cancel the bot flow and reset to a clean state.

    Registered both as a ConversationHandler fallback and as a top-level
    CommandHandler so ``/cancel`` works even when no conversation is active.
    """
    user = update.effective_user
    logger.info("User %s canceled the conversation.", user.full_name)
    # Drop any timed-out insurance reminder job before nuking user_data.
    prev_job: Job | None = context.user_data.get("insurance_timeout_job")
    try:
        if prev_job:
            prev_job.schedule_removal()
    except Exception:
        pass
    # Wipe everything so the next message starts a fresh session.
    context.user_data.clear()
    msg = update.effective_message
    if msg is not None:
        await msg.reply_text(
            "❌ Cancelled transaction — restarting new session.\n\n"
            "Use /start to open the menu, /insurance for the insurance flow, "
            "or /transactions to view recent transactions."
        )
    return ConversationHandler.END


async def handle_insurance_only_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Inline button: jump straight to insurance email flow."""
    query = update.callback_query
    await query.answer()
    context.user_data["insurance_only_mode"] = True
    context.user_data["insurance_pending"] = {"recipient_name": None, "reference_id": None}
    context.user_data["insurance_received"] = False
    await query.message.reply_text(
        "To 📧email 🛡️insurance: type the email now\n(Otherwise ignore this message)",
        parse_mode=None,
    )
    return State.WAITING_FOR_INSURANCE


async def handle_insurance_only_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """``/insurance`` command — same flow as the inline 🛡️INSURANCE🛡️ button."""
    context.user_data["insurance_only_mode"] = True
    context.user_data["insurance_pending"] = {"recipient_name": None, "reference_id": None}
    context.user_data["insurance_received"] = False
    msg = update.effective_message
    if msg is not None:
        await msg.reply_text(
            "To 📧email 🛡️insurance: type the email now\n(Otherwise ignore this message)",
            parse_mode=None,
        )
    return State.WAITING_FOR_INSURANCE


def _parse_insurance_credentials(text: str) -> tuple[str | None, str | None]:
    raw = (text or "").strip()
    if not raw:
        return None, None
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    email = lines[0] if lines else ""
    return (email or None), None


async def _insurance_timeout_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """After 10 minutes, if user didn't provide insurance, send completion message with prefix."""
    chat_id = getattr(ctx.job, "chat_id", None)
    if not chat_id:
        return
    ud = ctx.user_data
    if ud.get("insurance_received"):
        return
    pending = ud.get("insurance_pending") or {}
    recipient_name = pending.get("recipient_name") or "priv"
    ref = pending.get("reference_id") or None
    msg = "No insurance detected\n" + _format_send_complete_message(recipient_name, ref)
    try:
        await ctx.bot.send_message(chat_id=chat_id, text=msg, parse_mode=None)
    except Exception:
        pass
    try:
        ud.pop("insurance_pending", None)
        ud.pop("insurance_only_mode", None)
        ud.pop("insurance_timeout_job", None)
    except Exception:
        pass


async def _send_insurance_card_document(
    update: Update,
    *,
    pdf_bytes: bytes,
    policy_number: str,
    card_state: str | None,
) -> None:
    """Attach the insurance card PDF to the Telegram chat."""
    if not update.effective_chat or not pdf_bytes:
        return
    prefix = "nj-tei" if card_state == "NJ" else "ny-fs20"
    policy = (policy_number or "card").strip() or "card"
    fname = f"{prefix}-{policy}.pdf"
    bio = BytesIO(pdf_bytes)
    bio.name = fname
    await update.get_bot().send_document(
        chat_id=update.effective_chat.id,
        document=bio,
        filename=fname,
    )


async def handle_insurance_credentials(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Accept insurance email (1 line) and forward via email."""
    pending = context.user_data.get("insurance_pending")
    if not pending:
        return ConversationHandler.END

    email_to, _ = _parse_insurance_credentials(update.message.text if update.message else "")
    if not email_to:
        await update.message.reply_text(
            "To 📧email 🛡️insurance: type the email now\n(Example)\nTest@email.com",
            parse_mode=None,
        )
        return State.WAITING_FOR_INSURANCE

    application = context.application
    bot_config: BotConfig = application.bot_data["config"]  # type: ignore[assignment]

    ref = pending.get("reference_id")
    from .insurance_flow import build_and_send_insurance_card

    await update.message.reply_text(
        f"⏳ Building insurance card and emailing {email_to}…",
        parse_mode=None,
    )

    result = await build_and_send_insurance_card(
        email_to=email_to,
        reference_id=ref,
        bot_config=bot_config,
    )

    if result.ok:
        context.user_data["insurance_received"] = True
        job: Job | None = context.user_data.get("insurance_timeout_job")
        try:
            if job:
                job.schedule_removal()
        except Exception:
            pass
        if result.pdf_bytes:
            try:
                await _send_insurance_card_document(
                    update,
                    pdf_bytes=result.pdf_bytes,
                    policy_number=result.policy_number or "card",
                    card_state=result.card_state,
                )
            except Exception:
                logger.exception("Failed to send insurance card PDF to chat")
        quote = _get_bot_motivational()
        policy_line = (
            f"📋 Policy: {result.policy_number}\n"
            if result.policy_number
            else ""
        )
        portal_lines = ""
        if result.portal_email and result.portal_password:
            portal_lines = (
                f"\nPortal email: {result.portal_email}\n"
                f"Portal password: {result.portal_password}\n"
            )
        await update.message.reply_text(
            f"✅ Insurance card sent\n\n"
            f"📎 Insurance card PDF attached above\n"
            f"🛡️ Insurance card emailed to: {email_to}\n"
            f"{policy_line}"
            f"📧 Delivered to {email_to}"
            f"{portal_lines}\n"
            "Success! ✅ Insurance 🛡️ Emailed 📧\n\n"
            f"({quote})",
            parse_mode=None,
        )
        recipient_name = pending.get("recipient_name") or "priv"
        await update.message.reply_text(
            _format_send_complete_message(recipient_name, ref),
            parse_mode=None,
        )
        context.user_data.pop("insurance_pending", None)
        context.user_data.pop("insurance_only_mode", None)
        context.user_data.pop("insurance_timeout_job", None)
        return ConversationHandler.END

    logger.error("Insurance card flow failed for %s: %s", email_to, result.error)
    err_text = (result.error or "Unknown error.")[:500]
    await update.message.reply_text(
        f"❌ Could not email insurance card.\n\n{err_text}\n\n"
        "Send a new email to try again.",
        parse_mode=None,
    )
    return State.WAITING_FOR_INSURANCE


def _transactions_access_valid(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if the user has a valid transactions access code."""
    expires_at: datetime | None = context.user_data.get("tx_access_expires_at")
    if not expires_at:
        return False
    return datetime.now(timezone.utc) < expires_at


async def _prompt_for_tx_code(message):
    await message.reply_text(
        "🔐 This transactions view is restricted.\n\n"
        "Please enter the access code. It will be valid for 5 minutes.\n\n"
    )


async def _fetch_transactions_page(
    bot_config: BotConfig, page: int
) -> List[Dict]:
    """Fetch a single page of transactions from the API."""
    limit = 11  # request one extra to know if there is a next page
    offset = page * 10
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{bot_config.api_base_url}/transactions/public",
                params={"limit": limit, "offset": offset},
            )
            if response.status_code == 200:
                return response.json()
    except Exception as e:
        logger.error("Failed to fetch transactions: %s", e)
    return []


def _format_transactions_message(transactions: List[Dict]) -> str:
    lines: List[str] = ["📋 Recent Transactions:\n"]

    for tx in transactions[:10]:
        try:
            ts = datetime.fromisoformat(tx["timestamp_ny"].replace("Z", "+00:00"))
            time_block = _format_dt_ny_pretty(ts)
        except Exception:
            time_block = tx.get("timestamp_ny", "Unknown time")

        status_emoji = "✅" if tx.get("delivery_status") == "DELIVERED" else "⏳"
        ref_line = ""
        rid = (tx.get("reference_id") or "").strip()
        if rid:
            ref_line = f"   📋 Ref: {rid}\n"
        lines.append(
            f"{status_emoji} {tx['filename']}\n"
            f"{ref_line}"
            f"   👤 Issuer: {tx['telegram_name']}\n"
            f"   🚘 Driver: {tx.get('recipient_name') or 'Not recorded'}\n"
            f"   🕐 {time_block}\n"
        )

    return "\n".join(lines)


def _build_tx_pagination_keyboard(has_prev: bool, has_next: bool, page: int):
    buttons: List[List[InlineKeyboardButton]] = []
    row: List[InlineKeyboardButton] = []
    if has_prev:
        row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"tx_page_{page-1}"))
    if has_next:
        row.append(InlineKeyboardButton("Next ➡️", callback_data=f"tx_page_{page+1}"))
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(buttons) if buttons else None


def _resolve_issuer_group(
    user_handle: str | None, chat_id: int | None, bot_config: BotConfig
) -> str | None:
    # Primary rule: classify by username.
    normalized = (user_handle or "").strip().lower().lstrip("@")
    highkage_handles = {
        h.strip().lower().lstrip("@")
        for h in bot_config.highkage_group_handles.split(",")
        if h.strip()
    }
    if normalized and normalized in highkage_handles:
        return "highkage_group"
    return "sensei_group"


async def _send_transactions_page_from_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE, page: int
) -> None:
    """Send a transactions page in response to a normal message (/transactions)."""
    application = context.application
    bot_config: BotConfig = application.bot_data["config"]  # type: ignore[assignment]

    transactions = await _fetch_transactions_page(bot_config, page)
    if not transactions:
        await update.message.reply_text(
            "📋 No transactions yet. Send a document to get started!"
        )
        return

    has_next = len(transactions) > 10
    text = _format_transactions_message(transactions)
    keyboard = _build_tx_pagination_keyboard(page > 0, has_next, page)

    await update.message.reply_text(
        text,
        reply_markup=keyboard,
        parse_mode=None,
    )


async def _send_transactions_page_from_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE, page: int
) -> None:
    """Edit the existing message to show a new transactions page."""
    query = update.callback_query
    application = context.application
    bot_config: BotConfig = application.bot_data["config"]  # type: ignore[assignment]

    transactions = await _fetch_transactions_page(bot_config, page)
    if not transactions:
        await query.edit_message_text(
            "📋 No transactions yet. Send a document to get started!"
        )
        return

    has_next = len(transactions) > 10
    text = _format_transactions_message(transactions)
    keyboard = _build_tx_pagination_keyboard(page > 0, has_next, page)

    await query.edit_message_text(
        text,
        reply_markup=keyboard,
        parse_mode=None,
    )


async def show_transactions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /transactions command: gated by a short‑lived access code, with pagination.
    """
    # Check access
    if not _transactions_access_valid(context):
        # Ask for code and mark that we're waiting
        context.user_data["awaiting_tx_code"] = True
        await _prompt_for_tx_code(update.message)
        return

    # Already authenticated for transactions
    await _send_transactions_page_from_message(update, context, page=0)


async def handle_transactions_button(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Handle inline button callback for viewing transactions (from /start).
    """
    query = update.callback_query
    await query.answer()

    if not _transactions_access_valid(context):
        # Ask for code and mark that we're waiting
        context.user_data["awaiting_tx_code"] = True
        await _prompt_for_tx_code(query.message)
        return

    await _send_transactions_page_from_callback(update, context, page=0)


async def handle_tx_page_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle transactions pagination callbacks."""
    query = update.callback_query
    await query.answer()

    if not _transactions_access_valid(context):
        context.user_data["awaiting_tx_code"] = True
        await query.edit_message_text(
            "🔐 Access expired. Please enter the access code again."
        )
        return

    if not query.data or not query.data.startswith("tx_page_"):
        return

    try:
        page = max(0, int(query.data.replace("tx_page_", "")))
    except ValueError:
        page = 0

    await _send_transactions_page_from_callback(update, context, page=page)


async def handle_tx_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle user entering the transactions access code.
    """
    # Skip if user is in a conversation (sending file/client details)
    if context.user_data.get("pending_document") or context.user_data.get("client_details"):
        # User is in a conversation flow, let the conversation handler process this
        return
    
    if not context.user_data.get("awaiting_tx_code"):
        # Not expecting a code; ignore.
        return

    text = (update.message.text or "").strip()
    if text == "DispatchBackend":
        # Grant access for 5 minutes
        context.user_data["tx_access_expires_at"] = datetime.now(timezone.utc) + timedelta(
            minutes=5
        )
        context.user_data["awaiting_tx_code"] = False
        await update.message.reply_text(
            "✅ Access granted for 5 minutes.\n\nShowing recent transactions..."
        )
        await _send_transactions_page_from_message(update, context, page=0)
    else:
        await update.message.reply_text("❌ Invalid code. Please try again.")


def build_application(config: BotConfig):
    """
    Build the telegram Application with all handlers attached.
    """
    app = (
        ApplicationBuilder()
        .token(config.telegram_bot_token)
        .concurrent_updates(True)
        .build()
    )

    # Make config accessible to handlers via application.bot_data
    app.bot_data["config"] = config

    # Conversation for document → client details → recipient selection
    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Document.ALL, handle_document),
            CallbackQueryHandler(handle_insurance_only_button, pattern="^insurance_only$"),
            CommandHandler("insurance", handle_insurance_only_command),
        ],
        states={
            State.WAITING_FOR_CLIENT_DETAILS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_client_details),
            ],
            State.WAITING_FOR_RECIPIENT: [
                CallbackQueryHandler(handle_recipient_selection, pattern="^recipient_"),
            ],
            State.WAITING_FOR_CONFIRMATION: [
                CallbackQueryHandler(handle_confirmation, pattern="^confirm_(yes|no)$"),
            ],
            State.WAITING_FOR_INSURANCE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_insurance_credentials),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("transactions", show_transactions))
    app.add_handler(CallbackQueryHandler(handle_transactions_button, pattern="^view_transactions$"))
    app.add_handler(CallbackQueryHandler(handle_tx_page_callback, pattern=r"^tx_page_\d+$"))
    # Conversation handler should come before generic text handlers
    app.add_handler(conv_handler)
    # Global /cancel (works outside conversation too — clears all state).
    app.add_handler(CommandHandler("cancel", cancel))
    # Handler for access code input (comes after conversation handler to avoid conflicts)
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_tx_code,
        )
    )

    return app


def _wait_for_exclusive_polling(bot_token: str, max_wait: int = 120) -> bool:
    """
    Wait until no other process is polling this bot token.
    Render starts the new worker before killing the old one, so on redeploy
    the new process sees `Conflict: terminated by other getUpdates`. This
    loop repeatedly deletes any webhook and probes `getUpdates` until the
    slot is free (HTTP 200) or times out.

    Returns True when the slot is free, False if timed out.
    """
    token = (bot_token or "").strip()
    if not token:
        return False
    api = f"https://api.telegram.org/bot{token}"

    # Kill any webhook so nothing else is consuming updates.
    try:
        httpx.post(
            f"{api}/deleteWebhook",
            json={"drop_pending_updates": True},
            timeout=5.0,
        )
    except Exception:
        pass

    waited = 0
    backoff = 3
    attempt = 0
    while waited < max_wait:
        attempt += 1
        if attempt % 5 == 0:
            # Re-clear webhook in case something else set one during the wait.
            try:
                httpx.post(
                    f"{api}/deleteWebhook",
                    json={"drop_pending_updates": True},
                    timeout=5.0,
                )
            except Exception:
                pass
        try:
            r = httpx.post(
                f"{api}/getUpdates",
                json={"timeout": 1, "limit": 1},
                timeout=10.0,
            )
            if r.status_code == 200:
                logger.info(
                    "Polling slot is free (probe OK). Starting Krab Dispatch bot."
                )
                return True
            if r.status_code == 409:
                logger.info(
                    "Another instance still polling (409). Retrying in %ds "
                    "(%d/%ds elapsed)…",
                    backoff,
                    waited,
                    max_wait,
                )
                time.sleep(backoff)
                waited += backoff
                backoff = min(backoff + 2, 10)
                continue
            logger.warning(
                "getUpdates probe returned HTTP %s, retrying…", r.status_code
            )
            time.sleep(3)
            waited += 3
        except Exception as e:
            logger.warning("getUpdates probe failed: %s, retrying…", e)
            time.sleep(3)
            waited += 3

    logger.error(
        "Timed out waiting for exclusive polling slot after %ds.", max_wait
    )
    return False


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Hard-exit on Telegram polling conflicts so the process manager restarts us cleanly."""
    err = context.error
    if isinstance(err, Conflict):
        logger.error(
            "TELEGRAM CONFLICT: another process is polling this bot token. "
            "Hard-exiting so the process manager restarts a clean instance."
        )
        try:
            sys.stdout.flush()
        except Exception:
            pass
        # os._exit avoids sleeping in an asyncio cleanup while another poller holds the token.
        os._exit(1)
    logger.error("Unhandled error in update: %s", err, exc_info=err)


async def async_main() -> None:
    """
    Async entry point for the bot.
    """
    # Ensure DB is ready before starting the bot.
    init_db()

    config = BotConfig.from_env()

    # Wait for any previous instance (local or Render) to release the token.
    # Skip the probe if KRAB_SKIP_POLLING_WAIT=1 (e.g. dev reboots when you
    # know nothing else is running and want the bot up instantly).
    if os.getenv("KRAB_SKIP_POLLING_WAIT", "").strip().lower() not in (
        "1",
        "true",
        "yes",
    ):
        if not _wait_for_exclusive_polling(config.telegram_bot_token, max_wait=120):
            logger.error(
                "Could not acquire exclusive polling slot after 120s. Exiting."
            )
            sys.exit(1)

    app = build_application(config)
    app.add_error_handler(_error_handler)

    logger.info("Starting Krab Dispatch bot…")
    try:
        async with app:
            await app.start()
            await app.updater.start_polling(drop_pending_updates=True)
            # Keep running until stopped - wait indefinitely
            await asyncio.Event().wait()
    except Conflict:
        logger.error(
            "Conflict while polling — another process holds the token. "
            "Exiting so the process manager restarts us."
        )
        sys.exit(1)


def main() -> None:
    """
    Entry point for local development:

        python -m bot.main

    Uses asyncio.run() for Python 3.14+ compatibility.
    """
    try:
        asyncio.run(async_main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Krab Dispatch bot stopped.")
    except Conflict:
        logger.error("Conflict at startup — exiting so the process manager restarts.")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Failed to start bot: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    main()


