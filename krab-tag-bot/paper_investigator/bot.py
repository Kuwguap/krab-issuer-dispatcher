"""Paper Investigator — Telegram bot for tracking paper distribution to drivers."""
import html
import io
import logging
import os
import sys
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Conflict
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)
from config import Config
from utils.database import PaperDB
from utils import ai_receipt

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

db = PaperDB()

# Conversation states
ST_SET_ADDR_PICK = 1
ST_SET_ADDR_INPUT = 2
ST_ADD_PAPER_PICK = 3
ST_ADD_PAPER_QTY = 4
ST_RECEIPT_UPLOAD = 5
ST_APPROVE_QTY = 6

# ── Helpers ──────────────────────────────────────────────────────────────

def _is_supervisor(user_id: int) -> bool:
    sup = (Config.SUPERVISOR_TELEGRAM_ID or "").strip()
    return sup and str(user_id) == sup

def _is_paper_girl(user_id: int) -> bool:
    pg = (Config.PAPER_GIRL_TELEGRAM_ID or "").strip()
    return pg and str(user_id) == pg

def _parse_cid(val) -> int | None:
    if val is None:
        return None
    try:
        return int(str(val).strip())
    except (ValueError, TypeError):
        return None

async def _create_resupply_order_and_notify_paper_girl(
    context: ContextTypes.DEFAULT_TYPE,
    driver_id: str,
    driver_name: str,
    qty: int,
) -> tuple[bool, str]:
    """Create an approved `paper_delivery_orders` row and DM Paper Girl.

    Driver inventory is **not** updated here — `add_paper` runs when Paper Girl uploads
    a receipt (same as low-paper approve flow).
    """
    order = db.create_delivery_order(driver_id, qty)
    if not order:
        return False, "Could not create delivery order."
    if not db.approve_delivery_order(order["id"]):
        return False, "Could not approve delivery order."
    addr = db.get_driver_address(driver_id)
    addr_str = _addr_oneliner(addr)
    pg_cid = _parse_cid(Config.PAPER_GIRL_TELEGRAM_ID)
    if pg_cid:
        try:
            await context.bot.send_message(
                chat_id=pg_cid,
                text=(
                    f"📦 **Paper Delivery Request**\n\n"
                    f"👤 Driver: **{driver_name}**\n"
                    f"📄 Quantity: **{qty}** papers\n"
                    f"📍 Address: {addr_str}\n\n"
                    "Upload a receipt photo after delivery."
                ),
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.warning("Could not notify paper girl: %s", e)
    return True, ""


def _addr_oneliner(addr: dict | None) -> str:
    if not addr:
        return "⚠️ No address set"
    parts = [addr.get("address_line", "")]
    if addr.get("city"):
        parts.append(addr["city"])
    if addr.get("state"):
        parts.append(addr["state"])
    if addr.get("zip_code"):
        parts.append(addr["zip_code"])
    return ", ".join(p for p in parts if p)


# ── /start — main menu ──────────────────────────────────────────────────

def _main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 All Drivers & Paper", callback_data="menu_drivers")],
        [InlineKeyboardButton("📍 Set Driver Address", callback_data="menu_set_addr")],
        [InlineKeyboardButton("📄 Add Paper to Driver", callback_data="menu_add_paper")],
        [InlineKeyboardButton("📊 Usage Stats", callback_data="menu_stats")],
        [InlineKeyboardButton("📜 Recent History", callback_data="menu_history")],
    ])


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if not _is_supervisor(user_id) and not _is_paper_girl(user_id):
        await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return ConversationHandler.END

    if _is_paper_girl(user_id) and not _is_supervisor(user_id):
        orders = db.get_pending_delivery_orders()
        if orders:
            lines = ["📦 **Pending deliveries:**\n"]
            for o in orders:
                d = o.get("driver") or {}
                dname = d.get("driver_name", "?")
                qty = o.get("quantity", 0)
                lines.append(f"• {dname} — {qty} papers")
            lines.append("\nUpload receipt photos when deliveries are done.")
            await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        else:
            await update.message.reply_text("✅ No pending deliveries right now.")
        return ConversationHandler.END

    await update.message.reply_text(
        "📄 **Paper Investigator**\n\nManage paper distribution to drivers.",
        parse_mode="Markdown",
        reply_markup=_main_menu_keyboard(),
    )
    return ConversationHandler.END


