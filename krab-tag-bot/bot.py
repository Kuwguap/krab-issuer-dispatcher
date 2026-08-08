"""krab-tag-bot — Telegram staff interface + HTTP tag generator (one process).

Staff DM labeled client/vehicle fields; the bot generates the NJ 30-day temp
tag PDF and (optionally) sends the informational supervisory notice + the PDF
to a chosen dispatch group. The FastAPI side (POST /api/tag/generate) backs the
tristatetags.com/tag page. Canonical generator = krableadsV2/utils/tag_pdf.py,
copied into taggen/ at build time.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import secrets
import sys

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import tagcore
import ledger
import aiparse
from config import Config
from db import Database

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

db = Database()

_QUOTES = [
    "Small wins stack into big months. 📆",
    "Post more. Close more. Earn more. ⚡",
    "Daily action = Daily income. 💰",
    "Hustle harder. Get paid faster. 🚀",
    "More ads. More leads. More deals. 🎯",
    "Outwork. Out-earn. 👑",
    "Consistency compounds. 📈",
]


def _quote() -> str:
    return secrets.choice(_QUOTES)


def welcome_text(username: str) -> str:
    return (
        f"Welcome, @{username}! 👋\n\n"
        "📥 Send me everything however you like\n\n"
        "👤 Name\n"
        "🏠 Registration Address\n"
        "📍 Delivery Address\n"
        "🔢 VIN #\n"
        "🚘 Car (Year/Make/Model)\n"
        "🎨 Color\n"
        "🛡 Insurance Company & Policy #\n"
        "🕒 Date & Time\n"
        "📞 Phone Number\n"
        "💲 Price\n"
        "📝 Special request for issuers (optional)\n"
        "📝 Special request for drivers (optional)\n"
        "📧Email (required for insurance)\n"
        "🪪Driver license (required for insurance)\n\n"
        f"{_quote()}\n\n"
        "🏁Automated🏎Automotive"
    )

def supervisory_text(fields: dict, phone: str = "", reference: str = "") -> str:
    name = " ".join(w[:1].upper() + w[1:].lower() for w in (
        f"{fields.get('first','')} {fields.get('last','')}").split())
    csz = " ".join(x for x in (fields.get("city"), fields.get("state"), fields.get("zip")) if x)
    reg = ", ".join(x for x in (fields.get("address"), csz) if x)
    veh = " ".join(x for x in (str(fields.get("year") or ""), fields.get("make"), fields.get("model")) if x)
    if veh and fields.get("color"):
        veh = f"{veh}, {fields['color']}"
    lines = [
        "🛡️ SUPERVISORY MESSAGE",
        "🆕 New Lead",
        f"Reference: {reference}" if reference else None,
        f"Customer: {name}" if name else None,
        f"Phone: {phone}" if phone else None,
        f"Registration address: {reg}" if reg else None,
        f"VIN: {fields.get('vin')}" if fields.get("vin") else None,
        f"Vehicle: {veh}" if veh else None,
        f"Insurance: {fields.get('insurance_company')}" if fields.get("insurance_company") else None,
        f"Policy #: {fields.get('policy')}" if fields.get("policy") else None,
        "Service: 30-Day NJ Temp Tag",
        "Informational copy — not claimable from this message.",
        "Move Fast & Serve Client !",
    ]
    return "\n".join(l for l in lines if l)


def _chat_id(raw):
    try:
        return int(str(raw).lstrip("=").strip())
    except (TypeError, ValueError):
        return raw


def _is_pdf(doc) -> bool:
    mime = (getattr(doc, "mime_type", "") or "").lower()
    name = (getattr(doc, "file_name", "") or "").lower()
    return "pdf" in mime or name.endswith(".pdf")


def _review_text(fields: dict) -> str:
    def g(k):
        return fields.get(k) or "—"
    csz = " ".join(x for x in (fields.get("city"), fields.get("state"), fields.get("zip")) if x) or "—"
    veh = " ".join(x for x in (str(fields.get("year") or ""), fields.get("make"), fields.get("model")) if x) or "—"
    name = f"{fields.get('first', '')} {fields.get('last', '')}".strip() or fields.get("name") or "—"
    return (
        "📝 *Review the tag details*\n\n"
        f"👤 Name: {name}\n"
        f"🔢 VIN: {g('vin')}\n"
        f"🚘 Vehicle: {veh}\n"
        f"🎨 Color: {g('color')}\n"
        f"🏠 Address: {g('address')}\n"
        f"🏙 City/State/Zip: {csz}\n"
        f"🛡 Insurance: {g('insurance_company')}   Policy: {g('policy')}\n"
        f"📧 Email: {g('email')}\n\n"
        "Tap ✅ to generate the tag, or send corrected details to re-parse."
    )


_REVIEW_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("✅ Generate Tag", callback_data="p1_gen")],
    [InlineKeyboardButton("❌ Cancel", callback_data="p1_cancel")],
])
_BATCH_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("✅ Done — read them", callback_data="p1_done")],
    [InlineKeyboardButton("❌ Cancel", callback_data="p1_cancel")],
])


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    for k in ("media", "caption", "parsed", "tag", "sel_group"):
        context.user_data.pop(k, None)
    await update.message.reply_text(welcome_text(update.effective_user.username or "there"))


async def _download_media(context, msg):
    """Photo → jpeg/png bytes; PDF → first-page PNG. Returns (bytes, mime) or None."""
    try:
        if msg.photo:
            f = await context.bot.get_file(msg.photo[-1].file_id)
            bio = io.BytesIO()
            await f.download_to_memory(out=bio)
            mime = "image/png" if (f.file_path or "").lower().endswith(".png") else "image/jpeg"
            return bio.getvalue(), mime
        if msg.document and _is_pdf(msg.document):
            f = await context.bot.get_file(msg.document.file_id)
            bio = io.BytesIO()
            await f.download_to_memory(out=bio)
            av = aiparse._AV
            if av and hasattr(av, "pdf_first_page_to_png_bytes"):
                png = av.pdf_first_page_to_png_bytes(bio.getvalue())
                if png:
                    return png, "image/png"
    except Exception as e:
        logger.warning("download media failed: %s", e)
    return None


async def _parse_and_review(update, context, text: str = "", media=None) -> None:
    status = await update.effective_message.reply_text("🔎 Reading the details…")
    try:
        fields = await asyncio.to_thread(aiparse.extract, text, media)
    except Exception as e:
        logger.exception("parse failed")
        await status.edit_text(f"❌ Could not read the details: {e}")
        return
    if not fields.get("name") and not fields.get("vin"):
        await status.edit_text(
            "❌ I couldn't find a name or VIN. Send the client details as text or a clear photo."
        )
        return
    context.user_data["parsed"] = fields
    context.user_data["phone"] = fields.pop("phone", "") if fields.get("phone") else fields.get("phone", "")
    await status.edit_text(_review_text(context.user_data["parsed"]), parse_mode="Markdown", reply_markup=_REVIEW_KB)


async def handle_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if context.user_data.get("settings_await"):
        await _apply_settings_input(update, context, (msg.text or msg.caption or "").strip())
        return

    # Photos / PDFs accumulate; the user taps Done to run the AI on the batch.
    if msg.photo or (msg.document and _is_pdf(msg.document)):
        part = await _download_media(context, msg)
        if part:
            context.user_data.setdefault("media", []).append(part)
        cap = (msg.caption or "").strip()
        if cap:
            context.user_data["caption"] = (context.user_data.get("caption", "") + "\n" + cap).strip()
        n = len(context.user_data.get("media", []))
        await msg.reply_text(f"📎 Received {n} file(s). Send more, or tap Done.", reply_markup=_BATCH_KB)
        return

    text = (msg.text or "").strip()
    if not text:
        await msg.reply_text(welcome_text(update.effective_user.username or "there"))
        return
    await _parse_and_review(update, context, text=text)


async def on_batch_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    media = context.user_data.pop("media", [])
    caption = context.user_data.pop("caption", "")
    if not media:
        await query.edit_message_text("No files received — send the details again.")
        return
    try:
        await query.edit_message_text("🔎 Reading the files…")
    except Exception:
        pass
    await _parse_and_review(update, context, text=caption, media=media)


async def on_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    for k in ("media", "caption", "parsed", "tag", "sel_group"):
        context.user_data.pop(k, None)
    q = update.callback_query
    if q:
        await q.answer()
        await q.edit_message_text("❌ Cancelled. Send new details anytime.")
    else:
        await update.message.reply_text("❌ Cancelled.")


async def on_generate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parsed = context.user_data.get("parsed")
    if not parsed:
        await query.edit_message_text("⚠️ Expired — send the client details again.")
        return
    try:
        await query.edit_message_text("⏳ Generating tag…")
    except Exception:
        pass
    try:
        pdf, fields = await asyncio.to_thread(tagcore.generate_full, dict(parsed), db)
    except Exception as e:
        logger.exception("tag generation failed")
        await query.edit_message_text(f"❌ Could not generate the tag: {e}")
        return

    reference = ledger.generate_reference_id()
    u = update.effective_user
    issuer_handle = (u.username or "").strip()
    issuer_name = (u.full_name or issuer_handle or str(u.id)).strip()
    client_name = f"{fields.get('first', '')} {fields.get('last', '')}".strip()
    asyncio.create_task(asyncio.to_thread(
        ledger.log_tag, reference_id=reference, client_name=client_name,
        issuer_name=issuer_name, issuer_handle=issuer_handle, client_phone=context.user_data.get("phone", ""),
    ))
    context.user_data["tag"] = {
        "pdf": pdf, "plate": fields["plate"], "fields": fields,
        "reference": reference, "phone": context.user_data.get("phone", ""),
    }

    await query.message.reply_document(
        document=InputFile(io.BytesIO(pdf), filename=f"tag_{re.sub(r'[^A-Za-z0-9]', '', fields['plate']) or 'tag'}.pdf"),
        caption=f"🧾 NJ 30-Day Temp Tag — {fields['plate']}\nReference: {reference}",
    )
    groups = await asyncio.to_thread(db.list_active_groups)
    rows = [[InlineKeyboardButton(f"📤 {g.get('group_name') or 'group'}", callback_data=f"send_{g.get('id')}")]
            for g in groups[:20]]
    rows.append([InlineKeyboardButton("🚫 No group (keep the file)", callback_data="send_none")])
    await query.message.reply_text("👥 Choose a group to send the tag to:", reply_markup=InlineKeyboardMarkup(rows))


async def on_group_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    tag = context.user_data.get("tag")
    if not tag:
        await query.edit_message_text("⚠️ Expired — generate the tag again.")
        return
    if query.data == "send_none":
        await query.edit_message_text("✅ Done. Tag kept (not sent to a group).")
        return
    gid = query.data.replace("send_", "", 1)
    groups = await asyncio.to_thread(db.list_active_groups)
    grp = next((g for g in groups if str(g.get("id")) == gid), None)
    if not grp:
        await query.answer("Group not found", show_alert=True)
        return
    ref = tag.get("reference", "")
    try:
        await context.bot.send_message(
            chat_id=_chat_id(grp.get("group_telegram_id")),
            text=supervisory_text(tag["fields"], tag.get("phone", ""), ref),
        )
        await context.bot.send_document(
            chat_id=_chat_id(grp.get("group_telegram_id")),
            document=InputFile(io.BytesIO(tag["pdf"]), filename=f"tag_{tag['plate']}.pdf"),
            caption=f"🧾 NJ 30-Day Temp Tag — {tag['plate']}" + (f"\nReference: {ref}" if ref else ""),
        )
    except Exception as e:
        logger.warning("send-to-group failed: %s", e)
        await query.answer(f"Send failed: {e}", show_alert=True)
        return
    context.user_data["sel_group"] = grp.get("group_name")
    drivers = await asyncio.to_thread(db.list_drivers)
    rows = [[InlineKeyboardButton(f"🚗 {d.get('driver_name') or 'driver'}", callback_data=f"drv_{d.get('id')}")]
            for d in drivers[:20]]
    rows.append([InlineKeyboardButton("⏭ Skip driver", callback_data="drv_skip")])
    await query.edit_message_text(
        f"✅ Sent to {grp.get('group_name')}.\n\n🚗 Send the tag to a driver too?",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def on_driver_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    tag = context.user_data.get("tag")
    grp_name = context.user_data.get("sel_group", "group")
    if query.data == "drv_skip" or not tag:
        await query.edit_message_text(f"✅ Done — sent to {grp_name}.")
        return
    did = query.data.replace("drv_", "", 1)
    drivers = await asyncio.to_thread(db.list_drivers)
    drv = next((d for d in drivers if str(d.get("id")) == did), None)
    if not drv:
        await query.answer("Driver not found", show_alert=True)
        return
    ref = tag.get("reference", "")
    try:
        await context.bot.send_document(
            chat_id=_chat_id(drv.get("driver_telegram_id")),
            document=InputFile(io.BytesIO(tag["pdf"]), filename=f"tag_{tag['plate']}.pdf"),
            caption=f"🧾 NJ 30-Day Temp Tag — {tag['plate']}" + (f"\nReference: {ref}" if ref else ""),
        )
    except Exception as e:
        await query.answer(f"Couldn't DM the driver (have they started the bot?): {e}", show_alert=True)
        return
    await query.edit_message_text(f"✅ Done — sent to {grp_name} and driver {drv.get('driver_name')}.")


# ── /settings (admin only) ──────────────────────────────────────────────────

def _settings_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔢 Plate Numbers", callback_data="set_plates")],
        [InlineKeyboardButton("👥 Groups", callback_data="set_groups")],
        [InlineKeyboardButton("✖️ Close", callback_data="set_close")],
    ])


def _is_admin_update(update: Update) -> bool:
    u = update.effective_user
    return bool(u and Config.is_admin(u.id))


async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    await update.message.reply_text(
        f"Your Telegram ID: `{u.id}`\n"
        + ("✅ You are an admin." if Config.is_admin(u.id)
           else "Add this exact number to ADMIN_TELEGRAM_IDS (comma-separated) and redeploy to get access."),
        parse_mode="Markdown",
    )


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin_update(update):
        u = update.effective_user
        configured = len(Config.admin_ids())
        hint = (
            "No admins are configured yet — set ADMIN_TELEGRAM_IDS on the bot and redeploy."
            if configured == 0
            else "Ask an admin to add your ID to ADMIN_TELEGRAM_IDS (then redeploy)."
        )
        await update.message.reply_text(
            f"⛔ Settings are restricted to admins.\n\nYour Telegram ID: `{u.id}`\n{hint}",
            parse_mode="Markdown",
        )
        return
    context.user_data.pop("settings_await", None)
    await update.message.reply_text("⚙️ *Settings*", parse_mode="Markdown", reply_markup=_settings_main_kb())


async def _render_plates(query) -> None:
    s = await asyncio.to_thread(db.get_plate_settings) or {}
    pre = s.get("nj_plate_prefix", "H")
    suf = s.get("non_nj_plate_suffix", "V")
    txt = (
        "🔢 *Plate Numbers*\n\n"
        f"Resident (`{pre}######`) next: *{s.get('nj_plate_next_number', '—')}*\n"
        f"Non-Resident (`######{suf}`) next: *{s.get('non_nj_plate_next_number', '—')}*\n"
        f"Resident control next: *{s.get('nj_car_next_number', '—')}*\n"
        f"Non-Resident control next: *{s.get('non_nj_car_next_number', '—')}*\n\n"
        "Tap to set the NEXT value to be issued."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Set Resident plate #", callback_data="set_pf:nj_plate_next_number"),
         InlineKeyboardButton("Set Non-Res plate #", callback_data="set_pf:non_nj_plate_next_number")],
        [InlineKeyboardButton("Set Resident control #", callback_data="set_pf:nj_car_next_number"),
         InlineKeyboardButton("Set Non-Res control #", callback_data="set_pf:non_nj_car_next_number")],
        [InlineKeyboardButton("⬅️ Back", callback_data="set_menu")],
    ])
    await query.edit_message_text(txt, parse_mode="Markdown", reply_markup=kb)


async def _render_groups(query) -> None:
    groups = await asyncio.to_thread(db.list_groups)
    lines = ["👥 *Groups*\n"]
    rows = []
    for g in groups[:25]:
        active = g.get("is_active", True)
        lines.append(f"{'✅' if active else '⛔'} {g.get('group_name') or '(unnamed)'} `{g.get('group_telegram_id')}`")
        rows.append([InlineKeyboardButton(
            f"{'Disable' if active else 'Enable'} {g.get('group_name') or 'group'}"[:40],
            callback_data=f"set_gtog:{g.get('id')}",
        )])
    if not groups:
        lines.append("_No groups yet._")
    rows.append([InlineKeyboardButton("➕ Add Group", callback_data="set_gadd")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="set_menu")])
    await query.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows))


_PLATE_LABELS = {
    "nj_plate_next_number": "Resident plate number",
    "non_nj_plate_next_number": "Non-Resident plate number",
    "nj_car_next_number": "Resident control number",
    "non_nj_car_next_number": "Non-Resident control number",
}


async def handle_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not _is_admin_update(update):
        await query.answer("Admins only", show_alert=True)
        return
    data = query.data
    if data == "set_menu":
        context.user_data.pop("settings_await", None)
        await query.edit_message_text("⚙️ *Settings*", parse_mode="Markdown", reply_markup=_settings_main_kb())
    elif data == "set_plates":
        await _render_plates(query)
    elif data == "set_groups":
        await _render_groups(query)
    elif data == "set_close":
        context.user_data.pop("settings_await", None)
        await query.edit_message_text("⚙️ Settings closed.")
    elif data.startswith("set_pf:"):
        field = data.split(":", 1)[1]
        context.user_data["settings_await"] = {"kind": "plate", "field": field}
        await query.message.reply_text(
            f"Send the new *{_PLATE_LABELS.get(field, field)}* (digits only). It becomes the next value issued.",
            parse_mode="Markdown",
        )
    elif data.startswith("set_gtog:"):
        gid = data.split(":", 1)[1]
        groups = await asyncio.to_thread(db.list_groups)
        g = next((x for x in groups if str(x.get("id")) == gid), None)
        if g:
            await asyncio.to_thread(db.set_group_active, gid, not g.get("is_active", True))
        await _render_groups(query)
    elif data == "set_gadd":
        context.user_data["settings_await"] = {"kind": "add_group"}
        await query.message.reply_text(
            "Send the new group as: *Group Name | -100xxxxxxxxxx*", parse_mode="Markdown"
        )


async def _apply_settings_input(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    if not _is_admin_update(update):
        context.user_data.pop("settings_await", None)
        return
    await_state = context.user_data.pop("settings_await", None) or {}
    msg = update.message
    if await_state.get("kind") == "plate":
        field = await_state["field"]
        digits = re.sub(r"\D", "", text)
        if not digits:
            await msg.reply_text("❌ Please send digits only. Try /settings again.")
            return
        ok = await asyncio.to_thread(db.update_plate_settings, {field: int(digits)})
        await msg.reply_text(
            (f"✅ {_PLATE_LABELS.get(field, field)} set to {int(digits)}." if ok
             else "❌ Could not update (DB unavailable?)."),
            reply_markup=_settings_main_kb(),
        )
    elif await_state.get("kind") == "add_group":
        parts = [p.strip() for p in text.split("|")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            await msg.reply_text("❌ Format: Group Name | -100xxxxxxxxxx. Try again via /settings.")
            return
        ok = await asyncio.to_thread(db.add_group, parts[0], parts[1])
        await msg.reply_text(
            (f"✅ Added group “{parts[0]}”." if ok else "❌ Could not add the group."),
            reply_markup=_settings_main_kb(),
        )


def main() -> None:
    Config.validate()

    try:
        from api.server import start_in_background_thread
        start_in_background_thread(db)
        logger.info("FastAPI started (POST /api/tag/generate + /api/health)")
    except Exception as e:
        if os.getenv("RENDER") or os.getenv("PORT"):
            logger.error("FastAPI required on Render (healthCheckPath /api/health): %s", e, exc_info=True)
            sys.exit(1)
        logger.warning("FastAPI not started: %s", e)

    app = Application.builder().token(Config.TELEGRAM_BOT_TOKEN).build()
    # PRIVATE-ONLY input: the staff flow happens in DMs. The bot must never
    # reply to messages/commands in group chats (it only POSTS a tag to a group
    # when a staffer taps "Send to <group>"), so it can't spam the usage text.
    private = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler(["start", "help", "newtag"], cmd_start, filters=private))
    app.add_handler(CommandHandler("myid", cmd_myid, filters=private))
    app.add_handler(CommandHandler("cancel", on_cancel, filters=private))
    app.add_handler(CommandHandler("settings", cmd_settings, filters=private))
    app.add_handler(CallbackQueryHandler(handle_settings_callback, pattern=r"^set_"))
    app.add_handler(CallbackQueryHandler(on_batch_done, pattern=r"^p1_done$"))
    app.add_handler(CallbackQueryHandler(on_generate, pattern=r"^p1_gen$"))
    app.add_handler(CallbackQueryHandler(on_cancel, pattern=r"^p1_cancel$"))
    app.add_handler(CallbackQueryHandler(on_group_select, pattern=r"^send_"))
    app.add_handler(CallbackQueryHandler(on_driver_select, pattern=r"^drv_"))
    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.CAPTION | filters.PHOTO | filters.Document.ALL)
            & ~filters.COMMAND & private,
            handle_input,
        )
    )

    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    logger.info("krab-tag-bot polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
