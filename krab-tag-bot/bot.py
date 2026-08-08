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
from config import Config
from db import Database
from parsing import parse_details

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

db = Database()

USAGE = (
    "🧾 *NJ Temp-Tag Generator*\n\n"
    "Send the client details as labeled lines, for example:\n\n"
    "`Name: Josue Pavon`\n"
    "`Phone: 3474794095`\n"
    "`VIN: 5N1AL0MM8DC337962`\n"
    "`Year: 2013`\n"
    "`Make: Infiniti`\n"
    "`Model: JX35`\n"
    "`Color: White`\n"
    "`Address: 2815 Dewey Ave`\n"
    "`City: Bronx`  `State: NY`  `Zip: 10465`\n"
    "`Insurance: Progressive`\n"
    "`Policy: 9896095819`\n\n"
    "VIN alone fills year/make/model. I'll reply with the tag PDF and let you "
    "send it to a group."
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


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(USAGE, parse_mode="Markdown")


async def handle_details(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    text = (msg.text or msg.caption or "").strip()

    # If an admin is mid-way through a /settings edit, this text is the value.
    if context.user_data.get("settings_await"):
        await _apply_settings_input(update, context, text)
        return

    payload = parse_details(text)
    if not payload.get("name") and not payload.get("vin"):
        await msg.reply_text(USAGE, parse_mode="Markdown")
        return
    phone = payload.pop("phone", "")
    status = await msg.reply_text("⏳ Generating tag…")
    try:
        # Build ONCE — build_fields allocates a plate/control from the shared
        # sequence and hits the VIN decoder, so a second call would burn a
        # second plate and desync the supervisory line from the printed PDF.
        pdf, fields = await asyncio.to_thread(tagcore.generate_full, dict(payload), db)
    except Exception as e:
        logger.exception("tag generation failed")
        await status.edit_text(f"❌ Could not generate the tag: {e}")
        return
    plate = fields["plate"]

    # Reference # + log to tristatetags.com/backend (who generated which tag).
    reference = ledger.generate_reference_id()
    u = update.effective_user
    issuer_handle = (u.username or "").strip()
    issuer_name = (u.full_name or issuer_handle or str(u.id)).strip()
    client_name = f"{fields.get('first', '')} {fields.get('last', '')}".strip()
    asyncio.create_task(asyncio.to_thread(
        ledger.log_tag, reference_id=reference, client_name=client_name,
        issuer_name=issuer_name, issuer_handle=issuer_handle, client_phone=phone,
    ))

    # Stash for an optional group send.
    context.user_data["last_tag"] = {
        "pdf": pdf, "plate": plate, "fields": fields, "phone": phone, "reference": reference,
    }
    groups = await asyncio.to_thread(db.list_active_groups)
    kb = None
    if groups:
        rows = [[InlineKeyboardButton(f"📤 Send to {g.get('group_name') or 'group'}",
                                      callback_data=f"send_{g.get('id')}")] for g in groups[:20]]
        kb = InlineKeyboardMarkup(rows)
    await status.delete()
    await msg.reply_document(
        document=InputFile(io.BytesIO(pdf), filename=f"tag_{re.sub(r'[^A-Za-z0-9]','',plate) or 'tag'}.pdf"),
        caption=f"🧾 NJ 30-Day Temp Tag — {plate}\nReference: {reference}",
        reply_markup=kb,
    )


async def handle_send_to_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    gid = query.data.replace("send_", "", 1)
    last = context.user_data.get("last_tag")
    if not last:
        await query.edit_message_caption(caption="⚠️ This tag expired — generate it again.")
        return
    groups = await asyncio.to_thread(db.list_active_groups)
    grp = next((g for g in groups if str(g.get("id")) == gid), None)
    if not grp:
        await query.answer("Group not found", show_alert=True)
        return
    chat_id = grp.get("group_telegram_id")
    try:
        chat_id = int(str(chat_id).lstrip("=").strip())
    except (TypeError, ValueError):
        pass
    ref = last.get("reference", "")
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=supervisory_text(last["fields"], last.get("phone", ""), ref),
        )
        await context.bot.send_document(
            chat_id=chat_id,
            document=InputFile(io.BytesIO(last["pdf"]), filename=f"tag_{last['plate']}.pdf"),
            caption=f"🧾 NJ 30-Day Temp Tag — {last['plate']}" + (f"\nReference: {ref}" if ref else ""),
        )
        await query.edit_message_caption(
            caption=f"🧾 NJ 30-Day Temp Tag — {last['plate']}" + (f"\nReference: {ref}" if ref else "")
            + f"\n✅ Sent to {grp.get('group_name')}"
        )
    except Exception as e:
        logger.warning("send-to-group failed: %s", e)
        await query.answer(f"Send failed: {e}", show_alert=True)


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
    app.add_handler(CommandHandler("settings", cmd_settings, filters=private))
    app.add_handler(CallbackQueryHandler(handle_settings_callback, pattern=r"^set_"))
    app.add_handler(CallbackQueryHandler(handle_send_to_group, pattern=r"^send_"))
    app.add_handler(
        MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND & private, handle_details)
    )

    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    logger.info("krab-tag-bot polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
