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
from config import Config
from db import Database

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

# Label → canonical field. Longer aliases first so "insurance company" wins
# over "insurance", "policy number" over "policy", "body style" over "body".
_LABEL_ALIASES = {
    "customer name": "name", "customer": "name", "name": "name", "client": "name",
    "phone": "phone", "phone number": "phone", "tel": "phone", "mobile": "phone",
    "vin": "vin", "vin number": "vin",
    "year": "year", "yr": "year",
    "make": "make", "model": "model",
    "vehicle": "vehicle", "car": "vehicle", "vehicle info": "vehicle",
    "color": "color", "colour": "color",
    "body style": "body", "bodystyle": "body", "body type": "body", "body": "body",
    "registration address": "address", "reg address": "address",
    "mailing address": "address", "street address": "address",
    "street": "address", "address": "address",
    "city": "city",
    "state": "state", "st": "state",
    "zip": "zip", "zipcode": "zip", "zip code": "zip", "postal": "zip", "postal code": "zip",
    "city state zip": "city_state_zip", "city/state/zip": "city_state_zip",
    "insurance company": "insurance_company", "insurance carrier": "insurance_company",
    "carrier": "insurance_company", "insurer": "insurance_company", "insurance": "insurance_company",
    "policy number": "policy", "policy no": "policy", "policy #": "policy",
    "binder number": "policy", "binder": "policy", "policy": "policy",
    "issue date": "issued", "issued": "issued", "date issued": "issued",
    "expiration date": "expires", "expiration": "expires", "expires": "expires", "exp": "expires",
}
_LABELS_SORTED = sorted(_LABEL_ALIASES, key=len, reverse=True)
# A label is a known alias followed by ":" / "-" / "#" separators. The lookbehind
# stops it matching inside a word; matching anywhere (not just line start) lets a
# single line carry several fields, e.g. "City: Bronx  State: NY  Zip: 10465".
_LABEL_RE = re.compile(
    r"(?i)(?<![A-Za-z])(" + "|".join(re.escape(l) for l in _LABELS_SORTED) + r")\s*[:#\-]+\s*"
)


def parse_labeled(text: str) -> dict:
    """Tokenize free-form labeled text into canonical fields.

    Robust like krableadsV2's ingest parser: aliases, multiple fields per line,
    and a combined "Vehicle: 2013 Infiniti JX35" line split into year/make/model.
    First occurrence of a field wins.
    """
    s = text or ""
    matches = list(_LABEL_RE.finditer(s))
    out: dict = {}
    for i, m in enumerate(matches):
        key = _LABEL_ALIASES[m.group(1).lower()]
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(s)
        val = s[start:end].strip().strip(",").strip()
        if val and key not in out:
            out[key] = val

    # Combined "Vehicle: 2013 Infiniti JX35" → year/make/model (build_fields also
    # VIN-decodes any that are still blank).
    if out.get("vehicle") and not (out.get("year") and out.get("make") and out.get("model")):
        try:
            y, mk, md = tagcore.tag_pdf.parse_car_line(out["vehicle"])
            out.setdefault("year", y)
            out.setdefault("make", mk)
            out.setdefault("model", md)
        except Exception:
            pass
    out.pop("vehicle", None)
    return out


def supervisory_text(fields: dict, phone: str = "") -> str:
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
    payload = parse_labeled(text)
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
    # Stash for an optional group send.
    context.user_data["last_tag"] = {"pdf": pdf, "plate": plate, "fields": fields, "phone": phone}
    groups = await asyncio.to_thread(db.list_active_groups)
    kb = None
    if groups:
        rows = [[InlineKeyboardButton(f"📤 Send to {g.get('group_name') or 'group'}",
                                      callback_data=f"send_{g.get('id')}")] for g in groups[:20]]
        kb = InlineKeyboardMarkup(rows)
    await status.delete()
    await msg.reply_document(
        document=InputFile(io.BytesIO(pdf), filename=f"tag_{re.sub(r'[^A-Za-z0-9]','',plate) or 'tag'}.pdf"),
        caption=f"🧾 NJ 30-Day Temp Tag — {plate}",
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
    try:
        await context.bot.send_message(chat_id=chat_id, text=supervisory_text(last["fields"], last.get("phone", "")))
        await context.bot.send_document(
            chat_id=chat_id,
            document=InputFile(io.BytesIO(last["pdf"]), filename=f"tag_{last['plate']}.pdf"),
            caption=f"🧾 NJ 30-Day Temp Tag — {last['plate']}",
        )
        await query.edit_message_caption(
            caption=f"🧾 NJ 30-Day Temp Tag — {last['plate']}\n✅ Sent to {grp.get('group_name')}"
        )
    except Exception as e:
        logger.warning("send-to-group failed: %s", e)
        await query.answer(f"Send failed: {e}", show_alert=True)


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
