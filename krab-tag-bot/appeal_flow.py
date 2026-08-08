"""Delivery appeal flow: ref ID → confirm lead → upload proof → supervisor review."""

from __future__ import annotations

import html
import io
import logging
from typing import Any, Callable

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

logger = logging.getLogger(__name__)

STATE_WAITING_APPEAL_REF = 7
STATE_WAITING_APPEAL_CONFIRM = 8
STATE_WAITING_APPEAL_IMAGE = 9

_APPEAL_IMAGE_FILTER = (
    filters.PHOTO
    | filters.Document.MimeType("image/jpeg")
    | filters.Document.MimeType("image/png")
    | filters.Document.MimeType("image/webp")
)


def register_appeal_handlers(application: Application, deps: dict[str, Any]) -> None:
    """Register /appeal conversation + supervisor review callbacks."""
    db = deps["db"]
    empty_kb = deps["empty_inline_kb"]
    user_is_supervisor: Callable = deps["user_is_global_supervisor"]
    global_supervisory_ids: Callable = deps["global_supervisory_chat_ids"]
    driver_row_for_user: Callable = deps["driver_row_for_telegram_user"]
    driver_accepted_lead: Callable = deps["driver_accepted_this_lead"]
    client_name_from_lead: Callable = deps["client_display_name_from_lead"]
    short_uuid: Callable = deps["short_uuid"]
    long_uuid: Callable = deps["long_uuid"]
    telegram_download_url: Callable = deps["telegram_download_url_from_file_path"]
    clear_lead_ud: Callable = deps["clear_lead_conversation_user_data"]
    restart_bot: Callable = deps["restart_bot_from_top"]
    sanitize_phones: Callable = deps["sanitize_phones_for_send"]

    def _appeal_allowed(user_id: int, lead: dict) -> tuple[bool, str | None]:
        if lead.get("exclude_from_count"):
            return False, "❌ This lead already has an accepted appeal and is excluded from delivery counts."
        if db.get_pending_appeal_for_lead(lead["id"]):
            return False, "❌ An appeal for this lead is already pending supervisor review."
        st = db.get_lead_assignment_status(lead["id"])
        if not st or (st.get("status") or "").lower() != "accepted":
            return False, "❌ This lead has no accepted driver assignment yet."
        if user_is_supervisor(user_id):
            return True, None
        issuer_uid = lead.get("user_id")
        try:
            if issuer_uid is not None and int(issuer_uid) == int(user_id):
                return True, None
        except (TypeError, ValueError):
            pass
        drv = driver_row_for_user(user_id)
        if not drv:
            return (
                False,
                "❌ This Telegram account is not registered as a driver.\n"
                "Only the assigned driver, lead issuer, or a supervisor can file an appeal.",
            )
        if not driver_accepted_lead(drv["id"], lead["id"]):
            return False, "❌ You can only appeal deliveries for leads you accepted."
        return True, None

    def _submitter_display(user) -> tuple[str, str]:
        un = (user.username or "").strip()
        username = f"@{un}" if un else "—"
        name = (user.full_name or un or str(user.id)).strip()
        return username, name

    async def cmd_appeal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user = update.effective_user
        msg = update.effective_message
        if not user or not msg:
            return ConversationHandler.END
        db.set_user_state(user.id, "waiting_appeal_ref", {})
        await msg.reply_text(
            "📋 <b>Delivery appeal</b>\n\n"
            "Enter the <b>Reference ID</b> for the lead you need to appeal.\n"
            "You will confirm the client name and upload image proof.",
            parse_mode="HTML",
        )
        return STATE_WAITING_APPEAL_REF

    async def handle_appeal_ref_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user_id = update.effective_user.id
        msg = update.effective_message
        if not msg or not (getattr(msg, "text", None) or "").strip():
            if msg:
                await msg.reply_text("Please send the reference ID as text, or type /cancel.")
            return STATE_WAITING_APPEAL_REF
        reference_id = msg.text.strip().upper()
        lead = db.get_lead_by_reference_id(reference_id)
        if not lead:
            await msg.reply_text(
                "❌ Reference ID not found. Please check and try again.\n"
                "Or type /cancel to restart."
            )
            return STATE_WAITING_APPEAL_REF
        ok, err = _appeal_allowed(user_id, lead)
        if not ok:
            await msg.reply_text(err or "❌ Cannot appeal this lead.")
            db.clear_user_state(user_id)
            return ConversationHandler.END
        client_name = client_name_from_lead(lead)
        context.user_data["appeal_lead_id"] = lead["id"]
        context.user_data["appeal_reference_id"] = reference_id
        context.user_data["appeal_client_name"] = client_name
        db.set_user_state(user_id, "waiting_appeal_confirm", context.user_data)
        ref_esc = html.escape(reference_id, quote=False)
        name_esc = html.escape(client_name, quote=False)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirm & upload proof", callback_data="confirm_appeal")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel_appeal")],
        ])
        await msg.reply_text(
            f"✅ <b>Lead found</b>\n\n"
            f"Reference: <code>{ref_esc}</code>\n"
            f"Client name: <b>{name_esc}</b>\n\n"
            "Confirm this is the correct lead, then upload image proof.",
            parse_mode="HTML",
            reply_markup=kb,
        )
        return STATE_WAITING_APPEAL_CONFIRM

    async def handle_appeal_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        if not query:
            return ConversationHandler.END
        if query.data == "cancel_appeal":
            await query.answer("Cancelled")
            uid = query.from_user.id
            db.clear_user_state(uid)
            await query.message.reply_text("❌ Appeal cancelled.")
            return ConversationHandler.END
        await query.answer()
        uid = query.from_user.id
        db.set_user_state(uid, "waiting_appeal_image", context.user_data)
        await query.message.reply_text(
            "📸 <b>Upload proof</b>\n\n"
            "Send a photo or image file showing why this delivery should be cancelled.",
            parse_mode="HTML",
        )
        return STATE_WAITING_APPEAL_IMAGE

    async def handle_appeal_image_stray(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        msg = update.effective_message
        if msg:
            await msg.reply_text("Please send a photo or image file (JPG, PNG, or WebP) as proof.")
        return STATE_WAITING_APPEAL_IMAGE

    async def _notify_supervisors_new_appeal(
        context: ContextTypes.DEFAULT_TYPE,
        appeal: dict,
        *,
        submitter_username: str,
        submitter_name: str,
    ) -> None:
        ref = str(appeal.get("reference_id") or "N/A")
        client = str(appeal.get("lead_client_name") or "—")
        short_a = short_uuid(appeal["id"])
        ref_esc = html.escape(ref, quote=False)
        user_esc = html.escape(submitter_username, quote=False)
        name_esc = html.escape(submitter_name, quote=False)
        client_esc = html.escape(client, quote=False)
        body = (
            "🚨 <b>New delivery appeal</b>\n\n"
            f"From: {user_esc} ({name_esc})\n"
            f"Lead client: <b>{client_esc}</b>\n"
            f"Reference: <code>{ref_esc}</code>\n\n"
            "Tap below to review proof and accept or decline."
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("👁 View appeal", callback_data=f"apv_{short_a}")],
        ])
        for chat_id in global_supervisory_ids():
            try:
                await context.bot.send_message(
                    chat_id=int(chat_id),
                    text=body,
                    parse_mode="HTML",
                    reply_markup=kb,
                )
            except Exception as e:
                logger.error("Appeal supervisor notify failed chat=%s: %s", chat_id, e)

    async def handle_appeal_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user = update.effective_user
        if not user:
            return ConversationHandler.END
        user_id = user.id
        lead_id = context.user_data.get("appeal_lead_id")
        reference_id = context.user_data.get("appeal_reference_id")
        client_name = context.user_data.get("appeal_client_name")
        if not lead_id or not reference_id:
            await update.message.reply_text("❌ Appeal session expired. Send /appeal to start over.")
            db.clear_user_state(user_id)
            return ConversationHandler.END

        proof_file_id: str | None = None
        image_bytes: bytes | None = None
        file_name = "appeal.jpg"
        if update.message.photo:
            ph = update.message.photo[-1]
            proof_file_id = ph.file_id
            file = await context.bot.get_file(proof_file_id)
            bio = io.BytesIO()
            await file.download_to_memory(out=bio)
            image_bytes = bio.getvalue()
            file_name = file.file_path.split("/")[-1] if file.file_path else "appeal.jpg"
        elif update.message.document:
            doc = update.message.document
            mime = (doc.mime_type or "").lower()
            if not mime.startswith("image/"):
                await update.message.reply_text("❌ Please send a photo or image file (JPG, PNG, or WebP).")
                return STATE_WAITING_APPEAL_IMAGE
            proof_file_id = doc.file_id
            file = await context.bot.get_file(proof_file_id)
            bio = io.BytesIO()
            await file.download_to_memory(out=bio)
            image_bytes = bio.getvalue()
            file_name = doc.file_name or "appeal.jpg"
        else:
            await update.message.reply_text("❌ Please send a photo or image file.")
            return STATE_WAITING_APPEAL_IMAGE

        stored_url = db.upload_appeal_proof_to_storage(lead_id, reference_id, image_bytes, file_name)
        if not stored_url and proof_file_id:
            fp = (await context.bot.get_file(proof_file_id)).file_path or ""
            stored_url = telegram_download_url(fp)

        username, display_name = _submitter_display(user)
        appeal = db.create_lead_appeal(
            lead_id=lead_id,
            reference_id=reference_id,
            proof_image_url=stored_url,
            proof_file_id=proof_file_id,
            submitted_by_telegram_id=user_id,
            submitted_by_username=username.lstrip("@") if username != "—" else None,
            submitted_by_display_name=display_name,
            lead_client_name=client_name,
        )
        db.clear_user_state(user_id)
        if not appeal:
            await update.message.reply_text("❌ Could not save appeal. Try again or contact support.")
            return ConversationHandler.END

        ref_esc = html.escape(str(reference_id), quote=False)
        await update.message.reply_text(
            f"✅ <b>Appeal submitted</b>\n\n"
            f"Reference <code>{ref_esc}</code> is pending supervisor review.\n"
            "You will be notified when it is accepted or declined.",
            parse_mode="HTML",
        )
        try:
            await _notify_supervisors_new_appeal(
                context,
                appeal,
                submitter_username=username,
                submitter_name=display_name,
            )
        except Exception as e:
            logger.error("Supervisor appeal notification failed: %s", e, exc_info=True)
        return ConversationHandler.END

    async def cancel_from_appeal_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        msg = update.effective_message
        if not msg:
            return ConversationHandler.END
        user_id = update.effective_user.id
        clear_lead_ud(context)
        db.clear_user_state(user_id)
        await msg.reply_text("❌ Cancelled — restarting from the top.")
        await restart_bot(update, context)
        return ConversationHandler.END

    def _parse_appeal_callback(data: str, prefix: str) -> str | None:
        if not data.startswith(prefix):
            return None
        token = data[len(prefix):]
        if len(token) != 22:
            return None
        try:
            return long_uuid(token)
        except Exception:
            return None

    async def handle_appeal_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query:
            return
        if not user_is_supervisor(query.from_user.id):
            await query.answer("Supervisors only.", show_alert=True)
            return
        appeal_id = _parse_appeal_callback(query.data or "", "apv_")
        if not appeal_id:
            await query.answer("Invalid appeal.", show_alert=True)
            return
        appeal = db.get_appeal_by_id(appeal_id)
        if not appeal:
            await query.answer("Appeal not found.", show_alert=True)
            return
        await query.answer()
        ref = str(appeal.get("reference_id") or "N/A")
        client = str(appeal.get("lead_client_name") or "—")
        un = appeal.get("submitted_by_username") or ""
        user_line = f"@{un}" if un else "—"
        name = appeal.get("submitted_by_display_name") or "—"
        status = (appeal.get("status") or "pending").upper()
        short_a = short_uuid(appeal_id)
        ref_esc = html.escape(ref, quote=False)
        client_esc = html.escape(client, quote=False)
        user_esc = html.escape(user_line, quote=False)
        name_esc = html.escape(name, quote=False)
        detail = (
            f"📋 <b>Appeal detail</b> ({status})\n\n"
            f"From: {user_esc} ({name_esc})\n"
            f"Lead client: <b>{client_esc}</b>\n"
            f"Reference: <code>{ref_esc}</code>"
        )
        kb = None
        if (appeal.get("status") or "") == "pending":
            kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Accept appeal", callback_data=f"apa_{short_a}"),
                    InlineKeyboardButton("❌ Decline", callback_data=f"apd_{short_a}"),
                ],
            ])
        proof_url = (appeal.get("proof_image_url") or "").strip()
        proof_fid = (appeal.get("proof_file_id") or "").strip()
        try:
            if proof_fid:
                await context.bot.send_photo(
                    chat_id=query.message.chat_id,
                    photo=proof_fid,
                    caption=detail,
                    parse_mode="HTML",
                    reply_markup=kb,
                )
            elif proof_url:
                await context.bot.send_photo(
                    chat_id=query.message.chat_id,
                    photo=proof_url,
                    caption=detail,
                    parse_mode="HTML",
                    reply_markup=kb,
                )
            else:
                await query.message.reply_text(detail, parse_mode="HTML", reply_markup=kb)
        except Exception:
            await query.message.reply_text(detail, parse_mode="HTML", reply_markup=kb)

    async def _resolve_appeal_decision(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        accept: bool,
    ) -> None:
        query = update.callback_query
        if not query:
            return
        if not user_is_supervisor(query.from_user.id):
            await query.answer("Supervisors only.", show_alert=True)
            return
        prefix = "apa_" if accept else "apd_"
        appeal_id = _parse_appeal_callback(query.data or "", prefix)
        if not appeal_id:
            await query.answer("Invalid appeal.", show_alert=True)
            return
        appeal = db.get_appeal_by_id(appeal_id)
        if not appeal or (appeal.get("status") or "") != "pending":
            await query.answer("Appeal already resolved or not found.", show_alert=True)
            return
        reviewer = query.from_user
        rev_name = (reviewer.full_name or reviewer.username or str(reviewer.id)).strip()
        status = "accepted" if accept else "declined"
        resolved = db.resolve_lead_appeal(
            appeal_id,
            status=status,
            reviewed_by_telegram_id=reviewer.id,
            reviewed_by_display_name=rev_name,
        )
        if not resolved:
            await query.answer("Could not update appeal.", show_alert=True)
            return
        await query.answer(f"Appeal {status}.")
        try:
            await query.edit_message_reply_markup(reply_markup=empty_kb)
        except Exception:
            pass
        ref = str(appeal.get("reference_id") or "N/A")
        ref_esc = html.escape(ref, quote=False)
        verb = "accepted" if accept else "declined"
        note = (
            " This lead is excluded from delivery counts."
            if accept
            else " This lead remains in normal counts."
        )
        try:
            await query.message.reply_text(
                f"✅ Appeal for <code>{ref_esc}</code> has been <b>{verb}</b>.{note}",
                parse_mode="HTML",
            )
        except Exception:
            pass
        submitter_tid = appeal.get("submitted_by_telegram_id")
        if submitter_tid:
            try:
                await context.bot.send_message(
                    chat_id=int(submitter_tid),
                    text=(
                        f"📬 Your appeal for reference <code>{ref_esc}</code> has been "
                        f"<b>{verb}</b>."
                        + (" The lead stays on file but is marked excluded from counts." if accept else "")
                    ),
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.error("Appeal submitter notify failed tid=%s: %s", submitter_tid, e)

    async def handle_appeal_accept(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await _resolve_appeal_decision(update, context, accept=True)

    async def handle_appeal_decline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await _resolve_appeal_decision(update, context, accept=False)

    appeal_handler = ConversationHandler(
        entry_points=[CommandHandler("appeal", cmd_appeal)],
        states={
            STATE_WAITING_APPEAL_REF: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_appeal_ref_input),
            ],
            STATE_WAITING_APPEAL_CONFIRM: [
                CallbackQueryHandler(
                    handle_appeal_confirm_callback,
                    pattern="^(confirm_appeal|cancel_appeal)$",
                ),
            ],
            STATE_WAITING_APPEAL_IMAGE: [
                MessageHandler(_APPEAL_IMAGE_FILTER, handle_appeal_image),
                MessageHandler(
                    (filters.TEXT | filters.Document.ALL) & ~filters.COMMAND,
                    handle_appeal_image_stray,
                ),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_from_appeal_conversation),
            CommandHandler("start", deps["start_handler"]),
            CommandHandler("appeal", cmd_appeal),
        ],
    )

    application.add_handler(appeal_handler)
    application.add_handler(CallbackQueryHandler(handle_appeal_view, pattern=r"^apv_"))
    application.add_handler(CallbackQueryHandler(handle_appeal_accept, pattern=r"^apa_"))
    application.add_handler(CallbackQueryHandler(handle_appeal_decline, pattern=r"^apd_"))