# ── All Drivers & Paper ─────────────────────────────────────────────────

async def handle_menu_drivers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    inventory = db.get_all_inventory()
    if not inventory:
        await query.message.reply_text("No active drivers found.")
        return

    lines = ["📋 <b>Driver Paper Inventory</b>\n"]
    buttons = []
    for d in inventory:
        name = d["driver_name"]
        count = int(d.get("current_count") or 0)
        safe_name = html.escape(name)
        addr = d.get("address")
        addr_str = "✅" if addr else "⚠️"
        emoji = "🟢" if count >= Config.LOW_PAPER_THRESHOLD else "🔴"
        lines.append(f"{emoji} <b>{safe_name}</b> — <b>{count}</b> papers {addr_str}")
        btn_add = f"➕ Add ({count})"
        if len(btn_add) > 64:
            btn_add = f"➕ + papers ({count})"[:64]
        buttons.append([
            InlineKeyboardButton(btn_add, callback_data=f"qadd_{d['driver_id']}"),
            InlineKeyboardButton("📜 History", callback_data=f"qhist_{d['driver_id']}"),
        ])
    buttons.append([InlineKeyboardButton("⬅️ Main Menu", callback_data="menu_main")])
    await query.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ── Quick add paper from driver list ─────────────────────────────────────

async def handle_quick_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    driver_id = query.data.replace("qadd_", "")
    driver = db.get_driver_by_id(driver_id)
    if not driver:
        await query.message.reply_text("❌ Driver not found.")
        return ConversationHandler.END
    addr = db.get_driver_address(driver_id)
    if not addr:
        await query.message.reply_text(
            f"⚠️ **{driver['driver_name']}** has no address set.\n"
            "Set their address first before adding paper.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END
    context.user_data["add_paper_driver_id"] = driver_id
    context.user_data["add_paper_driver_name"] = driver["driver_name"]
    current = db.get_paper_count(driver_id)
    await query.message.reply_text(
        f"📄 How many papers to add to **{driver['driver_name']}**?\n"
        f"Current count: {current}\n\nSend a number:",
        parse_mode="Markdown",
    )
    return ST_ADD_PAPER_QTY


# ── Quick history from driver list ───────────────────────────────────────

async def handle_quick_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    driver_id = query.data.replace("qhist_", "")
    driver = db.get_driver_by_id(driver_id)
    if not driver:
        await query.message.reply_text("❌ Driver not found.")
        return
    history = db.get_driver_history(driver_id, 20)
    if not history:
        await query.message.reply_text(f"No history for {driver['driver_name']}.")
        return
    lines = [f"📜 **{driver['driver_name']} — Recent History**\n"]
    for t in history:
        sign = "+" if t["amount"] > 0 else ""
        ref = f" (ref: {t['reference_id']})" if t.get("reference_id") else ""
        note = f" — {t['note']}" if t.get("note") else ""
        lines.append(f"• {sign}{t['amount']} → bal {t['balance_after']}{ref}{note}")
    await query.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── Set Driver Address ───────────────────────────────────────────────────

async def handle_menu_set_addr(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    drivers = db.get_all_drivers()
    active = [d for d in drivers if d.get("is_active", True)]
    if not active:
        await query.message.reply_text("No active drivers.")
        return ConversationHandler.END
    buttons = []
    for d in active:
        addr = db.get_driver_address(d["id"])
        label = f"{'✅' if addr else '⚠️'} {d['driver_name']}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"setaddr_{d['id']}")])
    buttons.append([InlineKeyboardButton("⬅️ Cancel", callback_data="menu_main")])
    await query.message.reply_text(
        "📍 Pick a driver to set their address:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return ST_SET_ADDR_PICK


async def handle_set_addr_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "menu_main":
        await query.message.reply_text("Cancelled.", reply_markup=_main_menu_keyboard())
        return ConversationHandler.END
    driver_id = query.data.replace("setaddr_", "")
    driver = db.get_driver_by_id(driver_id)
    if not driver:
        await query.message.reply_text("❌ Driver not found.")
        return ConversationHandler.END
    context.user_data["addr_driver_id"] = driver_id
    context.user_data["addr_driver_name"] = driver["driver_name"]
    current = db.get_driver_address(driver_id)
    msg = f"📍 Enter address for **{driver['driver_name']}**"
    if current:
        msg += f"\n\nCurrent: {_addr_oneliner(current)}"
    msg += "\n\nFormat: `123 Main St, City, ST 07022`"
    await query.message.reply_text(msg, parse_mode="Markdown")
    return ST_SET_ADDR_INPUT


async def handle_set_addr_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    driver_id = context.user_data.get("addr_driver_id")
    driver_name = context.user_data.get("addr_driver_name", "Driver")
    parts = [p.strip() for p in text.split(",")]
    address_line = parts[0] if len(parts) > 0 else text
    city = parts[1] if len(parts) > 1 else ""
    state_zip = parts[2] if len(parts) > 2 else ""
    state, zip_code = "", ""
    if state_zip:
        sz = state_zip.split()
        state = sz[0] if len(sz) > 0 else ""
        zip_code = sz[1] if len(sz) > 1 else ""
    ok = db.set_driver_address(driver_id, address_line, city, state, zip_code)
    if ok:
        await update.message.reply_text(
            f"✅ Address set for **{driver_name}**:\n{text}",
            parse_mode="Markdown",
            reply_markup=_main_menu_keyboard(),
        )
    else:
        await update.message.reply_text("❌ Error saving address. Try again.")
    return ConversationHandler.END


# ── Add Paper ────────────────────────────────────────────────────────────

async def handle_menu_add_paper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    drivers = db.get_all_drivers()
    active = [d for d in drivers if d.get("is_active", True)]
    if not active:
        await query.message.reply_text("No active drivers.")
        return ConversationHandler.END
    buttons = []
    for d in active:
        addr = db.get_driver_address(d["id"])
        count = db.get_paper_count(d["id"])
        label = f"{d['driver_name']} ({count} papers)"
        if not addr:
            label += " ⚠️ NO ADDR"
        buttons.append([InlineKeyboardButton(label, callback_data=f"addp_{d['id']}")])
    buttons.append([InlineKeyboardButton("⬅️ Cancel", callback_data="menu_main")])
    await query.message.reply_text(
        "📄 Pick a driver to add paper to:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return ST_ADD_PAPER_PICK


async def handle_add_paper_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "menu_main":
        await query.message.reply_text("Cancelled.", reply_markup=_main_menu_keyboard())
        return ConversationHandler.END
    driver_id = query.data.replace("addp_", "")
    driver = db.get_driver_by_id(driver_id)
    if not driver:
        await query.message.reply_text("❌ Driver not found.")
        return ConversationHandler.END
    addr = db.get_driver_address(driver_id)
    if not addr:
        await query.message.reply_text(
            f"⚠️ **{driver['driver_name']}** has no address set!\n"
            "Set their address first (📍 Set Driver Address).",
            parse_mode="Markdown",
            reply_markup=_main_menu_keyboard(),
        )
        return ConversationHandler.END
    context.user_data["add_paper_driver_id"] = driver_id
    context.user_data["add_paper_driver_name"] = driver["driver_name"]
    current = db.get_paper_count(driver_id)
    await query.message.reply_text(
        f"📄 How many papers to add to **{driver['driver_name']}**?\n"
        f"Current count: {current}\nAddress: {_addr_oneliner(addr)}\n\nSend a number:",
        parse_mode="Markdown",
    )
    return ST_ADD_PAPER_QTY


async def handle_add_paper_qty(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    try:
        qty = int(text)
        if qty <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Enter a positive number.")
        return ST_ADD_PAPER_QTY
    driver_id = context.user_data.get("add_paper_driver_id")
    driver_name = context.user_data.get("add_paper_driver_name", "Driver")
    ok, err = await _create_resupply_order_and_notify_paper_girl(
        context, driver_id, driver_name, qty
    )
    if not ok:
        await update.message.reply_text(f"❌ {err}", reply_markup=_main_menu_keyboard())
        return ConversationHandler.END
    await update.message.reply_text(
        f"✅ Delivery order created for **{qty}** papers to **{driver_name}**.\n\n"
        "Paper Girl was notified. Their balance updates when she uploads the receipt "
        "(same flow as low-paper resupply).",
        parse_mode="Markdown",
        reply_markup=_main_menu_keyboard(),
    )
    return ConversationHandler.END


# ── Usage Stats ──────────────────────────────────────────────────────────

async def handle_menu_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    stats = db.get_usage_stats()
    if not stats:
        await query.message.reply_text("No stats available.")
        return
    lines = [
        "📊 **Paper Usage Stats**\n",
        f"📄 Total papers in field: **{stats.get('total_papers_in_field', 0)}**",
        f"👥 Active drivers: **{stats.get('total_drivers', 0)}**",
        f"🔴 Low paper drivers: **{stats.get('low_paper_drivers', 0)}**",
        f"➕ Recently added: **{stats.get('total_added_recent', 0)}**",
        f"➖ Used (orders): **{stats.get('total_used_recent', 0)}**",
        "",
        "**Per Driver:**",
    ]
    for d in stats.get("drivers", []):
        emoji = "🟢" if d["current_count"] >= Config.LOW_PAPER_THRESHOLD else "🔴"
        lines.append(f"{emoji} {d['driver_name']}: {d['current_count']} papers")
    await query.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Main Menu", callback_data="menu_main")]]),
    )


# ── Recent History ───────────────────────────────────────────────────────

async def handle_menu_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    txns = db.get_recent_transactions(30)
    if not txns:
        await query.message.reply_text("No transactions yet.")
        return
    lines = ["📜 **Recent Paper Transactions**\n"]
    for t in txns:
        dname = (t.get("driver") or {}).get("driver_name", "?")
        sign = "+" if t["amount"] > 0 else ""
        ref = f" ref:{t['reference_id']}" if t.get("reference_id") else ""
        lines.append(f"• {dname}: {sign}{t['amount']} → {t['balance_after']}{ref}")
    await query.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Main Menu", callback_data="menu_main")]]),
    )


# ── Back to main menu ───────────────────────────────────────────────────

async def handle_menu_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text(
        "📄 **Paper Investigator**",
        parse_mode="Markdown",
        reply_markup=_main_menu_keyboard(),
    )


# ── Low paper approve / decline ──────────────────────────────────────────

async def handle_low_paper_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    driver_id = query.data.replace("lpapprove_", "")
    driver = db.get_driver_by_id(driver_id)
    if not driver:
        await query.message.reply_text("❌ Driver not found.")
        return ConversationHandler.END
    context.user_data["approve_driver_id"] = driver_id
    context.user_data["approve_driver_name"] = driver["driver_name"]
    await query.message.reply_text(
        f"📄 How many papers should Paper Girl send to **{driver['driver_name']}**?\n\nSend a number:",
        parse_mode="Markdown",
    )
    return ST_APPROVE_QTY


async def handle_approve_qty(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    try:
        qty = int(text)
        if qty <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Enter a positive number.")
        return ST_APPROVE_QTY
    driver_id = context.user_data.get("approve_driver_id")
    driver_name = context.user_data.get("approve_driver_name", "Driver")
    ok, err = await _create_resupply_order_and_notify_paper_girl(
        context, driver_id, driver_name, qty
    )
    if not ok:
        await update.message.reply_text(f"❌ {err}", reply_markup=_main_menu_keyboard())
        return ConversationHandler.END
    await update.message.reply_text(
        f"✅ Order created — Paper Girl notified to send **{qty}** papers to **{driver_name}**.\n\n"
        "Balance updates when she uploads the delivery receipt.",
        parse_mode="Markdown",
        reply_markup=_main_menu_keyboard(),
    )
    return ConversationHandler.END


async def handle_low_paper_decline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    driver_id = query.data.replace("lpdecline_", "")
    driver = db.get_driver_by_id(driver_id)
    dname = driver["driver_name"] if driver else "Driver"
    try:
        await query.message.edit_text(f"❌ Declined paper resupply for {dname}.")
    except Exception:
        pass


# ── Paper Girl receipt upload ────────────────────────────────────────────

async def handle_paper_girl_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Paper Girl sends a photo — treat it as a delivery receipt."""
    user_id = update.effective_user.id
    if not _is_paper_girl(user_id):
        return ConversationHandler.END

    if not update.message.photo:
        await update.message.reply_text("📸 Please send a photo of the delivery receipt.")
        return ST_RECEIPT_UPLOAD

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    bio = io.BytesIO()
    await file.download_to_memory(out=bio)
    image_bytes = bio.getvalue()

    orders = db.get_pending_delivery_orders()
    if not orders:
        await update.message.reply_text("✅ No pending delivery orders to verify against.")
        return ConversationHandler.END

    expected = []
    for o in orders:
        d = o.get("driver") or {}
        driver_id = o.get("driver_id")
        addr = db.get_driver_address(driver_id)
        if addr:
            expected.append({
                "driver_name": d.get("driver_name", "?"),
                "address": _addr_oneliner(addr),
                "driver_id": driver_id,
                "order_id": o.get("id"),
            })

    await update.message.reply_text("⏳ Analyzing receipt with AI…")

    result = ai_receipt.verify_receipt_against_addresses(image_bytes, expected)

    for f in result.get("found", []):
        oid = f.get("order_id")
        if oid:
            db.mark_order_delivered(oid)
            did = f.get("driver_id")
            if did:
                order_data = next((o for o in orders if o.get("id") == oid), None)
                if order_data:
                    db.add_paper(did, order_data["quantity"], user_id, "Paper delivered")

    lines = [f"📋 **Receipt Verification**\n\n{result.get('summary', '')}"]
    if result.get("found"):
        lines.append("\n✅ **Verified:**")
        for f in result["found"]:
            lines.append(f"  • {f['driver_name']}: {f['address']}")
    if result.get("missing"):
        lines.append("\n❌ **Missing from receipt:**")
        for m in result["missing"]:
            lines.append(f"  • {m['driver_name']}: {m['address']}")
        lines.append("\nUpload their receipt or I'll remind you later.")

    sup_cid = _parse_cid(Config.SUPERVISOR_TELEGRAM_ID)
    if sup_cid:
        try:
            await context.bot.send_message(chat_id=sup_cid, text="\n".join(lines), parse_mode="Markdown")
        except Exception as e:
            logger.warning("Could not notify supervisor of receipt verification: %s", e)

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    return ConversationHandler.END


# ── Cancel ───────────────────────────────────────────────────────────────

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Cancelled.", reply_markup=_main_menu_keyboard())
    return ConversationHandler.END


# ── Background jobs ──────────────────────────────────────────────────────

async def job_auto_track_orders(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Auto-subtract paper when drivers accept leads in Krab Issuer."""
    try:
        unprocessed = db.get_unprocessed_accepted_assignments()
        sup_cid = _parse_cid(Config.SUPERVISOR_TELEGRAM_ID)

        for a in unprocessed:
            driver_id = a.get("driver_id")
            assignment_id = a.get("id")
            lead = a.get("lead") or {}
            ref = lead.get("reference_id", "")
            if not driver_id or not assignment_id:
                continue
            new_balance = db.subtract_paper(driver_id, 1, ref, "Order accepted in Krab Issuer")
            if new_balance < 0:
                continue
            db.mark_assignment_processed(assignment_id, driver_id)

            if new_balance < Config.LOW_PAPER_THRESHOLD:
                if not db.was_low_alert_sent(driver_id):
                    db.mark_low_alert_sent(driver_id)
                    driver = db.get_driver_by_id(driver_id)
                    dname = driver["driver_name"] if driver else "Driver"
                    if sup_cid:
                        try:
                            await context.bot.send_message(
                                chat_id=sup_cid,
                                text=(
                                    f"🔴 **Low Paper Alert**\n\n"
                                    f"**{dname}** has only **{new_balance}** paper(s) left.\n\n"
                                    "Approve sending more paper?"
                                ),
                                parse_mode="Markdown",
                                reply_markup=InlineKeyboardMarkup([[
                                    InlineKeyboardButton("✅ Approve", callback_data=f"lpapprove_{driver_id}"),
                                    InlineKeyboardButton("❌ Decline", callback_data=f"lpdecline_{driver_id}"),
                                ]]),
                            )
                        except Exception as e:
                            logger.warning("Could not send low paper alert: %s", e)
    except Exception as e:
        logger.error("Auto-track orders job failed: %s", e)


async def job_receipt_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remind Paper Girl about undelivered approved orders."""
    try:
        orders = db.get_orders_needing_reminder()
        if not orders:
            return
        pg_cid = _parse_cid(Config.PAPER_GIRL_TELEGRAM_ID)
        if not pg_cid:
            return
        for o in orders:
            d = o.get("driver") or {}
            dname = d.get("driver_name", "?")
            qty = o.get("quantity", 0)
            try:
                await context.bot.send_message(
                    chat_id=pg_cid,
                    text=(
                        f"⏰ **Delivery Reminder**\n\n"
                        f"📄 **{qty}** papers for **{dname}** still pending.\n"
                        "Upload receipt photo when delivered."
                    ),
                    parse_mode="Markdown",
                )
                db.mark_reminder_sent(o["id"])
            except Exception as e:
                logger.warning("Could not send receipt reminder: %s", e)
    except Exception as e:
        logger.error("Receipt reminder job failed: %s", e)


# ── Main ─────────────────────────────────────────────────────────────────

def main():
    import time

    logger.info("Paper Investigator starting…")
    sys.stdout.flush()

    try:
        Config.validate()
    except ValueError as e:
        logger.error("Config error: %s", e)
        return

    application = Application.builder().token(Config.TELEGRAM_BOT_TOKEN).build()

    # Conversation handler for supervisor flows
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", cmd_start),
            CallbackQueryHandler(handle_menu_set_addr, pattern="^menu_set_addr$"),
            CallbackQueryHandler(handle_menu_add_paper, pattern="^menu_add_paper$"),
            CallbackQueryHandler(handle_quick_add, pattern="^qadd_"),
            CallbackQueryHandler(handle_low_paper_approve, pattern="^lpapprove_"),
        ],
        states={
            ST_SET_ADDR_PICK: [
                CallbackQueryHandler(handle_set_addr_pick, pattern="^(setaddr_|menu_main)"),
            ],
            ST_SET_ADDR_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_set_addr_input),
            ],
            ST_ADD_PAPER_PICK: [
                CallbackQueryHandler(handle_add_paper_pick, pattern="^(addp_|menu_main)"),
            ],
            ST_ADD_PAPER_QTY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_add_paper_qty),
            ],
            ST_APPROVE_QTY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_approve_qty),
            ],
            ST_RECEIPT_UPLOAD: [
                MessageHandler(filters.PHOTO, handle_paper_girl_receipt),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )

    application.add_handler(conv)

    # Standalone callbacks (outside conversation)
    application.add_handler(CallbackQueryHandler(handle_menu_drivers, pattern="^menu_drivers$"))
    application.add_handler(CallbackQueryHandler(handle_menu_stats, pattern="^menu_stats$"))
    application.add_handler(CallbackQueryHandler(handle_menu_history, pattern="^menu_history$"))
    application.add_handler(CallbackQueryHandler(handle_menu_main, pattern="^menu_main$"))
    application.add_handler(CallbackQueryHandler(handle_quick_history, pattern="^qhist_"))
    application.add_handler(CallbackQueryHandler(handle_low_paper_decline, pattern="^lpdecline_"))

    # Paper Girl photo uploads (outside conversation)
    application.add_handler(MessageHandler(
        filters.PHOTO & filters.User(user_id=_parse_cid(Config.PAPER_GIRL_TELEGRAM_ID)) if Config.PAPER_GIRL_TELEGRAM_ID else filters.PHOTO,
        handle_paper_girl_receipt,
    ))

    # Background jobs
    if application.job_queue:
        application.job_queue.run_repeating(job_auto_track_orders, interval=120, first=30)
        logger.info("Auto-track orders job: every 2 min")
        application.job_queue.run_repeating(job_receipt_reminders, interval=Config.RECEIPT_REMINDER_HOURS * 3600, first=300)
        logger.info("Receipt reminders job: every %d hours", Config.RECEIPT_REMINDER_HOURS)

    async def error_handler(update, context):
        if isinstance(context.error, Conflict):
            logger.error("Conflict — hard-exiting for restart.")
            os._exit(1)
        logger.error("Error: %s", context.error, exc_info=context.error)

    application.add_error_handler(error_handler)

    logger.info("Paper Investigator is live.")
    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    except Conflict:
        logger.error("Conflict at startup — exiting.")
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Stopped by user.")
    except Exception as e:
        logger.error("Fatal: %s", e, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
