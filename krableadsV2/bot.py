"""Main Telegram bot application."""
import base64
import difflib
import io
import json
import logging
import os
import re
import hashlib
import hmac
import html
import sys
import requests
import secrets
import string
import uuid as _uuid_mod
import asyncio
import time
from datetime import datetime, time as dt_time, timedelta
import pytz
from typing import Optional
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, MessageEntity
from telegram.error import BadRequest, Conflict, RetryAfter
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    BaseUpdateProcessor,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes
)
from config import Config
from utils.database import Database, record_is_active
from utils.onetimesecret import OneTimeSecret
from utils.monday import MondayClient
from utils import ai_vision
from utils import motivation
from utils import driver_motivation
from utils import phone_redact
from utils import vin_lookup
from utils.api_lead_user import resolve_api_lead_user_sync

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Suppress duplicate error logging from telegram library for Conflict errors
logging.getLogger('telegram.ext.Updater').setLevel(logging.WARNING)
logging.getLogger('telegram.ext.Application').setLevel(logging.WARNING)

# Conversation states
STATE_PHASE1 = 1  # Waiting for vehicle and delivery details
STATE_PHASE2 = 2  # Waiting for phone number and price
STATE_SELECT_GROUP = 8   # Waiting for user to select which group (when assistants_choose_group is on)
STATE_SELECT_DRIVER = 3  # Waiting for user to select which driver(s) to notify
STATE_SELECT_CONTACT_SOURCE = 9  # After sending to drivers: select contact info source for this client
STATE_AI_REVIEW = 16  # AI parsed Phase 1: user confirms or edits fields
STATE_AI_EDIT_MENU = 17  # Pick which field to edit
STATE_AI_EDIT_INPUT = 18  # Waiting for new text for selected field
STATE_VIN_CHOICE = 10  # VIN checker returned different car; user picks stated vs API
STATE_VIN_RETYPE = 14  # User chose to retype VIN; waiting for new VIN input
STATE_MISSING_FIELD = 11  # User must add missing field (e.g. color)
STATE_ADD_FILES = 12  # Ask "Do you want to add files?"
STATE_WAITING_FILE = 13  # Waiting for user to send file(s)
STATE_SPECIAL_REQUEST_ISSUERS = 19  # After phone + price: note for group / issuers
STATE_SPECIAL_REQUEST_DRIVERS = 20  # Then: note only for drivers (before encrypt)
STATE_EDIT_FIELD_PROMPT = 29   # waiting for text input for editing a field
STATE_ADJUST_INPUT = 30        # review "adjust from image/text": waiting for media/text

# Phase 1: accumulate photos/PDFs; user taps Done to run vision extraction
PHASE1_VISION_MAX_FILES = 12
PHASE1_VISION_CANCEL_CB = "phase1_vision_cancel"
PHASE1_VISION_PHOTO_CB = "phase1_vision_photo"
PHASE1_VISION_DONE_CB = "phase1_vision_done"

# Supabase ``states.state`` value: issuer posted group approval and must wait for Accept before driver pick / dispatch
USER_STATE_AWAIT_GROUP_ACCEPT = "await_group_accept"

# Phase 2 (phone + price) — shared by file-flow callbacks and must stay in sync
PHASE2_INTRO_MESSAGE = (
    "✅ Phase 1 tag info received!\n\n"
    "📞💲Phase 2: Please type Phone Number then Price.\n"
    "In this format:\n"
    "(example: '+1234567890 $150')"
)

PHASE2_ISSUERS_PROMPT = (
    "✅ Phase 2 Phone and Price received!\n\n"
    "📝 Would you like to say any Special Requests to the temp tag issuers ? (optional)"
)

PHASE2_SUCCESS_BEFORE_FILES_MESSAGE = (
    "✅ Phone and price saved.\n\n"
    "Next: choose whether to attach files."
)

# Keys in user state data that are not Phase 1 vehicle/delivery fields
_PHASE1_STATE_EXCLUDE = frozenset({
    "phone_number", "price", "encrypted_data", "reference_id", "group_id", "selected_group",
    "resend", "lead_id", "follow_after_broadcast", "broadcast",
    "pending_phone_number", "pending_price",
    "special_request_note", "special_request_issuers", "special_request_drivers", "username",
    "reassign_lead_id", "approval_files_forwarded",
})

# Receipt submission states
STATE_WAITING_REFERENCE_ID = 4  # Waiting for reference ID input
STATE_WAITING_RECEIPT_CONFIRM = 5  # Waiting for receipt confirmation
STATE_WAITING_RECEIPT_IMAGE = 6  # Waiting for receipt image upload

# Initialize services
db = Database()
ots = OneTimeSecret()
monday = MondayClient() if Config.is_monday_configured() else None


def _resolve_receipt_detection_mode() -> str:
    """``strict`` = visible ``$`` only; ``lax`` = match amount to lead.

    Supabase ``settings.receipt_detection_mode`` (admin panel) wins over ``RECEIPT_DETECTION_MODE`` env
    so dashboard changes are not ignored when Render still has env set (e.g. lax).
    Env is used only when the setting is missing or invalid in the database.
    """
    raw = db.get_setting("receipt_detection_mode")
    if not (raw or "").strip():
        raw = db.get_setting("receipt_detection")  # legacy / typo key
    if (raw or "").strip():
        v = raw.strip().lower()
        if v in ("strict", "lax"):
            logger.info(
                "Receipt detection mode: %s (source=Supabase settings receipt_detection_mode, raw=%r)",
                v,
                raw,
            )
            return v
    env_mode = Config.receipt_detection_mode_from_env()
    if env_mode:
        logger.info(
            "Receipt detection mode: %s (source=RECEIPT_DETECTION_MODE env; no valid DB value)",
            env_mode,
        )
        return env_mode
    logger.info("Receipt detection mode: lax (default; no DB or env)")
    return "lax"


SUSPENSION_THRESHOLD = 5  # 5+ pending receipts = suspended
# Lead source picker: auto-complete without source if issuer does not tap within this window
CONTACT_SOURCE_TIMEOUT_SEC = 180

# Short-lived caches so repeated taps / messages don't hammer Supabase on every callback
_ALL_DRIVERS_CACHE: list | None = None
_ALL_DRIVERS_CACHE_TS: float = 0.0
_ALL_DRIVERS_TTL_SEC = 2.5
_SUSP_DRIVER_IDS_CACHE: tuple[set[str], float] | None = None
_SUSP_DRIVER_IDS_TTL_SEC = 2.0


def _get_all_drivers_cached() -> list:
    """Return all driver rows; refresh from DB at most every _ALL_DRIVERS_TTL_SEC."""
    global _ALL_DRIVERS_CACHE, _ALL_DRIVERS_CACHE_TS
    now = time.monotonic()
    if _ALL_DRIVERS_CACHE is not None and (now - _ALL_DRIVERS_CACHE_TS) < _ALL_DRIVERS_TTL_SEC:
        return _ALL_DRIVERS_CACHE
    _ALL_DRIVERS_CACHE = db.get_all_drivers()
    _ALL_DRIVERS_CACHE_TS = now
    return _ALL_DRIVERS_CACHE


def _callback_query_stale_message(msg: str) -> bool:
    lowered = (msg or "").lower()
    return (
        "too old" in lowered
        or "query id is invalid" in lowered
        or "response timeout" in lowered
        or "already been answered" in lowered
    )


async def _safe_answer_callback_query(
    query,
    text: Optional[str] = None,
    show_alert: bool = False,
) -> bool:
    """
    answerCallbackQuery. Telegram returns 400 if the tap is stale or the query was already answered.
    Returns True if answered, False if ignored as stale/invalid.
    """
    try:
        if text:
            await query.answer(text=text, show_alert=show_alert)
        else:
            await query.answer()
        return True
    except BadRequest as e:
        if _callback_query_stale_message(str(e)):
            logger.warning("Stale or invalid callback query ignored: %s", e)
            return False
        raise


# Clears inline keyboards on broadcast offer messages after accept/decline/taken
_EMPTY_INLINE_KB = InlineKeyboardMarkup([])

# Driver inline: add lead; after receipt success also offer owed-receipt flow
_DRIVER_ADD_LEAD_BTN = InlineKeyboardButton("➕ Add new lead", callback_data="driver_add_lead")
_DRIVER_ADD_RECEIPT_BTN = InlineKeyboardButton("🧾 Add new receipt", callback_data="driver_add_receipt")
_DRIVER_HELP_BTN = InlineKeyboardButton("❓ Help", callback_data="bot_help")


def _help_guide_text() -> str:
    """Plain-text user guide (shown by /help and the ❓ Help button)."""
    return (
        "📖 How to Use This Bot 🤖\n\n"
        "🚀 Main Commands\n\n"
        "▶️ /start — Open the bot\n"
        "➕ /lead or /client — Add a new client/lead\n"
        "📇 /followup — Client missing info for their temporary tag? The bot\n"
        "     texts/emails them a reminder on your schedule (start/stop/frequency)\n"
        "🗂 /followups — List your open follow-ups\n"
        "👁 /allfollowups — Supervisors: view, stop or delete any follow-up\n"
        "🧾 /receipts — Upload receipts\n"
        "📋 /appeal — Appeal / cancel a delivery (with proof)\n"
        "❌ /cancel — Cancel and restart\n"
        "❓ /help — Show this guide\n\n"
        "⸻\n\n"
        "➕ Add a New Lead\n\n"
        "Tap ➕ Add New Lead\n"
        "Send:\n"
        "📝 Client information (text)\n"
        "📸 Photos\n"
        "📄 PDFs\n"
        "Tap ✅ Done\n"
        "Review the summary\n"
        "Edit if needed\n"
        "Tap 🚀 Submit\n"
        "Choose:\n"
        "👥 Group\n"
        "🚗 Driver\n"
        "📍 Lead Source\n\n"
        "⸻\n\n"
        "🛡️ Insurance (NY FS-20)\n\n"
        "Required:\n\n"
        "📧 Email Address\n"
        "🪪 Driver License\n\n"
        "⸻\n\n"
        "🚗 Drivers: Upload Receipts\n\n"
        "Tap 🧾 Add New Receipt\n"
        "Or type /receipts\n"
        "Upload receipt for the reference ID\n\n"
        "👮 Supervisors: same /receipts flow to upload for any assigned driver\n\n"
        "⸻\n\n"
        "📋 Delivery appeals\n\n"
        "Type /appeal when a delivery has complications.\n"
        "Enter the reference ID, confirm the client name, upload image proof.\n"
        "Supervisors review and accept or decline; you are notified of the outcome.\n\n"
        "⸻\n\n"
        "💡 Helpful Tips\n\n"
        "🚘 Use a valid 17-character VIN\n"
        "❌ Stuck? Type /cancel\n"
        "❓ Need help? Type /help"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply with usage guide (/help). Registered outside ConversationHandler so it works in any state."""
    msg = update.effective_message
    if msg:
        await msg.reply_text(_help_guide_text())


def _driverblock_status_text(enabled: bool) -> str:
    if enabled:
        return (
            "🔒 *Driver phone redaction:* `ON`\n"
            "Drivers receive a OneTimeSecret link (clientsphonenumber.com) "
            "instead of the raw phone number.\n\n"
            "Use /driverblock to toggle."
        )
    return (
        "🔓 *Driver phone redaction:* `OFF`\n"
        "Drivers receive the raw phone number stored on the lead.\n\n"
        "Use /driverblock to toggle."
    )


async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Secret status command: show driver phone-redaction state. Supervisory-only."""
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return
    if not _user_is_global_supervisor(user.id):
        return
    await msg.reply_text(_driverblock_status_text(_driverblock_enabled()), parse_mode="Markdown")


async def cmd_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/stats and /leaderboard — who has entered the most clients.

    Names and counts, nothing else: a roster with phone numbers on it is a list of
    people to poach, and nobody needs one to see who is winning."""
    msg = update.effective_message
    if not msg:
        return
    rows = await asyncio.to_thread(db.get_lead_counts_by_sender)
    if not rows:
        await msg.reply_text("🏆 *Leaderboard*\n\n_No leads counted yet._",
                             parse_mode="Markdown")
        return
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    total = sum(n for _, n in rows)
    lines = [f"🏆 *Leaderboard* — {total} client{'s' if total != 1 else ''} entered", ""]
    for i, (name, n) in enumerate(rows[:40], start=1):
        mark = medals.get(i, f"{i}.")
        lines.append(f"{mark} {_telegram_md1_escape(name)} — *{n}*")
    if len(rows) > 40:
        lines.append("")
        lines.append(f"_…and {len(rows) - 40} more._")
    await msg.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_drivers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Supervisory-only roster: every driver with phone, email and chat id.

    The /settings screen is one editable message and cannot grow past Telegram's
    limit; this splits across messages so nobody is left off the list."""
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return
    if not _user_is_global_supervisor(user.id):
        return
    drivers = await asyncio.to_thread(_get_all_drivers_cached)
    suspended = await asyncio.to_thread(_get_suspended_driver_ids)
    total = len(drivers or [])
    if not total:
        await msg.reply_text("🚗 *Drivers*\n\n_No drivers yet._", parse_mode="Markdown")
        return
    # The same tappable list as /settings, split across messages so a long roster
    # is never cut. Tapping a driver opens their details and actions.
    keyboards = _driver_list_keyboard(drivers, suspended)
    for n, kb in enumerate(keyboards, start=1):
        head = (f"🚗 *Drivers — {total} on file*\n"
                "Tap one for phone, email, chat id — and to suspend, lift, "
                "disable or enable.")
        if len(keyboards) > 1:
            head += f"\n_(part {n} of {len(keyboards)})_"
        await msg.reply_text(head, parse_mode="Markdown", reply_markup=kb)


async def cmd_driverblock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Secret toggle: flip driver phone-redaction on/off. Supervisory-only."""
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return
    if not _user_is_global_supervisor(user.id):
        return
    new_val = not _driverblock_enabled()
    if not _set_driverblock_enabled(new_val):
        await msg.reply_text("⚠️ Could not save the setting. Try again.")
        return
    header = "✅ Updated.\n\n"
    await msg.reply_text(header + _driverblock_status_text(new_val), parse_mode="Markdown")


async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Self-check (anyone): your Telegram ID + whether the bot treats you as a
    supervisor — the gate for ALL AI features (talk-to-the-bot, update tag numbers
    by photo/PDF, /settings, /announce). Also shows if AI image reading is configured."""
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return
    is_sup = _user_is_global_supervisor(user.id)
    chat_type = update.effective_chat.type if update.effective_chat else "?"
    ai_on = Config.is_ai_vision_configured()
    uname = f"@{user.username}" if user.username else (user.full_name or "—")
    lines = [
        "🪪 <b>Your details</b>",
        f"• ID: <code>{user.id}</code>",
        f"• Username: {html.escape(uname, quote=False)}",
        f"• Chat: {chat_type}",
        f"• Supervisor / AI mode: {'✅ enabled' if is_sup else '❌ not a supervisor'}",
        f"• AI image reading: {'✅ on' if ai_on else '❌ OPENAI_API_KEY not set'}",
        f"• Build: <code>{(os.environ.get('RENDER_GIT_COMMIT') or 'local')[:7]}</code>",
    ]
    if not is_sup:
        lines += [
            "",
            "To turn on the supervisor AI features (talk-to-the-bot, update tag numbers "
            "by photo/PDF, /settings, /announce), add the ID above to "
            "<code>SUPERVISORY_TELEGRAM_ID</code> in the bot's environment, then redeploy.",
        ]
    else:
        lines += [
            "",
            "✅ You're set — there's no “mode” to turn on. Just DM me here and talk: "
            "“which drivers are active?”, “disable group HighKage”, “update resident tag "
            "number 553300”, or send/forward a photo or PDF of a tag to update the counter. "
            "(For photo→tag, make sure you're not mid-lead — send /cancel first.)",
        ]
    await msg.reply_text("\n".join(lines), parse_mode="HTML")


async def handle_help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inline ❓ Help — same text as /help."""
    query = update.callback_query
    if not query:
        return
    await _safe_answer_callback_query(query)
    if query.message:
        await query.message.reply_text(_help_guide_text())


def _driver_add_lead_keyboard_only() -> InlineKeyboardMarkup:
    """Default driver follow-up keyboard (single action — not receipt on every message)."""
    return InlineKeyboardMarkup([[_DRIVER_ADD_LEAD_BTN], [_DRIVER_HELP_BTN]])


def _driver_keyboard_lead_and_receipt() -> InlineKeyboardMarkup:
    """After receipt submitted: add another lead or open owed-receipts upload flow."""
    return InlineKeyboardMarkup([[_DRIVER_ADD_LEAD_BTN, _DRIVER_ADD_RECEIPT_BTN], [_DRIVER_HELP_BTN]])


def _driver_keyboard_after_accept(
    reference_id: str | None, reassign_lead_id: str | None = None
) -> InlineKeyboardMarkup:
    """Keyboard attached to the LEAD ACCEPTED message.

    ➕ Add new lead    → start a new lead
    🧾 Upload Receipt → jumps straight into the receipt-upload flow for
                         THIS specific lead's reference id, so the driver
                         doesn't have to pick from a list.
    🔄 Reassign        → driver changed their mind — send the lead back out
                         to the other drivers (only when reassign_lead_id set).
    ❓ Help            → usage guide
    """
    rows: list[list[InlineKeyboardButton]] = [[_DRIVER_ADD_LEAD_BTN]]
    ref = (str(reference_id) if reference_id is not None else "").strip()
    if ref:
        rows.append(
            [InlineKeyboardButton("🧾 Upload Receipt", callback_data=f"receipt_for_{ref}")]
        )
    else:
        # No reference id (shouldn't happen for accepted leads, but stay safe):
        # fall back to the generic owed-receipts opener.
        rows.append([_DRIVER_ADD_RECEIPT_BTN])
    if reassign_lead_id:
        rows.append([
            InlineKeyboardButton(
                "🔄 Reassign (can't do this one)",
                callback_data=f"reassign_lead_{reassign_lead_id}",
            )
        ])
    rows.append([_DRIVER_HELP_BTN])
    return InlineKeyboardMarkup(rows)


# ── Driver GPS tracking gate ───────────────────────────────────────────────
# When TRACKING_SITE_BASE_URL is set, accepting a lead/renewal first sends the
# driver a tracking-site link; the full delivery details are only DM'd after
# their location ping arrives (hard block — supervisors can override).

def _new_tracking_token() -> str:
    return secrets.token_urlsafe(24)


def _tracking_link(token: str) -> str:
    return f"{Config.TRACKING_SITE_BASE_URL}/t/{token}"


async def _forward_accepted_lead_files(context: ContextTypes.DEFAULT_TYPE, lead: dict, chat_id) -> None:
    """Send the lead's parsed images/PDFs to a chat that just accepted it.

    Reads the descriptors off the lead row (re-fetching when the caller passed a
    trimmed dict), and remembers what it already sent so a re-sent details message
    never duplicates the paperwork."""
    if not lead or not chat_id:
        return
    lead_id = str(lead.get("id") or "")
    att = lead.get("phase1_attached_files")
    if not (isinstance(att, list) and att) and lead_id:
        try:
            att = (db.get_lead_by_id(lead_id) or {}).get("phase1_attached_files")
        except Exception as e:
            logger.warning("accepted-lead files lookup failed: %s", e)
            att = None
    if not (isinstance(att, list) and att):
        return
    sent_key = f"lead_files_sent_{lead_id}" if lead_id else None
    if sent_key and context.user_data is not None:
        if context.user_data.get(sent_key):
            return
        context.user_data[sent_key] = True
    try:
        await _forward_phase1_attached_files_to_targets(context, att, chat_id)
    except Exception as e:
        logger.warning("forwarding accepted-lead files failed: %s", e)


async def _send_driver_lead_details(
    context: ContextTypes.DEFAULT_TYPE, lead: dict, chat_id, reassign_lead_id: str | None = None
) -> None:
    """DM the full accepted-lead details (HTML with plain-text fallback)."""
    confirmation_message = _build_driver_lead_accepted_message_html(lead)
    add_lead_kb = _driver_keyboard_after_accept(lead.get("reference_id"), reassign_lead_id)
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=confirmation_message,
            reply_markup=add_lead_kb,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except BadRequest:
        plain = re.sub(r"<[^>]+>", "", confirmation_message)
        await context.bot.send_message(chat_id=chat_id, text=plain, reply_markup=add_lead_kb)
    # Everything the issuer sent for parsing (title/registration/insurance shots and
    # PDFs) follows the lead to the driver who accepted it — they are the one who
    # needs the paperwork on the delivery.
    await _forward_accepted_lead_files(context, lead, chat_id)


async def _start_tracking_gate_or_send_details(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    kind: str,
    lead: dict,
    driver_id: str | None,
    driver_name: str | None,
    chat_id,
    renewal_id: str | None = None,
) -> None:
    """Send the delivery details immediately; location sharing is OPTIONAL.

    The driver gets the full details right away, then a tracking link they can
    tap so dispatch sees their trip live (and they get the automatic arrival
    receipt reminder). The session is created with details_sent_at already set
    so the tracking job never re-sends details or nags about a missing ping.
    """
    reassign_id = str(lead.get("id")) if (kind == "lead" and lead.get("id")) else None
    await _send_driver_lead_details(context, lead, chat_id, reassign_lead_id=reassign_id)
    if not Config.is_tracking_configured():
        return
    token = _new_tracking_token()
    delivery_addr = _delivery_block_plain(lead)
    if not delivery_addr or delivery_addr.strip().upper() == "N/A":
        delivery_addr = None
    from datetime import timezone as _tz
    sess = db.create_tracking_session(
        token=token,
        kind=kind,
        chat_id=str(chat_id),
        driver_id=driver_id,
        driver_name=driver_name,
        lead_id=lead.get("id"),
        renewal_id=renewal_id,
        reference_id=lead.get("reference_id"),
        delivery_address=delivery_addr,
        details_sent_at=datetime.now(_tz.utc).isoformat(),
    )
    if not sess:
        logger.error(
            "Tracking session insert failed — details were sent; no tracking link "
            "(run database/migration_driver_tracking.sql)"
        )
        return
    link = _tracking_link(token)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📍 Share my location", url=link)]])
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "📍 Optional — but it helps!\n\n"
                "Tap below to share your location: dispatch can follow your "
                "delivery live, and you'll get an automatic receipt reminder "
                "the moment you arrive.\n\n"
                f"{link}"
            ),
            reply_markup=kb,
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.warning("Could not send optional tracking link to %s: %s", chat_id, e)


def _drivers_sent_label(state_data: dict, drivers_list: list) -> str:
    """Who the lead went to, for the confirmation.

    A bare count ("1 driver(s)") does not say WHO, which is the one thing worth
    checking before walking away. Names are listed; when everyone was picked it says
    "All drivers" rather than a wall of names."""
    names = [str(d.get("driver_name") or "").strip()
             for d in (drivers_list or []) if d and d.get("id")]
    names = [n for n in names if n]
    if not names:
        return "no drivers"
    picked = str((state_data or {}).get("selected_driver_names") or "").strip()
    if picked.lower() == "all drivers":
        return f"All drivers ({len(names)})"
    # Everyone eligible got it even though it was not an explicit "all" pick.
    try:
        eligible = [d for d in _get_all_drivers_cached() if record_is_active(d)]
        suspended = _get_suspended_driver_ids()
        if len({str(d.get("id")) for d in eligible if str(d.get("id")) not in suspended}) == len(names) > 1:
            return f"All drivers ({len(names)})"
    except Exception:
        pass
    if len(names) <= 4:
        return ", ".join(names)
    return ", ".join(names[:4]) + f" +{len(names) - 4} more"


# ── $100 instant PDF ────────────────────────────────────────────────────────
# Pay, and the tag goes straight to the chosen driver — no dispatch team, no wait.
# The money is the approval.
#
# The bot never talks to Stripe. It asks the dashboard (which is already
# tristatetags.com/backend and holds the same database) for a Checkout link, and
# then watches the lead for `instant_pdf_paid_at`, which only Stripe's verified
# webhook ever sets. Nothing hangs: delivery is driven by a column, so a restart
# between payment and delivery delays the tag rather than losing it.
INSTANT_PDF_CB = "instantpdf_"


def _dashboard_base() -> str:
    return (os.getenv("RECEIPT_PORTAL_BASE")
            or "https://tristatetags.com/backend").strip().rstrip("/")


async def request_instant_pdf_link(lead_id, driver_id, reference_id="") -> tuple:
    """(url, error). Asks the dashboard to open a Stripe Checkout session."""
    key = (getattr(Config, "INTEGRATIONS_API_KEY", None) or "").strip()
    if not key:
        return None, "INTEGRATIONS_API_KEY is not set on the bot."

    def _post():
        return requests.post(
            f"{_dashboard_base()}/api/instant/checkout",
            json={"lead_id": str(lead_id), "driver_id": str(driver_id),
                  "reference_id": str(reference_id or "")},
            headers={"Authorization": f"Bearer {key}"},
            timeout=20,
        )

    try:
        resp = await asyncio.to_thread(_post)
    except Exception as e:
        logger.warning("instant pdf: checkout request failed: %s", e)
        return None, str(e)
    try:
        data = resp.json() if resp.content else {}
    except Exception:
        data = {}
    if not resp.ok or not data.get("url"):
        return None, (data.get("error") or f"checkout failed ({resp.status_code})")
    return data["url"], None


def _instant_pdf_keyboard(lead_id) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        "⚡ Instant PDF — $100 (skip dispatch)",
        callback_data=f"{INSTANT_PDF_CB}{lead_id}")]])


async def handle_instant_pdf_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """"Instant PDF" tapped — hand back a pay link for THIS lead's chosen driver."""
    query = update.callback_query
    await _safe_answer_callback_query(query)
    lead_id = (query.data or "").replace(INSTANT_PDF_CB, "", 1).strip()
    lead = await asyncio.to_thread(db.get_lead_by_id, lead_id) if lead_id else None
    if not lead:
        await query.message.reply_text("❌ That lead is gone — start a new one.")
        return

    # The driver already chosen for this lead. Without one there is nobody to send
    # the tag to, and charging for that would be worse than refusing.
    driver_id = ""
    try:
        st = await asyncio.to_thread(db.get_lead_assignment_status, lead_id)
        driver_id = str((st or {}).get("driver_id") or "")
    except Exception:
        driver_id = ""
    if not driver_id:
        ids = _resolve_dispatch_driver_ids(
            _issuer_state_data_from_lead(lead), group_id=lead.get("group_id"))
        driver_id = ids[0] if len(ids) == 1 else ""
    if not driver_id:
        await query.message.reply_text(
            "🚗 Pick ONE driver for this lead first — an instant tag goes straight "
            "to them, so there has to be exactly one.")
        return

    url, err = await request_instant_pdf_link(
        lead_id, driver_id, lead.get("reference_id") or "")
    if not url:
        await query.message.reply_text(f"❌ Could not open the payment page.{NL}{NL}{err}")
        return
    ref = html.escape(str(lead.get("reference_id") or "N/A"), quote=False)
    await query.message.reply_text(
        f"⚡ <b>Instant PDF — $100</b>{NL}{NL}"
        f"📋 Reference: <code>{ref}</code>{NL}"
        f"Pay and the tag goes straight to the driver — no dispatch approval, "
        f"no waiting.{NL}{NL}<i>It arrives within a minute of the payment clearing.</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("💳 Pay $100 and send", url=url)]]),
    )


async def deliver_paid_instant_pdfs(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Every paid instant tag that has not been delivered yet — send it now.

    Driven by the database rather than by the webhook's own request, so a payment
    that lands while the bot is restarting is still delivered on the next tick. The
    delivered stamp is written only after the document is actually in the chat."""
    try:
        rows = await asyncio.to_thread(db.get_paid_instant_pdfs_undelivered)
    except Exception as e:
        logger.warning("instant pdf sweep: %s", e)
        return
    for lead in rows or []:
        lead_id = str(lead.get("id") or "")
        driver_id = str(lead.get("instant_pdf_driver_id") or "")
        if not lead_id or not driver_id:
            continue
        driver = await asyncio.to_thread(_driver_row_by_id, driver_id)
        chat_id = _parse_chat_id((driver or {}).get("driver_telegram_id"))
        if not driver or chat_id is None:
            logger.error("instant pdf: lead %s is paid but driver %s has no chat id",
                         lead_id, driver_id)
            continue
        try:
            sent = await _send_instant_tag_to_driver(context, lead, driver, chat_id)
        except Exception as e:
            logger.error("instant pdf: delivery failed for %s: %s", lead_id, e)
            continue                      # left undelivered — the next tick retries
        if sent:
            await asyncio.to_thread(db.mark_instant_pdf_delivered, lead_id)
            logger.info("instant pdf delivered for lead %s to %s", lead_id,
                        driver.get("driver_name"))


async def _send_instant_tag_to_driver(context, lead, driver, chat_id) -> bool:
    """The tag PDF straight into the driver's chat. True only if it arrived.

    Uses the same builder as every other tag — it allocates the plate and control
    number once per lead and reports how many chats actually received the document,
    which is the only honest basis for marking this delivered."""
    ref = str(lead.get("reference_id") or "N/A")
    counts = await _send_all_tag_pdfs(
        context, lead, [chat_id],
        accepted_by=f"PAID INSTANT — {driver.get('driver_name') or 'driver'}")
    # EVERY car's tag, not just one. This return value is what stamps the lead
    # delivered and takes it out of the retry sweep, so "the first of two
    # arrived" must read as failure — the customer paid for both.
    if not counts or not all(counts):
        logger.error(
            "instant pdf: lead %s sent %s of %d tag(s) — NOT marking delivered",
            lead.get("id"), sum(1 for c in counts if c), len(counts) or 1,
        )
        return False
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=("⚡ This one was PAID for as an instant tag — it skipped dispatch. "
                  "Print and deliver."),
        )
    except Exception:
        pass                          # the PDF is what matters; the note is a nicety
    # The team is not in the loop, but it should not be in the dark either.
    try:
        team = followup_team_chat_id()
        if team:
            await context.bot.send_message(
                chat_id=team,
                text=(f"⚡ Instant tag PAID and sent{NL}"
                      f"📋 {ref}{NL}🚗 {driver.get('driver_name') or 'driver'}{NL}"
                      f"💵 $100 — dispatch bypassed"),
            )
    except Exception as e:
        logger.warning("instant pdf: could not tell the team: %s", e)
    return True


def _after_send_keyboard(lead_id: str) -> InlineKeyboardMarkup:
    """Buttons on the "lead sent" confirmation.

    Reassigning right after sending is routine — the driver is unavailable, or it
    belongs to a different dispatcher — and previously it meant waiting for a timeout
    or digging for the earlier message. Both callbacks are conversation ENTRY points,
    so they still work after the lead flow has ended."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚗 Reassign driver", callback_data=f"resend_driver_{lead_id}"),
         InlineKeyboardButton("🏢 Reassign dispatcher", callback_data=f"reassign_group_{lead_id}")],
        [InlineKeyboardButton("➕ Another tag (same client)", callback_data=f"another_tag_{lead_id}")],
        [InlineKeyboardButton("⚡ Instant PDF — $100 (skip dispatch)",
                              callback_data=f"{INSTANT_PDF_CB}{lead_id}")],
    ])


def _keyboard_lead_accept_decline(lead_id: str) -> InlineKeyboardMarkup:
    """New-lead offer: Accept / Different Driver (decline callback)."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Accept", callback_data=f"accept_lead_{lead_id}"),
            InlineKeyboardButton("🔄 Different Driver", callback_data=f"decline_lead_{lead_id}"),
        ],
    ])


def _keyboard_renewal_driver(short_r: str, short_d: str) -> InlineKeyboardMarkup:
    """Renewal driver offer."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Accept", callback_data=f"rda_{short_r}{short_d}"),
            InlineKeyboardButton("🔄 Reassign", callback_data=f"rdr_{short_r}{short_d}"),
        ],
    ])


def _keyboard_receipt_plus_rows(extra_rows: list) -> InlineKeyboardMarkup:
    """Per-reference upload rows only (no extra global receipt row — drivers use /receipts)."""
    return InlineKeyboardMarkup(list(extra_rows))

def _vin_conflict_body(_stated_car: str, api_car: str) -> str:
    """One short question instead of the old three-line preamble.

    The decoded vehicle is still shown on its own line — it is the same fact the
    long version ended with, and without it Yes/No would be a blind choice."""
    return f"Would you like to use DMV system?\n• DMV: {api_car}"


def _short_uuid(u: str) -> str:
    """Compress a UUID string (36 chars) to 22-char base64url (no padding)."""
    return base64.urlsafe_b64encode(_uuid_mod.UUID(u).bytes).rstrip(b"=").decode()

def _long_uuid(s: str) -> str:
    """Expand a 22-char base64url back to a standard UUID string."""
    padded = s + "==" 
    return str(_uuid_mod.UUID(bytes=base64.urlsafe_b64decode(padded)))


# Unpadded base64url of 16 bytes is always 22 characters; alphabet includes "_" and "-".
_SHORT_UUID_B64URL_LEN = 22


def _parse_paired_short_uuids(callback_data: str, prefix: str) -> tuple[str, str] | None:
    """Unpack two short UUID tokens from callback_data.

    Canonical form is ``prefix + token1 + token2`` (44 chars of id payload) so an
    underscore inside a token does not break parsing. Legacy form ``tok1_tok2`` is
    accepted only when both parts are exactly 22 chars (old buttons with no ``_``
    inside tokens).
    """
    if not callback_data.startswith(prefix):
        return None
    body = callback_data[len(prefix) :]
    L = _SHORT_UUID_B64URL_LEN
    if len(body) == L * 2:
        return body[:L], body[L:]
    if len(body) >= L * 2 + 1 and body[L] == "_":
        second = body[L + 1 : L + 1 + L]
        if len(second) == L:
            return body[:L], second
    if "_" in body:
        a, b = body.split("_", 1)
        if len(a) == L and len(b) == L:
            return a, b
    return None


def _bust_driver_caches() -> None:
    """Drop the driver and suspension caches so a /settings change shows at once
    (both are short-TTL memoizations, and a stale one makes an edit look ignored)."""
    global _SUSP_DRIVER_IDS_CACHE, _ALL_DRIVERS_CACHE, _ALL_DRIVERS_CACHE_TS
    _SUSP_DRIVER_IDS_CACHE = None
    _ALL_DRIVERS_CACHE = None
    _ALL_DRIVERS_CACHE_TS = 0.0


def _get_suspended_driver_ids() -> set[str]:
    """Driver IDs (as str) that may not receive new leads: those owing
    SUSPENSION_THRESHOLD+ receipts, plus anyone a supervisor suspended by hand."""
    global _SUSP_DRIVER_IDS_CACHE
    now = time.monotonic()
    if _SUSP_DRIVER_IDS_CACHE is not None:
        cached, ts = _SUSP_DRIVER_IDS_CACHE
        if (now - ts) < _SUSP_DRIVER_IDS_TTL_SEC:
            return cached
    try:
        s = db.get_driver_ids_with_pending_receipt_count_at_least(SUSPENSION_THRESHOLD)
    except Exception as e:
        logger.warning("_get_suspended_driver_ids: %s", e)
        return set()
    try:
        # set() on both sides: the method returns a set today, but `set | list` is
        # a TypeError and the except below swallows it — which would drop every
        # hand-suspended driver from the list without a word.
        s = set(s) | set(db.get_manually_suspended_driver_ids() or ())
    except Exception as e:
        logger.warning("manual suspensions unavailable: %s", e)
    _SUSP_DRIVER_IDS_CACHE = (s, now)
    return s


async def _notify_suspension_lifted(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    driver: dict,
    pending_after: list,
    reply_message=None,
) -> None:
    """Notify driver and all global supervisory IDs when receipt penalty suspension ends."""
    global _SUSP_DRIVER_IDS_CACHE
    _SUSP_DRIVER_IDS_CACHE = None

    driver_name = (driver.get("driver_name") or "Driver").strip()
    n_pending = len(pending_after)
    driver_txt = (
        "✅ **Suspension lifted!**\n"
        "You're back in action 👊💥\n"
        "🚗 You will now receive new leads again.\n"
        "🔔 Turn on Telegram notifications to grab them fast!\n\n"
        "🧾 Upload receipts immediately after every delivery.\n"
        f"⚠️ **{SUSPENSION_THRESHOLD}** missing receipts = automatic suspension again.\n\n"
        f"Outstanding receipts remaining: **{n_pending}**"
    )
    kb = _driver_keyboard_lead_and_receipt()
    driver_chat = _parse_chat_id(driver.get("driver_telegram_id"))
    sent_driver = False
    if driver_chat is not None:
        try:
            await context.bot.send_message(
                chat_id=driver_chat,
                text=driver_txt,
                parse_mode="Markdown",
                reply_markup=kb,
            )
            sent_driver = True
        except BadRequest:
            try:
                await context.bot.send_message(
                    chat_id=driver_chat,
                    text=driver_txt.replace("*", ""),
                    reply_markup=kb,
                )
                sent_driver = True
            except Exception as e:
                logger.warning("Could not DM driver suspension-lifted notice: %s", e)
        except Exception as e:
            logger.warning("Could not DM driver suspension-lifted notice: %s", e)
    if not sent_driver and reply_message is not None:
        try:
            await reply_message.reply_text(
                driver_txt,
                parse_mode="Markdown",
                reply_markup=kb,
            )
        except BadRequest:
            await reply_message.reply_text(
                driver_txt.replace("*", ""),
                reply_markup=kb,
            )

    ref_parts = []
    for p in pending_after or []:
        ref = (p.get("reference_id") or "").strip()
        if ref and ref.upper() != "N/A":
            ref_parts.append(_telegram_md1_escape(ref))
    refs_line = (
        f"\nReceipt references: {', '.join(ref_parts)}"
        if ref_parts
        else "\nReceipt references: (none on file)"
    )
    dn_esc = _telegram_md1_escape(driver_name)
    sup_txt = _prefix_supervisory_message(
        f"✅ **Suspension removed**\n\n"
        f"Driver: **{dn_esc}**\n"
        f"Remaining receipts: {n_pending}"
        f"{refs_line}"
    )
    sup_plain = (
        f"✅ Suspension removed\n\n"
        f"Driver: {driver_name}\n"
        f"Remaining receipts: {n_pending}"
        + (f"\nReceipt references: {', '.join(ref_parts)}" if ref_parts else "\nReceipt references: (none on file)")
    )
    sup_ids = _global_supervisory_chat_ids()
    if not sup_ids:
        logger.warning("Suspension lifted but SUPERVISORY_TELEGRAM_ID is empty — no supervisory alert sent")
    for sup_id in sup_ids:
        try:
            await context.bot.send_message(chat_id=sup_id, text=sup_txt, parse_mode="Markdown")
        except BadRequest:
            try:
                await context.bot.send_message(chat_id=sup_id, text=sup_plain)
            except Exception as e:
                logger.warning("Could not send suspension-lifted alert to supervisory %s: %s", sup_id, e)
        except Exception as e:
            logger.warning("Could not send suspension-lifted alert to supervisory %s: %s", sup_id, e)


def _norm_chat_id(cid) -> int | str | None:
    """Normalize Telegram chat id for set deduplication (int when possible)."""
    if cid is None:
        return None
    if isinstance(cid, bool):
        return None
    if isinstance(cid, int):
        return cid
    s = str(cid).strip().lstrip("=").strip()
    if not s:
        return None
    try:
        return int(s.split(".", 1)[0])
    except (ValueError, TypeError):
        return cid


def _build_driver_keyboard(drivers: list, exclude_suspended: bool = True, include_all: bool = True):
    """Build driver selection keyboard. Suspended drivers get driver_suspended_X callback and (PENALTY) label."""
    suspended = _get_suspended_driver_ids() if exclude_suspended else set()
    buttons = []
    for d in drivers:
        did = d.get("id")
        name = d.get("driver_name", "Unknown")
        if str(did) in suspended:
            buttons.append([
                InlineKeyboardButton(
                    f"🚫 {name} (PENALTY)",
                    callback_data=f"driver_suspended_{did}"
                )
            ])
        else:
            buttons.append([
                InlineKeyboardButton(f"🚗 {name}", callback_data=f"select_driver_{did}")
            ])
    if include_all:
        elig = [d for d in drivers if str(d.get("id")) not in suspended]
        if elig:
            buttons.append([InlineKeyboardButton("📢 Send to All Drivers", callback_data="select_driver_all")])
    return InlineKeyboardMarkup(buttons)


def _build_group_keyboard(groups: list, include_all: bool = True) -> InlineKeyboardMarkup:
    """Build group selection keyboard; optionally include broadcast-to-all."""
    buttons = [[InlineKeyboardButton(g.get("group_name", str(g["id"])), callback_data=f"select_group_{g['id']}")] for g in groups]
    if include_all and groups:
        buttons.append([InlineKeyboardButton("📢 Send to All Dispatchers", callback_data="select_group_all")])
    return InlineKeyboardMarkup(buttons)


# Per-image cap so we don't bloat the lead row's JSONB with huge base64 blobs.
# 5 MB raw == ~6.7 MB base64; Telegram bot uploads themselves cap at 10 MB
# for photos. Anything larger gets skipped (the lead itself still dispatches).
_PHASE1_MEDIA_MAX_BYTES = 5 * 1024 * 1024
# Caps for user-attached (title/license) photos so the inline-base64 attachments can't
# bloat the create_lead insert past the DB gateway limit and fail the whole save.
_MAX_ATTACH_COUNT = 6
_MAX_ATTACH_TOTAL_B64 = 12 * 1024 * 1024


async def _finalize_phase1_media_for_dispatch(
    context: ContextTypes.DEFAULT_TYPE,
    issuer_chat_id: int | str | None,
    known_phone: str | None = None,
) -> list[dict]:
    """Redact phone numbers in any Phase 1 vision media and return descriptors
    that ``_forward_phase1_attached_files_to_targets`` can send to the
    accepting group **without** the issuer ever seeing a copy of the
    censored image.

    Censored bytes are base64-encoded inline on each entry so the data
    survives DB persistence on the lead row and the forward step can rebuild
    a fresh upload with no intermediate Telegram messages. ``known_phone``
    (when supplied) anchors the AI detector on the exact phone we extracted
    from the lead, sharply reducing false positives compared to a generic
    phone-shape pattern.
    """
    if not context.user_data:
        return []
    # User-attached title/license photos (already inline base64 descriptors, no phone
    # redaction needed — they carry no issuer phone). Appended to whatever media the
    # vision path produces so both reach the accepting team.
    extras = [e for e in (context.user_data.get("phase1_extra_attachments") or []) if isinstance(e, dict)]
    cached = context.user_data.get("phase1_attached_files")
    if cached and isinstance(cached, list):
        return list(cached) + extras
    pending: list[dict] = list(context.user_data.get("phase1_pending_media") or [])
    # Fallback: rebuild from an in-memory batch if extraction ran but pending was not stashed.
    if not pending:
        batch = context.user_data.get("phase1_vision_batch") or []
        for item in batch:
            if item.get("kind") == "image":
                pending.append(
                    {
                        "kind": "image",
                        "bytes": item.get("bytes") or b"",
                        "mime": item.get("mime") or "image/jpeg",
                    }
                )
            elif item.get("kind") == "pdf":
                png = await asyncio.to_thread(
                    ai_vision.pdf_first_page_to_png_bytes, item.get("bytes") or b""
                )
                if png:
                    pending.append({"kind": "image", "bytes": png, "mime": "image/png"})
    if not pending:
        return extras

    attached: list[dict] = []
    skipped_unsafe = 0
    skipped_too_big = 0
    for item in pending:
        try:
            img_bytes = item.get("bytes") or b""
            mime = (item.get("mime") or "image/jpeg").lower()
            if not img_bytes:
                continue
            try:
                result = await asyncio.to_thread(
                    ai_vision.redact_phones_in_image_bytes,
                    img_bytes,
                    mime,
                    known_phone,
                )
            except Exception as e:
                logger.warning("Phone redaction call failed: %s", e)
                skipped_unsafe += 1
                continue

            # If the AI call itself failed we cannot prove the image is safe to
            # forward — drop it rather than leak the issuer's phone number.
            if not result.api_ok:
                skipped_unsafe += 1
                continue

            censored_bytes = result.image_bytes or b""
            if not censored_bytes:
                continue
            if len(censored_bytes) > _PHASE1_MEDIA_MAX_BYTES:
                skipped_too_big += 1
                continue

            ext = "jpg" if mime in ("image/jpeg", "image/jpg") else "png"
            attached.append(
                {
                    "type": "photo",
                    "mime": "image/jpeg" if ext == "jpg" else "image/png",
                    "filename": f"censored.{ext}",
                    "data_b64": base64.b64encode(censored_bytes).decode("ascii"),
                }
            )
        except Exception as e:
            logger.warning("Could not stage censored copy for dispatch: %s", e)

    if (skipped_unsafe or skipped_too_big) and issuer_chat_id:
        warn_parts: list[str] = []
        if skipped_unsafe:
            warn_parts.append(
                f"{skipped_unsafe} image(s) couldn't be auto-censored right now"
            )
        if skipped_too_big:
            warn_parts.append(
                f"{skipped_too_big} image(s) were too large to forward safely"
            )
        try:
            await context.bot.send_message(
                chat_id=issuer_chat_id,
                text=(
                    "⚠️ "
                    + " and ".join(warn_parts)
                    + " — unsafe/oversized files were not forwarded so your phone number stays hidden. "
                    "The lead itself was still sent."
                ),
                parse_mode="Markdown",
            )
        except Exception:
            pass

    if attached:
        context.user_data["phase1_attached_files"] = attached
    context.user_data.pop("phase1_pending_media", None)
    return attached + extras


async def _forward_phase1_attached_files_to_targets(
    context: ContextTypes.DEFAULT_TYPE,
    attached_files: list,
    target_chat_id: int | str | None,
) -> None:
    """Forward the Phase 1 files to one **accepting** chat.

    Every image/PDF the issuer sent for parsing rides along to whoever takes the
    lead: the group that accepts the offer, AND the driver who accepts it (they
    are the one doing the delivery, so they need the title/registration shots).
    Never sent during the approval broadcast or driver pick — only on Accept.
    Two payload shapes are supported:

    - ``{"type": "photo|document", "file_id": ...}`` — legacy Telegram
      file-id reference (pre-redaction code path).
    - ``{"type": "photo|document", "data_b64": ..., "filename": ...,
      "mime": ...}`` — inline base64 of the censored bytes, produced by
      ``_finalize_phase1_media_for_dispatch``. We rebuild a fresh upload
      so the issuer never sees the censored copy.
    """
    if not attached_files:
        return
    _group_cid = _parse_chat_id(target_chat_id) if target_chat_id is not None else None
    if not _group_cid:
        return
    # If censored inline payloads exist, do not also send legacy raw ``file_id``
    # photos for the same lead. That old/new mix is what causes a first
    # uncensored image followed by a censored copy.
    has_inline_photo_payload = any(
        isinstance(f, dict)
        and (f.get("type") or "").lower() == "photo"
        and bool(f.get("data_b64"))
        for f in attached_files
    )
    for f in attached_files:
        if not isinstance(f, dict):
            continue
        ftype = (f.get("type") or "").lower()
        data_b64 = f.get("data_b64")
        caption = (f.get("caption") or "").strip()[:1024] or None
        try:
            if data_b64:
                try:
                    blob = base64.b64decode(data_b64)
                except Exception as e:
                    logger.warning("Could not decode censored file payload: %s", e)
                    continue
                if not blob:
                    continue
                filename = f.get("filename") or (
                    "censored.jpg" if ftype == "photo" else "censored.bin"
                )
                upload = InputFile(io.BytesIO(blob), filename=filename)
                if ftype == "photo":
                    await context.bot.send_photo(
                        chat_id=_group_cid, photo=upload, caption=caption
                    )
                else:
                    await context.bot.send_document(
                        chat_id=_group_cid, document=upload, caption=caption
                    )
                continue
            fid = f.get("file_id")
            if not fid:
                continue
            if has_inline_photo_payload and ftype == "photo":
                # Prefer censored inline bytes; skip raw historical photo file_id.
                continue
            if ftype == "photo":
                await context.bot.send_photo(chat_id=_group_cid, photo=fid, caption=caption)
            else:
                await context.bot.send_document(
                    chat_id=_group_cid, document=fid, caption=caption
                )
        except Exception as e:
            logger.warning("Could not forward attached file to group: %s", e)


async def _post_single_group_approval(
    context: ContextTypes.DEFAULT_TYPE,
    lead: dict,
    group: dict,
) -> tuple[int, list[tuple[str, str]]]:
    """Send a short approval request (not full lead HTML) to one group chat; create group_lead_offer row."""
    gid = group.get("id")
    chat_id = _parse_chat_id(group.get("group_telegram_id"))
    if not gid or not chat_id:
        logger.warning(
            "Single-group approval skipped for %s: missing id or group_telegram_id",
            group.get("group_name"),
        )
        return 0, [(group.get("group_name") or str(gid) or "Unknown group", "missing group_telegram_id")]

    reference_id = lead.get("reference_id", "N/A")
    group_offer_message = (
        "🏷 NEW CLIENT — Team approval\n"
        f"📋 Ref ID: `{reference_id}`\n\n"
        "✅ Double-check the tag for mistakes\n"
        "📲 Send tag with Krab Dispatch (@KrabIssuerBot)\n"
        "📋 Copy/paste client phone, address, and delivery time\n\n"
        "Tap **Accept** when ready — the lead creator can notify drivers **after** your team accepts."
    )
    short_lead = _short_uuid(lead["id"])
    short_gid = _short_uuid(gid)
    offer_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Accept", callback_data=f"ag_{short_lead}{short_gid}"),
        InlineKeyboardButton("🔄 Different Team", callback_data=f"dt_{short_lead}{short_gid}"),
    ]])

    db.create_group_lead_offer(lead["id"], gid, group_chat_id=str(chat_id), group_message_id=None)
    failures: list[tuple[str, str]] = []
    try:
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text=group_offer_message,
            parse_mode="Markdown",
            reply_markup=offer_kb,
        )
        db.update_group_lead_offer_message(lead["id"], gid, str(chat_id), msg.message_id)
        return 1, failures
    except RetryAfter as e:
        wait_s = int(getattr(e, "retry_after", 1) or 1)
        logger.warning("Single-group approval rate-limited; retrying in %ss", wait_s)
        await asyncio.sleep(wait_s)
        try:
            msg = await context.bot.send_message(
                chat_id=chat_id,
                text=group_offer_message,
                parse_mode="Markdown",
                reply_markup=offer_kb,
            )
            db.update_group_lead_offer_message(lead["id"], gid, str(chat_id), msg.message_id)
            return 1, failures
        except Exception as e2:
            logger.error("Error sending single-group approval after retry: %s", e2)
            failures.append((group.get("group_name") or str(gid) or "Unknown group", f"{type(e2).__name__}: {e2}"))
            return 0, failures
    except Exception as e:
        logger.error("Error sending single-group approval: %s", e)
        failures.append((group.get("group_name") or str(gid) or "Unknown group", f"{type(e).__name__}: {e}"))
        return 0, failures


async def _post_lead_to_all_groups_for_approval(
    context: ContextTypes.DEFAULT_TYPE,
    lead: dict,
    active_groups: list,
) -> None:
    """Broadcast Accept buttons to every active group (first team to accept wins)."""
    reference_id = lead.get("reference_id", "N/A")
    group_offer_message = (
        "🏷 NEW CLIENT\n"
        f"📋 Ref ID: `{reference_id}`\n\n"
        "✅ Double-check the tag for mistakes\n"
        "📲 Send tag with Krab Dispatch (@KrabIssuerBot)\n"
        "📋 Copy/paste client phone, address, and delivery time"
    )
    short_lead = _short_uuid(lead["id"])
    for g in active_groups:
        gid = g.get("id")
        chat_id = _parse_chat_id(g.get("group_telegram_id"))
        if not gid or not chat_id:
            continue
        short_gid = _short_uuid(gid)
        offer_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Accept", callback_data=f"ag_{short_lead}{short_gid}"),
            InlineKeyboardButton("🔄 Different Team", callback_data=f"dg_{short_lead}{short_gid}"),
        ]])
        db.create_group_lead_offer(lead["id"], gid, group_chat_id=str(chat_id), group_message_id=None)
        try:
            msg = await context.bot.send_message(
                chat_id=chat_id,
                text=group_offer_message,
                parse_mode="Markdown",
                reply_markup=offer_kb,
            )
            db.update_group_lead_offer_message(lead["id"], gid, str(chat_id), msg.message_id)
        except Exception as e:
            logger.warning("All-groups approval send failed for %s: %s", g.get("group_name"), e)


async def _api_lead_auto_dispatch_after_group_accept(
    context: ContextTypes.DEFAULT_TYPE,
    lead: dict,
    winner_group: dict,
    *,
    accepted_by: str | None = None,
) -> None:
    """Website (API-ingested) lead: a team accepted — dispatch drivers now.

    There is no human issuer to hand-pick drivers, so the winning group gets
    the full tag (now sent here, after accept — not at dispatch) and every
    eligible driver gets Accept/Decline immediately.
    """
    reference_id = lead.get("reference_id", "N/A")
    try:
        await _send_full_group_lead_to_chat(
            context,
            winner_group,
            lead,
            html_prefix="<b>✅ Your group claimed this website client</b>\n\n",
            mirror_supervisory=False,
            accepted_by=accepted_by,
        )
    except Exception as e:
        logger.warning("API lead: could not post full lead to winner group: %s", e)
    count, driver_names, fail_reason, driver_scope = await _send_driver_requests_for_group(
        context, lead, winner_group,
    )
    gcid = _parse_chat_id(winner_group.get("group_telegram_id"))
    try:
        if count > 0:
            await context.bot.send_message(
                chat_id=gcid,
                text=f"🚗 Sent to driver(s): **{driver_names}**\nReference: `{reference_id}`",
                parse_mode="Markdown",
            )
        else:
            await context.bot.send_message(
                chat_id=gcid,
                text=_group_accept_notify_fail_text(reference_id, fail_reason, driver_scope),
                parse_mode="Markdown",
            )
    except Exception as e:
        logger.warning("API lead: could not notify winner group about driver dispatch: %s", e)


def _resolve_all_active_driver_ids() -> list[str]:
    try:
        suspended = _get_suspended_driver_ids()
    except Exception:
        suspended = set()
    pool = _get_all_drivers_cached() or []
    return [
        str(d.get("id"))
        for d in pool
        if d and record_is_active(d) and str(d.get("id")) not in suspended
    ]


async def process_pending_api_lead_dispatches(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Poll Supabase for HTTP-ingested (website) leads and dispatch each to
    ALL active groups (full tag post) plus ALL active drivers (Accept/Decline).

    Website leads skip the group-accept step: drivers are asked directly whether
    they can deliver, exactly like the issuer driver flow. The lead is claimed
    (ingest_dispatch_pending -> False) before sending so a slow send can't
    double-fire on the next poll — no lead goes out twice.
    """
    rows = db.list_leads_pending_ingest_dispatch(limit=10)
    if not rows:
        return
    groups = db.get_all_groups()
    active_groups = [g for g in groups if record_is_active(g)]
    if not active_groups:
        logger.warning("API ingest leads pending but no active groups configured")
        return

    for lead in rows:
        lead_id = str(lead.get("id") or "")
        if not lead_id:
            continue
        try:
            # Primary group (owns the lead row until a team accepts) = the one
            # assigned at ingest, or the first active group.
            main_group = db.get_group_by_id(lead.get("group_id")) if lead.get("group_id") else None
            if not main_group or not record_is_active(main_group):
                main_group = active_groups[0]

            # Claim the lead BEFORE sending so the 10s poller can't re-dispatch it.
            db.update_lead(lead_id, {
                "ingest_dispatch_pending": False,
                "awaiting_group_accept": True,
                "group_id": main_group.get("id"),
            })

            # Same flow as issuer broadcast leads: Accept offers to EVERY active
            # group — first team to Accept wins the lead (the other groups'
            # offers flip to "taken"), then drivers are dispatched automatically
            # (see the website-lead branch in handle_accept_group_offer).
            await _post_lead_to_all_groups_for_approval(context, lead, active_groups)
            # Post the informational SUPERVISORY "New Lead" copy alongside the
            # claimable offer above. The temp-tag PDF (and insurance, if opted in)
            # is NOT sent here — it goes out only after a team ACCEPTS the lead,
            # via the website-lead branch in handle_accept_group_offer →
            # _api_lead_auto_dispatch_after_group_accept → _send_full_group_lead_to_chat.
            try:
                await _send_web_order_supervisory_notice(context, lead, active_groups)
            except Exception as e:
                logger.warning("Web-order supervisory notice failed for %s: %s", lead_id, e)
            offers = db.get_group_lead_offers(lead_id) or []
            logger.info(
                "API ingest: lead %s ref %s offered to %d/%d group(s) for first-accept",
                lead_id, lead.get("reference_id"), len(offers), len(active_groups),
            )
            if not offers:
                logger.error(
                    "API ingest: lead %s ref %s reached NO groups — check group chat ids / bot membership",
                    lead_id, lead.get("reference_id"),
                )
        except Exception as e:
            logger.error("process_pending_api_lead_dispatches failed for %s: %s", lead_id, e)


def _extra_info_value(extra_info: str, key: str) -> str:
    """Pull "Key: value" out of the pipe-joined extra_info line."""
    for part in str(extra_info or "").split("|"):
        part = part.strip()
        if part.lower().startswith(key.lower() + ":"):
            return part.split(":", 1)[1].strip()
    return ""


def _fmt_price_usd(price) -> str:
    s = str(price or "").strip()
    if not s:
        return ""
    return s if s.startswith("$") else f"${s}"


def _build_web_order_supervisory_text(lead: dict) -> str:
    """The informational SUPERVISORY 'New Lead' copy for a website order —
    matches the tristatetags format (no claim buttons)."""
    phase1 = _phase1_from_stored_lead(lead)
    order_id = (lead.get("external_order_id") or lead.get("reference_id") or "").strip()
    name = " ".join(w[:1].upper() + w[1:].lower() for w in (phase1.get("name") or "").split())
    reg = ", ".join(x for x in (phase1.get("address"), phase1.get("city_state_zip")) if x)
    delv = ", ".join(x for x in (phase1.get("delivery_address"), phase1.get("delivery_city_state_zip")) if x) or reg
    color = phase1.get("color") or ""
    vehicle = (phase1.get("car") or "").strip()
    if vehicle and color:
        vehicle = f"{vehicle}, {color[:1].upper() + color[1:].lower()}"
    extra = phase1.get("extra_info") or ""
    delivery_method = _extra_info_value(extra, "Delivery method") or "Email Delivery"
    service = _extra_info_value(extra, "Service") or "30-Day NJ Temp Tag"
    lines = [
        "🛡️ SUPERVISORY MESSAGE",
        "🆕 New Lead",
        f"Order #{order_id}" if order_id else None,
        f"Customer: {name}" if name else None,
        f"Phone: {lead.get('phone_number')}" if lead.get("phone_number") else None,
        f"Delivery email: {lead.get('email')}" if lead.get("email") else None,
        f"Delivery method: {delivery_method}",
        f"Registration address: {reg}" if reg else None,
        f"Delivery address: {delv}" if delv else None,
        f"VIN: {phase1.get('vin')}" if phase1.get("vin") else None,
        f"Vehicle: {vehicle}" if vehicle else None,
        f"Insurance: {phase1.get('insurance_company')}" if phase1.get("insurance_company") else None,
        f"Policy #: {phase1.get('insurance_policy_number')}" if phase1.get("insurance_policy_number") else None,
        f"Service: {service}",
        f"Price: {_fmt_price_usd(lead.get('price'))}" if lead.get("price") else None,
        "Informational copy — not claimable from this message.",
        "Move Fast & Serve Client !",
    ]
    return "\n".join(l for l in lines if l)


async def _send_web_order_supervisory_notice(
    context: ContextTypes.DEFAULT_TYPE, lead: dict, groups: list
) -> None:
    """Send the informational supervisory 'New Lead' text to each group chat."""
    text = _build_web_order_supervisory_text(lead)
    seen: set = set()
    for g in groups:
        cid = _parse_chat_id(g.get("group_telegram_id"))
        if not cid or cid in seen:
            continue
        seen.add(cid)
        try:
            await context.bot.send_message(chat_id=cid, text=text)
        except Exception as e:
            logger.warning("Could not send supervisory notice to group %s: %s", cid, e)


def _parse_chat_id(raw: str | int | None) -> int | str | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    # Render/GUI copy-paste mistakes sometimes include a leading '=' (e.g. "= -100123...")
    s = str(raw).strip()
    if not s:
        return None
    s = s.lstrip("=").strip()
    try:
        return int(s)
    except (ValueError, TypeError):
        try:
            f = float(s)
            if f.is_integer():
                return int(f)
        except (ValueError, TypeError):
            pass
        return s


SUPERVISORY_MESSAGE_HEADER = "SUPERVISORY MESSAGE"


def _plain_already_has_supervisory_header(text: str) -> bool:
    """True if body already starts with current or legacy supervisory header (avoid double prefix)."""
    u = (text or "").strip().upper()
    if u.startswith("SUPERVISORY MESSAGE"):
        return True
    return False


def _html_already_has_supervisory_header(text: str) -> bool:
    u = (text or "").strip().upper()
    if u.startswith("<B>SUPERVISORY MESSAGE") or u.startswith("SUPERVISORY MESSAGE"):
        return True
    return False


def _raw_supervisory_tokens(*sources: object) -> list[str]:
    """Split comma-separated supervisory ID strings (env, per-group DB field) into tokens."""
    out: list[str] = []
    for src in sources:
        if src is None:
            continue
        s = str(src).strip()
        if not s:
            continue
        for part in s.split(","):
            p = part.strip()
            if p:
                out.append(p)
    return out


def _prefix_supervisory_message(text: str) -> str:
    """Prefix plaintext / Markdown supervisory DMs."""
    t = (text or "").strip()
    if not t:
        return t
    if _plain_already_has_supervisory_header(t):
        return t
    return f"{SUPERVISORY_MESSAGE_HEADER}\n\n{t}"


def _prefix_supervisory_html(text: str) -> str:
    """Prefix HTML supervisory messages (bold header)."""
    t = (text or "").strip()
    if not t:
        return t
    if _html_already_has_supervisory_header(t):
        return t
    return f"<b>{SUPERVISORY_MESSAGE_HEADER}</b>\n\n{t}"


# Supervisors added from inside /settings, on top of SUPERVISORY_TELEGRAM_ID. The
# env ones are FIXED: they cannot be removed here, so the last door is never locked.
EXTRA_SUPERVISORS_KEY = "extra_supervisor_ids"
_EXTRA_SUP_TTL_SEC = 30
_extra_sup_cache: dict = {"at": 0.0, "rows": []}


def _extra_supervisors(force: bool = False) -> list:
    """[{"id": "123", "label": "Name"}, …] — briefly cached, since every gate calls it."""
    now = time.time()
    if not force and (now - float(_extra_sup_cache.get("at") or 0)) < _EXTRA_SUP_TTL_SEC:
        return list(_extra_sup_cache.get("rows") or [])
    rows: list = []
    try:
        raw = db.get_setting(EXTRA_SUPERVISORS_KEY)
        if raw:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                for r in parsed:
                    if isinstance(r, dict) and str(r.get("id") or "").strip():
                        rows.append({"id": str(r["id"]).strip(),
                                     "label": str(r.get("label") or "").strip()})
    except Exception as e:
        logger.warning("extra supervisors read failed: %s", e)
        rows = list(_extra_sup_cache.get("rows") or [])
    _extra_sup_cache["at"] = now
    _extra_sup_cache["rows"] = rows
    return list(rows)


def _save_extra_supervisors(rows: list) -> bool:
    ok = False
    try:
        ok = bool(db.set_setting(EXTRA_SUPERVISORS_KEY, json.dumps(rows or [])))
    except Exception as e:
        logger.warning("extra supervisors write failed: %s", e)
    _extra_sup_cache["at"] = 0.0            # next read is live, whatever happened
    return ok


def _add_extra_supervisor(chat_id: str, label: str = "") -> bool:
    rows = _extra_supervisors(force=True)
    key = _norm_chat_id(chat_id)
    for r in rows:
        if _norm_chat_id(r.get("id")) == key:
            r["label"] = label or r.get("label") or ""
            return _save_extra_supervisors(rows)
    rows.append({"id": str(chat_id).strip(), "label": (label or "").strip()})
    return _save_extra_supervisors(rows)


def _remove_extra_supervisor(chat_id: str) -> bool:
    key = _norm_chat_id(chat_id)
    rows = [r for r in _extra_supervisors(force=True) if _norm_chat_id(r.get("id")) != key]
    return _save_extra_supervisors(rows)


def _global_supervisory_chat_ids() -> list:
    """Chat IDs from SUPERVISORY_TELEGRAM_ID (comma-separated in env)."""
    out: list = []
    seen: set = set()
    for tok in _raw_supervisory_tokens(Config.SUPERVISORY_TELEGRAM_ID):
        cid = _parse_chat_id(tok)
        if cid is None:
            continue
        key = _norm_chat_id(cid)
        if key is not None and key in seen:
            continue
        if key is not None:
            seen.add(key)
        out.append(cid)
    return out


def _user_is_global_supervisor(user_id) -> bool:
    """True for SUPERVISORY_TELEGRAM_ID *or* anyone added under /settings."""
    target = _norm_chat_id(user_id)
    if target is None:
        return False
    for cid in _global_supervisory_chat_ids():
        if _norm_chat_id(cid) == target:
            return True
    try:
        for row in _extra_supervisors():
            if _norm_chat_id(row.get("id")) == target:
                return True
    except Exception:
        pass                                # env supervisors still get in
    return False


# Persistent runtime toggle: when True (default), driver-facing DMs replace the
# client phone with a OneTimeSecret link (clientsphonenumber.com); when False,
# drivers see the raw phone_number stored on the lead.
DRIVERBLOCK_SETTING_KEY = "driverblock_phone_redaction"


def _driverblock_enabled() -> bool:
    """Read current driver phone-redaction state. Defaults to True (redacted)."""
    try:
        v = db.get_setting(DRIVERBLOCK_SETTING_KEY)
    except Exception as e:
        logger.warning("Could not read %s: %s", DRIVERBLOCK_SETTING_KEY, e)
        return True
    if v is None:
        return True
    return str(v).strip().lower() in ("1", "true", "on", "yes", "y")


def _set_driverblock_enabled(value: bool) -> bool:
    """Persist new driver phone-redaction state. Returns True on success."""
    try:
        return bool(db.set_setting(DRIVERBLOCK_SETTING_KEY, "on" if value else "off"))
    except Exception as e:
        logger.warning("Could not write %s: %s", DRIVERBLOCK_SETTING_KEY, e)
        return False


def _driver_phone_display(lead: dict) -> str:
    """Phone string used in driver-facing DMs.

    Honors ``/driverblock`` toggle: when redaction is ON, prefer ``encrypted_link``
    (OneTimeSecret URL); when OFF, prefer the raw stored ``phone_number``.
    Falls back to whichever value is present if the preferred one is missing.
    """
    if not isinstance(lead, dict):
        return "N/A"
    raw = (lead.get("phone_number") or "").strip()
    link = (lead.get("encrypted_link") or "").strip()
    if _driverblock_enabled():
        return link or raw or "N/A"
    return raw or link or "N/A"


def _supervisory_delivery_chat_ids(group_supervisory_raw: object) -> list:
    """Per-group supervisory token(s) + global SUPERVISORY_TELEGRAM_ID token(s), deduped."""
    seen: set = set()
    out: list = []
    for raw in _raw_supervisory_tokens(group_supervisory_raw, Config.SUPERVISORY_TELEGRAM_ID):
        cid = _parse_chat_id(raw)
        if cid is None:
            continue
        if isinstance(cid, int):
            dedupe_key = cid
        else:
            try:
                dedupe_key = int(str(cid).strip().lstrip("=").split(".", 1)[0])
            except (ValueError, TypeError):
                dedupe_key = str(cid)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        out.append(cid)
    return out


def _new_lead_supervisory_notice_text(
    reference_id: str,
    group_display: str,
    driver_display: str,
    issuer_display: str,
    *,
    client_name: str = "—",
    source_label: Optional[str] = None,
    include_lead_issuer: bool = True,
    driver_count: Optional[int] = None,
) -> str:
    """Body when an issuer completes sending a lead.

    ``include_lead_issuer``: Telegram @ of submitter — **only** for group/env supervisory chats,
    not for ``st_telegram_id`` (see ``_finish_lead_send``).

    ``driver_count``: number of drivers notified (for Send Mode). If omitted, inferred from
    comma-separated ``driver_names``.

    ``source_label``: lead/contact source (e.g. Facebook); shown as — if not set yet.
    """
    def one_line(s: str) -> str:
        return (s or "").replace("\n", " ").replace("\r", " ").strip() or "N/A"

    ref = one_line(str(reference_id))
    gn = (group_display or "").strip() or "N/A"
    dn = (driver_display or "").strip() or "N/A"
    cn = one_line(client_name)
    if source_label and str(source_label).strip():
        src = one_line(str(source_label))
    else:
        src = "—"
    if driver_count is None:
        parts = [p.strip() for p in re.split(r",|•", re.sub(r"<[^>]+>", "", dn)) if p.strip()]
        n = len(parts)
    else:
        n = int(driver_count)
    if n <= 0:
        send_mode = "Send Mode: —"
    elif n == 1:
        send_mode = "Send Mode: 1 Driver"
    else:
        send_mode = "Send Mode: Multiple Drivers"
    by_line = (issuer_display or "").strip() or "Unknown"
    lines = [
        "📬 New lead sent",
        "",
        f"Reference: {ref}",
        f"Group: {gn}",
        f"Driver(s): {dn}",
        send_mode,
        f"Client name: {cn}",
        f"Source: {src}",
    ]
    if include_lead_issuer:
        lines.append(f"Lead issued by: {by_line}")
    return "\n".join(lines)


def _telegram_user_link_html(user_id: object, label: str) -> str:
    """Clickable Telegram user mention for supervisory lines."""
    uid = str(user_id or "").strip()
    txt = html.escape((label or "").strip() or uid or "Unknown", quote=False)
    if uid.lstrip("-").isdigit():
        return f'<a href="tg://user?id={uid}">{txt}</a>'
    return txt


def _telegram_chat_link_html(chat_id: object, label: str) -> str:
    """Best-effort chat link for group/supergroup IDs."""
    cid = str(chat_id or "").strip()
    txt = html.escape((label or "").strip() or cid or "N/A", quote=False)
    if not cid:
        return txt
    if cid.startswith("-100") and cid[4:].isdigit():
        return f'<a href="https://t.me/c/{cid[4:]}">{txt}</a>'
    if cid.lstrip("-").isdigit():
        return f'<a href="tg://user?id={cid}">{txt}</a>'
    return txt


def _issuer_display_html_from_lead(lead: dict) -> str:
    """Lead issuer line with username first, then Telegram-ID fallback."""
    un = (lead.get("telegram_username") or "").strip()
    uid = lead.get("user_id")
    if un and un.lower() != "unknown":
        label = un if un.startswith("@") else f"@{un}"
        return _telegram_user_link_html(uid, label)
    if uid is not None:
        return _telegram_user_link_html(uid, str(uid))
    return "Unknown"


_TELEGRAM_FILE_API_MARKER = "https://api.telegram.org/file/bot"


# Where a driver can upload a receipt from a phone. The bytes go straight into the
# database; nothing about the link expires, unlike a Telegram file URL.
RECEIPT_PORTAL_BASE = (
    os.getenv("RECEIPT_PORTAL_BASE") or "https://tristatetags.com/backend"
).strip().rstrip("/")
_RECEIPT_LINK_SECRET = (
    os.getenv("RECEIPT_LINK_SECRET") or (Config.SUPABASE_KEY or "") or "krab-receipt-portal"
).strip()


def receipt_portal_url(lead_id) -> str:
    """The upload page for one lead. Must match admin_dashboard.receipt_token —
    same secret, same message, or the page will not recognise the link."""
    mac = hmac.new(
        _RECEIPT_LINK_SECRET.encode("utf-8"),
        f"receipt:{lead_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:24]
    return f"{RECEIPT_PORTAL_BASE}/r/{lead_id}.{mac}"


async def _store_receipt_bytes(lead_id, data: bytes, *, content_type="image/jpeg",
                               reference_id="", driver_id="") -> bool:
    """Mirror a Telegram-sent receipt into the database, so it outlives the file."""
    if not data:
        return False
    try:
        got = await asyncio.to_thread(
            db.save_receipt_file, str(lead_id), data=data, content_type=content_type,
            reference_id=str(reference_id or ""), driver_id=str(driver_id or ""),
            source="telegram")
        return bool(got)
    except Exception as e:
        logger.warning("could not store receipt bytes for %s: %s", lead_id, e)
        return False


def _normalize_receipt_image_url(url: str) -> str:
    """Fix doubled Telegram file CDN prefix (e.g. bot path pasted into another bot URL)."""
    u = (url or "").strip()
    if not u or _TELEGRAM_FILE_API_MARKER not in u:
        return u
    chunks = [c for c in u.split(_TELEGRAM_FILE_API_MARKER) if c]
    if len(chunks) >= 2:
        return _TELEGRAM_FILE_API_MARKER + chunks[-1]
    return u


def _telegram_download_url_from_file_path(file_path: str) -> str:
    """Build a single correct Telegram file URL; ``file_path`` is usually ``photos/file_N.jpg``."""
    fp = (file_path or "").strip()
    if not fp:
        return ""
    if fp.startswith("https://") or fp.startswith("http://"):
        return _normalize_receipt_image_url(fp)
    tok = (Config.TELEGRAM_BOT_TOKEN or "").strip()
    return f"{_TELEGRAM_FILE_API_MARKER}{tok}/{fp.lstrip('/')}"


async def _notify_initiator_and_supervisor(context: ContextTypes.DEFAULT_TYPE, lead: dict, text: str) -> None:
    """Send a notification to the lead initiator and global supervisor(s) (if configured)."""
    initiator_id = lead.get("user_id")
    sup_norms = {_norm_chat_id(x) for x in _global_supervisory_chat_ids()}
    init_norm = _norm_chat_id(initiator_id) if initiator_id is not None else None
    if initiator_id is not None and init_norm not in sup_norms:
        try:
            await context.bot.send_message(chat_id=int(initiator_id), text=text, parse_mode="Markdown")
        except Exception as e:
            logger.warning("Could not notify initiator %s: %s", initiator_id, e)
    sup_text = _prefix_supervisory_message(text)
    for sup_cid in _global_supervisory_chat_ids():
        try:
            await context.bot.send_message(chat_id=sup_cid, text=sup_text, parse_mode="Markdown")
        except Exception as e:
            logger.warning("Could not notify supervisor %s: %s", sup_cid, e)


async def _notify_initiator_lead_accepted_summary(
    context: ContextTypes.DEFAULT_TYPE,
    lead: dict,
    *,
    accepting_driver_name: str,
) -> None:
    """One DM to the lead adder when a driver accepts — reference, group, driver, branded footer."""
    initiator_id = lead.get("user_id")
    if initiator_id is None:
        return
    try:
        cid = int(initiator_id)
    except (TypeError, ValueError):
        logger.warning("Invalid lead user_id for initiator summary: %s", initiator_id)
        return
    lid = lead.get("id")
    lead_row = db.get_lead_by_id(str(lid)) if lid else lead
    ref = (lead_row.get("reference_id") or "N/A").strip() or "N/A"
    group_label = _group_display_name_from_lead(lead_row) or "N/A"
    dn = (accepting_driver_name or "Driver").strip() or "Driver"
    # Deliberately NAME ONLY. A driver's phone and email live in /settings and nowhere
    # near a lead — this notice can be forwarded, so keep their details out of it.
    text = (
        "Accepted! ✅ Lead 📈Notification🔔\n"
        "We start serving the client now\n\n"
        f"📋Reference🧾: {ref}\n"
        f"🙋Group Accepted✅: {group_label}\n"
        f"🙋‍♀️Driver Accepted✅: {dn}\n\n"
        "🏁Automated🏎️Automotive💨"
    )
    # Dispatcher-side escape hatch: reassign the driver right from the summary.
    kb = None
    if lead_row.get("id"):
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "🔄 Reassign driver", callback_data=f"reassign_lead_{lead_row.get('id')}"
            )
        ]])
    try:
        await context.bot.send_message(chat_id=cid, text=text, parse_mode=None, reply_markup=kb)
    except Exception as e:
        logger.warning("Could not send initiator lead summary to %s: %s", cid, e)


def _driver_offer_message_text(lead: dict) -> str:
    """The accept/decline offer DM body (shared by dispatch and reassign)."""
    reference_id = lead.get("reference_id", "N/A")
    extra_safe = _sanitize_phones_for_send(lead.get("extra_info") or "")
    spec = _lead_driver_note(lead)
    msg = (
        f"👋Hi! New client 💸 available📈❗️\n\n"
        f"📍 Delivery (City, State, Zip): {lead.get('delivery_details', '')}\n"
        f"📋 Reference ID: `{reference_id}`\n"
        f" Delivery Time 🏷️: {extra_safe}\n"
        f"Please have Car, Driver License, and Laser Printer Ready✅\n\n"
        f"_Tap Accept below, or just reply *accept*._"
    )
    if spec:
        msg += f"\n\n📝 Special request (driver): {_sanitize_phones_for_send(spec)}"
    return msg


async def _send_driver_requests_for_group(
    context: ContextTypes.DEFAULT_TYPE,
    lead: dict,
    group: dict,
    exclude_driver_id: str | None = None,
) -> tuple[int, str, str | None, str]:
    """Send accept/decline requests after a group claims a broadcast lead.

    Uses drivers linked in ``group_drivers`` when present; otherwise the same global pool
    as issuer driver pick ("Drivers work for all groups") — no admin Group↔Driver rows required.

    Returns (assigned_count, driver_names, reason_code_or_none, scope) where scope is
    ``group_linked`` or ``all_drivers``.
    """
    group_id = group.get("id")
    group_label = group.get("group_name") or "this group"
    if not group_id:
        return (0, "", "no_drivers_linked", "group_linked")

    linked_rows = db.get_group_driver_rows_for_group(group_id)
    if linked_rows:
        rows = linked_rows
        scope = "group_linked"
    else:
        rows = [d for d in _get_all_drivers_cached() if d]
        scope = "all_drivers"
        if rows:
            logger.info(
                "Group '%s': no Group↔Driver assignments; notifying all active drivers (issuer-style pool).",
                group_label,
            )

    suspended = _get_suspended_driver_ids()
    active_rows = [d for d in rows if d and record_is_active(d)]
    if not active_rows:
        return (0, "", "all_inactive", scope)

    selected_drivers = [d for d in active_rows if str(d.get("id")) not in suspended]
    if not selected_drivers:
        return (0, "", "all_suspended", scope)

    without_tg = [d for d in selected_drivers if _parse_chat_id(d.get("driver_telegram_id")) is None]
    if without_tg and len(without_tg) == len(selected_drivers):
        names = ", ".join(d.get("driver_name", "?") for d in selected_drivers)
        logger.warning(
            "Group %s: %s driver(s) have no parseable Telegram ID",
            group_label,
            len(without_tg),
        )
        return (0, names, "missing_telegram", scope)

    driver_request_message = _driver_offer_message_text(lead)
    accept_keyboard = _keyboard_lead_accept_decline(str(lead["id"]))
    assigned_count = 0
    for driver in selected_drivers:
        if exclude_driver_id and str(driver.get("id")) == str(exclude_driver_id):
            continue
        cid = _parse_chat_id(driver.get("driver_telegram_id"))
        if not cid:
            continue
        try:
            db.create_lead_assignment(lead["id"], driver["id"], group_id)
            await context.bot.send_message(chat_id=cid, text=driver_request_message, parse_mode="Markdown", reply_markup=accept_keyboard)
            assigned_count += 1
        except Exception as e:
            logger.error("Error sending driver request to %s: %s", driver.get("driver_name"), e)
    driver_names = ", ".join(d.get("driver_name", "?") for d in selected_drivers)
    if assigned_count == 0:
        return (0, driver_names, "send_failed", scope)
    return (assigned_count, driver_names, None, scope)


def _group_accept_notify_fail_text(
    reference_id: str, reason: str | None, scope: str = "group_linked",
) -> str:
    """User-visible explanation when group accept did not reach any driver."""
    ref = f"Reference: `{reference_id}`"
    global_pool = scope == "all_drivers"
    if reason == "no_drivers_linked":
        return (
            "⚠️ **No drivers are linked to this group** in the admin dashboard.\n\n"
            "Add drivers under Group ↔ Driver assignments for this team.\n\n"
            + ref
        )
    if reason == "all_inactive":
        if global_pool:
            return (
                "⚠️ **No active drivers** in the system (admin).\n\n"
                "Add or re-activate drivers.\n\n"
                + ref
            )
        return (
            "⚠️ **All drivers linked to this group are inactive** in admin.\n\n"
            "Re-activate a driver or fix assignments.\n\n"
            + ref
        )
    if reason == "all_suspended":
        if global_pool:
            return (
                "⚠️ **Every driver is suspended** (pending receipts penalty).\n\n"
                "Resolve strikes in admin.\n\n"
                + ref
            )
        return (
            "⚠️ **All drivers in this group are suspended** (pending receipts penalty).\n\n"
            "Resolve strikes in admin or notify drivers another way.\n\n"
            + ref
        )
    if reason == "missing_telegram":
        if global_pool:
            return (
                "⚠️ **No driver has a valid Telegram user ID** in admin.\n\n"
                "Set each driver’s numeric Telegram ID so the bot can DM them.\n\n"
                + ref
            )
        return (
            "⚠️ **Drivers in this group have no valid Telegram user ID** in admin.\n\n"
            "Set each driver’s Telegram ID (numeric) so the bot can DM them.\n\n"
            + ref
        )
    if reason == "send_failed":
        return (
            "⚠️ **Could not DM any driver** (Telegram blocked or wrong chat ID).\n\n"
            "Drivers must open a private chat with the bot and press **Start**.\n\n"
            + ref
        )
    return (
        "⚠️ **No drivers could be notified** for this group.\n\n"
        + ref
    )


def generate_reference_id() -> str:
    """Generate a unique reference ID for lead tracking."""
    # Generate 8-character alphanumeric ID
    alphabet = string.ascii_uppercase + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(8))


# A reference id as generate_reference_id() makes them: 8 uppercase alphanumerics.
# The looser second form covers ids issued before that.
_REF_ID_RE = re.compile(r"\b([A-Z0-9]{8}|[A-Z]{2}\d{5,8})\b")
# "A B C 1 2 3 4 5" — a phone transcribing someone reading an id out loud.
_REF_SPACED_RE = re.compile(r"\b(?:[A-Z0-9]\s+){5,}[A-Z0-9]\b")


def extract_reference_id(text: str, exists=None) -> str:
    """The reference id inside a message, or the message itself.

    Drivers send "ref ABC12345", "Reference: ABC12345", "here it is ABC12345"
    and "ABC12345 thanks" — all of which used to be handed whole to an exact
    database match and come back "not found".

    ``exists`` confirms a candidate before it is preferred, and the raw message
    stays the fallback, so this can only turn a failed lookup into a working one.
    """
    raw = (text or "").strip()
    up = raw.upper()
    candidates = []
    for m in _REF_SPACED_RE.finditer(up):
        joined = re.sub(r"\s+", "", m.group(0))
        if joined not in candidates:
            candidates.append(joined)
    for m in _REF_ID_RE.finditer(up):
        if m.group(1) not in candidates:
            candidates.append(m.group(1))
    if exists is None:
        return candidates[0] if candidates else up
    for c in candidates:
        try:
            if exists(c):
                return c
        except Exception:
            break
    return up


def parse_phase1_structured(message_text: str) -> dict:
    """
    Parse Phase 1 structured input into individual fields.
    
    Expected structure (one item per line):
      1) Name
      2) Address
      3) City, State, ZIP
      4) Delivery address
      5) Delivery city, State, ZIP
      6) VIN
      7) Car (year, make, model)
      8) Color
      9) Insurance company
      10) Insurance policy number
      11) Extra info
    """
    lines = [line.strip() for line in message_text.splitlines() if line.strip()]
    
    def get_line(idx: int) -> str:
        return lines[idx] if idx < len(lines) else ""
    
    def field(idx: int) -> str:
        """One line of the block — blank if the model apologised on it instead of
        answering. Dropping just that line keeps the rest of a good extraction."""
        v = get_line(idx)
        return "" if _value_is_refusal(v) else v

    name = field(0)
    address = field(1)
    city_state_zip = field(2)
    delivery_address = field(3)
    delivery_city_state_zip = field(4)
    vin = field(5)
    car = field(6)
    color = ai_vision.normalize_phase1_color(field(7))
    insurance_company = field(8)
    insurance_policy_number = field(9)
    extra_info = field(10)
    
    # Vehicle details (for supervisor / group high-level view)
    vehicle_lines = [
        name,
        address,
        city_state_zip,
        vin,
        car,
        color,
        insurance_company,
        insurance_policy_number,
        extra_info,
    ]
    vehicle_details = "\n".join([l for l in vehicle_lines if l])
    
    # Delivery details (for driver)
    delivery_lines = [
        delivery_address,
        delivery_city_state_zip,
    ]
    delivery_details = "\n".join([l for l in delivery_lines if l])
    
    return {
        "name": name,
        "address": address,
        "city_state_zip": city_state_zip,
        "delivery_address": delivery_address,
        "delivery_city_state_zip": delivery_city_state_zip,
        "vin": vin,
        "car": car,
        "color": color,
        "insurance_company": insurance_company,
        "insurance_policy_number": insurance_policy_number,
        "extra_info": extra_info,
        "vehicle_details": vehicle_details,
        "delivery_details": delivery_details,
        "raw_text": message_text,
    }


# ── Splitting a whole address into street + city/ST/ZIP ─────────────────────
# The card keeps the street line and the city/state/ZIP line in SEPARATE fields, but
# people type (and dictate) an address as one string: "123 Main St, Newark NJ 07102".
# Without this the entire string landed in the street field and city/ST/ZIP stayed "-".
_US_STATE_ABBR = frozenset("""
AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV
NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC PR VI GU AS MP
""".split())
# Full state names (incl. the multi-word ones) → abbreviation, so a spoken
# "…Newark New Jersey 07102" still splits correctly.
_US_STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI",
    "wyoming": "WY", "district of columbia": "DC", "puerto rico": "PR",
}
_ZIP_RE = re.compile(r"^\d{5}(?:-\d{4})?$")
# Tokens that belong to the STREET half — the city walk stops when it meets one, so
# "Ocean Ave" is never mistaken for a city name.
# Deliberately NO bare compass directions here — they are handled separately below,
# because a compass word can belong to EITHER half ("Pennsylvania Ave NW" is street,
# "North Bergen" is city).
# NOTE: no "park"/"heights"/"square"/"gardens" — those are common in CITY names
# (Cliffside Park, Hasbrouck Heights), and mis-stopping there would split wrongly.
_STREET_TOKEN_RE = re.compile(
    r"^(?:st|street|ave|avenue|av|rd|road|blvd|boulevard|dr|drive|ln|lane|way|ct|court|"
    r"pl|place|ter|terrace|cir|circle|pkwy|parkway|hwy|highway|route|rte|apt|apartment|"
    r"suite|ste|unit|fl|floor|rm|room|box|po|turnpike|tpke|pike|expressway|expy|"
    r"freeway|fwy|plaza|plz|trail|trl|crossing|xing|bypass|byp|extension|ext|loop|"
    r"alley|aly|plaza)$",
    re.I,
)
# An ABBREVIATED compass is practically always a street directional ("1600
# Pennsylvania Ave NW"), never the start of a city name.
_COMPASS_ABBR = frozenset({"n", "s", "e", "w", "ne", "nw", "se", "sw"})
# A SPELLED-OUT compass usually starts a city ("North Bergen", "East Orange") — unless
# it trails a numbered route ("500 Route 46 West"), which these two tests detect.
_COMPASS_WORD = frozenset({"north", "south", "east", "west", "northeast",
                           "northwest", "southeast", "southwest"})
_ROUTE_WORD_RE = re.compile(r"^(?:route|rte|rt|highway|hwy|us|interstate|i|county|state)$", re.I)


def _csz_tail_len(tokens: list) -> int:
    """How many trailing tokens of `tokens` form a 'City ST ZIP' tail (0 = none).

    Walks backwards: optional ZIP, then a state (abbreviation or full name), then up
    to four city words — stopping at anything with a digit or a street-type word."""
    i = len(tokens) - 1
    if i < 0:
        return 0
    saw_zip = False
    if _ZIP_RE.match(tokens[i].strip(",.")):
        saw_zip = True
        i -= 1
    if i < 0:
        return 0
    # state: two-letter abbreviation, or a one/two-word full name
    tok = tokens[i].strip(",.").upper()
    two = " ".join(tokens[max(i - 1, 0):i + 1]).strip(",.").lower()
    if len(tok) == 2 and tok in _US_STATE_ABBR:
        i -= 1
    elif two in _US_STATE_NAMES and i >= 1:
        i -= 2
    elif tokens[i].strip(",.").lower() in _US_STATE_NAMES:
        i -= 1
    elif not saw_zip:
        return 0                      # neither state nor ZIP → no tail at all
    # city words (bounded, and a comma right before the city ends the walk)
    city_start = i + 1
    words = 0
    while i >= 0 and words < 4:
        raw = tokens[i]
        word = raw.strip(",.")
        if not word or any(ch.isdigit() for ch in word):
            break
        if _STREET_TOKEN_RE.match(word):
            break
        low = word.lower()
        if low in _COMPASS_ABBR:
            break                     # "…Ave NW" — the directional belongs to the street
        if low in _COMPASS_WORD:
            prev = tokens[i - 1].strip(",.") if i >= 1 else ""
            prev2 = tokens[i - 2].strip(",.") if i >= 2 else ""
            # "500 Route 46 West" → street; "…Apt 2 North Bergen" → city
            if any(ch.isdigit() for ch in prev) and _ROUTE_WORD_RE.match(prev2):
                break
        city_start = i
        words += 1
        if raw.endswith(","):         # "…Apt 3B, Fort Lee NJ" — comma is the boundary
            break
        i -= 1
    return len(tokens) - city_start


def _split_street_and_csz(value: str) -> tuple:
    """Split one typed address into (street, city_state_zip).

    '123 Main St, Newark NJ 07102' → ('123 Main St', 'Newark NJ 07102')
    'Newark NJ 07102'              → ('', 'Newark NJ 07102')
    '123 Main St'                  → ('123 Main St', '')
    Returns ('', '') when there is nothing to split."""
    v = " ".join((value or "").split()).strip().strip(",")
    if not v:
        return ("", "")
    # A comma is the strongest boundary: take the EARLIEST one whose tail is a
    # complete city/ST/ZIP, so "88 Ocean Ave Apt 3B, Fort Lee, NJ 07024" keeps the
    # apartment on the street line.
    if "," in v:
        parts = v.split(",")
        for k in range(len(parts) - 1):
            tail = ",".join(parts[k + 1:]).strip()
            street = ",".join(parts[: k + 1]).strip().strip(",")
            tail_toks = tail.replace(",", " ").split()
            if tail and tail_toks and _csz_tail_len(tail_toks) == len(tail_toks):
                return (street, " ".join(tail.split()))
    toks = v.split()
    n = _csz_tail_len(toks)
    if not n:
        return (v, "")
    cut = len(toks) - n
    # A street of just a house number means the walk ate the street name too
    # ("123 Broadway New York NY 10001"). Give one word back.
    if cut == 1 and any(ch.isdigit() for ch in toks[0]) and n > 1:
        cut += 1
    street = " ".join(toks[:cut]).strip().strip(",")
    csz = " ".join(toks[cut:]).strip().strip(",")
    return (street, csz)


# Which city/ST/ZIP field pairs with which street field.
_ADDR_TO_CSZ_EK = {"addr": "csz", "daddr": "dcsz"}


def _expand_address_pair(edit_key: str, value: str) -> list:
    """Turn one (street-field, whole address) edit into the one or two edits it really
    is, so 'address 123 Main St, Newark NJ 07102' fills BOTH the street line and the
    city/ST/ZIP line — and both get reported as updated."""
    csz_ek = _ADDR_TO_CSZ_EK.get(edit_key)
    if not csz_ek:
        return [(edit_key, value)]
    street, csz = _split_street_and_csz(value)
    if not csz:
        return [(edit_key, value)]
    if not street:                      # the value was ONLY a city/ST/ZIP
        return [(csz_ek, csz)]
    return [(edit_key, street), (csz_ek, csz)]


async def _ai_split_addresses_if_needed(state_data: dict) -> list:
    """Last resort for an address the deterministic splitter couldn't divide: ask the
    AI to separate street from city/ST/ZIP. Only runs when a street field holds several
    words while its city/ST/ZIP partner is still empty, so the normal case costs
    nothing. Returns the labels that changed."""
    if not Config.is_ai_vision_configured():
        return []
    changed: list = []
    for street_ek, csz_ek in _ADDR_TO_CSZ_EK.items():
        street_key = _INLINE_EK_STATE_KEY[street_ek]
        csz_key = _INLINE_EK_STATE_KEY[csz_ek]
        street_val = str(state_data.get(street_key) or "").strip()
        csz_val = str(state_data.get(csz_key) or "").strip()
        if csz_val and csz_val != "-":
            continue                        # already split
        if len(street_val.split()) < 4 or street_val == "-":
            continue                        # too short to hold a city/ST/ZIP as well
        try:
            res = await asyncio.to_thread(ai_vision.split_address, street_val)
        except Exception as e:
            logger.warning("AI address split failed: %s", e)
            continue
        if not isinstance(res, dict):
            continue
        street, csz = res.get("street") or "", res.get("city_state_zip") or ""
        if not csz:
            continue
        _apply_single_phase1_edit(state_data, csz_ek, csz)
        changed.append(_INLINE_EDIT_KEY_LABEL[csz_ek])
        if street and street != street_val:
            _apply_single_phase1_edit(state_data, street_ek, street)
            changed.append(_INLINE_EDIT_KEY_LABEL[street_ek])
    if changed:
        _apply_single_address_as_both(state_data)
    return changed


def _apply_single_address_as_both(state_data: dict) -> None:
    """When only one address is provided (registration or delivery), use it for both."""
    def _has(v: str) -> bool:
        return bool(v and str(v).strip() and str(v).strip() != "-")
    addr = (state_data.get("address") or "").strip()
    csz = (state_data.get("city_state_zip") or "").strip()
    daddr = (state_data.get("delivery_address") or "").strip()
    dcsz = (state_data.get("delivery_city_state_zip") or "").strip()
    has_reg = _has(addr) or _has(csz)
    has_del = _has(daddr) or _has(dcsz)
    if has_reg and not has_del:
        state_data["delivery_address"] = addr or "-"
        state_data["delivery_city_state_zip"] = csz or "-"
    elif has_del and not has_reg:
        state_data["address"] = daddr or "-"
        state_data["city_state_zip"] = dcsz or "-"
    _clean_vin_and_car(state_data)


# Exactly 17 alphanumeric: the only valid VIN structure. Never cut or truncate.
VIN_PATTERN = re.compile(r"\b[A-Za-z0-9]{17}\b")


def _extract_vin_17(text: str) -> Optional[str]:
    """Return the first 17-character alphanumeric VIN found in text, or None. No truncation."""
    if not text:
        return None
    m = VIN_PATTERN.search(text)
    return m.group(0) if m else None


def _normalize_car_for_compare(car: str) -> str:
    """Normalize car string for comparison (lower, single spaces)."""
    return " ".join((car or "").lower().split())


def _vin_check_after_phase1(state_data: dict) -> tuple:
    """
    Run VIN lookup when we have a 17-char VIN. Uses provider from Config (.env).
    Returns:
      (alert_msg, conflict) where
      alert_msg: optional warning to show before Phase 2 (no result / not 17).
      conflict: (api_car_line, stated_car) if VIN returned different car; else None.
    """
    vin = (state_data.get("vin") or "").strip()
    if not vin or vin == "-" or len(vin) != 17:
        return ("⚠️ VIN not 17 characters; car not verified.", None)
    if not Config.is_vin_lookup_configured():
        return (None, None)
    result = vin_lookup.vin_lookup(
        vin,
        provider=Config.VIN_PROVIDER,
        api_key=Config.API_NINJAS_API_KEY,
    )
    if not result:
        return ("⚠️ VIN returned no result. Ensure it's 17 characters.", None)
    api_car = (result.get("car_line") or "").strip()
    stated = (state_data.get("car") or "").strip()
    if not api_car:
        return (None, None)
    if _normalize_car_for_compare(api_car) == _normalize_car_for_compare(stated):
        return (None, None)
    return (None, (api_car, stated))


def _vin_choice_keyboard(api_car: str, stated_car: str) -> InlineKeyboardMarkup:
    """Yes takes the DMV decode; No keeps the vehicle already on the card.

    Retyping no longer needs a button of its own — saying or typing "retype vin"
    still reaches the same handler, which stays registered."""
    _ = api_car, stated_car  # context shown in the message body above the buttons
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes", callback_data="vin_use"),
        InlineKeyboardButton("❌ No", callback_data="vin_keep"),
    ]])

def _extract_email_and_dl_from_text(text: str) -> tuple[Optional[str], Optional[str]]:
    """Best-effort email + driver-license-id extraction from freeform text.

    - Email: any RFC-5322-ish token; first match wins; lower-cased.
    - DL ID: matches labelled lines ``DriverLicenseID:``, ``Driver License:``,
      ``DL:``, ``DAQ:``, ``DMV ID:``. Returns raw value uppercased (alphanumeric/-/space).
    """
    if not text:
        return (None, None)
    # Email
    email_match = re.search(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", text)
    email_val: Optional[str] = None
    if email_match:
        email_val = ai_vision.normalize_email(email_match.group(0)) or None
    # Driver-license ID via labels
    dl_val: Optional[str] = None
    dl_label_pat = re.compile(
        r"^\s*(?:driver\s*license\s*id|driverlicenseid|driver\s*license|dl\s*id|dl|daq|dmv\s*id|license\s*id)\s*[:#-]\s*(.+?)\s*$",
        re.IGNORECASE | re.MULTILINE,
    )
    m = dl_label_pat.search(text)
    if m:
        dl_val = ai_vision.normalize_driver_license_id(m.group(1)) or None
    return (email_val, dl_val)


def _extract_phone_price_notes_from_text(text: str) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Return (phone, price, issuer_note, driver_note).
       Stops phone from being scraped across lines; picks a line that looks like a phone.
    """
    # ---- 1. split into lines ----
    lines = [line.strip() for line in text.splitlines()]

    # ---- 2. find a phone line ----
    phone = None
    phone_line_index = None
    # pattern for a line that is likely a phone (starts with +, or has typical phone formatting)
    for idx, line in enumerate(lines):
        if not line or line.startswith("$"):
            continue
        # check if the whole line looks like a phone candidate (no alphabetic chars, short)
        if re.fullmatch(r'^[+\d]?[\d\s.\-()]+$', line):
            # extract digits
            digits = re.sub(r'\D', '', line)
            if 9 <= len(digits) <= 12:
                # Normalize to +1 + 10 digits
                if digits.startswith('1') and len(digits) >= 10:
                    digits = digits[1:]   # strip USA country code
                if len(digits) == 10:
                    phone = '+1' + digits
                elif len(digits) == 9:   # missing digit – keep as is, user can fix
                    phone = '+1' + digits
                else:
                    continue   # ambiguous, try next line
                phone_line_index = idx
                break

    # if no line-based phone found, fallback to old regex (whole text)
    if not phone:
        pattern = r'(?:\+?1\s*[.\-]?\s*)?(?:\(\d{3}\)\s*|\d{3}[.\-]?\s*)\d{3}[.\-]?\s*\d{4}'
        for m in re.finditer(pattern, text):
            d = re.sub(r'\D', '', m.group())
            if len(d) == 11 and d.startswith('1'):
                d = d[1:]
            if len(d) == 10:
                phone = '+1' + d
                break

    # ---- 3. price extraction (unchanged) ----
    price = None
    label_price = re.search(r'(?i)^\s*Price\s*:\s*(.+?)\s*$', text, re.MULTILINE)
    if label_price:
        val = label_price.group(1).strip()
        m = re.search(r'\d+(?:\.\d{2})?', val)
        if m:
            # m matches digits only — always prepend "$" (a "Price: $450" line
            # used to come back as bare "450", which validation then rejected).
            price = '$' + m.group()
    else:
        m = re.search(r'\$\s*\d+(?:\.\d{2})?', text)
        if m:
            price = m.group().replace(' ', '')

    # ---- 4. notes: remove phone line (if found) and price line ----
    cleaned = []
    for idx, line in enumerate(lines):
        if idx == phone_line_index:
            continue
        if phone and phone in line:
            continue
        if price and price in line:
            continue
        if line.strip():
            cleaned.append(line.strip())

    issuer_note = None
    driver_note = None
    if len(cleaned) >= 2:
        driver_note = cleaned[-1]
        issuer_note = cleaned[-2]
    elif len(cleaned) == 1:
        driver_note = cleaned[0]

    return phone, price, issuer_note, driver_note

# AI Phase 1: human review — field edit keys (keep callback_data short; max 64 bytes)
PH1_REVIEW_ACCEPT = "ph1_accept"
PH1_REVIEW_EDIT = "ph1_edit"
PH1_REVIEW_VIN_CHECK = "ph1_vin_check"
PH1_EDIT_BACK = "ph1_back"
PH1_EDIT_MORE = "ph1_more"
PH1_EDIT_DONE = "ph1_done"
PH1_FINAL_CONFIRM = "ph1_final_ok"
# edit key -> state_data key (None = first/last name parts)
PH1_EDIT_TO_STATE_KEY = {
    "fn": None,
    "ln": None,
    "addr": "address",
    "csz": "city_state_zip",
    "daddr": "delivery_address",
    "dcsz": "delivery_city_state_zip",
    "vin": "vin",
    "car": "car",
    "col": "color",
    "ins": "insurance_company",
    "pol": "insurance_policy_number",
    "xtra": "extra_info",
    "phone": "pending_phone_number",
    "price": "pending_price",
    "issuer": "special_request_issuers",
    "driver": "special_request_drivers",
    "email": "email",
    "dl": "driver_license_id",
}
# ── Colour picker for the review card's Color field ─────────────────────────
# Tapping a colour is far faster (and far less error-prone) than typing one, and the
# same prompt still accepts typing, a voice note, or a photo of the car.
PH1_COLOR_CB = "ph1col_"
# Buttons show the colour NAME only. The 3-letter DMV code the tag PDF needs is
# looked up separately (tag_pdf.color_code), never shown to the issuer.
_PH1_COLORS = [
    "White", "Black",
    "Gray", "Silver",
    "Blue - Dark", "Blue - Light",
    "Red", "Burgundy",
    "Green", "Green - Dark",
    "Green - Light", "Teal",
    "Brown", "Beige",
    "Bronze", "Copper",
    "Gold", "Tan",
    "Cream", "Champagne",
    "Orange", "Yellow",
    "Purple", "Amethyst",
    "Navy", "Charcoal",
    "Maroon", "Pink",
    "Pearl",
]
# A dot that hints at the actual colour, so the list is scannable at a glance.
_PH1_COLOR_DOT = {
    "White": "⚪", "Black": "⚫", "Gray": "🩶", "Silver": "🩶",
    "Blue - Dark": "🔵", "Blue - Light": "🔵", "Navy": "🔵", "Teal": "🩵",
    "Red": "🔴", "Burgundy": "🔴", "Maroon": "🔴", "Pink": "🩷",
    "Green": "🟢", "Green - Dark": "🟢", "Green - Light": "🟢",
    "Brown": "🟤", "Beige": "🟤", "Tan": "🟤", "Bronze": "🟤", "Copper": "🟤",
    "Gold": "🟡", "Yellow": "🟡", "Champagne": "🟡", "Cream": "🟡",
    "Orange": "🟠", "Purple": "🟣", "Amethyst": "🟣",
    "Charcoal": "⚫", "Pearl": "⚪",
}


def _color_picker_keyboard() -> InlineKeyboardMarkup:
    """Two-per-row colour buttons, then Cancel. Callback data is the colour itself
    (short enough for Telegram's 64-byte limit)."""
    rows = []
    for i in range(0, len(_PH1_COLORS), 2):
        rows.append([
            InlineKeyboardButton(
                f"{_PH1_COLOR_DOT.get(c, '🎨')} {c}",
                callback_data=f"{PH1_COLOR_CB}{c}",
            )
            for c in _PH1_COLORS[i:i + 2]
        ])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="edit_cancel")])
    return InlineKeyboardMarkup(rows)


# What the Color prompt says: every input route spelled out, because all four work.
_PH1_COLOR_PROMPT = "✏️ type, speak, click, or upload a picture to read the color"

# ── Price picker ────────────────────────────────────────────────────────────
# The same few prices come up all day, so tapping beats typing. Any other amount is
# still typed or spoken at this prompt.
PH1_PRICE_CB = "ph1prc_"
PH1_PRICE_TOLL = "toll"          # the toggle rides the same callback prefix
_PH1_PRICES = ["90", "100", "120", "150", "200", "250"]
_PH1_PRICE_PROMPT = "💲 type, speak, or tap the price"
# The same job is quoted with or without the toll, so the amount carries it rather
# than living in a separate field: "$150" or "$150 + toll".
_PH1_TOLL_SUFFIX = " + toll"
_TOLL_RE = re.compile(r"\btolls?\b", re.IGNORECASE)

# Saying the word "toll" is not the same as wanting one. "150 no toll" used to
# bill the client for a toll they had just declined, because the only test was
# whether the word appeared at all. The window is short and stops at a full stop
# so a later sentence cannot reach back and negate this one.
_TOLL_NEG_RE = re.compile(
    r"\b(?:no|not|non|without|minus|less|excluding|except|w/?o|drop|remove|skip)\b"
    r"[^.]{0,14}?\btolls?\b"
    r"|\btolls?\b[^.]{0,20}?\b(?:not\s+included|excluded|off)\b",
    re.IGNORECASE,
)


def _price_has_toll(price) -> bool:
    """True only when a toll is actually WANTED.

    The word alone is not consent: "150 no toll", "150 without toll" and
    "150 toll not included" all name the toll in order to refuse it.
    """
    text = str(price or "")
    if _TOLL_NEG_RE.search(text):
        return False
    return bool(_TOLL_RE.search(text))


def _price_with_toll(price: str, on: bool) -> str:
    """Add or strip the toll on an amount, leaving the number itself alone."""
    base = _TOLL_RE.sub("", str(price or "")).strip().rstrip("+&").strip()
    base = re.sub(r"\s*(?:\+|plus|and|&)\s*$", "", base, flags=re.IGNORECASE).strip()
    if not base:
        return ""
    return base + _PH1_TOLL_SUFFIX if on else base


def _price_picker_keyboard(toll: bool = False) -> InlineKeyboardMarkup:
    """The common prices three per row, then the toll toggle, then Cancel."""
    rows = []
    for i in range(0, len(_PH1_PRICES), 3):
        rows.append([
            InlineKeyboardButton(f"${p}", callback_data=f"{PH1_PRICE_CB}{p}")
            for p in _PH1_PRICES[i:i + 3]
        ])
    rows.append([InlineKeyboardButton(
        "✅ Toll added — tap to remove" if toll else "➕ Plus toll",
        callback_data=f"{PH1_PRICE_CB}{PH1_PRICE_TOLL}",
    )])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="edit_cancel")])
    return InlineKeyboardMarkup(rows)


def _fresh_price_picker(context, state_data) -> InlineKeyboardMarkup:
    """Opening the prompt starts from the card: the toggle shows a toll only if the
    amount already carries one, and any half-armed toll from last time is dropped."""
    try:
        context.user_data.pop("phase1_price_toll", None)
    except Exception:
        pass
    return _price_picker_keyboard(_price_has_toll((state_data or {}).get("pending_price")))


# Every button on the review card, in one place: the conversation state and the
# entry point MUST offer the same set, or a card outlives the conversation that can
# answer it (see _adopt_review_message).
PH1_REVIEW_CB_PATTERN = (
    r"^(ph1_accept|ph1_edit|ph1_vin_check|ph1_add_image|ph1_adjust|ph1_attach|"
    r"ph1_ins_toggle|adjust_cancel|ph1_back|ph1_pick_group|ph1_pick_driver|"
    r"ph1_pick_source|selgrp_|seldrv_|selsrc_|ph1_sel_back|driver_suspended_|"
    r"edit_cancel|ph1edit_|ph1_add_car|ph1car_|ph1carrm_)"
)
# ``ph1edit_[a-z]+`` silently excluded every per-car key (v2vin, v2col, ...):
# no digit could match, the pattern is anchored, and an unmatched callback in
# this state replies nothing at all. [a-z0-9]+ is a strict superset — no
# existing callback contains a digit, so nothing re-routes.
PH1_EDIT_MENU_CB_PATTERN = (
    r"^(ph1_back|ph1_accept|ph1_add_car|ph1car_\d+|ph1carrm_\d+|ph1edit_[a-z0-9]+)$"
)
PH1_VIN_CHOICE_CB_PATTERN = r"^(vin_use|vin_keep|vin_retype)$"


def _adopt_review_message(context, query) -> None:
    """Re-learn WHICH message is the review card from the card itself.

    The handlers edit the card by remembered message id, and a restart wipes that
    memory — so every button on a card that outlived the restart did nothing at all,
    with no reply and no log. The tapped message identifies itself: only the review
    card and its edit picker carry ph1_accept / ph1edit_ buttons."""
    try:
        if context.user_data.get("review_message_id"):
            return
    except Exception:
        return
    msg = getattr(query, "message", None)
    rows = getattr(getattr(msg, "reply_markup", None), "inline_keyboard", None)
    if not msg or not rows:
        return
    on_the_card = any(
        str(getattr(b, "callback_data", "") or "").startswith(("ph1edit_", "ph1_accept", "ph1_edit"))
        for row in rows for b in row
    )
    if on_the_card:
        context.user_data["review_message_id"] = msg.message_id
        context.user_data["review_chat_id"] = msg.chat_id


PH1_EDIT_PROMPT_LABEL = {
    "fn": "First name",
    "ln": "Last name",
    "addr": "Registration address (street)",
    "csz": "Registration city, state, ZIP",
    "daddr": "Delivery address (street)",
    "dcsz": "Delivery city, state, ZIP",
    "vin": "VIN (17 characters if known)",
    "car": "Car (year make model)",
    "col": "Color",
    "ins": "Insurance company",
    "pol": "Insurance policy number",
    "xtra": "Delivery date/time and extra notes",
    "phone": "Phone number",
    "price": "Price",
    "issuer": "Issuer note",
    "driver": "Driver note",
    "email": "Email (required for insurance)",
    "dl": "Driver license (required for insurance)",
}


# ── Extra-car buttons ───────────────────────────────────────────────────────
# ``ph1edit_v2vin`` etc. carry the car number in the callback itself, so one
# grammar covers a 2nd, 3rd or 20th tag without a new constant per car.
PH1_ADD_CAR_CB = "ph1_add_car"
PH1_CAR_MENU_CB = "ph1car_"       # ph1car_2 -> open the 2nd Tag's field picker
PH1_CAR_REMOVE_CB = "ph1carrm_"   # ph1carrm_2 -> drop the 2nd Tag
_VEHICLE_EDIT_KEY_RE = re.compile(r"^v(\d+)([a-z]+)$")

# Which of car 1's edit keys an extra car also has. No delivery, phone, price,
# note, email or DL: one transaction owns those, not one car.
VEHICLE_EDIT_TO_FIELD = {
    "addr": "address",
    "csz": "city_state_zip",
    "vin": "vin",
    "car": "car",
    "col": "color",
    "ins": "insurance_company",
    "pol": "insurance_policy_number",
}
# fn/ln are two buttons over the single stored ``name``, exactly as car 1 works.
VEHICLE_EDIT_KEYS = ("fn", "ln") + tuple(VEHICLE_EDIT_TO_FIELD)


def _vehicle_edit_key(n: int, base: str) -> str:
    """(2, 'vin') -> 'v2vin'."""
    return f"v{int(n)}{base}"


def _vehicle_edit_key_parts(ek: str):
    """'v2vin' -> (2, 'vin'). None for car 1's own keys, so callers can tell them
    apart without a second lookup table."""
    m = _VEHICLE_EDIT_KEY_RE.match(str(ek or ""))
    if not m:
        return None
    n = int(m.group(1))
    base = m.group(2)
    if n < 2 or base not in VEHICLE_EDIT_KEYS:
        return None
    return (n, base)


def _edit_key_base(ek: str) -> str:
    """The underlying field of an edit key, car number stripped. Lets the colour
    palette and price picker branch on 'col'/'price' without caring which car."""
    parts = _vehicle_edit_key_parts(ek)
    return parts[1] if parts else str(ek or "")


def _is_known_edit_key(ek: str) -> bool:
    """Guard for both edit entry points, which silently no-op on an unknown key."""
    return ek in PH1_EDIT_PROMPT_LABEL or _vehicle_edit_key_parts(ek) is not None


def _edit_prompt_label(ek: str) -> str:
    """'v2vin' -> '2nd Tag — VIN (17 characters if known)', so the prompt says
    which car it is about. Nine identical 'VIN' prompts would be a trap."""
    parts = _vehicle_edit_key_parts(ek)
    if not parts:
        return PH1_EDIT_PROMPT_LABEL.get(ek, ek)
    n, base = parts
    return f"{_ordinal_tag_label(n)} — {PH1_EDIT_PROMPT_LABEL.get(base, base)}"


def _vehicle_at(state_data: dict, n: int) -> dict:
    """Car n's dict, or {} when it does not exist (a stale button after a
    removal, or after a restart)."""
    vehicles = _extra_vehicles(state_data)
    idx = int(n) - 2
    return vehicles[idx] if 0 <= idx < len(vehicles) else {}


def _apply_vehicle_edit(state_data: dict, ek: str, new_text: str) -> bool:
    """Write one typed/tapped value onto an extra car. True if it landed.

    Kept separate from _apply_single_phase1_edit on purpose: that function's
    fall-through routes by content, so a bare "Progressive" typed at the 2nd
    Tag's insurer prompt would have been filed against car 1.
    """
    parts = _vehicle_edit_key_parts(ek)
    if not parts:
        return False
    n, base = parts
    vehicles = _extra_vehicles(state_data)
    idx = n - 2
    if not (0 <= idx < len(vehicles)):
        return False
    vehicle = vehicles[idx]
    text = (new_text or "").strip()
    cleared = text in ("", "-")

    if base in ("fn", "ln"):
        first, last = _name_parts_from_full(vehicle.get("name"))
        first = "" if first == "-" else first
        last = "" if last == "-" else last
        if base == "fn":
            first = "" if cleared else text
        else:
            last = "" if cleared else text
        vehicle["name"] = ((first + " " + last).strip() or "") if (first or last) else ""
    elif base == "addr":
        # "11530 Mango terrace drive apt.102 Seffner Florida 33584" typed as one
        # line fills BOTH address rows, mirroring _expand_address_pair for car 1.
        if cleared:
            vehicle["address"] = ""
        else:
            street, csz = _split_street_and_csz(text)
            vehicle["address"] = street or text
            if csz and _is_blank_field(vehicle.get("city_state_zip")):
                vehicle["city_state_zip"] = csz
    elif base == "col":
        vehicle["color"] = "" if cleared else (
            ai_vision.normalize_phase1_color(_clean_spoken_color(text) or text) or text)
    else:
        vehicle[VEHICLE_EDIT_TO_FIELD[base]] = "" if cleared else text

    _clean_vehicle_vin(vehicle)
    state_data[EXTRA_VEHICLES_KEY] = vehicles
    return True


# Tokens that mark a business/company name (case-insensitive). Used to keep
# company names like "Global Transport LLC" from getting bisected into a
# first-name "Global" / last-name "Transport LLC" pair by the simple split.
_CORP_SUFFIX_TOKENS = frozenset({
    "llc", "l.l.c.", "l.l.c",
    "inc", "inc.",
    "corp", "corp.", "corporation",
    "ltd", "ltd.", "limited",
    "co", "co.", "company",
    "pllc", "p.l.l.c.",
    "lp", "l.p.", "llp", "l.l.p.",
    "pc", "p.c.",
    "trust", "group", "holdings", "enterprises",
})


def _name_parts_from_full(name: str) -> tuple:
    """Split the registered-owner string into (first_name, last_name) for the edit UI.

    Convention: the user types first name + last name (when there is one) BEFORE
    the business name. So:
      - "John Doe"                          -> ("John", "Doe")
      - "Isabelle Reyes"                    -> ("Isabelle", "Reyes")
      - "Global Transport LLC"              -> ("-", "Global Transport LLC")
      - "John Doe Global Transport LLC"     -> ("John", "Doe Global Transport LLC")
    """
    n = (name or "").strip()
    if not n or n == "-":
        return ("-", "-")
    tokens = n.split()
    if not tokens:
        return ("-", "-")

    suffix_idx = next(
        (
            i
            for i, t in enumerate(tokens)
            if t.lower().rstrip(",").rstrip(".") in _CORP_SUFFIX_TOKENS
            or t.lower() in _CORP_SUFFIX_TOKENS
        ),
        -1,
    )
    if suffix_idx >= 0:
        tokens_before = tokens[:suffix_idx]
        # Whole string looks like a company name (≤ 2 tokens before the
        # corp suffix, e.g. "Global Transport LLC") — keep it intact.
        if len(tokens_before) < 3:
            return ("-", n)
        # Person-and-company: keep the first token as first name, and put the
        # last name + the rest of the business name in the "last name" slot
        # so the company suffix is preserved verbatim.
        first = tokens_before[0]
        last = " ".join(tokens[1:])
        return (first, last)

    first = tokens[0]
    last = " ".join(tokens[1:]) if len(tokens) > 1 else "-"
    return (first, last)


def _display_name_parts(state_data: dict) -> tuple:
    """(first, last) for the review/edit UI. Uses the explicitly-edited split first/
    last names ONLY while they still match the combined ``name`` (so setting only the
    last name shows correctly); if anything else rewrote ``name`` afterwards (an AI
    re-parse, a full 'name …' edit) the splits are stale and we derive from ``name``."""
    if "first_name" in state_data or "last_name" in state_data:
        f = (state_data.get("first_name") or "").strip()
        l = (state_data.get("last_name") or "").strip()
        if (f + " " + l).strip() == (state_data.get("name") or "").strip():
            return (f or "-", l or "-")
    return _name_parts_from_full(state_data.get("name"))


def _set_full_name(state_data: dict, first: str, last: str) -> None:
    f, l = (first or "").strip(), (last or "").strip()
    if l in ("", "-"):
        state_data["name"] = f if f else "-"
    else:
        state_data["name"] = f"{f} {l}".strip() if f else l
    # The combined name is now authoritative; drop any split first/last helpers so a
    # later "edit first name" / "edit last name" re-seeds cleanly from this name.
    state_data.pop("first_name", None)
    state_data.pop("last_name", None)


# ── Extra vehicles: one client, one transaction, one tag PER CAR ────────────
# Car 1 stays exactly where it always was (the 11-line ``vehicle_details`` blob
# plus ``plate``/``tag_control_number``), so every existing lead, query, message
# and PDF is byte-identical. Cars 2..N live in their own JSONB column, never
# appended to the blob: ``_clean_vin_and_car`` rewrites that blob from the eleven
# car-1 keys on every single edit, and ``_phase1_from_stored_lead`` force-writes
# the FIRST VIN it finds anywhere in it into car 1's slot — so an appended block
# would be erased by the next keystroke, and until then could print car 2's VIN
# on car 1's tag.
EXTRA_VEHICLES_KEY = "extra_vehicles"

# The eight fields an extra car carries, in the order the operator listed them.
# Delivery address, date/time, phone, price, notes, email and DL are deliberately
# absent: those belong to the transaction, not to a car.
VEHICLE_FIELD_KEYS = (
    "name", "address", "city_state_zip", "vin", "car", "color",
    "insurance_company", "insurance_policy_number",
)

# Values that LOOK filled in but mean "nothing here". One copy so the review
# card, the submit gate and the insurance rule cannot drift apart.
#
# Compared case-folded, and with backslashes read as "/", so every way an
# operator writes "nothing" lands here: "N/A", "n/a", "N\\A" from a mangled
# paste, "NONE", "null", "unknown". The hand-written tuple this replaced was
# case-SENSITIVE and listed five spellings, so a car whose insurer field read
# "none" counted as insured and never got the policy it needed.
_BLANK_FIELD_WORDS = frozenset({"-", "\u2014", "n/a", "na", "none", "null", "unknown"})


def _is_blank_field(value) -> bool:
    """True when a field is empty or holds one of the "nothing here" placeholders."""
    v = str(value or "").strip()
    if not v:
        return True
    return v.replace(chr(92), "/").casefold() in _BLANK_FIELD_WORDS


def _extra_vehicles(obj) -> list:
    """The extra cars on a lead row or a review state, always as a list of dicts.

    Tolerant on purpose: the column may be absent (migration not run yet), JSON
    null, or a string rather than parsed JSON. Any of those must read as "no
    extra cars" and never raise in the middle of a dispatch.
    """
    raw = (obj or {}).get(EXTRA_VEHICLES_KEY)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return []
    if not isinstance(raw, list):
        return []
    return [dict(v) for v in raw if isinstance(v, dict)]


def _lead_vehicle_indices(obj) -> list:
    """[1] for an ordinary lead, [1, 2] for a two-car lead, and so on.

    Every "send the tag" path loops this, so a single-car lead makes exactly the
    one call it makes today.
    """
    return list(range(1, len(_extra_vehicles(obj)) + 2))


def _vehicle_count(obj) -> int:
    return len(_extra_vehicles(obj)) + 1


def _blank_vehicle() -> dict:
    """A fresh, empty extra car."""
    return {k: "" for k in VEHICLE_FIELD_KEYS}


def _vehicle_is_empty(vehicle: dict) -> bool:
    """True when nothing has been typed into this car yet."""
    return all(_is_blank_field((vehicle or {}).get(k)) for k in VEHICLE_FIELD_KEYS)


def _ordinal_tag_label(n: int) -> str:
    """2 -> '2nd Tag'. Car 1 is the lead itself, so this only ever sees 2 and up."""
    n = int(n)
    if 11 <= (n % 100) <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix} Tag"


def _vehicle_needs_coverage(vehicle: dict) -> bool:
    """True when this car arrived with no insurer of its own.

    The operator's rule: "if insurance is missing it means it needs tristate
    coverage for that". A car that comes in with Geico or Progressive already has
    a policy and must not be issued a second one.
    """
    return all(
        _is_blank_field((vehicle or {}).get(k))
        for k in ("insurance_company", "insurance_policy_number")
    )


def _all_vins_17(text: str) -> list:
    """Every distinct 17-character VIN in the text, in the order they appear.

    ``_extract_vin_17`` returns only the first, which is right for one car and is
    exactly what hides the second one in a pasted two-car message. Runs that are
    all digits or all letters are rejected: a real VIN mixes both, and a 17-digit
    number is far more likely to be an account or policy number.
    """
    seen, out = set(), []
    for m in VIN_PATTERN.finditer(text or ""):
        v = m.group(0).upper()
        if v in seen:
            continue
        if not (any(c.isdigit() for c in v) and any(c.isalpha() for c in v)):
            continue
        seen.add(v)
        out.append(v)
    return out


def _clean_vehicle_vin(vehicle: dict) -> None:
    """Validate an extra car's VIN in place, the way ``_clean_vin_and_car`` does
    for car 1 — which only ever looks at car 1, so without this an extra car's
    VIN would reach a printed tag with no length check at all.

    A 17-char VIN typed into the Car field is rescued, and a VIN that is not 17
    characters is kept (so the operator can see and fix their typo) but never
    silently treated as valid.
    """
    if not isinstance(vehicle, dict):
        return
    vin = str(vehicle.get("vin") or "").strip()
    car = str(vehicle.get("car") or "").strip()
    if len(vin) != 17:
        found = _extract_vin_17(vin) or _extract_vin_17(car)
        if found:
            vin = found
            if car and _extract_vin_17(car) == found:
                car = car.replace(found, "").strip(" ,;:-") or car
    vehicle["vin"] = vin.upper() if len(vin) == 17 else vin
    vehicle["car"] = car


def _format_extra_vehicle_lines(vehicle: dict, n: int) -> str:
    """One extra car rendered with the same labels, in the same order, as the
    main card — the block the operator asked for."""
    first, last = _name_parts_from_full((vehicle or {}).get("name"))
    v = vehicle or {}
    return "\n".join([
        f"🚘 {_ordinal_tag_label(n)}",
        f"👤First name: {first}",
        f"👤Last name: {last}",
        f"🏠Registration address: {v.get('address') or '-'}",
        f"🏠Registration city, state, ZIP: {v.get('city_state_zip') or '-'}",
        f"🔢VIN: {v.get('vin') or '-'}",
        f"🚘Car: {v.get('car') or '-'}",
        f"🎨Color: {v.get('color') or '-'}",
        f"🛡Insurance company: {v.get('insurance_company') or '-'}",
        f"🛡Insurance policy #: {v.get('insurance_policy_number') or '-'}",
    ])


def _format_all_extra_vehicle_lines(obj) -> str:
    """Every extra car as one appended block, or "" when there are none — so a
    single-car card renders byte-identically to before this feature existed."""
    vehicles = _extra_vehicles(obj)
    if not vehicles:
        return ""
    blocks = [_format_extra_vehicle_lines(v, i + 2) for i, v in enumerate(vehicles)]
    return "\n\n" + "\n\n".join(blocks)


def _format_phase1_field_lines(state_data: dict) -> str:
    """Plain-text list of all Phase 1 fields (same labels as the edit picker).
    Always renders Issuer note / Driver note so the summary matches the edit menu.
    """
    first, last = _display_name_parts(state_data)
    lines = [
        f"👤First name: {first}",
        f"👤Last name: {last}",
        f"🏠Registration address: {state_data.get('address') or '-'}",
        f"🏠Registration city, state, ZIP: {state_data.get('city_state_zip') or '-'}",
        f"📍Delivery address: {state_data.get('delivery_address') or '-'}",
        f"📍Delivery city, state, ZIP: {state_data.get('delivery_city_state_zip') or '-'}",
        f"🔢VIN: {state_data.get('vin') or '-'}",
        f"🚘Car: {state_data.get('car') or '-'}",
        f"🎨Color: {state_data.get('color') or '-'}",
        f"🛡Insurance company: {state_data.get('insurance_company') or '-'}",
        f"🛡Insurance policy #: {state_data.get('insurance_policy_number') or '-'}",
        f"🕒Delivery Date/Time & Notes: {state_data.get('extra_info') or '-'}",
        f"📞Phone: {state_data.get('pending_phone_number') or '-'}",
        f"💲Price: {state_data.get('pending_price') or '-'}",
        f"📝Issuer note: {state_data.get('special_request_issuers') or '-'}",
        f"📝Driver note: {state_data.get('special_request_drivers') or '-'}",
        f"📧Email (required for insurance): {state_data.get('email') or '-'}",
        f"🪪Driver license (required for insurance): {state_data.get('driver_license_id') or '-'}",
    ]
    # Extra cars append their own block. With none, this adds "" and the card is
    # byte-identical to before the feature existed.
    return "\n".join(lines) + _format_all_extra_vehicle_lines(state_data)


def _format_phase1_ai_review_text(state_data: dict) -> str:
    """Human-readable summary of how the bot understood Phase 1 (AI path). Plain text (safe for special chars).
    Insurance ON/OFF and every action live on the inline buttons — the message body stays the
    clean field list so the review card is as short as possible and always visible."""
    return (
        "📝 Review & ✍️Edit Before Dispatching ✅\n\n"
        + _format_phase1_field_lines(state_data)
    )


def _preview_value_after_phase1_edit(state_data: dict, edit_key: str) -> str:
    """Current display value for a field after an edit (for recent-changes list)."""
    if edit_key == "fn":
        first, _ = _display_name_parts(state_data)
        return first
    if edit_key == "ln":
        _, last = _display_name_parts(state_data)
        return last
    sk = PH1_EDIT_TO_STATE_KEY.get(edit_key)
    if sk:
        return str(state_data.get(sk) or "-")
    return "-"


def _format_phase1_final_review_text(state_data: dict, recent_edits: list) -> str:
    """After Done — show full field list, then confirm."""
    blocks = ["📋 Final review.\n"]
    blocks.append(
        "📄 All fields (same list as when you pick a field to edit):\n"
        + _format_phase1_field_lines(state_data)
    )
    blocks.append(
        "\nDone with Edits, or Need another Edit?"
    )
    return "\n".join(blocks)


def _truncate_btn_val(val: str, max_len: int = 22) -> str:
    v = (val if val and str(val).strip() else "-").strip()
    v = re.sub(r"\s+", " ", v)
    return (v[: max_len - 1] + "…") if len(v) > max_len else v


# The noun, however the operator says it.
_INS_NOUN = r"(?:insurance|coverage)"

# TALKING ABOUT insurance is not ASKING FOR it. "we need insurance info from him"
# is a note to self about a missing document; it used to switch the policy on.
_INS_REMARK_RE = re.compile(
    rf"\b{_INS_NOUN}\s+(?:info|information|company|carrier|number|card\s+number|"
    rf"paperwork|details|docs?|documents?|on\s+file|about)\b"
    rf"|\b(?:no|any)\s+{_INS_NOUN}\s+(?:company|carrier|info|details)\b",
    re.I,
)

# Refusal, in the shapes people actually use. The apostrophe has to live INSIDE
# the alternation: \bn't\b never matches inside "don't", which is why
# "they don't need insurance" used to turn insurance ON.
_INS_OFF_RE = re.compile(
    rf"\b(?:no|not|never|remove|disable|skip|without|drop|cancel|scratch|forget)\b"
    rf"[^.]{{0,16}}?{_INS_NOUN}\b"
    rf"|\b(?:do|does|did|would|could|will|is|are|was|were|ca|wo)n'?t\b"
    rf"[^.]{{0,16}}?{_INS_NOUN}\b"
    rf"|{_INS_NOUN}[^.]{{0,14}}?\b(?:off|out)\b"
    rf"|\balready\s+(?:has|have|had|got|is|are)\b[^.]{{0,14}}?{_INS_NOUN}\b"
    rf"|\balready\s+insured\b"
    rf"|\b(?:he|she|they|it)(?:'(?:s|re)|\s+(?:is|are))?\s+(?:already\s+)?covered\b"
    rf"|^\s*{_INS_NOUN}\s*(?:off|no)\s*[.!]*$",
    re.I,
)

# Asking for it. Stems are inflected ("need/needs", "want/wants") because the
# operator talks about the client in the third person.
_INS_ON_RE = re.compile(
    rf"\b(?:add|adds|enable|turn\s*on|switch\s*on|include|includes|want|wants|"
    rf"with|need|needs|do|get|gets|issue|issues|give|gives|sell|sells)\s+"
    rf"(?:the\s+|an?\s+|him\s+|her\s+|them\s+)?{_INS_NOUN}\b"
    rf"|\bturn\s+(?:the\s+)?{_INS_NOUN}\s+on\b"
    rf"|\bput\s+{_INS_NOUN}\s+on\b"
    rf"|^\s*{_INS_NOUN}\s*(?:on|yes|please)\s*[.!]*$",
    re.I,
)


def _insurance_intent(text: str):
    """True = turn insurance on, False = off, None = not an insurance command.
    Whole-ish phrase match so a value like 'insurance GEICO' stays a field edit."""
    t = (text or "").strip()
    if not t:
        return None
    # Order is load-bearing: a remark about insurance is neither an on nor an off,
    # and a refusal has to be read before the affirmation it contains ("do NOT add
    # insurance" contains "add insurance").
    if _INS_REMARK_RE.search(t):
        return None
    if _INS_OFF_RE.search(t):
        return False
    if _INS_ON_RE.search(t):
        return True
    return None


def _build_review_keyboard_with_selections(state_data):
    group_label = state_data.get("selected_group_name", "auto")
    driver_label = state_data.get("selected_driver_names", "auto")
    source_label = state_data.get("selected_source_label") or "none"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"🏢 {group_label}", callback_data="ph1_pick_group"),
            InlineKeyboardButton(f"🚗 {driver_label}", callback_data="ph1_pick_driver"),
            InlineKeyboardButton(f"📊 {source_label}", callback_data="ph1_pick_source"),
        ],
        [
            InlineKeyboardButton("✏️ Edit", callback_data=PH1_REVIEW_EDIT),
            InlineKeyboardButton("🔍 VIN", callback_data=PH1_REVIEW_VIN_CHECK),
            InlineKeyboardButton("✅ Submit", callback_data=PH1_REVIEW_ACCEPT),
        ],
        [InlineKeyboardButton(
            "🛡 Insurance: ON" if state_data.get("wants_insurance") else "🛡 Add insurance",
            callback_data="ph1_ins_toggle",
        )],
        [InlineKeyboardButton("🖼 Add image (title / license)", callback_data="ph1_add_image")],
    ] + _add_car_button_rows(state_data))

async def _edit_message_keyboard(context, chat_id, message_id, keyboard):
    try:
        await context.bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=keyboard,
        )
    except Exception as e:
        logger.warning(f"Could not edit keyboard: {e}")

async def _update_review_message_text(context, state_data):
    chat_id = context.user_data.get("review_chat_id")
    mid = context.user_data.get("review_message_id")
    if not chat_id or not mid:
        logger.info("🔎DIAG update_review NO-OP: chat=%s mid=%s (ids missing)", chat_id, mid)
        return
    new_text = _format_phase1_ai_review_text(state_data)
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=mid,
            text=new_text,
            reply_markup=_build_review_keyboard_with_selections(state_data),
        )
        logger.info("🔎DIAG update_review EDITED chat=%s mid=%s", chat_id, mid)
    except Exception as e:
        logger.info("🔎DIAG update_review EDIT FAILED chat=%s mid=%s: %s", chat_id, mid, e)

async def _update_review_text(context, state_data):
    # Only updates the text, not the keyboard (used in some places)
    chat_id = context.user_data.get("review_chat_id")
    mid = context.user_data.get("review_message_id")
    if not chat_id or not mid:
        return
    new_text = _format_phase1_ai_review_text(state_data)
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=mid,
            text=new_text,
        )
    except Exception as e:
        logger.warning("Could not update review text: %s", e)


async def _reanchor_review_card(context, chat_id, state_data) -> None:
    """Repost the review card at the BOTTOM of the chat (below any pictures the user kept
    in view) and delete the old one, so there is only ever a single card and the parsed
    result sits directly UNDER the photo for a spelling double-check. Selections and
    insurance state carry over because both the text and keyboard are rebuilt from
    state_data. Falls back to an in-place edit if the repost fails."""
    old_mid = context.user_data.get("review_message_id")
    old_cid = context.user_data.get("review_chat_id")
    try:
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text=_format_phase1_ai_review_text(state_data),
            reply_markup=_build_review_keyboard_with_selections(state_data),
        )
    except Exception as e:
        logger.warning("Could not re-anchor review card: %s", e)
        await _update_review_message_text(context, state_data)
        return
    context.user_data["review_message_id"] = msg.message_id
    context.user_data["review_chat_id"] = msg.chat_id
    # Remove the old card only after the new one is up (never a gap with no review).
    if old_mid and old_cid and old_mid != msg.message_id:
        await _safe_delete_chat_message(context, old_cid, old_mid)


def _add_car_button_rows(state_data: dict) -> list:
    """The ➕ Add Car row, plus one shortcut per car already added.

    Returned as rows so both the review card and the edit picker share exactly
    one definition — a second copy is how the two screens drift apart.
    """
    rows = [[InlineKeyboardButton("➕ Add Car", callback_data=PH1_ADD_CAR_CB)]]
    vehicles = _extra_vehicles(state_data)
    if not vehicles:
        return rows
    shortcuts = [
        InlineKeyboardButton(
            f"🚘 {_ordinal_tag_label(i + 2)}",
            callback_data=f"{PH1_CAR_MENU_CB}{i + 2}",
        )
        for i in range(len(vehicles))
    ]
    # Two per row so twenty cars stay usable on a phone.
    rows += [shortcuts[i:i + 2] for i in range(0, len(shortcuts), 2)]
    return rows


def _vehicle_edit_fields_keyboard(state_data: dict, n: int) -> InlineKeyboardMarkup:
    """One extra car's own field picker.

    A sub-screen rather than nine more rows on the main picker: the main card is
    already 18 lines and 19 buttons, and this keeps its size fixed however many
    cars the client has.
    """
    v = _vehicle_at(state_data, n)
    first, last = _name_parts_from_full(v.get("name"))
    k = lambda base: f"ph1edit_{_vehicle_edit_key(n, base)}"
    rows = [
        [
            InlineKeyboardButton(f"First name: {_truncate_btn_val(first)}", callback_data=k("fn")),
            InlineKeyboardButton(f"Last name: {_truncate_btn_val(last)}", callback_data=k("ln")),
        ],
        [
            InlineKeyboardButton(f"Reg address: {_truncate_btn_val(v.get('address'))}", callback_data=k("addr")),
            InlineKeyboardButton(f"Reg city/ST/ZIP: {_truncate_btn_val(v.get('city_state_zip'))}", callback_data=k("csz")),
        ],
        [
            InlineKeyboardButton(f"VIN: {_truncate_btn_val(v.get('vin'), 18)}", callback_data=k("vin")),
            InlineKeyboardButton(f"Car: {_truncate_btn_val(v.get('car'))}", callback_data=k("car")),
        ],
        [
            InlineKeyboardButton(f"Color: {_truncate_btn_val(v.get('color'))}", callback_data=k("col")),
            InlineKeyboardButton(f"Insurance: {_truncate_btn_val(v.get('insurance_company'))}", callback_data=k("ins")),
        ],
        [
            InlineKeyboardButton(f"Policy #: {_truncate_btn_val(v.get('insurance_policy_number'))}", callback_data=k("pol")),
            InlineKeyboardButton("🗑 Remove this car", callback_data=f"{PH1_CAR_REMOVE_CB}{n}"),
        ],
        [
            InlineKeyboardButton("✅ Submit", callback_data=PH1_REVIEW_ACCEPT),
            InlineKeyboardButton("⬅️ Back to review", callback_data=PH1_EDIT_BACK),
        ],
    ]
    return InlineKeyboardMarkup(rows)


async def _show_vehicle_edit_picker(context, state_data, n: int, fallback_chat_id=None) -> None:
    """Render the review message as one extra car's field picker."""
    chat_id = context.user_data.get("review_chat_id")
    mid = context.user_data.get("review_message_id")
    if not chat_id or not mid:
        if fallback_chat_id:
            await _reanchor_review_card(context, fallback_chat_id, state_data)
        return
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=mid,
            text=f"🚘 {_ordinal_tag_label(n)} — tap a field to fill it in, "
                 "then ⬅️ Back to review.\n\n"
                 + _format_phase1_field_lines(state_data),
            reply_markup=_vehicle_edit_fields_keyboard(state_data, n),
        )
    except Exception as e:
        logger.warning("Could not show vehicle edit picker: %s", e)


async def _handle_vehicle_menu_action(update, context, data: str, state_data: dict):
    """➕ Add Car / open a car / remove a car. Returns the next state, or None if
    ``data`` was not one of those.

    Shared by BOTH callback entry points. The edit-menu state runs its own
    handler first, so a copy living in only one of them would be a dead button on
    whichever screen the operator happened to be on.
    """
    query = update.callback_query
    user_id = query.from_user.id
    chat_id = query.message.chat_id if query.message else None

    if data == PH1_ADD_CAR_CB:
        vehicles = _extra_vehicles(state_data)
        # An untouched blank car already on the card: open that one instead of
        # stacking up empties on repeated taps.
        existing_blank = next(
            (i for i, v in enumerate(vehicles) if _vehicle_is_empty(v)), None)
        if existing_blank is None:
            vehicles.append(_blank_vehicle())
            state_data[EXTRA_VEHICLES_KEY] = vehicles
            db.set_user_state(user_id, "phase1", state_data)
            n = len(vehicles) + 1
        else:
            n = existing_blank + 2
        await _close_open_field_prompt(context, chat_id)
        await _show_vehicle_edit_picker(context, state_data, n, fallback_chat_id=chat_id)
        return STATE_AI_REVIEW

    if data.startswith(PH1_CAR_MENU_CB):
        try:
            n = int(data.replace(PH1_CAR_MENU_CB, "", 1))
        except ValueError:
            return STATE_AI_REVIEW
        if not _vehicle_at(state_data, n):
            # Stale button (car removed, or a restart). Say so rather than no-op.
            await _show_edit_picker(context, state_data, fallback_chat_id=chat_id)
            if chat_id:
                await _send_vanishing(context, chat_id, "That car is no longer on this lead.")
            return STATE_AI_REVIEW
        await _close_open_field_prompt(context, chat_id)
        await _show_vehicle_edit_picker(context, state_data, n, fallback_chat_id=chat_id)
        return STATE_AI_REVIEW

    if data.startswith(PH1_CAR_REMOVE_CB):
        try:
            n = int(data.replace(PH1_CAR_REMOVE_CB, "", 1))
        except ValueError:
            return STATE_AI_REVIEW
        vehicles = _extra_vehicles(state_data)
        idx = n - 2
        if 0 <= idx < len(vehicles):
            vehicles.pop(idx)
            state_data[EXTRA_VEHICLES_KEY] = vehicles
            db.set_user_state(user_id, "phase1", state_data)
        await _close_open_field_prompt(context, chat_id)
        await _show_edit_picker(context, state_data, fallback_chat_id=chat_id)
        if chat_id:
            await _send_vanishing(context, chat_id, f"🗑 Removed the {_ordinal_tag_label(n)}.")
        return STATE_AI_REVIEW

    return None


def _phase1_edit_fields_keyboard(state_data: dict) -> InlineKeyboardMarkup:
    first, last = _display_name_parts(state_data)
    rows = [
        [
            InlineKeyboardButton(f"First name: {_truncate_btn_val(first)}", callback_data="ph1edit_fn"),
            InlineKeyboardButton(f"Last name: {_truncate_btn_val(last)}", callback_data="ph1edit_ln"),
        ],
        [
            InlineKeyboardButton(f"Reg address: {_truncate_btn_val(state_data.get('address'))}", callback_data="ph1edit_addr"),
            InlineKeyboardButton(f"Reg city/ST/ZIP: {_truncate_btn_val(state_data.get('city_state_zip'))}", callback_data="ph1edit_csz"),
        ],
        [
            InlineKeyboardButton(f"Deliv address: {_truncate_btn_val(state_data.get('delivery_address'))}", callback_data="ph1edit_daddr"),
            InlineKeyboardButton(f"Deliv city/ST/ZIP: {_truncate_btn_val(state_data.get('delivery_city_state_zip'))}", callback_data="ph1edit_dcsz"),
        ],
        [
            InlineKeyboardButton(f"VIN: {_truncate_btn_val(state_data.get('vin'), 18)}", callback_data="ph1edit_vin"),
            InlineKeyboardButton(f"Car: {_truncate_btn_val(state_data.get('car'))}", callback_data="ph1edit_car"),
        ],
        [
            InlineKeyboardButton(f"Color: {_truncate_btn_val(state_data.get('color'))}", callback_data="ph1edit_col"),
            InlineKeyboardButton(f"Insurance: {_truncate_btn_val(state_data.get('insurance_company'))}", callback_data="ph1edit_ins"),
        ],
        [
            InlineKeyboardButton(f"Policy #: {_truncate_btn_val(state_data.get('insurance_policy_number'))}", callback_data="ph1edit_pol"),
            InlineKeyboardButton(f"Date/Time: {_truncate_btn_val(state_data.get('extra_info'))}", callback_data="ph1edit_xtra"),
        ],
        [
            InlineKeyboardButton(f"Phone: {_truncate_btn_val(state_data.get('pending_phone_number') or '-', 18)}", callback_data="ph1edit_phone"),
            InlineKeyboardButton(f"Price: {_truncate_btn_val(state_data.get('pending_price') or '-', 12)}", callback_data="ph1edit_price"),
        ],
        [
            InlineKeyboardButton(f"Issuer note: {_truncate_btn_val(state_data.get('special_request_issuers') or '-')}", callback_data="ph1edit_issuer"),
            InlineKeyboardButton(f"Driver note: {_truncate_btn_val(state_data.get('special_request_drivers') or '-')}", callback_data="ph1edit_driver"),
        ],
        [
            InlineKeyboardButton(f"📧 Email: {_truncate_btn_val(state_data.get('email') or '-', 22)}", callback_data="ph1edit_email"),
            InlineKeyboardButton(f"🪪 DL: {_truncate_btn_val(state_data.get('driver_license_id') or '-', 18)}", callback_data="ph1edit_dl"),
        ],
        [
            InlineKeyboardButton("✅ Submit", callback_data=PH1_REVIEW_ACCEPT),
            InlineKeyboardButton("⬅️ Back to review", callback_data=PH1_EDIT_BACK),
        ],
    ] + _add_car_button_rows(state_data)
    return InlineKeyboardMarkup(rows)


async def _show_edit_picker(context, state_data, fallback_chat_id=None) -> None:
    """Render the review message as the field-by-field edit picker (with current
    values on every button), so the issuer can edit one field after another and
    then tap ✅ Submit — without re-opening Edit each time."""
    chat_id = context.user_data.get("review_chat_id")
    mid = context.user_data.get("review_message_id")
    if not chat_id or not mid:
        # A restart lost track of the card. Draw a fresh one rather than no-op —
        # silence here is what made the edit buttons look dead.
        if fallback_chat_id:
            await _reanchor_review_card(context, fallback_chat_id, state_data)
        return
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=mid,
            text="✏️ Tap a field to change it, edit as many as you like, then tap ✅ Submit.\n\n"
            + _format_phase1_field_lines(state_data),
            reply_markup=_phase1_edit_fields_keyboard(state_data),
        )
    except Exception as e:
        logger.warning("Could not show edit picker: %s", e)


def _phase1_after_edit_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✏️ Edit", callback_data=PH1_EDIT_MORE),
            InlineKeyboardButton("✅ Done", callback_data=PH1_EDIT_DONE),
        ]
    ])


def _phase1_final_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Done with Edits", callback_data=PH1_FINAL_CONFIRM)],
        [InlineKeyboardButton("✏️ Need another Edit", callback_data=PH1_EDIT_MORE)],
    ])


def _default_phase1_review_source_label() -> str:
    """Prefer admin-configured contact source whose label matches Facebook (case-insensitive)."""
    try:
        sources = db.get_contact_info_sources()
    except Exception:
        sources = []
    needle = "facebook"
    for s in sources:
        lab = (s.get("label") or "").strip()
        if lab.lower() == needle:
            return lab
    for s in sources:
        lab = (s.get("label") or "").strip()
        if needle in lab.lower():
            return lab
    return "Facebook"


def _resolve_contact_source_label(data: dict | None) -> str:
    """Return a persisted lead source label, defaulting to configured Facebook."""
    raw = (data or {}).get("selected_source_label")
    label = str(raw or "").strip()
    return label or _default_phase1_review_source_label()


def _resolve_dispatch_driver_ids(
    data: dict | None,
    *,
    group_id: str | None = None,
    is_all_groups: bool = False,
) -> list[str]:
    """Resolve driver IDs for deferred dispatch after group accept.

    Priority:
    1) Explicit selected_driver_ids from review/selection state.
    2) Fallback pool (group-linked for single-group, global for broadcast).
    Filters to active, non-suspended, and parseable Telegram IDs.
    """
    ids, _ = _dispatch_drivers_with_reasons(
        data, group_id=group_id, is_all_groups=is_all_groups)
    return ids


def _dispatch_drivers_with_reasons(
    data: dict | None,
    *,
    group_id: str | None = None,
    is_all_groups: bool = False,
):
    """(reachable_driver_ids, dropped) where dropped is [(name, why), …].

    The issuer's CHOICE and what that choice resolves to are different things, and
    conflating them is what sent a one-driver lead to the whole roster: an explicit
    pick that resolved to nothing looked exactly like no pick at all, and no pick
    means "use the pool". An explicit pick therefore never widens here — if it comes
    to nothing the caller reports it instead of broadcasting."""
    payload = data or {}
    all_rows = _get_all_drivers_cached() or []
    id_set = {str(x).strip() for x in (payload.get("selected_driver_ids") or []) if str(x).strip()}
    dropped: list[tuple[str, str]] = []

    if id_set:
        by_id = {str(d.get("id")): d for d in all_rows if d}
        selected = []
        for did in sorted(id_set):
            row = by_id.get(did)
            if row is None:
                dropped.append((f"driver {did}", "no longer on the roster"))
            else:
                selected.append(row)
    else:
        if is_all_groups:
            pool = all_rows
        else:
            try:
                linked = db.get_group_driver_rows_for_group(group_id) if group_id else []
            except Exception:
                linked = []
            pool = linked or all_rows
        selected = list(pool or [])

    suspended = _get_suspended_driver_ids()
    out_ids: list[str] = []
    for d in selected:
        name = str((d or {}).get("driver_name") or "that driver")
        if not d or not record_is_active(d):
            dropped.append((name, "switched off in /settings"))
            continue
        did = str(d.get("id") or "").strip()
        if not did:
            dropped.append((name, "no id on the record"))
            continue
        if did in suspended:
            dropped.append((name, "suspended"))
            continue
        if _parse_chat_id(d.get("driver_telegram_id")) is None:
            dropped.append((name, "no Telegram chat id on file — see /drivers"))
            continue
        out_ids.append(did)
    # Only an explicit pick needs explaining; a pool that thins out is routine.
    return out_ids, (dropped if id_set else [])


def _dropped_drivers_note(dropped: list) -> str:
    """One line per driver the lead could not reach, so a silent non-delivery
    becomes a message the issuer can act on."""
    if not dropped:
        return ""
    return "\n".join(f"• {name} — {why}" for name, why in dropped)


async def _send_phase1_ai_review(target_message, state_data: dict, context, user_id) -> None:
    # Default dispatch selections on the review row (🏢 🚗 📊): all groups, all eligible drivers, Facebook.
    groups = db.get_all_groups()
    active_groups = [g for g in groups if record_is_active(g)]
    if active_groups:
        state_data["selected_group_id"] = "all"
        state_data["selected_group_name"] = "All Dispatchers"
    else:
        state_data["selected_group_id"] = None
        state_data["selected_group_name"] = "None"

    drivers = _get_all_drivers_cached()
    active_drivers = [d for d in drivers if record_is_active(d)]
    suspended = _get_suspended_driver_ids()
    eligible = [d for d in active_drivers if str(d.get("id")) not in suspended]
    if eligible:
        state_data["selected_driver_ids"] = [d["id"] for d in eligible]
        state_data["selected_driver_names"] = "All Drivers"
    else:
        state_data["selected_driver_ids"] = []
        state_data["selected_driver_names"] = "None"

    state_data["selected_source_label"] = _default_phase1_review_source_label()

    db.set_user_state(user_id, "phase1", state_data)

    keyboard = _build_review_keyboard_with_selections(state_data)
    msg = await target_message.reply_text(
        _format_phase1_ai_review_text(state_data),
        reply_markup=keyboard,
    )
    context.user_data["review_message_id"] = msg.message_id
    context.user_data["review_chat_id"] = msg.chat_id


async def _repost_review_card(target_message, state_data: dict, context, user_id) -> None:
    """Re-post the review card from EXISTING lead data (used to RESTORE an orphaned card
    after the bot's in-memory conversation state was lost on a restart / redeploy). Unlike
    _send_phase1_ai_review it PRESERVES any dispatch selections already chosen — only
    filling in defaults that are missing — and re-establishes the live review_message_id so
    the very next inline edit updates this fresh card in place."""
    if not state_data.get("selected_group_name"):
        groups = [g for g in db.get_all_groups() if record_is_active(g)]
        state_data["selected_group_id"] = "all" if groups else None
        state_data["selected_group_name"] = "All Dispatchers" if groups else "None"
    if not state_data.get("selected_driver_names"):
        drivers = [d for d in _get_all_drivers_cached() if record_is_active(d)]
        suspended = _get_suspended_driver_ids()
        eligible = [d for d in drivers if str(d.get("id")) not in suspended]
        state_data["selected_driver_ids"] = [d["id"] for d in eligible] if eligible else []
        state_data["selected_driver_names"] = "All Drivers" if eligible else "None"
    if not state_data.get("selected_source_label"):
        state_data["selected_source_label"] = _default_phase1_review_source_label()
    db.set_user_state(user_id, "phase1", state_data)
    msg = await target_message.reply_text(
        _format_phase1_ai_review_text(state_data),
        reply_markup=_build_review_keyboard_with_selections(state_data),
    )
    context.user_data["review_message_id"] = msg.message_id
    context.user_data["review_chat_id"] = msg.chat_id


async def _continue_phase1_after_ai_review(message, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> int:
    """After user accepts AI interpretation (or finishes edits): VIN check → missing fields → files."""
    state = db.get_user_state(user_id)
    if not state or not state.get("data"):
        await message.reply_text("❌ Phase 1 data not found. Please start over with /start")
        return ConversationHandler.END
    state_data = state["data"].copy()
    _apply_single_address_as_both(state_data)
    _clean_vin_and_car(state_data)
    _sanitize_phase1_pending_phone_price(state_data)
    _prune_empty_extra_vehicles(state_data)
    db.set_user_state(user_id, "phase1", state_data)
    blocked = _extra_vehicles_submit_block(state_data)
    if blocked:
        await message.reply_text(blocked)
        return STATE_AI_REVIEW
    # VIN lookup is now opt-in via the "🔍 VIN" button on the review
    # screen; Submit no longer triggers a DMV decode automatically.
    # Re-check missing fields against a synthetic blob so detector still works
    blob = "\n".join(
        str(state_data.get(k) or "")
        for k in ("name", "address", "city_state_zip", "delivery_address", "delivery_city_state_zip", "vin", "car", "color", "insurance_company", "insurance_policy_number", "extra_info")
    )
    missing = ai_vision.detect_missing_fields(state_data, blob)
    # Price / tag info / date-time are optional: no question when unfilled —
    # they simply show as "-" on the final review.
    missing = [f for f in missing if f not in PHASE1_OPTIONAL_FIELDS
               and not _field_already_filled(state_data, f)]
    asked = await _ask_next_missing(message, context, user_id, missing, state_data)
    if asked is not None:
        return asked
    return await _ensure_phone_price_before_files(message, context, user_id)


# Optional Phase 1 fields: never block with a question — blank shows as "-" in review.
PHASE1_OPTIONAL_FIELDS = {"insurance_company", "insurance_policy_number", "extra_info", "delivery_date"}

# What an extra car must have before a tag can be printed for it. Insurance is
# absent on purpose: a blank insurer is the SIGNAL that this car needs coverage.
_VEHICLE_REQUIRED_FOR_TAG = (
    ("vin", "VIN"),
    ("name", "first and last name"),
    ("city_state_zip", "registration city, state, ZIP"),
)


def _prune_empty_extra_vehicles(state_data: dict) -> None:
    """Drop extra cars nothing was ever typed into.

    Tapping ➕ Add Car and changing your mind must not block the lead, and must
    not mint a plate for a car that does not exist.
    """
    vehicles = _extra_vehicles(state_data)
    kept = [v for v in vehicles if not _vehicle_is_empty(v)]
    if len(kept) != len(vehicles):
        state_data[EXTRA_VEHICLES_KEY] = kept


def _extra_vehicles_submit_block(state_data: dict) -> str:
    """Why this lead cannot be submitted yet, or "" when it can.

    Deliberately NOT routed through the missing-field prompt queue: that
    machinery walks one question at a time off an AI-produced list and has a
    history of asking for values already on the card. Naming the car and the
    field in one message is both clearer and far less fragile.
    """
    vehicles = _extra_vehicles(state_data)
    if not vehicles:
        return ""
    problems = []
    for i, v in enumerate(vehicles):
        label = _ordinal_tag_label(i + 2)
        for key, human in _VEHICLE_REQUIRED_FOR_TAG:
            if _is_blank_field(v.get(key)):
                problems.append(f"• {label}: {human} is missing")
        vin = str(v.get("vin") or "").strip()
        if vin and not _is_blank_field(vin) and len(vin) != 17:
            problems.append(
                f"• {label}: VIN is {len(vin)} characters, not 17 — {vin}")

    # The same car entered twice would mint two plates for one vehicle, and
    # nothing else in this project checks for a duplicate VIN.
    all_vins = [str(state_data.get("vin") or "").strip().upper()] + [
        str(v.get("vin") or "").strip().upper() for v in vehicles
    ]
    seen = set()
    for i, vin in enumerate(all_vins):
        if not vin or _is_blank_field(vin):
            continue
        if vin in seen:
            where = "the first car" if i == 0 else _ordinal_tag_label(i + 1)
            problems.append(f"• {where} repeats VIN {vin} — one car, one tag")
        seen.add(vin)

    if not problems:
        return ""
    return (
        "🚘 Nearly there — the extra cars need a little more before their tags "
        "can be printed:\n\n" + "\n".join(problems)
        + "\n\nTap the car on the review card to fill it in, or 🗑 Remove it."
    )


def _track_missing_prompt(context: ContextTypes.DEFAULT_TYPE, sent_message) -> None:
    """Remember a missing-field question so it can disappear once answered."""
    try:
        context.user_data.setdefault("missing_field_prompt_ids", []).append(
            (sent_message.chat_id, sent_message.message_id)
        )
    except Exception:
        pass


async def _clear_missing_prompts(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete every tracked missing-field question message (chat stays clean)."""
    ids = context.user_data.pop("missing_field_prompt_ids", None) or []
    for chat_id, message_id in ids:
        await _safe_delete_chat_message(context, chat_id, message_id)


def _track_vin_flow_msg(context: ContextTypes.DEFAULT_TYPE, sent_message) -> None:
    """Remember a VIN-check/choice/retype message so the whole exchange can be wiped
    once the VIN is resolved — the review card stays the single source of truth."""
    try:
        context.user_data.setdefault("vin_flow_msg_ids", []).append(
            (sent_message.chat_id, sent_message.message_id)
        )
    except Exception:
        pass


async def _clear_vin_flow_msgs(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete every tracked VIN-flow message (the DMV lookup card, the 'Please type the
    correct VIN' prompt, any repeat conflict cards) so nothing lingers after a retype."""
    ids = context.user_data.pop("vin_flow_msg_ids", None) or []
    for chat_id, message_id in ids:
        await _safe_delete_chat_message(context, chat_id, message_id)
    # Back-compat: also drop the legacy single-id pointer if it wasn't in the list.
    legacy = context.user_data.pop("vin_conflict_msg_id", None)
    # (chat_id is unknown for the legacy pointer; the tracked list above covers the
    # real deletes — this pop just keeps stale state from leaking to the next lead.)
    _ = legacy


def _apply_single_phase1_edit(state_data: dict, edit_key: str, new_text: str) -> None:
    """Apply one field edit from the AI review flow."""
    new_text = (new_text or "").strip()
    # An extra car's key ("v2vin") writes into that car and returns; a car-1 key
    # is a no-op here and falls through to everything below, unchanged.
    if _apply_vehicle_edit(state_data, edit_key, new_text):
        return
    if edit_key in ("fn", "ln"):
        # Track first/last separately so editing them in EITHER order (or one without
        # the other) never clobbers the part you already set. Seed from the validated
        # split names (or the combined name if the splits are stale/absent).
        pf, pl = _display_name_parts(state_data)
        f = "" if pf == "-" else pf
        l = "" if pl == "-" else pl
        if edit_key == "fn":
            f = new_text if new_text and new_text != "-" else ""
        else:
            l = new_text if new_text and new_text != "-" else ""
        state_data["first_name"] = f
        state_data["last_name"] = l
        state_data["name"] = ((f + " " + l).strip() or "-") if (f or l) else "-"
        return
    if edit_key == "email":
        if not new_text or new_text == "-":
            state_data["email"] = ""
            return
        state_data["email"] = ai_vision.normalize_email(new_text) or ""
        return
    if edit_key == "dl":
        if not new_text or new_text == "-":
            state_data["driver_license_id"] = ""
            return
        state_data["driver_license_id"] = ai_vision.normalize_driver_license_id(new_text)
        return
    sk = PH1_EDIT_TO_STATE_KEY.get(edit_key)
    if sk:
        state_data[sk] = new_text if new_text else "-"


def _clean_vin_and_car(state_data: dict) -> None:
    """Identify VIN only as a 17 alphanumeric string (no cutting). Clean car from phones and any stray VIN."""
    vin_raw = (state_data.get("vin") or "").strip()
    car_raw = (state_data.get("car") or "").strip()
    # Search for exactly 17 alphanumeric in vin field first, then vin+car, so we never miss a merged line
    search_for_vin = vin_raw + " " + car_raw
    vin_17 = _extract_vin_17(phone_redact.strip_phone_patterns(search_for_vin))
    if not vin_17:
        vin_17 = _extract_vin_17(vin_raw + " " + car_raw)
    state_data["vin"] = vin_17 if vin_17 else "-"
    # Car: strip phones and remove the 17-char VIN if it ended up in the car line (so we don't duplicate or leave fragment)
    car_cleaned = phone_redact.strip_phone_patterns(car_raw)
    if vin_17 and vin_17 in car_cleaned:
        car_cleaned = car_cleaned.replace(vin_17, " ", 1)
    car_cleaned = " ".join(car_cleaned.split()).strip()
    state_data["car"] = car_cleaned or "-"
    co = state_data.get("color")
    if co is not None and str(co).strip() and str(co).strip() != "-":
        state_data["color"] = ai_vision.normalize_phase1_color(str(co))
    # Extra cars get the same VIN discipline. Their block is NOT part of the
    # vehicle_details blob rebuilt below, so this only normalises the array.
    extra = _extra_vehicles(state_data)
    if extra:
        for v in extra:
            _clean_vehicle_vin(v)
            c2 = v.get("color")
            if c2 is not None and str(c2).strip() and str(c2).strip() != "-":
                v["color"] = ai_vision.normalize_phase1_color(str(c2))
        state_data[EXTRA_VEHICLES_KEY] = extra
    # Rebuild derived fields. Every line is coerced to a string with a "-" fallback:
    # a card that is still EMPTY (only dispatch selections saved, no field keys yet)
    # has these keys MISSING, and joining a None crashed the whole edit — the handler
    # died mid-apply and the typed edit silently did nothing.
    vehicle_lines = [
        str(state_data.get(k) or "-")
        for k in (
            "name", "address", "city_state_zip", "delivery_address",
            "delivery_city_state_zip", "vin", "car", "color",
            "insurance_company", "insurance_policy_number", "extra_info",
        )
    ]
    state_data["vehicle_details"] = "\n".join(vehicle_lines)
    delivery_lines = [
        state_data.get("delivery_address"),
        state_data.get("delivery_city_state_zip"),
    ]
    state_data["delivery_details"] = "\n".join([str(l) for l in delivery_lines if l])


_PHASE1_ADJUST_FIELD_ORDER = (
    "name", "address", "city_state_zip", "delivery_address", "delivery_city_state_zip",
    "vin", "car", "color", "insurance_company", "insurance_policy_number", "extra_info",
)

_PHASE1_ADJUST_LABELS = {
    "name": "name", "address": "reg address", "city_state_zip": "reg city/ST/ZIP",
    "delivery_address": "delivery address", "delivery_city_state_zip": "delivery city/ST/ZIP",
    "vin": "VIN", "car": "car", "color": "color", "insurance_company": "insurance",
    "insurance_policy_number": "policy #", "extra_info": "date/time",
}


def _apply_caption_to_lead(state_data: dict, caption: str) -> list:
    """Read the words sent WITH a picture.

    An image and its caption are ONE message, so both must land. The caption used to
    be handed to the model as loose context only, which meant a caption like
    "name John Damian price 150" contributed nothing when the picture itself carried
    the vehicle. Labeled values are applied deterministically and WIN over what vision
    made of the picture (the person typed them on purpose); an unlabeled caption still
    gives up its phone/price/email/licence, but only into fields that are still empty.
    Returns the labels that changed."""
    text = (caption or "").strip()
    if not text:
        return []
    labeled = _apply_inline_review_text(state_data, text)
    if labeled:
        return labeled
    changed: list = []

    def _empty(key: str) -> bool:
        return str(state_data.get(key) or "").strip() in ("", "-")

    phone, price, issuer_note, driver_note = _extract_phone_price_notes_from_text(text)
    if phone and _empty("pending_phone_number"):
        state_data["pending_phone_number"] = phone
        changed.append("phone")
    if price and _empty("pending_price"):
        state_data["pending_price"] = price
        changed.append("price")
    email, dl = _extract_email_and_dl_from_text(text)
    if email and _empty("email"):
        state_data["email"] = email
        changed.append("email")
    if dl and _empty("driver_license_id"):
        state_data["driver_license_id"] = dl
        changed.append("driver license")
    if changed:
        _sanitize_phase1_pending_phone_price(state_data)
    return changed


def _ai_vin_line(structured_text: str) -> str:
    """Whatever the AI put on the VIN line of its 11-line reply, unvalidated.

    Used only to tell the issuer what was mis-read when it isn't a usable VIN —
    reading the line itself avoids guessing at random long tokens elsewhere."""
    try:
        normalized = _normalize_ai_phase1_text(structured_text or "")
        lines = [l.strip() for l in normalized.splitlines()]
        parsed = parse_phase1_structured("\n".join(lines[: ai_vision.PHASE1_LINE_COUNT]))
        return re.sub(r"[^A-Za-z0-9]", "", str(parsed.get("vin") or "")).upper()
    except Exception:
        return ""


def _merge_phase1_adjust(state_data: dict, structured_text: str, only_empty: bool = False) -> list[str]:
    """Merge an AI extraction into state_data — ONLY the fields actually found.

    Empty / "-" / placeholder values never overwrite existing data. Returns the
    human labels of the fields that changed. When ``only_empty`` is set, a field that
    already holds a real (non-placeholder) value is left untouched — used for the
    single-line voice-dictation path so a mis-detected note can never overwrite good
    data that's already on the card.
    """
    normalized = _normalize_ai_phase1_text(structured_text or "")
    lines = [l.strip() for l in normalized.splitlines()]
    parsed = parse_phase1_structured("\n".join(lines[: ai_vision.PHASE1_LINE_COUNT]))
    placeholders = {"", "-", "n/a", "na", "none", "unknown", "not found"}

    def _blocked(sk: str) -> bool:
        # In fill-only mode, keep an already-filled field (never overwrite).
        return only_empty and str(state_data.get(sk) or "").strip().lower() not in placeholders

    updated: list[str] = []
    for key in _PHASE1_ADJUST_FIELD_ORDER:
        val = str(parsed.get(key) or "").strip()
        if val.lower() in placeholders:
            continue
        if _blocked(key):
            continue
        if val != str(state_data.get(key) or "").strip():
            state_data[key] = val
            updated.append(_PHASE1_ADJUST_LABELS.get(key, key))
    # A dictated lead often puts the whole address on the street line and leaves the
    # city/ST/ZIP line empty — split it so both review fields are filled.
    for _street_key, _csz_key, _csz_label in (
        ("address", "city_state_zip", "reg city/ST/ZIP"),
        ("delivery_address", "delivery_city_state_zip", "delivery city/ST/ZIP"),
    ):
        if str(state_data.get(_csz_key) or "").strip().lower() in placeholders:
            _street, _csz = _split_street_and_csz(str(state_data.get(_street_key) or ""))
            if _csz:
                state_data[_csz_key] = _csz
                state_data[_street_key] = _street or "-"
                if _csz_label not in updated:
                    updated.append(_csz_label)
    # Labeled extra lines (12+): Phone / Price / Email / DriverLicenseID.
    for l in lines[ai_vision.PHASE1_LINE_COUNT:]:
        if ":" not in l:
            continue
        label, _, v = l.partition(":")
        v = v.strip()
        low = label.strip().lower()
        if v.lower() in placeholders:
            continue
        if low.startswith("phone"):
            if not _blocked("pending_phone_number") and len(re.sub(r"\D", "", v)) >= 10:
                state_data["pending_phone_number"] = v
                updated.append("phone")
        elif low.startswith("price"):
            if not _blocked("pending_price"):
                state_data["pending_price"] = v
                updated.append("price")
        elif low.startswith("email"):
            em = ai_vision.normalize_email(v)
            if em and not _blocked("email"):
                state_data["email"] = em
                updated.append("email")
        elif low.startswith("driverlicense") or low == "dl":
            dl = ai_vision.normalize_driver_license_id(v)
            if dl and not _blocked("driver_license_id"):
                state_data["driver_license_id"] = dl
                updated.append("driver license")
    if updated:
        for key in _PHASE1_ADJUST_FIELD_ORDER:
            if state_data.get(key) is None:
                state_data[key] = "-"
        # Only one address given? Use it for both. The typed and edit paths already
        # did this; an AI extraction (a photo of a registration, a dictated lead)
        # did not, so the delivery line stayed "-" and the driver got no address.
        _apply_single_address_as_both(state_data)   # also rebuilds the derived lines
    return updated


async def handle_phase1_adjust_input(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                     fill_only_empty: bool = False) -> int:
    """Review 'Adjust from image/text': read media/text, merge only found fields. When
    fill_only_empty is set (single-line voice dictation), never overwrite a field that
    already holds a real value."""
    _cr = _cancel_restart_kind_from_update(update)
    if _cr:
        return await _do_cancel_or_restart(update, context, _cr)
    user_id = update.effective_user.id
    state = db.get_user_state(user_id)
    if not state or not state.get("data"):
        await update.message.reply_text("❌ Data lost. Please start over with /start")
        return ConversationHandler.END
    state_data = state["data"]
    message = update.message

    note = await message.reply_text("🤖 Reading…")
    structured = None
    pdf_vin = None                      # exact VIN from a PDF text layer, if any
    adjust_caption = (message.caption or "").strip() or None
    # Keep the downloaded bytes so the SAME upload can both be read for fields AND ride
    # along to the dispatch group (no second download, no separate button).
    attach_blob, attach_ftype, attach_mime, attach_filename = None, "photo", "image/jpeg", "attachment.jpg"
    try:
        if message.photo:
            f = await context.bot.get_file(message.photo[-1].file_id)
            bio = io.BytesIO()
            await f.download_to_memory(out=bio)
            _img = bio.getvalue()
            attach_blob = _img
            structured = await asyncio.to_thread(
                lambda: ai_vision.extract_structured_from_media_parts(
                    [(_img, "image/jpeg")], typed_text=adjust_caption
                )
            )
        elif message.document:
            doc = message.document
            f = await context.bot.get_file(doc.file_id)
            bio = io.BytesIO()
            await f.download_to_memory(out=bio)
            raw = bio.getvalue()
            mime = (doc.mime_type or "").lower()
            attach_blob = raw
            if "pdf" in mime or (doc.file_name or "").lower().endswith(".pdf"):
                attach_ftype, attach_mime, attach_filename = "document", "application/pdf", (doc.file_name or "attachment.pdf")
                # Read the VIN from the PDF's TEXT layer — exact, where reading 17
                # small characters off a page render regularly missed or mangled it.
                pdf_vin = await asyncio.to_thread(ai_vision.vin_from_pdf, raw)
                png = await asyncio.to_thread(ai_vision.pdf_first_page_to_png_bytes, raw)
                if png:
                    structured = await asyncio.to_thread(
                        lambda: ai_vision.extract_structured_from_media_parts(
                            [(png, "image/png")], typed_text=adjust_caption
                        )
                    )
            elif mime.startswith("image/"):
                attach_ftype, attach_mime, attach_filename = "photo", mime, (doc.file_name or "attachment.jpg")
                structured = await asyncio.to_thread(
                    lambda: ai_vision.extract_structured_from_media_parts(
                        [(raw, mime or "image/jpeg")], typed_text=adjust_caption
                    )
                )
            else:
                attach_ftype, attach_mime, attach_filename = "document", (mime or "application/octet-stream"), (doc.file_name or "attachment.bin")
                structured = await asyncio.to_thread(
                    lambda: ai_vision.extract_structured_from_media_parts(
                        [(raw, mime or "image/jpeg")], typed_text=adjust_caption
                    )
                )
        else:
            text = (message.text or "").strip()
            if text:
                structured = await asyncio.to_thread(ai_vision.extract_structured_from_text, text)
    except ai_vision.AIVisionQuotaError:
        structured = None
    except Exception as e:
        logger.warning("Adjust-input extraction failed: %s", e)
        structured = None
    await _safe_delete_chat_message(context, note.chat_id, note.message_id)

    is_media = bool(message.photo or message.document)

    # Pure text that couldn't be read → guide the user (no image to keep/attach).
    if not structured and not is_media:
        await message.reply_text(
            "⚠️ Couldn't read that. Paste the text as a labeled edit (e.g. “price $500”) "
            "or send a photo/PDF."
        )
        return STATE_AI_REVIEW

    # Captured BEFORE the merge — afterwards the new VIN is already in place and the
    # comparison would never see a change (so the DMV check never ran).
    vin_at_start = str(state_data.get("vin") or "").strip().upper()
    updated = _merge_phase1_adjust(state_data, structured, only_empty=fill_only_empty) if structured else []
    # The caption is part of the same message as the picture — apply it AFTER the
    # image so anything the sender labeled by hand wins over the vision read.
    if is_media and adjust_caption:
        for _lbl in _apply_caption_to_lead(state_data, adjust_caption):
            if _lbl not in updated:
                updated.append(_lbl)
    # A VIN read from the PDF's text layer is exact — it beats whatever vision made of
    # the page render (this is the "PDF parse misses the VIN" fix).
    if pdf_vin and str(state_data.get("vin") or "").strip().upper() != pdf_vin:
        state_data["vin"] = pdf_vin
        _clean_vin_and_car(state_data)
        if "VIN" not in updated:
            updated.append("VIN")
    # A picture of just a VIN ("VIN:4S4…") is a normal thing to send, and it used to do
    # nothing: the 11-line parser reads only its own VIN line, and a VIN of the wrong
    # length was blanked to "-" while the toast still said "Read: VIN".
    vin_warning = None
    if structured and not pdf_vin:
        if str(state_data.get("vin") or "").strip().upper() in ("", "-"):
            # Nothing usable on the VIN line — scan the WHOLE reply, so a VIN written
            # in prose or on another line still lands.
            found = ai_vision.vin_from_text(structured)
            if found:
                state_data["vin"] = found
                _clean_vin_and_car(state_data)
                if "VIN" not in updated:
                    updated.append("VIN")
            else:
                # Something VIN-shaped but the wrong length: say so instead of
                # silently showing "-". Check the AI's own VIN line as well as any
                # labelled token, since the 11-line block carries no "VIN:" label.
                near = ai_vision.vin_near_miss_from_text(structured) or _ai_vin_line(structured)
                if near and len(near) != 17:
                    vin_warning = (
                        f"⚠️ I read the VIN as `{near}` — that's {len(near)} characters "
                        "and a VIN is 17, so I didn't save it.\nSend a clearer picture, "
                        "or type `vin <the 17 characters>`."
                    )
                if "VIN" in updated:
                    updated.remove("VIN")          # never claim a VIN we refused
    vin_now = str(state_data.get("vin") or "").strip().upper()
    vin_changed = bool(vin_now) and vin_now != "-" and vin_now != vin_at_start
    db.set_user_state(user_id, "phase1", state_data)
    await _autoclean_user_msg(update, context)  # text is cleared; media is always kept

    if is_media:
        # ONE upload does everything: fields were read above (so spelling can be
        # double-checked), the picture stays visible, AND it rides along to the dispatch
        # group. Keep the review card directly UNDER the picture.
        upd_txt = ", ".join(dict.fromkeys(updated)) if updated else ""
        if attach_blob is None:
            # The file couldn't be fetched — nothing was read or attached. Say so plainly
            # instead of claiming a (0) attachment.
            note_txt = "⚠️ Couldn't read that file — please send it again."
        else:
            cap_note = _add_extra_attachment(context, attach_ftype, attach_mime, attach_filename, attach_blob, adjust_caption)
            n_att = len(context.user_data.get("phase1_extra_attachments") or [])
            if cap_note:
                # A size/count cap blocked the attach — but any parsed fields WERE saved,
                # so surface both, not just the cap notice.
                note_txt = (f"✅ Read: {upd_txt}\n{cap_note}") if upd_txt else cap_note
            elif updated:
                note_txt = f"✅ Read & attached ({n_att}): {upd_txt}"
            else:
                note_txt = f"📎 Image attached ({n_att}) — it'll go to the team that accepts the lead."
        await _send_vanishing(context, message.chat_id, note_txt)
        await _reanchor_review_card(context, message.chat_id, state_data)
    else:
        # Typed text: vanishing toast + edit the card in place (nothing pushes it up).
        toast = ("✅ Updated: " + ", ".join(dict.fromkeys(updated))) if updated else "ℹ️ Nothing new found — the review is unchanged."
        await _send_vanishing(context, message.chat_id, toast)
        await _update_review_message_text(context, state_data)
    if vin_warning:
        # Stays on screen: it needs an action, unlike the vanishing toasts.
        await message.reply_text(vin_warning, parse_mode="Markdown")
    elif vin_changed:
        # A VIN that just arrived from a picture/PDF is exactly when the DMV decode is
        # wanted — run it without making the issuer hunt for the button.
        return await _run_vin_check_for_review(update, context, user_id, state_data)
    return STATE_AI_REVIEW


# ── Inline review edits: type a change (no Edit button) ──────────────────────
# Human labels/aliases → the edit_key understood by _apply_single_phase1_edit.
# Longest aliases are tried first so "last name"/"policy number" beat "name"/"policy".
# Bare single words that commonly START an ordinary sentence (first, last, note,
# date, time, driver, issuer, city, street, client, number, cell, license …) are
# intentionally NOT aliases, so casual chat like "driver is en route" or "email me
# later" isn't misread as an edit — use the fuller label ("driver note", "first name").
# ── Car insurance carriers ──────────────────────────────────────────────────
# "policy", "policy name", "insurance policy" and "company policy" all get said for
# BOTH halves of the insurance, so the VALUE decides which one is meant: a carrier
# name goes to the company field, a number to the policy #, and "geico 8829301"
# fills both.
#
# Split in two because some carriers are ordinary English words. A STRONG name is
# unmistakable and counts on its own ("Geico" typed alone is the carrier, never the
# client's name). A WEAK one only counts once insurance is in play — either the user
# labelled it, or the word "insurance" is in the value — so a driver note reading
# "root of the problem" or a client named Shelter is left alone.
_INSURERS_STRONG = {
    "GEICO": ("geico", "gieco", "geiko", "gieko", "geico general"),
    "State Farm": ("state farm", "statefarm", "state farm mutual"),
    "Progressive": ("progressive", "progresive"),
    "Allstate": ("allstate", "all state"),
    "USAA": ("usaa",),
    "Liberty Mutual": ("liberty mutual", "liberty mutual fire"),
    "Farmers": ("farmers", "farmers insurance"),
    "Nationwide": ("nationwide", "nation wide"),
    "Travelers": ("travelers", "travellers"),
    "American Family": ("american family", "amfam"),
    "Auto-Owners": ("auto owners", "auto-owners"),
    "Safeco": ("safeco", "safe co"),
    "Esurance": ("esurance", "e surance"),
    "Kemper": ("kemper",),
    "Plymouth Rock": ("plymouth rock", "plymouth rock assurance"),
    "NJM": ("njm", "new jersey manufacturers", "nj manufacturers"),
    "The Hanover": ("hanover",),
    "MetLife": ("metlife", "met life"),
    "Amica": ("amica",),
    "Chubb": ("chubb",),
    "The Hartford": ("hartford",),
    "National General": ("national general", "natgen", "nat gen"),
    "Dairyland": ("dairyland", "dairy land"),
    "Bristol West": ("bristol west",),
    "21st Century": ("21st century", "twenty first century"),
    "Clearcover": ("clearcover", "clear cover"),
    "GAINSCO": ("gainsco",),
    "CURE": ("cure auto", "cure insurance", "cure auto insurance"),
    "Palisades": ("palisades",),
    "High Point": ("high point",),
    "Rutgers Casualty": ("rutgers casualty", "rutgers"),
    "Franklin Mutual": ("franklin mutual",),
    "Preferred Mutual": ("preferred mutual",),
    "Wawanesa": ("wawanesa",),
    "Donegal": ("donegal",),
    "Penn National": ("penn national",),
    "Utica National": ("utica", "utica national"),
    "Trexis": ("trexis",),
    "SafeAuto": ("safeauto", "safe auto"),
    "Fred Loya": ("fred loya",),
    "Mendota": ("mendota",),
    "Ocean Harbor": ("ocean harbor", "ocean harbour"),
    "Good2Go": ("good2go", "good to go"),
    "Assurance America": ("assurance america",),
    "Alliance United": ("alliance united",),
    "Bluefire": ("bluefire", "blue fire"),
    "Wawa Casualty": ("wawa casualty",),
    "Amtrust": ("amtrust", "am trust",),
    "Direct Auto": ("direct auto", "direct general"),
    "Anchor General": ("anchor general",),
    "Aspire General": ("aspire general",),
    "First Chicago": ("first chicago",),
    "Home State": ("home state county mutual",),
    "United Automobile": ("united automobile", "united auto"),
    "Star Casualty": ("star casualty",),
    "Pronto": ("pronto insurance",),
    "Hallmark": ("hallmark insurance",),
    "Freeway": ("freeway insurance",),
    "Acceptance": ("acceptance insurance",),
    "Infinity": ("infinity insurance",),
    "Titan": ("titan insurance",),
}
_INSURERS_WEAK = {
    # Car makes (Mercury, Plymouth), towns (Erie, Westfield) and everyday words —
    # all real carriers, none of them safe to assume from a bare value.
    "Mercury": ("mercury",),
    "Erie": ("erie",),
    "Selective": ("selective",),
    "Encompass": ("encompass",),
    "Foremost": ("foremost",),
    "Midvale": ("midvale",),
    "Westfield": ("westfield",),
    "Grange": ("grange",),
    "Redpoint": ("redpoint",),
    "Victoria": ("victoria",),
    "Root": ("root",),
    "Hugo": ("hugo",),
    "Elephant": ("elephant",),
    "Lemonade": ("lemonade",),
    "Shelter": ("shelter",),
    "Sentry": ("sentry",),
    "The General": ("the general", "general"),
    "AAA": ("aaa", "triple a", "auto club"),
    "Alfa": ("alfa",),
    "Country Financial": ("country financial", "country"),
    "Slide": ("slide",),
    "Loya": ("loya",),
    "Cure": ("cure",),
}
# Words a carrier is routinely said with, dropped before matching so
# "Geico Insurance Company" and "geico" are the same carrier.
_INSURER_NOISE_RE = re.compile(
    r"\b(insurance|insurances|insurer|ins|company|companies|co|corp|corporation|"
    r"group|agency|mutual|casualty|indemnity|underwriters|assurance|auto|automobile|"
    r"car|vehicle|policy|carrier|coverage|name|provider|the|of|my|is|for)\b",
    re.IGNORECASE,
)
# The value itself says this is insurance, even for a carrier we don't know.
_INSURANCE_WORD_RE = re.compile(
    r"\b(insurance|insurer|assurance|casualty|indemnity|underwriters)\b", re.IGNORECASE)


def _insurer_lookup(table: dict) -> dict:
    return {alias: canon for canon, aliases in table.items() for alias in aliases}


_INSURER_STRONG_MAP = _insurer_lookup(_INSURERS_STRONG)
_INSURER_WEAK_MAP = _insurer_lookup(_INSURERS_WEAK)


def _insurer_re(lookup: dict) -> "re.Pattern":
    # Longest alias first so "liberty mutual fire" wins over "liberty mutual".
    return re.compile(
        r"\b(" + "|".join(re.escape(a) for a in sorted(lookup, key=len, reverse=True)) + r")\b",
        re.IGNORECASE,
    )


_INSURER_STRONG_RE = _insurer_re(_INSURER_STRONG_MAP)
_INSURER_WEAK_RE = _insurer_re(_INSURER_WEAK_MAP)


def _insurer_name(value, labeled_insurance: bool = False) -> str:
    """The carrier this value names, canonically spelled, or "" if it names none.

    ``labeled_insurance`` = the user already said insurance/policy, which is what
    lets the everyday-word carriers (Root, Hugo, The General) count."""
    raw = re.sub(r"\s+", " ", str(value or "")).strip()
    if not raw:
        return ""
    m = _INSURER_STRONG_RE.search(raw)
    if m:
        return _INSURER_STRONG_MAP[m.group(1).lower()]
    said_insurance = bool(_INSURANCE_WORD_RE.search(raw))
    if not (labeled_insurance or said_insurance):
        return ""
    m = _INSURER_WEAK_RE.search(raw)
    if m:
        return _INSURER_WEAK_MAP[m.group(1).lower()]
    # A carrier we simply don't have on the list — "Ocean Harbor Insurance". Keep
    # the user's own spelling; only the noise words come off.
    if said_insurance:
        core = _INSURER_NOISE_RE.sub(" ", raw)
        core = re.sub(r"[^\w&/\- ]+", " ", core)
        core = re.sub(r"\s+", " ", core).strip(" .,-")
        if core and len(core.split()) <= 5 and not re.search(r"\d", core):
            return core
    return ""


# A US phone as it appears inside a sentence: optional +1, then 3-3-4 with any of
# the usual separators. Used to lift the number out of surrounding words.
_PHONE_IN_TEXT_RE = re.compile(
    r"(?:\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}\b")


# A policy / binder number: 5+ characters with at least one digit.
_POLICY_NUM_RE = re.compile(r"\b(?=[A-Za-z0-9][A-Za-z0-9\-]*\d)[A-Za-z0-9\-]{5,}\b")


def _carrier_is_never_a_person(edit_key: str, value: str) -> str:
    """Send an unmistakable carrier to the insurance field even when the line said
    "name". A company name and a person's name are different things, and however
    the label was worded the VALUE settles which one this is.

    Only the carriers we recognise outright move — an unknown company still
    honours the label, so a real client is never dragged out of the name field."""
    if edit_key not in ("name", "fn", "ln"):
        return edit_key
    return "ins" if _insurer_name(value) else edit_key


def _expand_insurance_pair(edit_key: str, value: str):
    """Turn one insurance edit into the field(s) it really is, or None when this
    isn't an insurance edit at all.

    "policy geico" is the carrier, "policy 8829301" is the number, and
    "policy geico 8829301" is both — all three get said."""
    if edit_key not in ("ins", "pol"):
        return None
    raw = str(value or "").strip()
    if not raw or raw == "-":
        return None
    carrier = _insurer_name(raw, labeled_insurance=True)
    number = ""
    rest = raw
    if carrier:
        # Don't mistake part of the carrier's own name for the policy number.
        for alias in sorted(set(_INSURER_STRONG_MAP) | set(_INSURER_WEAK_MAP) | {carrier.lower()},
                            key=len, reverse=True):
            rest = re.sub(r"\b" + re.escape(alias) + r"\b", " ", rest, flags=re.IGNORECASE)
    m = _POLICY_NUM_RE.search(rest)
    if m:
        number = m.group(0)
    if carrier and number:
        return [("ins", carrier), ("pol", number)]
    if carrier:
        return [("ins", carrier)]
    if number:
        return [("pol", number)]
    # Neither — a bare word after "insurance"/"policy". Keep the field they named.
    return None


_INLINE_EDIT_ALIASES = {
    "first name": "fn", "firstname": "fn",
    "last name": "ln", "lastname": "ln",
    "name": "name", "full name": "name", "client name": "name",
    # Both halves asked for together mean the whole name — see also
    # _merge_double_name_labels for the phrasings not listed here.
    "first and last name": "name", "first and last": "name",
    "first last name": "name", "first name and last name": "name",
    "reg address": "addr", "registration address": "addr", "address": "addr", "addr": "addr",
    "reg city": "csz", "city/st/zip": "csz", "city state zip": "csz", "csz": "csz",
    "delivery address": "daddr", "deliv address": "daddr", "delivery addr": "daddr", "daddr": "daddr", "drop off": "daddr", "dropoff": "daddr",
    "delivery city": "dcsz", "deliv city": "dcsz", "delivery city/st/zip": "dcsz", "dcsz": "dcsz",
    "vin": "vin",
    "car": "car", "make/model": "car",
    "color": "col", "colour": "col",
    "insurance": "ins", "insurance company": "ins", "carrier": "ins",
    "insurance carrier": "ins", "insurance name": "ins", "insurer": "ins",
    "insurance co": "ins", "company name": "ins",
    # "name" is a field of its own, so these have to be listed whole or the phrase
    # splits at it and the carrier is filed as the client (see _LABEL_TAIL_WORDS).
    "insurance company name": "ins", "insurance company's name": "ins",
    "insurance companys name": "ins", "carrier name": "ins",
    "name of the insurance company": "ins", "name of insurance company": "ins",
    "name of the insurance": "ins", "name of insurance": "ins",
    "insurance provider": "ins", "provider": "ins",
    "policy number": "pol", "policy #": "pol", "policy": "pol", "binder": "pol", "binder #": "pol",
    # Said for either half — _expand_insurance_pair reads the value to tell
    # the carrier ("policy geico") from the number ("policy 8829301").
    "policy name": "ins", "insurance policy": "pol", "company policy": "ins",
    "policy company": "ins",
    "date/time": "xtra", "delivery time": "xtra", "extra info": "xtra",
    "phone number": "phone", "phone": "phone",
    "price": "price", "cost": "price", "amount": "price", "quote": "price",
    "issuer note": "issuer",
    "driver note": "driver",
    "email": "email", "e-mail": "email",
    "driver license": "dl", "drivers license": "dl", "driver's license": "dl", "dln": "dl",
}
_INLINE_EDIT_KEY_LABEL = {
    "fn": "first name", "ln": "last name", "name": "name", "addr": "reg address", "csz": "reg city/ST/ZIP",
    "daddr": "delivery address", "dcsz": "delivery city/ST/ZIP", "vin": "VIN", "car": "car", "col": "color",
    "ins": "insurance", "pol": "policy #", "xtra": "date/time", "phone": "phone", "price": "price",
    "issuer": "issuer note", "driver": "driver note", "email": "email", "dl": "driver license",
}
# edit_key → the state_data field it writes, so we can report only what actually
# changed (the phone/price sanitizer may reject an invalid value after we apply it).
_INLINE_EK_STATE_KEY = {
    "fn": "name", "ln": "name", "name": "name",
    "addr": "address", "csz": "city_state_zip", "daddr": "delivery_address", "dcsz": "delivery_city_state_zip",
    "vin": "vin", "car": "car", "col": "color", "ins": "insurance_company", "pol": "insurance_policy_number", "xtra": "extra_info",
    "phone": "pending_phone_number", "price": "pending_price", "email": "email", "dl": "driver_license_id",
    "issuer": "special_request_issuers", "driver": "special_request_drivers",
}
# A label said in the plural or possessive is the same label: "the colors is black",
# "phone numbers", "client's name". Kept OUTSIDE the capture group so the alias
# lookup still sees the singular it was written as.
_ALIAS_PLURAL = r"(?:'s|e?s)?"
# Label, then a separator (":", "=", or whitespace), then the value.
_INLINE_LABEL_RE = re.compile(
    r"^\s*(" + "|".join(re.escape(k) for k in sorted(_INLINE_EDIT_ALIASES, key=len, reverse=True))
    + r")" + _ALIAS_PLURAL + r"\s*(?:[:=]|\s)\s*(.+)$",
    re.IGNORECASE,
)


def _clean_inline_value(edit_key: str, value: str) -> str:
    """Validate/normalize a typed value for its field. Returns "" if the value
    doesn't fit the field — so e.g. 'phone is dead' or 'email me later' are NOT
    treated as edits, and 'price 50' becomes '$50' (the sanitizer needs the $)."""
    # Drop a leading filler word for every field ("color is white" -> "white",
    # "change name to john doe" -> "john doe", "phone: 555-123-4567" -> "555-123-4567").
    value = value.strip().lstrip(",;:").strip()          # "…, john doe"
    value = re.sub(r"^(?:is|are|to|=|:)\s+", "", value, flags=re.IGNORECASE).strip()
    if not value:
        return ""
    # "color black please" is the colour black. Only for fields where a trailing
    # courtesy is never part of the value — see _TAIL_STRIPPABLE_EKS.
    if edit_key in _TAIL_STRIPPABLE_EKS:
        prev = None
        while value and value != prev:
            prev = value
            value = _TAIL_SCAFFOLD_RE.sub("", value).strip()
        if not value:
            value = prev or ""
        if not value:
            return ""
    if edit_key == "price":
        m = re.search(r"\d[\d,]*(?:\.\d+)?", value)
        if not m:
            return ""
        amount = "$" + m.group(0).replace(",", "")
        # "150 + toll" / "150 plus tolls" quotes the same job with the toll on top.
        return amount + _PH1_TOLL_SUFFIX if _price_has_toll(value) else amount
    if edit_key in ("name", "fn", "ln"):
        # "name of the insurance company State Farm" left "of the" behind.
        if all(w.strip(".,'").lower() in _FILLER_NOT_A_NAME for w in value.split()):
            return ""
    if edit_key == "ins":
        # "gieco" / "state farm insurance co" -> the carrier's real name.
        return _insurer_name(value, labeled_insurance=True) or value
    if edit_key == "phone":
        # Pull the number OUT rather than accept the whole string: a phone said
        # mid-sentence used to be stored with the rest of the sentence attached
        # ("551-301-3737. The colors is black.").
        m = _PHONE_IN_TEXT_RE.search(value)
        if m:
            return re.sub(r"\s+", " ", m.group(0)).strip(" .,-")
        return value if len(re.sub(r"\D", "", value)) >= 10 else ""
    if edit_key == "email":
        return value if "@" in value else ""
    if edit_key == "vin":
        v = value.replace(" ", "")
        return v if (len(v) >= 11 and v.isalnum()) else ""
    if edit_key == "dl":
        return value if len(value.split()) <= 4 else ""
    # "…is black." — the full stop belongs to the sentence, not to the colour.
    return value.rstrip(" .,;:!").strip() or value


# Words that may precede the first field label without making it "prose"
# ("enter phone …", "set the price 200", "change color to white").
# Words that may PRECEDE a field label without making the line prose. Safe to
# grow: a word here only ever gets skipped on the way to finding the label.
_FIELD_LEAD_FILLERS = frozenset({
    "enter", "set", "change", "make", "update", "put", "add", "please", "the", "a",
    "an", "my", "to", "for", "is", "are", "it", "its", "it's", "this", "that", "new",
    "lead", "client", "can", "you", "i", "we", "and", "with", "of", "also", "now",
    # Grown for fluency: determiners, modals and the noises people make while
    # thinking. Deliberately NO nouns (customer, guy, lady, owner) and no words
    # that are common given names — see _FILLER_NOT_A_NAME below for why.
    "their", "his", "her", "our", "your", "hers", "theirs", "them", "could",
    "would", "should", "i'd", "id", "i'm", "im", "i've", "ive", "like", "want",
    "need", "wanna", "gonna", "let", "lets", "let's", "go", "ahead", "just",
    "ok", "okay", "um", "uh", "so", "well", "hey", "yo", "actually", "sorry",
    "alright", "real", "quick", "gimme", "kindly", "wait", "anyway", "still",
})

# A value made ENTIRELY of these is not a name — "change name to the" stores
# nothing. Frozen at the 33 words that set held before it was grown for fluency,
# and deliberately NOT the same object: every word added above would otherwise
# become a name that cannot be typed. "Will", "Mark", "Just", "Guy", "Quick" and
# "Still" are all real names, and the failure is silent — a green "Updated"
# toast over a field that was cleared.
_FILLER_NOT_A_NAME = frozenset({
    "enter", "set", "change", "make", "update", "put", "add", "please", "the", "a",
    "an", "my", "to", "for", "is", "are", "it", "its", "it's", "this", "that", "new",
    "lead", "client", "can", "you", "i", "we", "and", "with", "of", "also", "now",
})

# Politeness and filler that arrive AFTER a value: "black please", "Susan thanks".
# Applied only to the fields in _TAIL_STRIPPABLE_EKS — a note must keep its
# "thanks", and an address must keep its "OK" (Oklahoma).
_TAIL_SCAFFOLD_RE = re.compile(
    r"(?:\s|^)(?:please|pls|plz|thanks|thank\s+you|thx|ty|cheers|"
    r"for\s+(?:me|us|now)|asap|alright|okay|ok)\s*[.!,]*\s*$",
    re.IGNORECASE,
)
# Which fields may have their tail shaved. The EXCLUSIONS are the design:
#   issuer/driver/xtra  — a note must keep its "thanks"
#   addr/csz/daddr/dcsz — "Tulsa OK" would be shaved to "Tulsa"; OK is a state
#   phone/price/email   — these EXTRACT rather than accept, so nothing to shave
_TAIL_STRIPPABLE_EKS = frozenset({"col", "fn", "ln", "name", "dl", "ins", "car", "pol", "vin"})
# Every alias as a standalone word, longest-first (so "delivery address" beats "address").
_MULTIFIELD_ALIAS_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in sorted(_INLINE_EDIT_ALIASES, key=len, reverse=True))
    + r")" + _ALIAS_PLURAL + r"\b",
    re.IGNORECASE,
)
# Structured fields whose value is a number/code — a strong signal a new field starts.
_STRUCTURED_EK = frozenset({"phone", "price", "email", "vin", "pol", "dl"})
# Common English words that ALSO happen to be aliases and routinely appear inside a
# free-text value (a street "12 Car St", "30 Color Ave"). These only start a new field
# when the field before them was structured (so they don't chop up an address/note).
# Words that FINISH a label instead of starting a field when they follow one
# immediately: "insurance company name", "policy number name".
_LABEL_TAIL_WORDS = frozenset({"name", "names", "number", "numbers"})
_WEAK_ALIASES = frozenset({"car", "color", "colour", "cost", "amount", "quote", "carrier", "binder"})


_NAME_EKS = frozenset({"fn", "ln", "name"})


def _merge_double_name_labels(boundaries, matches, line):
    """"first name and last name, john doe" is ONE edit — the whole name.

    Said that way the first label has no value of its own, so it used to be dropped
    while the last label swallowed the lot ("Maria , john doe"). Where name labels
    run together with nothing but filler between them, keep only the last and treat
    what follows as the full name."""
    if len(boundaries) < 2:
        return boundaries
    out = []
    for i, (mi, ek) in enumerate(boundaries):
        if i + 1 < len(boundaries) and ek in _NAME_EKS:
            next_mi, next_ek = boundaries[i + 1]
            if next_ek in _NAME_EKS:
                between = line[matches[mi].end():matches[next_mi].start()]
                tokens = [t for t in re.split(r"[\s,]+", between.strip().lower()) if t]
                if all(t.strip(".,") in _FIELD_LEAD_FILLERS for t in tokens):
                    boundaries[i + 1] = (next_mi, "name")   # the value is the whole name
                    continue                                 # and this label carries none
        out.append((mi, ek))
    return out


def _parse_multi_field_line(line: str):
    """Split one line into (edit_key, value) pairs, so 'price 200 address 321 Main St
    Fort Lee NJ 07024' sets BOTH fields. A weak alias (car/color/cost…) only starts a
    new field after a structured one, so it never chops up an address/note value.
    Returns [(edit_key, value), ...] or None when the line isn't clean labeled edits."""
    matches = list(_MULTIFIELD_ALIAS_RE.finditer(line))
    if not matches:
        return None
    # Text before the first alias must be only filler words — else it's prose.
    head = line[: matches[0].start()]
    for tok in re.split(r"[\s,]+", head.strip().lower()):
        tok = re.sub(r"'s$", "", tok.strip(".:;-_'\""))   # "client's name …"
        if tok and tok not in _FIELD_LEAD_FILLERS:
            return None
    # Decide which alias matches actually START a new field (are boundaries).
    boundaries: list[tuple[int, str]] = []
    cur_ek = None
    for i, m in enumerate(matches):
        alias = m.group(1).lower().strip()
        ek = _INLINE_EDIT_ALIASES.get(alias)
        if ek is None:
            return None
        # "insurance company NAME Geico": the label ran straight into this one with
        # no value between them, so this word finishes the label rather than
        # starting a field. Without this the carrier was filed as the client.
        if (alias in _LABEL_TAIL_WORDS and boundaries
                and not line[matches[i - 1].end():m.start()].strip()):
            continue
        if i == 0 or alias not in _WEAK_ALIASES or (cur_ek in _STRUCTURED_EK):
            boundaries.append((i, ek))
            cur_ek = ek
        # else: weak alias inside a free-text value → absorb into the current field
    boundaries = _merge_double_name_labels(boundaries, matches, line)
    pairs: list[tuple[str, str]] = []
    for bi, (mi, ek) in enumerate(boundaries):
        m = matches[mi]
        end = matches[boundaries[bi + 1][0]].start() if bi + 1 < len(boundaries) else len(line)
        raw = re.sub(r"^\s*[:=]?\s*", "", line[m.end():end])
        # "policy geico 8829301" is the carrier AND the number, whichever of the
        # insurance words was used to say it. Split before cleaning: cleaning an
        # insurance value canonicalises the carrier and would drop the number.
        ins = _expand_insurance_pair(ek, raw.strip())
        if ins:
            pairs.extend((p_ek, _clean_inline_value(p_ek, p_val) or p_val)
                         for p_ek, p_val in ins)
            continue
        ek = _carrier_is_never_a_person(ek, raw)
        val = _clean_inline_value(ek, raw)
        if val:
            pairs.append((ek, val))
        elif pairs:  # empty structured value → the alias was part of the previous value
            prev_ek, prev_val = pairs[-1]
            merged = _clean_inline_value(prev_ek, (prev_val + " " + line[m.start():end]).strip())
            pairs[-1] = (prev_ek, merged or prev_val)
    return pairs or None


# ── Two (or more) cars in one pasted message ────────────────────────────────
_YEAR_IN_LINE_RE = re.compile(r"\b(19[89]\d|20[0-4]\d)\b")
_MOSTLY_DIGITS_RE = re.compile(r"^[\s#:.\-]*\d[\d\s.\-]{4,}$")
_COLOR_LABEL_RE = re.compile(r"^\s*colou?r\b[\s:.\-]*", re.IGNORECASE)


def _split_vehicle_blocks(text: str):
    """Split a paste into one block per car, or None when there is only one car.

    Returns ``(blocks, shared)`` where ``blocks`` has one entry per VIN and
    ``shared`` is everything that belongs to the job rather than to a car — the
    header and the delivery/phone tail.

    Paragraphs are the primary boundary because that is how these pastes actually
    arrive; two VINs inside one paragraph fall back to cutting at the line holding
    the second VIN.
    """
    vins = _all_vins_17(text)
    if len(vins) < 2:
        return None

    paras, current = [], []
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        if line.strip():
            current.append(line)
        elif current:
            paras.append(current)
            current = []
    if current:
        paras.append(current)

    def vins_in(lines):
        return _all_vins_17("\n".join(lines))

    # One paragraph holding two VINs: cut it at the line carrying the later one.
    expanded = []
    for p in paras:
        found = vins_in(p)
        if len(found) < 2:
            expanded.append(p)
            continue
        chunk, seen_one = [], False
        for line in p:
            if seen_one and _all_vins_17(line):
                expanded.append(chunk)
                chunk, seen_one = [line], True
                continue
            if _all_vins_17(line):
                seen_one = True
            chunk.append(line)
        if chunk:
            expanded.append(chunk)

    blocks, shared, pending = [], [], []
    for p in expanded:
        if vins_in(p):
            # Text sitting just above a car belongs to that car (its owner name).
            blocks.append(pending + p)
            pending = []
        elif blocks:
            pending = pending + p          # might belong to the next car
        else:
            shared.append(p)               # header, before any car
    if pending:
        # Nothing after it, so it is the job's tail: delivery, phone, price.
        shared.append([ln for p in [pending] for ln in p])

    if len(blocks) < 2:
        return None
    return (["\n".join(b) for b in blocks],
            "\n".join("\n".join(p) for p in shared))


def _fields_from_vehicle_block(block: str) -> dict:
    """One car's fields out of its own block of a paste.

    Deliberately ordered and consume-as-you-go: each line is claimed by the first
    rule that recognises it, so the same line can never fill two fields.
    """
    v = _blank_vehicle()
    lines = [ln.strip() for ln in (block or "").split("\n") if ln.strip()]
    used = [False] * len(lines)

    def claim(i):
        used[i] = True

    # 1. VIN — the anchor the whole block was found by.
    for i, ln in enumerate(lines):
        found = _extract_vin_17(ln)
        if found:
            v["vin"] = found.upper()
            claim(i)
            break

    # 2. Colour — an explicit "Color: grey" label, else a line that is just a
    #    colour word ("grey" on its own line, as these pastes are written).
    for i, ln in enumerate(lines):
        if used[i]:
            continue
        if _COLOR_LABEL_RE.match(ln):
            v["color"] = ai_vision.normalize_phase1_color(
                _clean_spoken_color(_COLOR_LABEL_RE.sub("", ln)) or "") or ""
            claim(i)
            break
        if len(ln.split()) <= 2 and ln.replace("-", " ").split()[0].casefold() in _COMMON_COLORS:
            v["color"] = ai_vision.normalize_phase1_color(_clean_spoken_color(ln) or ln) or ""
            claim(i)
            break

    # 3. Car — a year makes it unambiguous, and an address line never has one
    #    that is not also a house number, which the csz test below catches first.
    for i, ln in enumerate(lines):
        if used[i] or not _YEAR_IN_LINE_RE.search(ln):
            continue
        street, csz = _split_street_and_csz(ln)
        if csz:
            continue                       # "... Seffner Florida 33584" is an address
        v["car"] = ln
        claim(i)
        break

    # 4. Address, written on one line OR two.
    #
    #    One line ("9 hibiscus Lane Monticello New York 13701") splits cleanly.
    #    Two lines are just as common:
    #        11530 Mango terrace drive apt.102
    #        Seffner Florida 33584
    #    and neither half yields both parts, which is why this used to come out
    #    blank. _split_street_and_csz hands back the whole line as "street"
    #    whenever it finds no city/state/ZIP, so it says yes to the owner's name
    #    and the policy number too — _STREET_RE is what actually recognises a
    #    street, and of every line in one of these blocks only a real address
    #    line matches it.
    placed_address = False
    for i, ln in enumerate(lines):
        if used[i]:
            continue
        street, csz = _split_street_and_csz(ln)
        if street and csz:
            v["address"] = street
            v["city_state_zip"] = csz
            claim(i)
            placed_address = True
            break

    if not placed_address:
        street_i = next((i for i, ln in enumerate(lines)
                         if not used[i] and _STREET_RE.search(ln)), None)
        # The city/state/ZIP normally sits just under the street, so look there
        # first and only then anywhere else in the block.
        def _csz_at(i):
            return _split_street_and_csz(lines[i])[1] if 0 <= i < len(lines) and not used[i] else ""

        if street_i is not None:
            csz_i = next((j for j in range(street_i + 1, len(lines)) if _csz_at(j)), None)
            if csz_i is None:
                csz_i = next((j for j in range(len(lines))
                              if j != street_i and _csz_at(j)), None)
            if csz_i is not None:
                v["address"] = _split_street_and_csz(lines[street_i])[0] or lines[street_i]
                v["city_state_zip"] = _csz_at(csz_i)
                claim(street_i)
                claim(csz_i)
                placed_address = True

    if not placed_address:
        # A city/state/ZIP with no street line at all is still worth keeping: the
        # tag prints the city, state and ZIP, and a missing street is visible on
        # the card where a missing everything is not.
        for i, ln in enumerate(lines):
            if used[i]:
                continue
            csz = _split_street_and_csz(ln)[1]
            if csz:
                v["city_state_zip"] = csz
                claim(i)
                break

    # 5. Insurer, then the policy number POSITIONALLY. The classifier in this
    #    codebase reads "0407306000" as a phone number, so asking it would file
    #    Geico's policy as the client's phone.
    for i, ln in enumerate(lines):
        if used[i]:
            continue
        carrier = _insurer_name(ln)
        if not carrier:
            continue
        v["insurance_company"] = carrier
        claim(i)
        for j in range(i + 1, len(lines)):
            if used[j]:
                continue
            if _MOSTLY_DIGITS_RE.match(lines[j]):
                v["insurance_policy_number"] = lines[j].strip(" #:.-")
                claim(j)
            break
        break

    # 6. Name — whatever is left that reads like one.
    for i, ln in enumerate(lines):
        if used[i]:
            continue
        words = ln.replace(",", " ").split()
        if 2 <= len(words) <= 5 and all(w.replace(".", "").replace("-", "").isalpha() for w in words):
            v["name"] = " ".join(words)
            claim(i)
            break

    _clean_vehicle_vin(v)
    return v


def _apply_multi_vehicle_paste(state_data: dict, text: str):
    """Pull cars 2..N out of a paste and put them on the card.

    Returns ``(car1_text, added)``: the text still to be parsed for car 1 (its own
    block plus the shared header/tail), and how many extra cars were added. On a
    single-car paste returns ``(text, 0)`` and changes nothing.
    """
    if _extra_vehicles(state_data):
        return (text, 0)                   # extra cars already on the card
    split = _split_vehicle_blocks(text)
    if not split:
        return (text, 0)
    blocks, shared = split
    extras = [_fields_from_vehicle_block(b) for b in blocks[1:]]
    extras = [v for v in extras if not _vehicle_is_empty(v)]
    if not extras:
        return (text, 0)
    state_data[EXTRA_VEHICLES_KEY] = extras

    # Car 1 gets the same per-block treatment as the others. Handing its block to
    # the generic parser along with the shared lines is what let a header line
    # ("Client Charles") win the name and let Geico's policy number go missing —
    # that classifier reads a 10-digit policy as a phone number.
    first = _fields_from_vehicle_block(blocks[0])
    for key in VEHICLE_FIELD_KEYS:
        val = first.get(key)
        if not _is_blank_field(val):
            state_data[key] = val

    # Only the header and tail go on to the generic parser: phone, price,
    # delivery address and date/time notes belong to the job, and that parser
    # handles them well.
    return (shared, len(extras))


def _apply_bulk_review_text(state_data: dict, text: str):
    """Apply a MULTI-LINE paste line by line, keeping what each line gives.

    The strict parser is all-or-nothing on purpose (mixed prose belongs to the AI),
    but for a bulk send that meant one unreadable line threw away every readable one:
    "rrod782@gmail.com / Email now / Color white" lost the colour as well. Here each
    line is tried on its own; what is not a labeled edit comes back as leftovers for
    the caller to place or hand to the AI. Returns (labels_applied, leftover_lines)."""
    labels: list[str] = []
    leftovers: list[str] = []
    for line in [l.strip() for l in re.split(r"[\n;]+", text or "") if l.strip()]:
        got = _apply_inline_review_text(state_data, line)
        if got:
            labels.extend(l for l in got if l not in labels)
        else:
            leftovers.append(line)
    return labels, leftovers


async def _place_bulk_leftover(state_data: dict, line: str) -> list:
    """Place ONE leftover line from a bulk paste — conservatively.

    Strong structured signals (email, phone, price, VIN, street address) or a
    confident AI classification only. Deliberately NOT the loose "one to four plain
    words must be a name" guess used for a single typed value: in a bulk paste that
    turned a stray note into the client's name, and overwrote a good one that came
    from an earlier line in the SAME message. A field already filled is never
    replaced by a leftover."""
    ek = _structured_value_ek(line)
    value = line
    if not ek and Config.is_ai_vision_configured():
        try:
            res = await asyncio.to_thread(ai_vision.classify_field_value, line)
        except Exception as e:
            logger.warning("bulk leftover classify failed: %s", e)
            res = None
        if isinstance(res, dict) and res.get("field") and res.get("field") != "unknown":
            ek = res["field"]
            value = str(res.get("value") or line)
    if not ek:
        return []
    sk = _INLINE_EK_STATE_KEY.get(ek)
    if sk and str(state_data.get(sk) or "").strip() not in ("", "-"):
        return []                      # keep what the labeled lines already set
    return _apply_ek_value(state_data, ek, value)


def _apply_inline_review_text(state_data: dict, text: str) -> list[str]:
    """Apply typed labeled edits directly to the review. Supports MULTIPLE fields on
    one line ('price 200 address 321 Main St …') and per-line edits. EVERY non-empty
    line must be a clean set of labeled edits; if any isn't, returns [] so the caller
    falls back to the AI parser. Returns the labels that actually changed after the
    phone/price sanitizer runs (so a rejected value never lies)."""
    lines = [l.strip() for l in re.split(r"[\n;]+", text) if l.strip()]
    if not lines:
        return []
    pending: list[tuple[str, str]] = []
    for line in lines:
        parsed = _parse_multi_field_line(line)
        if not parsed:
            return []  # a non-labeled/prose line → hand the whole message to the AI parser
        pending.extend(parsed)
    if not pending:
        return []
    # A whole address typed into the street field is really two edits (street +
    # city/ST/ZIP) — expand before tracking so BOTH are applied and reported.
    expanded: list[tuple[str, str]] = []
    for ek, value in pending:
        expanded.extend(_expand_address_pair(ek, value))
    pending = expanded

    tracked = {_INLINE_EK_STATE_KEY[ek] for ek, _ in pending}
    before = {k: str(state_data.get(k) or "").strip() for k in tracked}
    for ek, value in pending:
        if ek == "name":
            parts = value.split()
            _set_full_name(state_data, parts[0], " ".join(parts[1:]))
        else:
            _apply_single_phase1_edit(state_data, ek, value)
    _apply_single_address_as_both(state_data)
    _clean_vin_and_car(state_data)
    _sanitize_phase1_pending_phone_price(state_data)

    updated: list[str] = []
    for ek, _ in pending:
        k = _INLINE_EK_STATE_KEY[ek]
        now = str(state_data.get(k) or "").strip()
        if now and now != "-" and now != before.get(k):
            lbl = _INLINE_EDIT_KEY_LABEL.get(ek, ek)
            if lbl not in updated:
                updated.append(lbl)
    return updated


# Field-label anchor words — one regex per field so synonyms count once. A message that
# names several of these (a dictated lead: "Name John … Address 123 … VIN … Car …
# Colour Red … Insurance … Policy … Phone … Price …") is a multi-field block worth
# AI-extracting even on a single line, since voice notes arrive with no line breaks.
# Only true LABEL words — deliberately NOT value words that live inside ordinary field
# values ('street'/'city'/'state' appear in addresses like 'Kansas City' / 'Main Street',
# so they'd inflate the anchor count on a single address value).
_FIELD_ANCHOR_RES = [
    re.compile(r"\bnames?\b", re.I),
    re.compile(r"\baddress\b", re.I),
    re.compile(r"\bdelivery\b", re.I),
    re.compile(r"\bvin\b", re.I),
    re.compile(r"\b(?:car|vehicle)\b", re.I),
    re.compile(r"\bcolou?r\b", re.I),
    re.compile(r"\binsurance\b", re.I),
    re.compile(r"\b(?:policy|binder)\b", re.I),
    re.compile(r"\bphone\b", re.I),
    re.compile(r"\bprice\b", re.I),
    re.compile(r"\be-?mail\b", re.I),
    re.compile(r"\b(?:licen[cs]e|dln)\b", re.I),
]


def _count_field_anchors(text: str) -> int:
    """How many DISTINCT field labels a message names (a dictated-lead signal)."""
    return sum(1 for rx in _FIELD_ANCHOR_RES if rx.search(text or ""))


def _looks_like_multifield_block(text: str) -> bool:
    """True for a clearly-structured message worth AI re-parsing: a MULTI-LINE block (one
    field per line), text containing a full 17-char VIN, or a single-line dictation that
    NAMES several fields (>=3 distinct labels — e.g. a whole lead spoken in one voice
    note). A single ambiguous value is NOT this (it gets smart-placement / a hint instead
    of the AI guessing which one field it is)."""
    t = (text or "").strip()
    if ("\n" in t) or bool(_extract_vin_17(t)):
        return True
    return _count_field_anchors(t) >= 3


_REVIEW_EDIT_HINT = (
    "✍️ To change a field, tap ✏️ Edit — or just type it with a label:\n"
    "• name John Doe\n"
    "• address 123 Main St, Newark NJ 07102\n"
    "• car 2020 Toyota Camry\n"
    "• color white\n"
    "• phone 555-123-4567\n"
    "• price 200\n"
    "• email a@b.com\n"
    "• dl 12345678\n"
    "Change several at once: price 200 color white"
)

# Canonical label for each inline edit-key (to re-apply a single value as a labeled edit).
_COMMON_COLORS = frozenset({
    "white", "black", "gray", "grey", "silver", "red", "blue", "green", "yellow",
    "orange", "brown", "gold", "beige", "tan", "maroon", "navy", "purple", "pink",
    "charcoal", "burgundy", "bronze", "champagne", "cream", "teal", "ivory", "pearl",
})
_CAR_MAKE_RE = re.compile(
    r"\b(toyota|honda|ford|chevy|chevrolet|nissan|bmw|mercedes|benz|audi|kia|hyundai|"
    r"jeep|dodge|ram|gmc|lexus|mazda|subaru|volkswagen|vw|tesla|acura|infiniti|"
    r"infinity|cadillac|buick|chrysler|volvo|mitsubishi|porsche|jaguar|mini|fiat|"
    r"genesis|lincoln|scion|hummer|maserati|bentley|alfa|land\s*rover|range\s*rover)\b",
    re.I,
)
_STREET_RE = re.compile(
    r"\b(st|street|ave|avenue|rd|road|blvd|boulevard|dr|drive|lane|ln|way|ct|court|"
    r"pl|place|apt|apartment|suite|ste|hwy|highway|pkwy|parkway|cir|circle|ter|"
    r"terrace|unit)\b|#\d",
    re.I,
)


def _structured_value_ek(v: str):
    """Clear-cut field for a bare value from strong signals (email/vin/phone/price/
    address). Returns an inline edit-key or None (then AI + name/color/car heuristics)."""
    v = (v or "").strip()
    if not v:
        return None
    if re.search(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", v):
        return "email"
    # "Geico" on its own is the carrier. Without this the 1-to-4-plain-words rule
    # below filed it as the client's name. A value that reads as a vehicle is a
    # vehicle, whatever it shares a name with.
    if not (_CAR_MAKE_RE.search(v) or re.search(r"\b(19|20)\d{2}\b", v)) and _insurer_name(v):
        return "ins"
    if _extract_vin_17(v):
        return "vin"
    digits = re.sub(r"\D", "", v)
    if v.startswith("$"):
        return "price"
    if re.fullmatch(r"\d[\d,]*(?:\.\d{1,2})?", v):
        return "phone" if len(digits) >= 10 else "price"
    if 10 <= len(digits) <= 15 and re.fullmatch(r"[\d\s\-\(\)\+\.]+", v):
        return "phone"
    # A street line: has a digit AND (a street-type word OR a comma), OR a leading
    # house number. A lone ZIP is NOT enough — 'Fort Lee NJ 07024' is city/state/zip,
    # so leave that to the AI classifier. Guard against a year+make ('2019 Honda
    # Accord') claiming the address slot: an explicit street token still wins, but a
    # bare leading number / comma does NOT when the value looks like a vehicle.
    looks_vehicle = bool(_CAR_MAKE_RE.search(v) or re.search(r"\b(19|20)\d{2}\b", v))
    # A trailing money word means this is an amount, whatever else it contains.
    # "150 plus toll" has a digit and used to be filed as the registration
    # address — and then mirrored into the delivery address.
    if re.search(r"(?:dollars?|bucks|tolls?|flat|even|each|total|usd)\s*[.!]*$", v, re.I):
        return "price"
    if re.search(r"\d", v) and _STREET_RE.search(v):
        return "addr"
    if not looks_vehicle and (("," in v and re.search(r"\d", v))
                              or re.match(r"^\s*\d+\s+[A-Za-z]", v)):
        return "addr"
    return None


def _alpha_value_ek_heuristic(v: str):
    """Fallback field for a bare word/phrase when the AI is unavailable: color word →
    color, 'City ST 07024' → city/state/zip, year/known-make → car, 1–4 plain words →
    name."""
    v = (v or "").strip()
    words = v.split()
    if words and len(words) <= 2 and all(w.strip(".").lower() in _COMMON_COLORS for w in words):
        return "col"
    # 'Fort Lee NJ 07024' — a ZIP with words but no street token → city/state/zip.
    if re.search(r"\b\d{5}\b", v) and not _STREET_RE.search(v) and re.search(r"[A-Za-z]", v):
        return "csz"
    if _CAR_MAKE_RE.search(v):
        return "car"
    # A leading year-range number + a word is ambiguous (house number vs model year).
    # With no car-make word to confirm a vehicle, default it to registration address in
    # this no-AI fallback — matches the old deterministic behavior ('2015 Broadway').
    if re.match(r"^\s*(?:19|20)\d{2}\s+[A-Za-z]", v):
        return "addr"
    if re.search(r"\b(19|20)\d{2}\b", v):
        return "car"
    if 1 <= len(words) <= 4 and re.fullmatch(r"[A-Za-z][A-Za-z.'\-]*(?:\s+[A-Za-z][A-Za-z.'\-]*){0,3}", v):
        return "name"
    return None


def _apply_ek_value(state_data: dict, ek: str, value: str) -> list:
    """Apply ONE already-classified (edit_key, value) to the review and return the human
    label(s) that actually changed ([] on no-op / rejected / unknown key). Direct writer
    used by _smart_place_single_value instead of re-parsing 'alias value' (which could
    mis-split a mangled label back into the wrong field)."""
    if ek not in _INLINE_EDIT_KEY_LABEL:          # not one of the 19 review fields
        return []
    ek = _carrier_is_never_a_person(ek, value)
    # Same per-field validation/normalization the labeled path uses ('500' -> '$500',
    # phone needs >=10 digits, email needs '@', …). Returns '' when the value doesn't
    # fit the field, so a mis-guess is dropped instead of written.
    # One insurance value can be the carrier, the policy number, or both — split it
    # before cleaning, which canonicalises the carrier and would drop the number.
    ins_pairs = _expand_insurance_pair(ek, (value or "").strip())
    if ins_pairs:
        pairs = [(p_ek, _clean_inline_value(p_ek, p_val) or p_val) for p_ek, p_val in ins_pairs]
    else:
        value = _clean_inline_value(ek, (value or "").strip())
        if not value:
            return []
        # One spoken/typed address covers the street line AND the city/ST/ZIP line.
        pairs = _expand_address_pair(ek, value)

    def _norm(s: str) -> str:                     # compare content, not case/spacing
        return re.sub(r"\s+", " ", str(s or "")).strip().lower()

    before = {p_ek: _norm(state_data.get(_INLINE_EK_STATE_KEY[p_ek])) for p_ek, _ in pairs}
    for p_ek, p_val in pairs:
        if p_ek == "name":
            parts = p_val.split()
            _set_full_name(state_data, parts[0], " ".join(parts[1:]))
        else:
            _apply_single_phase1_edit(state_data, p_ek, p_val)
    # Same post-processing the labeled-edit path runs, so phone/price/vin/address are
    # normalized and a rejected value never reports a false change.
    _apply_single_address_as_both(state_data)
    _clean_vin_and_car(state_data)
    _sanitize_phase1_pending_phone_price(state_data)
    # Case/whitespace-insensitive diff — the applied value is re-normalized on write
    # (e.g. colors title-cased), so a pure re-normalization is not a real change.
    changed: list = []
    for p_ek, _ in pairs:
        now = str(state_data.get(_INLINE_EK_STATE_KEY[p_ek]) or "").strip()
        if now and now != "-" and _norm(now) != before.get(p_ek):
            label = _INLINE_EDIT_KEY_LABEL.get(p_ek, p_ek)
            if label not in changed:
                changed.append(label)
    return changed


async def _smart_place_single_value(state_data: dict, value: str, guess_name: bool = True) -> list:
    """Place one free-text/voice value ('first name John', 'white', '$200', '555-…') into
    the best field and return the changed labels (or [] if it couldn't be placed).
    Order: deterministic strong signals → AI {field,value} classifier → name/color/car
    heuristic fallback (only when the AI is unavailable)."""
    # (a) FAST-PATH: unambiguous structured signals apply instantly, no AI round-trip.
    ek = _structured_value_ek(value)
    if ek:
        return _apply_ek_value(state_data, ek, value)
    # (b) AI: choose among all 19 fields AND return the label-stripped value.
    if Config.is_ai_vision_configured():
        try:
            res = await asyncio.to_thread(ai_vision.classify_field_value, value)
        except Exception as e:
            logger.warning("field-value AI classify failed: %s", e)
            res = None
        if isinstance(res, dict):
            ek2 = res.get("field")
            if ek2 and ek2 != "unknown":
                return _apply_ek_value(state_data, ek2, str(res.get("value") or ""))
            if ek2 == "unknown":
                return []          # AI is confident it's a command/gibberish → hint
    # (c) FALLBACK: AI unconfigured/errored → deterministic alpha heuristic, orig value.
    ek3 = _alpha_value_ek_heuristic(value)
    # Its LAST rule is "one to four plain words must be a name". Useful for idle
    # text, wrong at a field prompt, where it would file a fumbled price as the
    # client. The colour / car / city rules above it still apply.
    if ek3 == "name" and not guess_name:
        return []
    if ek3:
        return _apply_ek_value(state_data, ek3, value)
    return []


# A phrase that looks like a (mis-typed / mis-heard) COMMAND — never smart-place it as
# a field value. So a select/submit/VIN command that didn't classify shows the hint
# instead of turning 'choose the driver kita' or 'submit' into a name.
# ── Speaking to the bot the way you would speak to a person ─────────────────
# Everything below rewrites a message ONLY so the existing recognisers can see
# the command inside it. It never produces a value that gets stored: every value
# is still sliced out of the raw text the operator typed.
#
# Deliberately NOT containing any _CMD_VERB word (choose/select/pick/set/use/
# assign/change/update/switch/make/put/go with) — those are what make the
# residue match once the scaffolding in front of them is gone.
_LEAD_SCAFFOLD = frozenset({
    "i", "i'd", "id", "i'm", "im", "i've", "ive", "we", "we'd", "wed", "you",
    "can", "could", "would", "will", "should", "please", "pls", "plz", "kindly",
    "like", "want", "wanna", "need", "gonna", "going", "let", "lets", "let's",
    "just", "maybe", "ok", "okay", "alright", "so", "well", "um", "uh", "erm",
    "hey", "hi", "yo", "yeah", "yep", "yup", "sure", "actually", "sorry",
    "quick", "quickly", "real", "anyway", "also", "then", "now", "and", "but",
    "for", "this", "one", "here", "there", "gimme", "lemme", "wait", "hold",
    "they", "he", "she", "it", "client", "customer", "guy",
    "to", "do", "does", "did", "we'd", "could", "should",
})
# Trailing courtesy on a whole message: "driver Susan please", "... thanks man".
_TAIL_SCAFFOLD_MSG_RE = re.compile(
    r"(?:\s|^)(?:please|pls|plz|thanks|thank\s+you|thx|ty|cheers|for\s+(?:me|us|now)|"
    r"asap|alright|okay|ok|man|bro|buddy|sir|maam|ma'am)\s*[.!,]*\s*$",
    re.IGNORECASE,
)
# A label followed by punctuation instead of a space. Anchored to a KNOWN label,
# which is the whole safety story: "address, 321 Main St" is repaired, while
# "Fort Lee, NJ 07024", "$1,500" and "a@b.com" are untouched because Lee, 1 and
# a are not labels.
_ALIAS_SEP_RE = None          # built lazily, after both alias tables exist
# "all drivers" means what "driver all" means. The quantifier has to be followed
# by a selection noun AND end the message, so "all drivers are late" is prose.
_QUANT_NOUN = {
    "drivers": "driver", "driver": "driver", "drv": "driver",
    "dispatchers": "dispatcher", "dispatcher": "dispatcher",
    "dispatch": "dispatcher", "disp": "dispatcher",
    "groups": "group", "group": "group",
    "teams": "team", "team": "team",
    "crews": "crew", "crew": "crew",
}
_QUANT_RE = re.compile(
    r"\b(?:all|every|each|the\s+whole|the\s+entire)\s+(?:of\s+)?(?:the\s+)?"
    r"(" + "|".join(sorted(_QUANT_NOUN, key=len, reverse=True)) + r")\s*[.!]*\s*$",
    re.IGNORECASE,
)
# "everyone" / "everybody" with no noun at all. Drivers are what a lead is
# broadcast to, so that is what it resolves to.
_QUANT_BARE_RE = re.compile(r"\b(?:everyone|everybody|every\s*one)\s*[.!]*\s*$", re.I)
# What may stand in front of a quantifier without making the line prose: the
# command verbs, the ways of saying "send", and pure filler. Anything else and
# the rewrite is refused — "tell all drivers I said hi" keeps its meaning.
_QUANT_LEAD_OK = frozenset({
    "choose", "select", "pick", "set", "use", "assign", "change", "update",
    "switch", "make", "put", "go", "with", "send", "dispatch", "notify",
    "blast", "broadcast", "text", "message", "ping", "alert", "add", "give",
    "do", "want", "wants", "need", "needs", "get", "let", "lets", "let's",
    "to", "the", "a", "an", "my", "this", "it", "them", "out", "over", "on",
    "and", "please", "for", "of", "i", "we", "you", "can", "could", "would",
    "just", "ok", "okay", "hey", "now",
})


def _quantifier_rewrite(t: str):
    """"send it to all the drivers" -> "driver all", or None if it is not that.

    Everything before the quantifier has to be a verb or filler; one content
    word and this refuses, which is what keeps "tell all drivers I said hi" and
    "all drivers are running late" out.
    """
    m = _QUANT_RE.search(t) or _QUANT_BARE_RE.search(t)
    if not m:
        return None
    for tok in re.split(r"[\s,]+", t[: m.start()].strip().lower()):
        tok = tok.strip(".:;-_'\"")
        if tok and tok not in _QUANT_LEAD_OK:
            return None
    noun = _QUANT_NOUN.get(m.group(1).lower()) if m.re is _QUANT_RE else "driver"
    return f"{noun} all" if noun else None
_LEAD_SCAFFOLD_MAX_TOKENS = 5
_LEAD_SCAFFOLD_MAX_CHARS = 28


def _alias_sep_re():
    """Built once, on first use, so it sees both alias tables however they grow."""
    global _ALIAS_SEP_RE
    if _ALIAS_SEP_RE is None:
        labels = sorted(set(_SELECT_ALIAS_KIND) | set(_INLINE_EDIT_ALIASES),
                        key=len, reverse=True)
        _ALIAS_SEP_RE = re.compile(
            r"\b(" + "|".join(re.escape(k) for k in labels) + r")\s*[:=,.\-\u2013\u2014]+\s*",
            re.IGNORECASE)
    return _ALIAS_SEP_RE


def _norm_command_text(text: str) -> str:
    """The same instruction with the conversation stripped off it.

    "I'd like to select all drivers please" -> "driver all", which the existing
    patterns already understand. Returns the input unchanged when there is
    nothing to do, and NEVER returns empty.

    Pure: no I/O, no context, no database. Bails on anything long or multi-line
    (that is a paste, not a command) and on KRAB_FLUENCY=0.
    """
    t = (text or "").strip()
    if not t or len(t) > 400 or "\n" in t or os.getenv("KRAB_FLUENCY", "1") == "0":
        return t
    original = t

    # 1. Peel conversational scaffolding off the front, a bounded amount of it.
    tokens = t.split()
    dropped_tokens = dropped_chars = 0
    while (tokens and dropped_tokens < _LEAD_SCAFFOLD_MAX_TOKENS
           and dropped_chars < _LEAD_SCAFFOLD_MAX_CHARS):
        head = tokens[0].lower().strip(".,;:!?'\"")
        if head not in _LEAD_SCAFFOLD:
            break
        dropped_chars += len(tokens[0]) + 1
        dropped_tokens += 1
        tokens = tokens[1:]
    if tokens:
        t = " ".join(tokens)

    # 2. And the courtesy off the end.
    prev = None
    while t and t != prev:
        prev = t
        t = _TAIL_SCAFFOLD_MSG_RE.sub("", t).strip()
    if not t:
        t = prev or original

    # 3. "driver: Susan" / "driver, susan" — how a phone transcribes a pause.
    t = _alias_sep_re().sub(lambda m: m.group(1) + " ", t).strip()

    # 4. "all drivers" -> "driver all". The quantifier must END the message and
    #    everything before it must be verbs or filler, so "all drivers are
    #    running late" and "tell all drivers I said hi" both stay prose.
    q = _quantifier_rewrite(t)
    if q:
        t = q

    return t or original


_COMMAND_LIKE_RE = re.compile(
    # A bare yes/no is an ANSWER, never a field value — without this a stray "yes"
    # (e.g. after a redeploy dropped the question) was filed as the client's name.
    r"^\s*(?:y|n|yes|no|yeah|yep|yup|nope|nah|sure|ok|okay)\b[\s.!,]*$"
    r"|^\s*(?:choose|select|pick|assign|use|go\s+with|send|submit|dispatch|deploy|ship|"
    r"done|finish(?:ed|ing)?|run|check|look\s*up|lookup|decode|verify)\b"
    # a bare select-noun at the START ('driver', 'the dispatcher', 'group …') — anchored
    # so a real surname/company containing the word (e.g. 'Ryan Driver', 'Acme Group')
    # is still placed as a value.
    r"|^\s*(?:the\s+|a\s+|my\s+)?(?:drivers?|drv|dispatchers?|group|team|crew)\b",
    re.I,
)


# ── Natural-language commands during review (voice or typed) ─────────────────
_CMD_VERB = (r"(?:choose|select|pick|set|use|assign|change|update|switch|make|put|"
             r"go\s+with|send\s+to|dispatch\s+to)?")
_ART = r"(?:the\s+|a\s+|an\s+|my\s+|this\s+)?"  # optional article between verb and noun
_SELECT_SOURCE_RE = re.compile(rf"^\s*{_CMD_VERB}\s*{_ART}(?:client'?s?\s+source|contact\s+source|lead\s+source|client\s+info|contact\s+info|came\s+from|found\s+us|source|origin|src)\s+(.+)$", re.I)
# The GROUPS/teams are the "dispatchers" in the UI, so 'choose dispatcher X' selects a
# group. The DELIVERY people stay "drivers": 'choose driver X' selects a driver.
_SELECT_GROUP_RE = re.compile(rf"^\s*{_CMD_VERB}\s*{_ART}(?:group|team|crew|dispatchers?|dispatch|disp)\s+(.+)$", re.I)
_SELECT_DRIVER_RE = re.compile(rf"^\s*{_CMD_VERB}\s*{_ART}(?:drivers?|drv)\s+(.+)$", re.I)
# The VIN prompt asks a Yes/No question, so it must accept those words — typed or
# spoken. Deliberately NOT part of the general review classifier: a bare "yes" only
# means "use the DMV result" while that question is actually on screen.
_YES_RE = re.compile(
    r"^\s*(?:y|ya|yes|yea|yeah|yep|yup|sure|ok|okay|correct|right|affirmative|"
    r"use\s+it|use\s+that|do\s+it|go\s+ahead|please\s+do)\b[\s.!,]*$", re.I)
_NO_RE = re.compile(
    r"^\s*(?:n|no|nope|nah|negative|don'?t|do\s+not|keep\s+it|keep\s+mine|"
    r"leave\s+it|skip|pass)\b[\s.!,]*$", re.I)
_VIN_KEEP_RE = re.compile(r"\b(keep|same|leave\s+it|leave\s+alone|as\s+is|as-is|don'?t\s+change|current|mine|stated|original)\b", re.I)
_VIN_RETYPE_RE = re.compile(r"\b(retype|re-?enter|redo|fix|correct)\b.*\bvin\b|\btype\b.*\bvin\b.*\bagain\b", re.I)
_VIN_USE_RE = re.compile(r"\buse\b.*\b(vin|dmv|new|decoded|lookup|api|theirs?|that)\b", re.I)
_RUN_VIN_RE = re.compile(r"\b(run|check|lookup|look\s+up|decode|verify|pull|scan)\b.*\bvin\b|\bvin\b.*\b(check|lookup|decode|scan)\b", re.I)
# The quantifier must be the WHOLE answer, optionally naming what it quantifies.
# Matching anything merely STARTING with "all" meant "all called already" — the
# tail of "the drivers all called already" — broadcast the lead to every driver.
_ALL_SELECT_RE = re.compile(
    r"^\s*(?:all|every\s*one|everybody|everything|every)"
    r"(?:\s+(?:of\s+)?(?:the\s+)?"
    r"(?:drivers?|drv|dispatchers?|dispatch|disp|groups?|teams?|crews?|sources?))?"
    r"\s*[.!]*\s*$",
    re.I,
)


# "submit" / "send lead" / "dispatch" / "send it" → dispatch the lead now. Whole-
# message match so "send to HighKage" (a group pick) and "send driver note …" are
# NOT caught here.
_SUBMIT_RE = re.compile(
    # The verb. "finished"/"finishing" are spoken far more often than "finish".
    r"^\s*(?:submit|send|dispatch|deploy|ship|done|go\s*ahead|"
    r"finish(?:ed|ing)?(?:\s*up)?|send\s*out|push(?:\s*it)?)"
    # Optional trailing nouns. "dispatch" is here as well as in the verb list so
    # "send dispatch" and "finished dispatch" both read as one whole command.
    r"(?:\s+(?:the|this|that|my|it|out|now|please|lead|leads|tag|tags|"
    r"client|sale|dispatch))*\s*[.!,]*$",
    re.I,
)


# ── Several selections in one message ───────────────────────────────────────
# "driver Kita dispatch HighKage" is TWO instructions. The single-selection regexes
# are greedy — the first noun swallowed the rest, so the bot hunted for a driver
# literally called "Kita dispatch HighKage" and the dispatcher was lost. This splits
# the line at each selection noun, the same way labeled field edits are split.
_SELECT_ALIAS_KIND = {
    "driver": "SELECT_DRIVER", "drivers": "SELECT_DRIVER", "drv": "SELECT_DRIVER",
    "dispatcher": "SELECT_GROUP", "dispatchers": "SELECT_GROUP",
    "dispatch": "SELECT_GROUP", "disp": "SELECT_GROUP",
    "group": "SELECT_GROUP", "groups": "SELECT_GROUP",
    "team": "SELECT_GROUP", "teams": "SELECT_GROUP",
    "crew": "SELECT_GROUP", "crews": "SELECT_GROUP",
    "contact source": "SELECT_SOURCE", "lead source": "SELECT_SOURCE",
    "contact info": "SELECT_SOURCE", "source": "SELECT_SOURCE", "origin": "SELECT_SOURCE",
}
# Longest first so "contact source" wins over "source".
_SELECT_ALIAS_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in sorted(_SELECT_ALIAS_KIND, key=len, reverse=True))
    + r")\b", re.I)
# Words allowed before the first noun without making the line prose.
_SELECT_LEAD_FILLERS = frozenset({
    "choose", "select", "pick", "set", "use", "assign", "send", "dispatch", "to", "the",
    "a", "an", "my", "this", "and", "please", "for", "with", "go", "id",
    # Grown so a multi-selection survives being asked for politely:
    # "make driver Susan and dispatcher HighKage", "put driver Susan and team X".
    # Still refused: "the driver said dispatch was late" — _SELECT_NOT_A_NAME
    # catches the sentence words in the payload.
    "make", "do", "change", "update", "switch", "put", "want", "wants",
    "need", "needs", "it", "them", "out", "over", "on", "all", "every", "both",
})
# Separators between one selection and the next ("Kita and dispatcher HighKage").
_SELECT_TAIL_RE = re.compile(r"(?:\s+and|\s*[,;/]+)\s*$", re.I)
# Words that never appear in a dispatcher/driver/source NAME. Their presence means the
# line is prose that happens to contain "driver" and "dispatch" ("the driver said
# dispatch was late"), not a list of picks.
_SELECT_NOT_A_NAME = frozenset({
    "said", "says", "say", "was", "were", "is", "are", "am", "be", "been", "being",
    "will", "would", "should", "could", "can", "cant", "has", "have", "had", "did",
    "does", "do", "went", "going", "came", "coming", "arrived", "arriving", "called",
    "calling", "told", "tell", "late", "early", "needs", "need", "wants", "wanted",
    "because", "when", "then", "why", "how", "not", "no", "yet", "still", "just",
})


def _looks_like_a_pick_name(name: str) -> bool:
    """A dispatcher/driver/source name: a few words, none of them sentence words."""
    words = [w.strip(".,;:'\"").lower() for w in (name or "").split()]
    if not words or len(words) > 4:
        return False
    return not any(w in _SELECT_NOT_A_NAME for w in words)


# A driver NOTE is never a driver pick, on either path.
_SELECT_NOTE_HEADS = ("note", "notes", "license", "licence")


def _parse_multi_select_line(text: str):
    """Split "driver Kita dispatch HighKage" into [(SELECT_DRIVER,'Kita'),
    (SELECT_GROUP,'HighKage')]. Returns None unless the line really is two or more
    clean selections, so a single one keeps its existing (tested) handling."""
    line = (text or "").strip()
    matches = list(_SELECT_ALIAS_RE.finditer(line))
    if len(matches) < 2:
        return None
    head = line[: matches[0].start()]
    for tok in re.split(r"[\s,]+", head.strip().lower()):
        tok = tok.strip(".:;-_'\"")
        if tok and tok not in _SELECT_LEAD_FILLERS:
            return None                       # prose, not a list of selections
    out = []
    for i, m in enumerate(matches):
        kind = _SELECT_ALIAS_KIND.get(re.sub(r"\s+", " ", m.group(1).lower().strip()))
        end = matches[i + 1].start() if i + 1 < len(matches) else len(line)
        name = line[m.end():end]
        name = re.sub(r"^\s*(?:to|is|are|=|:)\s+", " ", name).strip(" ,:;-")
        name = _SELECT_TAIL_RE.sub("", name).strip(" ,:;-")
        if not kind or not name:
            continue
        if not _looks_like_a_pick_name(name):
            return None                       # prose, not a list of picks
        # "driver note call the dispatcher first" is a NOTE. The single-selection
        # path has always known that; this path did not, and widening the head
        # gate above makes this path reachable far more often.
        if kind == "SELECT_DRIVER" and name.split()[0].lower() in _SELECT_NOTE_HEADS:
            return None
        out.append((kind, name))
    # Two nouns but one had no name ("driver dispatch Kita") is not a clean list.
    return out if len(out) >= 2 else None


async def _apply_selection(kind: str, payload: str, state_data: dict, user_id: int):
    """Apply ONE dispatcher/driver/source pick. Returns (ok, note) so the single and
    multi paths share the same matching rules and can never drift apart."""
    payload = (payload or "").strip()
    if kind == "SELECT_GROUP":
        if _ALL_SELECT_RE.match(payload):
            _select_group(state_data, user_id, "all")
            return True, "Dispatcher → All Dispatchers"
        g = _resolve_pick_name(payload, [x for x in db.get_all_groups() if record_is_active(x)],
                        "group_name")
        if not g:
            return False, f"No dispatcher matched “{payload}”"
        _select_group(state_data, user_id, g)
        return True, f"Dispatcher → {g.get('group_name', '?')}"
    if kind == "SELECT_DRIVER":
        if _ALL_SELECT_RE.match(payload):
            _select_driver(state_data, user_id, "all")
            return True, "Driver → All Drivers"
        active = [d for d in _get_all_drivers_cached() if record_is_active(d)]
        suspended = _get_suspended_driver_ids()
        eligible = [d for d in active if str(d.get("id")) not in suspended]
        d = _resolve_pick_name(payload, eligible, "driver_name")
        if not d:
            susp = _resolve_pick_name(payload, [x for x in active if str(x.get("id")) in suspended],
                               "driver_name")
            if susp:
                return False, f"{susp.get('driver_name', 'Driver')} is suspended (PENALTY)"
            return False, f"No driver matched “{payload}”"
        _select_driver(state_data, user_id, d)
        return True, f"Driver → {d.get('driver_name', '?')}"
    if kind == "SELECT_SOURCE":
        s = _source_by_exact_label(payload) or _resolve_pick_name(
            payload, db.get_contact_info_sources(), "label")
        if not s:
            return False, f"No source matched “{payload}”"
        _select_source(state_data, user_id, s)
        return True, f"Source → {s.get('label', '?')}"
    return False, ""


# A selection's value picks up the little joining words on the way in:
# "set source to Instagram", "driver is Kita".
_SELECT_VALUE_FILLER_RE = re.compile(
    r"^(?:to|is|as|=|:|be|the|a|an|on|from|via|through|at|by|in)\s+", re.IGNORECASE)


def _selection_payload(raw: str) -> str:
    prev = None
    out = (raw or "").strip().lstrip(":=").strip()
    while out and out != prev:                 # "set source to the Instagram"
        prev = out
        out = _SELECT_VALUE_FILLER_RE.sub("", out).strip()
    # _match_name never strips punctuation from the query, so "Susan." used to
    # reach a driver called Susan only by falling through to the 0.55 fuzzy rung.
    return out.strip(" .,;:!?")


def _clause_head(payload: str) -> str:
    """The first clause of a payload: "Susan, send it" -> "Susan".

    The selection regexes capture to end of line, so any word after the name is
    glued to it and then compared against the whole driver name — which is why
    "driver Susan, send it" found nobody.
    """
    return re.split(r"\s*[,;]\s*", str(payload or ""), maxsplit=1)[0].strip(" .,;:!?")


def _payload_is_prose(payload: str) -> bool:
    """True when a selection payload is plainly a sentence, not a name.

    "driver needs to ring the bell twice" classifies as a driver pick because
    the line opens with the word "driver". It is a note. Callers use this to
    fall THROUGH to note handling rather than opening a picker over the top of
    what the operator was actually writing.
    """
    p = str(payload or "").strip()
    if not p:
        return True
    if _ALL_SELECT_RE.match(p):
        return False
    return not _looks_like_a_pick_name(p)


def _resolve_pick_name(payload: str, candidates: list, name_key: str):
    """Find a driver/dispatcher/source by name, tolerating the words around it.

    Runs the plain match FIRST, so it can never change an answer that already
    worked; the extra rungs only ever rescue a None.

    The prose guard is the price of admission for widening what counts as a
    command elsewhere: "driver needs to ring the bell twice" must stay a note,
    not become a hunt for a driver called "needs to ring the bell twice".
    """
    p = str(payload or "").strip()
    if not p:
        return None
    hit = _match_name(p, candidates, name_key)
    if hit:
        return hit
    if not _looks_like_a_pick_name(p) and not _ALL_SELECT_RE.match(p):
        return None
    stripped = p.strip(" .,;:!?")
    if stripped != p:
        hit = _match_name(stripped, candidates, name_key)
        if hit:
            return hit
    head = _clause_head(p)
    if head and head != stripped:
        return _match_name(head, candidates, name_key)
    return None


def _source_by_exact_label(text: str):
    """The source a bare word names, or None.

    Deliberately strict — exact once punctuation and spacing are ignored, so
    dictation's "face book" still finds Facebook while an ordinary word never
    hijacks the picker the way _match_name's fuzzy pass would."""
    squash = lambda v: re.sub(r"[^a-z0-9]+", "", str(v or "").casefold())
    q = squash(text)
    if not q or len(q) < 3:
        return None
    for src in (db.get_contact_info_sources() or []):
        if squash(src.get("label")) == q:
            return src
    return None


# Words that end a clause hard enough to start a new instruction after them.
# Without one of these there is no boundary, and "send the tag out to the client"
# would split into a note plus a submit.
_SUBMIT_SPLIT_RE = re.compile(
    r"^(?P<head>.+?)[\s,;]+(?P<tail>(?:"
    r"send\s+it(?:\s+out)?|send\s+out|ship\s+it|submit\s+it|submit|dispatch\s+it|"
    r"go\s+ahead|push\s+it|fire\s+it\s+off|let'?s\s+go|do\s+it"
    r"))\s*[.!]*$",
    re.IGNORECASE,
)
# A head made only of these is an opinion, not a field value: "looks good",
# "that's everything", "I think we're good". Discarding it is the point — letting
# it reach _smart_place_single_value files an assessment as the client's name.
_ASSESSMENT_ONLY = frozenset({
    "looks", "look", "looking", "good", "great", "fine", "perfect", "nice",
    "ok", "okay", "alright", "all", "set", "that's", "thats", "that", "is",
    "everything", "done", "ready", "i", "think", "we're", "were", "we", "yeah",
    "yep", "cool", "sweet", "and", "so", "then", "now", "it", "this",
})


def _split_trailing_submit(text: str):
    """("looks good", "send it out") when a message both comments and dispatches.

    OFF unless KRAB_FLUENCY_SUBMIT=1. Submitting is irreversible, and
    "looks good send it out" is genuinely indistinguishable from a driver note
    that happens to end that way — so this ships dark and gets switched on
    deliberately.

    Returns None unless every gate passes: a hard boundary word before the tail,
    one line, at most twelve words, and a head that is not a note.
    """
    if os.getenv("KRAB_FLUENCY_SUBMIT", "0") != "1":
        return None
    t = (text or "").strip()
    if not t or "\n" in t or len(t.split()) > 12:
        return None
    m = _SUBMIT_SPLIT_RE.match(t)
    if not m:
        return None
    head = m.group("head").strip(" ,;.")
    if not head:
        return None
    # A head that is a real field edit stays a field edit — and a NOTE never
    # splits, because a note is allowed to end with "send it out".
    kind, payload = _classify_review_command_once(head, vin_pending=False)
    if kind == "FIELD_EDITS" and any(ek in _PROSE_EKS for ek, _ in (payload or [])):
        return None
    return (head, m.group("tail"))


def _is_default_selection(label, ids=None) -> bool:
    """True when a dispatch slot still holds its default rather than a choice.

    A fresh card reads "All Drivers" / "All Dispatchers" — that is what the bot
    put there, not what the operator picked.

    ``ids`` is checked as well, and either half counts. _select_driver writes the
    label and the ids together so they normally agree, but this guard protects a
    choice the operator made from being silently replaced, and that is not worth
    resting on two fields staying in step.
    """
    v = str(label or "").strip()
    if v and v.lower() not in ("all drivers", "all dispatchers", "all", "auto"):
        return False
    if not v and ids:
        # A slot holding an id but no label is not a shape this bot writes — a
        # default card carries "All Drivers"/"All Dispatchers". Treat the unknown
        # as a choice: refusing to pick costs a convenience, overwriting a real
        # dispatcher costs a delivery.
        return False
    # Only the LIST form carries this signal. Drivers are selected as a list, so
    # exactly one entry means one driver was named; a group is a single scalar id
    # that is present even on a default card, where the label is the only signal.
    if isinstance(ids, (list, tuple)):
        picked = [i for i in ids if i]
        if len(picked) == 1:
            return False
    return True


def _bare_name_pick(text: str, state_data: dict):
    """("SELECT_DRIVER", "Susan") for "give it to Susan" — or None.

    OFF unless KRAB_FLUENCY_BARENAME=1. This is the only rule in the file that
    acts on a message containing no command vocabulary at all: the sole evidence
    is that a word in it happens to name somebody on the roster. That is exactly
    how a client called Will Smith stops being a client.

    So the gates are severe:
      * strong matches only — an exact full name, an exact name token, or a
        prefix of at least four characters. Never the fuzzy rung, which returns
        "Ana Lopez" for the query "an".
      * one pool only. A word naming both a driver and a dispatcher is ambiguous
        and returns None rather than a guess.
      * the operator must not have chosen already. The card arrives with its
        slots DEFAULTED to "All Drivers"/"All Dispatchers", which is the absence
        of a choice, not one — an earlier version tested for an empty slot and so
        never fired at all on a real card.
    """
    if os.getenv("KRAB_FLUENCY_BARENAME", "0") != "1":
        return None
    t = (text or "").strip()
    if not t or len(t.split()) > 8:
        return None

    def strong(pool, key):
        q = t.casefold()
        best = None
        for row in pool or []:
            name = str(row.get(key) or "").strip()
            if not name:
                continue
            low = name.casefold()
            if low == q or low in q.split() or (len(q) >= 4 and low.startswith(q)):
                if best is not None and best is not row:
                    return None              # two candidates — not evidence
                best = row
            elif len(low) >= 4 and low in q:
                if best is not None and best is not row:
                    return None
                best = row
        return best

    try:
        drivers = [d for d in _get_all_drivers_cached() if record_is_active(d)]
        groups = [g for g in db.get_all_groups() if record_is_active(g)]
    except Exception:
        return None
    card = state_data or {}
    d = strong(drivers, "driver_name") if _is_default_selection(
        card.get("selected_driver_names"), card.get("selected_driver_ids")) else None
    g = strong(groups, "group_name") if _is_default_selection(
        card.get("selected_group_name"), card.get("selected_group_id")) else None
    if d and g:
        return None                          # names both — ask, never guess
    if d:
        return ("SELECT_DRIVER", str(d.get("driver_name") or ""))
    if g:
        return ("SELECT_GROUP", str(g.get("group_name") or ""))
    return None


def _classify_review_command(text: str, *, vin_pending: bool = True):
    """What the operator just asked for, in their own words.

    Two passes, and the order is the whole safety guarantee:

        strict(text) or fluent(normalised(text))

    The fluent pass may only ever UPGRADE a ("NONE", None). It can never
    overrule a verdict the strict pass already reached, so nothing that works
    today can start meaning something else tomorrow. Any future "pass 1 is
    wrong" case is fixed by narrowing pass 1 — the moment pass 2 can win, the
    property that makes this safe on a live bot is gone.

    ``vin_pending`` is such a narrowing: with no DMV question on screen the VIN
    verbs are skipped entirely, because _VIN_KEEP_RE's bare \\b(keep|same)\\b is
    the loosest recogniser in this file and "keep the gate code handy" is a note.
    """
    t = (text or "").strip()
    if not t:
        return ("NONE", None)
    kind, payload = _classify_review_command_once(t, vin_pending=vin_pending)
    if kind != "NONE":
        return (kind, payload)
    normalised = _norm_command_text(t)
    if normalised and normalised != t:
        return _classify_review_command_once(normalised, vin_pending=vin_pending)
    return ("NONE", None)


def _classify_review_command_once(text: str, *, vin_pending: bool = True):
    """One pass of the classifier. Field edits win first (so 'driver note …' and
    'phone …' stay field edits); then submit; then selections; then VIN verbs."""
    t = (text or "").strip()
    if not t:
        return ("NONE", None)
    # A) labeled field edits (incl. multi-field one-liners)
    fe: list[tuple[str, str]] = []
    clean = True
    for line in re.split(r"[\n;]+", t):
        line = line.strip()
        if not line:
            continue
        parsed = _parse_multi_field_line(line)
        if not parsed:
            clean = False
            break
        fe.extend(parsed)
    if clean and fe:
        return ("FIELD_EDITS", fe)
    # A2) submit / send the lead
    if _SUBMIT_RE.match(t):
        return ("SUBMIT", None)
    # B0) several selections in one message ("driver Kita dispatch HighKage") —
    # checked before the single-selection regexes, which would swallow the rest.
    multi = _parse_multi_select_line(t)
    if multi:
        return ("SELECTIONS", multi)
    # B) selections: source → group → driver (distinct nouns, order avoids overlap)
    # Just the source, no label — "Facebook". Strict match only (see the helper).
    if _source_by_exact_label(t):
        return ("SELECT_SOURCE", t.strip())
    m = _SELECT_SOURCE_RE.match(t)
    if m:
        return ("SELECT_SOURCE", _selection_payload(m.group(1)))
    m = _SELECT_GROUP_RE.match(t)
    if m:
        return ("SELECT_GROUP", _selection_payload(m.group(1)))
    m = _SELECT_DRIVER_RE.match(t)
    if m:
        name = _selection_payload(m.group(1))
        first = name.split()[0].lower() if name.split() else ""
        if first not in ("note", "notes", "license", "licence"):  # 'driver note/license' are field edits
            return ("SELECT_DRIVER", name)
    # C) VIN verbs. Order matters: "keep/same" first ("use the same" = keep); then
    # "use <vin>" ("use vin lookup"/"use the new" = use decoded); retype; else run.
    #
    # Skipped entirely unless a DMV question is actually on screen. _VIN_KEEP_RE
    # searches for a bare "keep" or "same" ANYWHERE in the line, so with no
    # question pending it claims "keep the gate code handy" and "Same Day
    # Delivery" — both of which are notes.
    if not vin_pending:
        return ("NONE", None)
    if _VIN_KEEP_RE.search(t):
        return ("VIN_KEEP", None)
    if _VIN_RETYPE_RE.search(t):
        return ("VIN_RETYPE", None)
    if _VIN_USE_RE.search(t):
        return ("VIN_USE", None)
    if _RUN_VIN_RE.search(t):
        return ("RUN_VIN", None)
    return ("NONE", None)


def _match_name(query: str, candidates: list, name_key: str):
    """Auto-pick the closest candidate by name (exact → prefix → substring → fuzzy).
    Returns the chosen dict, or None only when nothing is even close."""
    q = (query or "").strip().casefold()
    if not q or not candidates:
        return None
    named = [(str(c.get(name_key) or "").strip(), c) for c in candidates]
    named = [(n, c) for n, c in named if n]
    if not named:
        return None

    def _closest(pool):
        best = difflib.get_close_matches(q, [n.casefold() for n, _ in pool], n=1, cutoff=0.0)
        return next((c for n, c in pool if n.casefold() == best[0]), None) if best else None

    for n, c in named:  # 1. exact full name
        if n.casefold() == q:
            return c
    tok = [(n, c) for n, c in named if q in n.casefold().split()]  # 2. exact word (first/last name)
    if tok:
        return tok[0][1] if len(tok) == 1 else _closest(tok)
    starts = [(n, c) for n, c in named if n.casefold().startswith(q)]  # 3. prefix
    if starts:
        return starts[0][1] if len(starts) == 1 else _closest(starts)
    subs = [(n, c) for n, c in named if q in n.casefold()]  # 4. substring
    if subs:
        return subs[0][1] if len(subs) == 1 else _closest(subs)
    close = difflib.get_close_matches(q, [n.casefold() for n, _ in named], n=1, cutoff=0.55)  # 5. fuzzy
    if close:
        return next((c for n, c in named if n.casefold() == close[0]), None)
    return None


async def _cleanup_voice_echo(context: ContextTypes.DEFAULT_TYPE, chat_id) -> None:
    """Delete the '🎙️ Heard: …' echo the global voice handler posted, if any."""
    mid = context.user_data.pop("voice_echo_msg_id", None)
    if mid and chat_id:
        await _safe_delete_chat_message(context, chat_id, mid)


async def _send_vanishing(context: ContextTypes.DEFAULT_TYPE, chat_id, text: str, *, delay: float = 4.0) -> None:
    """Send a transient confirmation that auto-deletes after ``delay`` seconds, so the
    review card stays the last visible message. Best-effort; never blocks the flow."""
    if not chat_id or not text:
        return
    try:
        msg = await context.bot.send_message(chat_id=chat_id, text=text)
    except Exception:
        return

    async def _vanish():
        try:
            await asyncio.sleep(delay)
            await _safe_delete_chat_message(context, chat_id, msg.message_id)
        except Exception:
            pass

    asyncio.create_task(_vanish())


# Shared selection writers — used by both the review buttons and the NL commands.
def _select_group(state_data: dict, user_id: int, group) -> None:
    if group == "all":
        state_data["selected_group_id"] = "all"
        state_data["selected_group_name"] = "All Dispatchers"
    else:
        state_data["selected_group_id"] = group.get("id")
        state_data["selected_group_name"] = group.get("group_name", "?")
    db.set_user_state(user_id, "phase1", state_data)


def _select_driver(state_data: dict, user_id: int, driver) -> None:
    if driver == "all":
        active = [d for d in _get_all_drivers_cached() if record_is_active(d)]
        suspended = _get_suspended_driver_ids()
        selected = [d for d in active if str(d["id"]) not in suspended]
        state_data["selected_driver_ids"] = [d["id"] for d in selected]
        state_data["selected_driver_names"] = "All Drivers"
    else:
        state_data["selected_driver_ids"] = [driver["id"]]
        state_data["selected_driver_names"] = driver.get("driver_name", "?")
    db.set_user_state(user_id, "phase1", state_data)


def _select_source(state_data: dict, user_id: int, source) -> None:
    state_data["selected_source_label"] = source.get("label", "") if source else ""
    db.set_user_state(user_id, "phase1", state_data)


# Reopen the same pickers the buttons use — the no-match fallback for NL selection.
async def _open_group_picker(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = context.user_data.get("review_chat_id"); mid = context.user_data.get("review_message_id")
    if not chat_id or not mid:
        return
    active = [g for g in db.get_all_groups() if record_is_active(g)]
    if not active:
        return
    buttons = [[InlineKeyboardButton(g.get("group_name", str(g["id"])), callback_data=f"selgrp_{g['id']}")] for g in active]
    buttons.append([InlineKeyboardButton("📢 Send to All Dispatchers", callback_data="selgrp_all")])
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="ph1_sel_back")])
    await _edit_message_keyboard(context, chat_id, mid, InlineKeyboardMarkup(buttons))


async def _open_driver_picker(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = context.user_data.get("review_chat_id"); mid = context.user_data.get("review_message_id")
    if not chat_id or not mid:
        return
    active = [d for d in _get_all_drivers_cached() if record_is_active(d)]
    suspended = _get_suspended_driver_ids()
    buttons = []
    for d in active:
        did = d.get("id"); name = d.get("driver_name", "Unknown")
        if str(did) in suspended:
            buttons.append([InlineKeyboardButton(f"🚫 {name} (PENALTY)", callback_data=f"driver_suspended_{did}")])
        else:
            buttons.append([InlineKeyboardButton(f"🚗 {name}", callback_data=f"seldrv_{did}")])
    if [d for d in active if str(d.get("id")) not in suspended]:
        buttons.append([InlineKeyboardButton("📢 Send to All Drivers", callback_data="seldrv_all")])
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="ph1_sel_back")])
    await _edit_message_keyboard(context, chat_id, mid, InlineKeyboardMarkup(buttons))


async def _open_source_picker(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = context.user_data.get("review_chat_id"); mid = context.user_data.get("review_message_id")
    if not chat_id or not mid:
        return
    sources = db.get_contact_info_sources()
    if not sources:
        return
    buttons = [[InlineKeyboardButton(s.get("label", str(s["id"])), callback_data=f"selsrc_{s['id']}")] for s in sources]
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="ph1_sel_back")])
    await _edit_message_keyboard(context, chat_id, mid, InlineKeyboardMarkup(buttons))


# What the model may do to the lead on screen. Deliberately a small set: these
# are the operations the review card itself offers, so an AI-chosen one lands in
# exactly the same code a tap does.
_AI_CARD_TOOLS = frozenset({
    "update_lead", "select_driver", "select_dispatcher", "add_vehicle",
    "submit_lead",
})


async def _ai_review_command(update, context, user_id, state_data, text):
    """The model's reading of a message the local rules did not understand.

    Returns the next conversation state, or None to carry on falling through —
    which is what happens with no key, no credit, a timeout, or a message the
    model also declines to claim.
    """
    from utils import nl_router

    # A parked call is waiting on one answer, so THIS message is that answer.
    # Taken before anything else, and popped whatever happens: a slot left open
    # is how the next unrelated sentence gets filed as somebody's name.
    parked = nl_router.take_parked(context.user_data)
    if parked:
        # Unless the answer is plainly a new instruction, in which case the old
        # call is abandoned rather than fed the word "cancel" as a driver name.
        if not _COMMAND_LIKE_RE.search(text) and not _cancel_restart_kind(text):
            args = dict(parked["args"])
            args[parked["needs"]] = text.strip()
            return await _run_ai_card_tool(update, context, user_id, state_data,
                                           parked["tool"], args)

    if not nl_router.is_configured():
        return None
    try:
        cls = await asyncio.to_thread(nl_router.classify, text, card=state_data)
    except ai_vision.AIVisionQuotaError:
        await _warn_ai_unavailable(update, context)
        return None
    except Exception as e:
        logger.warning("ai review command failed: %s", e)
        return None
    if not cls:
        return None

    tool = cls.get("tool") or ""
    if tool not in _AI_CARD_TOOLS:
        return None                    # not a card operation — let it fall through
    args = cls.get("args") or {}

    needs = nl_router.missing_args(tool, args)
    if needs:
        nl_router.park(context.user_data, tool, args, needs[0])
        await update.effective_message.reply_text(nl_router.ask_for(tool, needs[0]))
        return STATE_AI_REVIEW

    return await _run_ai_card_tool(update, context, user_id, state_data, tool, args)


async def _run_ai_card_tool(update, context, user_id, state_data, tool, args):
    """Execute one AI-chosen card operation through the SAME code a tap uses.

    Nothing is reimplemented here — each branch hands off to the function the
    button already calls, so a tap and a sentence cannot drift apart.
    """
    logger.info("ai card tool: %s %s", tool, {k: str(v)[:40] for k, v in args.items()})
    try:
        import sentry_sdk
        sentry_sdk.set_tag("ai_invoked", tool)
    except Exception:
        pass

    if tool == "update_lead":
        ek = _AI_FIELD_TO_EK.get(args.get("field") or "")
        if not ek:
            return None
        vehicle = args.get("vehicle") or 1
        if int(vehicle) > 1:
            ek = _vehicle_edit_key(int(vehicle), _AI_FIELD_TO_VEHICLE_EK.get(
                args.get("field") or "", ek))
        _apply_single_phase1_edit(state_data, ek, str(args.get("value") or ""))
        _clean_vin_and_car(state_data)
        db.set_user_state(user_id, "phase1", state_data)
        await _update_review_message_text(context, state_data)
        return STATE_AI_REVIEW

    if tool in ("select_driver", "select_dispatcher"):
        kind = "SELECT_DRIVER" if tool == "select_driver" else "SELECT_GROUP"
        payload = args.get("driver") or args.get("dispatcher") or ""
        # Straight into the interpreter's own selection branch, so an ambiguous
        # or unknown name opens the same picker a typed one does.
        return await _interpret_review_command(
            update, context, user_id, state_data,
            f"{'driver' if kind == 'SELECT_DRIVER' else 'dispatcher'} {payload}")

    if tool == "add_vehicle":
        return await _handle_vehicle_menu_action(
            update, context, PH1_ADD_CAR_CB, state_data)

    if tool == "submit_lead":
        return await _continue_phase1_after_ai_review(
            update.effective_message, context, user_id)

    return None


# The tool's field name → the edit key the card already uses.
_AI_FIELD_TO_EK = {
    "first_name": "fn", "last_name": "ln", "address": "addr",
    "city_state_zip": "csz", "delivery_address": "daddr",
    "delivery_city_state_zip": "dcsz", "vin": "vin", "car": "car",
    "color": "col", "insurance_company": "ins",
    "insurance_policy_number": "pol", "phone": "phone", "price": "price",
    "issuer_note": "issuer", "driver_note": "driver", "email": "email",
    "driver_license": "dl",
}
# An extra car carries only the eight fields it owns.
_AI_FIELD_TO_VEHICLE_EK = {
    "first_name": "fn", "last_name": "ln", "address": "addr",
    "city_state_zip": "csz", "vin": "vin", "car": "car", "color": "col",
    "insurance_company": "ins", "insurance_policy_number": "pol",
}


async def _interpret_review_command(update, context, user_id, state_data, text):
    """Execute a natural-language review command (group/driver/source select, VIN).
    Returns the next conversation state, or None when the text is not a command (the
    caller then falls back to the AI parser)."""
    # The DMV question is only "on screen" when a decode conflict is actually
    # waiting. Without that, the VIN verbs must not fire at all.
    kind, payload = _classify_review_command(
        text, vin_pending=bool(context.user_data.get("vin_choice_api_car")))
    if kind == "NONE":
        # Last resort, and only with their flags on. Both act on messages that
        # ordinary rules found nothing in, so they run AFTER everything else has
        # declined — never instead of it.
        split = _split_trailing_submit(text)
        if split:
            head, _tail = split
            head_kind, _ = _classify_review_command_once(head, vin_pending=False)
            if head_kind in ("NONE", "FIELD_EDITS"):
                # "looks good" is an opinion; only a real edit is worth applying.
                if head_kind == "FIELD_EDITS" and not all(
                        w.strip(".,'").lower() in _ASSESSMENT_ONLY for w in head.split()):
                    _apply_inline_review_text(state_data, head)
                    db.set_user_state(user_id, "phase1", state_data)
                logger.info("trailing submit: %r -> submit", text[:60])
                kind, payload = "SUBMIT", None
        if kind == "NONE":
            guess = _bare_name_pick(text, state_data)
            if guess:
                logger.info("bare-name pick: %r -> %s %s", text[:60], *guess)
                kind, payload = guess
        if kind == "NONE":
            # Nothing local understood it. Ask the model what was meant.
            handled = await _ai_review_command(update, context, user_id,
                                               state_data, text)
            if handled is not None:
                return handled
    if kind in ("NONE", "FIELD_EDITS"):
        return None
    chat_id = update.effective_chat.id if update.effective_chat else None

    async def _finish(toast):
        try:
            await update.message.delete()
        except Exception:
            pass
        await _cleanup_voice_echo(context, chat_id)
        if toast and chat_id:
            await _send_vanishing(context, chat_id, toast)

    if kind == "SUBMIT":
        try:
            await update.message.delete()
        except Exception:
            pass
        await _cleanup_voice_echo(context, chat_id)
        return await _continue_phase1_after_ai_review(update.message, context, user_id)

    if kind == "SELECTIONS":
        # "driver Kita dispatch HighKage" — apply every pick in one go, then report
        # once. Anything that didn't match is named, so nothing fails silently.
        done, failed = [], []
        for one_kind, one_name in payload:
            ok, note = await _apply_selection(one_kind, one_name, state_data, user_id)
            (done if ok else failed).append(note)
        db.set_user_state(user_id, "phase1", state_data)
        await _update_review_message_text(context, state_data)
        lines = ([f"✅ {n}" for n in done] + [f"🤔 {n}" for n in failed])
        await _finish("\n".join(lines) if lines else None)
        if failed and chat_id:
            # Offer a picker for whichever kind didn't match, so a typo is one tap
            # from being fixed rather than needing the whole line retyped.
            joined = " ".join(failed).lower()
            if "driver" in joined:
                await _open_driver_picker(context)
            elif "dispatcher" in joined:
                await _open_group_picker(context)
            elif "source" in joined:
                await _open_source_picker(context)
        return STATE_AI_REVIEW

    if kind == "SELECT_GROUP":
        if _ALL_SELECT_RE.match(payload or ""):
            _select_group(state_data, user_id, "all")
            await _update_review_message_text(context, state_data)
            await _finish("✅ Dispatcher → All Dispatchers")
            return STATE_AI_REVIEW
        g = _resolve_pick_name(payload, [x for x in db.get_all_groups() if record_is_active(x)], "group_name")
        if not g:
            if _payload_is_prose(payload):
                return None               # a note, not a pick
            await _open_group_picker(context)
            await update.message.reply_text(f"🤔 No dispatcher matched “{payload}”. Pick one above.")
            return STATE_AI_REVIEW
        _select_group(state_data, user_id, g)
        await _update_review_message_text(context, state_data)
        await _finish(f"✅ Dispatcher → {g.get('group_name', '?')}")
        return STATE_AI_REVIEW

    if kind == "SELECT_DRIVER":
        if _ALL_SELECT_RE.match(payload or ""):
            _select_driver(state_data, user_id, "all")
            await _update_review_message_text(context, state_data)
            await _finish("✅ Driver → All Drivers")
            return STATE_AI_REVIEW
        active = [d for d in _get_all_drivers_cached() if record_is_active(d)]
        suspended = _get_suspended_driver_ids()
        eligible = [d for d in active if str(d.get("id")) not in suspended]
        d = _resolve_pick_name(payload, eligible, "driver_name")
        if not d:
            susp = _resolve_pick_name(payload, [x for x in active if str(x.get("id")) in suspended], "driver_name")
            if susp:
                await update.message.reply_text(f"🚫 {susp.get('driver_name', 'Driver')} is suspended (PENALTY).")
                return STATE_AI_REVIEW
            if _payload_is_prose(payload):
                return None               # a note, not a pick — let it be written
            await _open_driver_picker(context)
            await update.message.reply_text(f"🤔 No driver matched “{payload}”. Pick one above.")
            return STATE_AI_REVIEW
        _select_driver(state_data, user_id, d)
        await _update_review_message_text(context, state_data)
        await _finish(f"✅ Driver → {d.get('driver_name', '?')}")
        return STATE_AI_REVIEW

    if kind == "SELECT_SOURCE":
        s = _resolve_pick_name(payload, db.get_contact_info_sources(), "label")
        if not s:
            if _payload_is_prose(payload):
                return None               # a note, not a pick
            await _open_source_picker(context)
            await update.message.reply_text(f"🤔 No source matched “{payload}”. Pick one above.")
            return STATE_AI_REVIEW
        _select_source(state_data, user_id, s)
        await _update_review_message_text(context, state_data)
        await _finish(f"✅ Source → {s.get('label', '?')}")
        return STATE_AI_REVIEW

    if kind == "RUN_VIN":
        result = await _handle_phase1_vin_check_button(context, update, user_id, state_data)
        await _finish(None)
        return result

    if kind in ("VIN_USE", "VIN_KEEP"):
        # Only a real VIN command when a conflict is pending — otherwise phrases like
        # "keep the group the same" would silently vanish. Fall through to the AI parser.
        if not context.user_data.get("vin_choice_api_car"):
            return None
        result = await _apply_vin_choice(context, update.message, chat_id, user_id, "use" if kind == "VIN_USE" else "keep")
        await _finish(None)
        return result

    if kind == "VIN_RETYPE":  # "retype vin" — explicit (regex requires the word "vin")
        result = await _apply_vin_choice(context, update.message, chat_id, user_id, "retype")
        await _finish(None)
        return result

    return None


def _extra_attachments(context: ContextTypes.DEFAULT_TYPE) -> list:
    """User-attached title/license photos (inline descriptors), if any."""
    return [e for e in (context.user_data.get("phase1_extra_attachments") or []) if isinstance(e, dict)]


def _dispatch_attach_files(context: ContextTypes.DEFAULT_TYPE, lead_data: dict) -> list:
    """attached_files for a lead payload = STATE_ADD_FILES files + the user's attached
    title/license photos, so they reach the accepting team on EVERY dispatch path."""
    return list(lead_data.get("attached_files") or []) + _extra_attachments(context)


def _add_extra_attachment(context: ContextTypes.DEFAULT_TYPE, ftype: str, mime: str,
                          filename: str, blob: bytes, caption: str | None) -> str | None:
    """Store an uploaded photo/PDF so it rides along to the dispatch group. Returns None
    on success, or a short user-facing reason string when a size/count cap blocks it (the
    caller shows it as a vanishing note). Caps keep the inline-base64 attachments from
    bloating the create_lead insert past the DB gateway limit and failing the whole save."""
    if not blob:
        return None
    if len(blob) > _PHASE1_MEDIA_MAX_BYTES:
        return "⚠️ That image is too large to include with the dispatch (max 5 MB)."
    b64 = base64.b64encode(blob).decode("ascii")
    extras = context.user_data.get("phase1_extra_attachments") or []
    if len(extras) >= _MAX_ATTACH_COUNT:
        return f"📎 Max {_MAX_ATTACH_COUNT} images — dispatch this lead, then add more to the next."
    # Count any already-finalized vision media too — they ride in the SAME payload.
    vision_b64 = sum(len(f.get("data_b64") or "") for f in (context.user_data.get("phase1_attached_files") or []))
    if vision_b64 + sum(len(e.get("data_b64") or "") for e in extras) + len(b64) > _MAX_ATTACH_TOTAL_B64:
        return "📎 Attachments are getting large — dispatch this lead first, then add more."
    extras.append({
        "type": ftype, "mime": mime, "filename": filename,
        "data_b64": b64,
        "caption": (caption or "").strip()[:200] or "📎 Attachment",
    })
    context.user_data["phase1_extra_attachments"] = extras
    return None


async def handle_phase1_review_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """STATE_AI_REVIEW: accept typed text / photo / PDF inline — no Edit button.

    Short labeled edits ('price $50', 'phone 232-232-2322') apply instantly;
    photos, PDFs, and anything not recognized as a labeled edit go through the same
    AI parser as the '🖼 Adjust from image/text' button."""
    message = update.message
    # SELF-HEAL: the live card id lives in context.user_data, which is RAM-only and wiped on
    # a process restart / redeploy. If it's gone but the DB still holds the "phase1" lead,
    # re-post the card here so THIS edit has a live card to update in place (otherwise
    # _update_review_message_text silently no-ops and the edit "does nothing"). This makes
    # the handler work no matter how it was reached (normal state OR restart re-entry).
    if message and not context.user_data.get("review_message_id") and update.effective_user:
        try:
            _st = db.get_user_state(update.effective_user.id)
            if _st and _st.get("state") in _LEAD_REVIEWABLE_DB_STATES:
                await _repost_review_card(message, dict(_st.get("data") or {}), context, update.effective_user.id)
        except Exception as e:
            logger.warning("review card self-heal failed: %s", e)
    if message and (message.photo or message.document):
        # ONE behavior for every image/PDF: read it for fields, keep it visible, and
        # include it with the dispatch. No mode toggle, no second button.
        result = await handle_phase1_adjust_input(update, context)
        return STATE_AI_REVIEW if result == STATE_ADJUST_INPUT else result

    text = ((message.text if message else "") or "").strip()
    if not text:
        return STATE_AI_REVIEW

    _cr = _cancel_restart_kind(text)
    if _cr:
        return await _do_cancel_or_restart(update, context, _cr)

    user_id = update.effective_user.id
    try:
        state = db.get_user_state(user_id)
    except Exception as e:
        # A storage hiccup must NOT kill the edit silently — tell the user to retry.
        logger.warning("review_msg: get_user_state failed: %s", e)
        await _send_vanishing(
            context,
            update.effective_chat.id if update.effective_chat else message.chat_id,
            "⚠️ Storage hiccup — please send that again in a few seconds.",
        )
        return STATE_AI_REVIEW
    logger.info("🔎DIAG review_msg uid=%s text=%r has_review_id=%s db_state=%s data_empty=%s",
                user_id, text[:60], bool(context.user_data.get("review_message_id")),
                (state or {}).get("state"), not bool((state or {}).get("data")))
    if not state or not state.get("data"):
        # A live card in RAM + an empty DB read is usually a transient storage miss,
        # not a real wipe (a wipe clears user_data in the same process). Warn once
        # and let the user resend; a SECOND consecutive miss means the row really
        # is gone — drop the stale card ids so we never loop on the warning.
        if context.user_data.get("review_message_id") and not context.user_data.get("state_miss_once"):
            context.user_data["state_miss_once"] = True
            await _send_vanishing(
                context,
                update.effective_chat.id if update.effective_chat else message.chat_id,
                "⚠️ Storage hiccup — please send that again in a few seconds.",
            )
            return STATE_AI_REVIEW
        context.user_data.pop("state_miss_once", None)
        context.user_data.pop("review_message_id", None)
        context.user_data.pop("review_chat_id", None)
        await message.reply_text("❌ Data lost. Please start over with /start")
        return ConversationHandler.END
    context.user_data.pop("state_miss_once", None)
    state_data = state["data"]

    # 0. Insurance on/off by voice/text ("add insurance", "no insurance") — before the
    #    field editor, so "insurance" isn't swallowed as an insurance-company edit.
    _ins = _insurance_intent(text)
    if _ins is not None:
        state_data["wants_insurance"] = _ins
        # Strip the insurance phrase; if the same message also carried field edits
        # ("add insurance, price 200"), apply them too instead of dropping them.
        remainder = (_INS_ON_RE if _ins else _INS_OFF_RE).sub(" ", text)
        remainder = remainder.strip(" ,;.\t\n")
        also = _apply_inline_review_text(state_data, remainder) if remainder else []
        db.set_user_state(user_id, "phase1", state_data)
        chat_id = update.effective_chat.id if update.effective_chat else message.chat_id
        try:
            await message.delete()
        except Exception:
            pass
        await _cleanup_voice_echo(context, chat_id)
        # Flip the button in place, no insurance on/off message. Only toast (vanishing)
        # if the same message also carried real field edits, so that feedback isn't lost.
        await _update_review_message_text(context, state_data)
        if also:
            await _send_vanishing(context, chat_id, "✅ Updated: " + ", ".join(dict.fromkeys(also)))
        return STATE_AI_REVIEW

    # 0b. A whole lead spoken/typed in ONE single-line message that names several fields
    #     (a voice dictation: "Name John … Address 123 … VIN … Car … Colour Red …") goes
    #     STRAIGHT to the AI extractor. The strict labeled parser below can't handle a
    #     multi-field message with no separators — it greedily absorbs everything into the
    #     first field (name) or mis-splits values — so a >=3-label message is routed here
    #     instead. Fill-only-empty so it can never clobber data already on the card; a
    #     single-field edit ("color blue" / "first name John") stays <3 and is unaffected.
    #     (Multi-line pastes / VIN blocks keep their existing spot at step 3a.)
    if ("\n" not in text) and not _extract_vin_17(text) and _count_field_anchors(text) >= 3:
        result = await handle_phase1_adjust_input(update, context, fill_only_empty=True)
        await _cleanup_voice_echo(context, update.effective_chat.id if update.effective_chat else message.chat_id)
        return STATE_AI_REVIEW if result == STATE_ADJUST_INPUT else result

    # Typed text in review → clean it up (edits/commands below also delete; this also
    # covers the AI-reparse fallback so nothing the issuer types lingers).
    await _autoclean_user_msg(update, context)

    # 1. Labeled field edits (incl. multi-field one-liners) → apply, delete msg, toast.
    updated = _apply_inline_review_text(state_data, text)
    if updated:
        updated += await _ai_split_addresses_if_needed(state_data)
        db.set_user_state(user_id, "phase1", state_data)
        chat_id = update.effective_chat.id if update.effective_chat else message.chat_id
        try:
            await message.delete()
        except Exception:
            pass
        await _cleanup_voice_echo(context, chat_id)
        await _send_vanishing(context, chat_id, "✅ Updated: " + ", ".join(dict.fromkeys(updated)))
        await _update_review_message_text(context, state_data)
        return STATE_AI_REVIEW

    # 2. Natural-language command (choose group/driver/source, run/use VIN).
    handled = await _interpret_review_command(update, context, user_id, state_data, text)
    if handled is not None:
        return handled

    # 2a. TWO CARS IN ONE PASTE: a whole job for a client with more than one car.
    #     Cars 2..N come off the text here so car 1 is parsed from its own block,
    #     by the same parser as always. A one-VIN paste is untouched by this.
    text, _added_cars = _apply_multi_vehicle_paste(state_data, text)
    if _added_cars:
        db.set_user_state(user_id, "phase1", state_data)
        _chat = update.effective_chat.id if update.effective_chat else message.chat_id
        await _send_vanishing(
            context, _chat,
            f"🚘 Found {_added_cars + 1} cars — added the "
            + ", ".join(_ordinal_tag_label(i + 2) for i in range(_added_cars))
            + ". One client, one price, one receipt; a tag for each car.",
            delay=8.0,
        )

    # 2b. BULK PASTE: several lines where only some are labeled edits. Apply those,
    #     then let each remaining line find its own field (a bare email, phone, price
    #     or colour places itself). Only what is still unplaced goes to the AI — one
    #     unreadable line used to discard every readable one in the same message.
    if "\n" in text or ";" in text:
        bulk_labels, leftovers = _apply_bulk_review_text(state_data, text)
        if bulk_labels:
            still: list[str] = []
            for line in leftovers:
                placed = ([] if _COMMAND_LIKE_RE.search(line)
                          else await _place_bulk_leftover(state_data, line))
                if placed:
                    bulk_labels.extend(p for p in placed if p not in bulk_labels)
                else:
                    still.append(line)
            bulk_labels += await _ai_split_addresses_if_needed(state_data)
            db.set_user_state(user_id, "phase1", state_data)
            chat_id = update.effective_chat.id if update.effective_chat else message.chat_id
            await _cleanup_voice_echo(context, chat_id)
            note = "✅ Updated: " + ", ".join(dict.fromkeys(bulk_labels))
            if still:
                note += "\n🤔 Not understood: " + "; ".join(still[:3])
            await _send_vanishing(context, chat_id, note, delay=8.0)
            await _update_review_message_text(context, state_data)
            return STATE_AI_REVIEW

    # 3a. A real multi-field block (a paste OR a dictated lead naming several fields,
    #     e.g. a whole lead spoken in one voice note) → AI re-parse fills every field.
    if _looks_like_multifield_block(text):
        # A deliberate multi-line paste / VIN block is a full correction (may overwrite);
        # a single-line ">=3 labels" dictation only FILLS EMPTY fields, so a message that
        # merely names fields can never clobber good data already on the card.
        fill_only = ("\n" not in text) and not _extract_vin_17(text)
        result = await handle_phase1_adjust_input(update, context, fill_only_empty=fill_only)
        await _cleanup_voice_echo(context, update.effective_chat.id if update.effective_chat else message.chat_id)
        return STATE_AI_REVIEW if result == STATE_ADJUST_INPUT else result

    # 3b. A single bare value typed one-at-a-time ('John Doe', 'white', '$200',
    #     '555-…') → place it in the RIGHT field (strong signals, then the AI
    #     classifier, then name/color/car heuristics). A command-looking phrase that
    #     didn't classify (e.g. 'choose the driver kita', 'submit') is NOT placed as a
    #     value — it gets the hint instead.
    chat_id = update.effective_chat.id if update.effective_chat else message.chat_id
    updated = [] if _COMMAND_LIKE_RE.search(text) else await _smart_place_single_value(state_data, text)
    if updated:
        updated += await _ai_split_addresses_if_needed(state_data)
        db.set_user_state(user_id, "phase1", state_data)
        await _cleanup_voice_echo(context, chat_id)
        await _send_vanishing(context, chat_id, "✅ Updated: " + ", ".join(dict.fromkeys(updated)))
        await _update_review_message_text(context, state_data)
        return STATE_AI_REVIEW

    # 3c. Truly couldn't tell → show the labeled-edit how-to (nothing mis-filed).
    await _send_vanishing(context, chat_id, _REVIEW_EDIT_HINT, delay=12.0)
    return STATE_AI_REVIEW


# ── Voice notes: transcribe and process as if the user had typed it ──────────
async def _transcribe_update_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[str]:
    """Download a voice/audio note from the update and return its transcript (or None)."""
    msg = update.effective_message
    media = getattr(msg, "voice", None) or getattr(msg, "audio", None)
    if not media:
        return None
    # A failed status send must not abort the pipeline — the transcript injection
    # below is what makes the voice note act as typed text.
    note = None
    try:
        note = await msg.reply_text("🎙️ Transcribing…")
    except Exception as e:
        logger.warning("Transcribing note send failed: %s", e)
    transcript = None
    try:
        f = await context.bot.get_file(media.file_id)
        bio = io.BytesIO()
        await f.download_to_memory(out=bio)
        fname = "voice.ogg"
        if getattr(media, "file_name", None):
            fname = media.file_name
        elif getattr(media, "mime_type", None) and "/" in media.mime_type:
            ext = media.mime_type.split("/")[-1].split(";")[0].strip()
            fname = f"voice.{ext or 'ogg'}"
        transcript = await asyncio.to_thread(ai_vision.transcribe_voice, bio.getvalue(), fname)
    except Exception as e:
        logger.warning("Voice download/transcription failed: %s", e)
        transcript = None
    if note:
        await _safe_delete_chat_message(context, note.chat_id, note.message_id)
    return transcript


async def _global_voice_to_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global (group -1) pre-processor: transcribe ANY private-chat voice/audio note
    and inject the transcript as the message text, then let the update flow on to
    whatever handler would process a typed message at that point. This makes voice
    work EVERYWHERE — every conversation state, every step — with no per-state wiring.

    Runs before the conversation handlers (group 0); on success it returns normally
    so the (now text-bearing) update reaches them. On failure it stops the update so
    a raw voice note never trips a handler that would reject it."""
    msg = update.effective_message
    if not msg:
        return
    # Only the private lead flow uses voice; never transcribe group/channel voice.
    if getattr(msg, "chat", None) is not None and msg.chat.type != "private":
        return
    if getattr(msg, "text", None):
        return
    if not (getattr(msg, "voice", None) or getattr(msg, "audio", None)):
        return
    transcript = await _transcribe_update_voice(update, context)
    if not transcript:
        await msg.reply_text("⚠️ Couldn't understand that voice note. Please try again or type it.")
        raise ApplicationHandlerStop  # nothing to route — don't let a handler mis-fire
    # Inject so downstream TEXT handlers (filters.TEXT) match and read it as typed.
    object.__setattr__(msg, "text", transcript)
    heard = await msg.reply_text("🎙️ Heard: " + transcript)
    # Stash the echo id so a review command can delete it ("my text disappears").
    context.user_data["voice_echo_msg_id"] = heard.message_id
    # Return normally → update continues to later groups, now as a text message.


# ── Review edits from ANY state: the state-independent safety net ────────────
# Conversation states that HAVE a text listener, so typed/voice edits are handled
# by the conversation itself and the group -2 safety net below must stand down.
# Any state NOT in this set (a legacy/future button-only state) would silently
# swallow text — those are exactly what the safety net catches.
_REVIEW_TEXT_CAPABLE_STATES = frozenset({
    STATE_PHASE1, STATE_PHASE2, STATE_AI_REVIEW, STATE_ADJUST_INPUT,
    STATE_AI_EDIT_MENU, STATE_AI_EDIT_INPUT, STATE_EDIT_FIELD_PROMPT,
    STATE_MISSING_FIELD, STATE_SPECIAL_REQUEST_ISSUERS, STATE_SPECIAL_REQUEST_DRIVERS,
    STATE_VIN_CHOICE, STATE_VIN_RETYPE,
    STATE_SELECT_GROUP, STATE_SELECT_DRIVER, STATE_SELECT_CONTACT_SOURCE,
})

# The main lead ConversationHandler, so the safety net can read (and repair) its
# in-memory state. Set once in main(); stays None in unit tests.
_MAIN_CONV_HANDLER = None
# The /settings ConversationHandler — the plate-image reader must NOT defer to it.
_SETTINGS_CONV_HANDLER = None


def _main_conv_state(update: Update):
    """The main lead conversation's ACTIVE in-memory state for this chat/user, or
    None when idle. Reads the same PTB internals as _user_in_active_conversation;
    degrades to None if they ever change."""
    h = _MAIN_CONV_HANDLER
    if h is None:
        return None
    try:
        return h._conversations.get(h._get_key(update))
    except Exception:
        return None


def _nonlead_conv_active(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """True when a conversation OTHER than the main lead flow (receipt, appeal,
    follow-up, settings) is active for this chat/user — their text inputs must win."""
    try:
        groups = context.application.handlers
    except Exception:
        return False
    for group in groups.values():
        for h in group:
            if isinstance(h, ConversationHandler) and h is not _MAIN_CONV_HANDLER:
                try:
                    if h._conversations.get(h._get_key(update)) is not None:
                        return True
                except Exception:
                    continue
    return False


async def handle_review_edit_anywhere(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Group -2 safety net: a typed/spoken edit for a LIVE review card is never
    dropped, no matter which state PTB's conversation is in. Fires only when the
    main conversation sits in a state with no text listener while the DB still
    holds the live "phase1" lead. Everything else passes through untouched: idle
    text keeps flowing to handle_idle_lead_start (which re-enters the review),
    and text-capable states keep their own handlers."""
    msg = update.effective_message
    if not msg or not update.effective_user:
        return
    text = (msg.text or "").strip()
    if not text:
        return
    if _cancel_restart_kind(text):
        return  # cancel/restart keep their normal (state-aware) handling
    conv_state = _main_conv_state(update)
    if conv_state is None or conv_state in _REVIEW_TEXT_CAPABLE_STATES:
        return
    if _nonlead_conv_active(update, context):
        return
    user_id = update.effective_user.id
    try:
        db_state = db.get_user_state(user_id)
    except Exception as e:
        logger.warning("review-anywhere: get_user_state failed: %s", e)
        return
    if not db_state or db_state.get("state") not in _LEAD_REVIEWABLE_DB_STATES or not db_state.get("data"):
        return
    logger.info("🔎DIAG review-anywhere FIRED uid=%s conv_state=%r text=%r",
                user_id, conv_state, text[:60])
    result = await handle_phase1_review_message(update, context)
    # Re-arm (or clear) the conversation to match what the review handler decided,
    # so the card's buttons work again after a ghost-state rescue.
    try:
        h = _MAIN_CONV_HANDLER
        key = h._get_key(update)
        if result == ConversationHandler.END:
            h._conversations.pop(key, None)
        else:
            h._conversations[key] = result if result is not None else STATE_AI_REVIEW
    except Exception:
        pass  # edits still work (this net keeps catching them); only buttons lag
    raise ApplicationHandlerStop


class _TypedAsTap:
    """Presents a typed or spoken answer to code written for a button tap.

    Reassigning should not require tapping: saying the driver's name is faster, and
    the resend/pick handlers only touch a handful of attributes, so a shim lets them
    run unchanged instead of duplicating their logic."""

    class _Query:
        def __init__(self, message, data, from_user):
            self.message = message
            self.data = data
            self.from_user = from_user

        async def answer(self, *a, **k):
            return None

    def __init__(self, update: Update, data: str):
        self.callback_query = self._Query(update.effective_message, data,
                                          update.effective_user)
        self.effective_user = update.effective_user
        self.effective_chat = update.effective_chat
        self.effective_message = update.effective_message
        self.message = update.effective_message


async def handle_media_in_any_state(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
    """A picture or PDF sent at ANY step of the lead flow is read into the card.

    Eight states accepted typed text but registered no photo handler, so an image sent
    while (say) the DMV question or a picker was on screen was dropped by PTB with no
    reply at all. Returns None so the conversation STAYS where it was — the question or
    picker above is still answerable once the image has been read."""
    msg = update.effective_message
    if not msg or not (msg.photo or msg.document):
        return None
    try:
        st = db.get_user_state(update.effective_user.id) if update.effective_user else None
    except Exception as e:
        logger.warning("media-any-state: get_user_state failed: %s", e)
        st = None
    if st and st.get("state") in _LEAD_REVIEWABLE_DB_STATES and st.get("data"):
        await handle_phase1_adjust_input(update, context)
        return None
    # Past the review (a dispatch pick, a resend) there is no card to fold it into.
    await _send_vanishing(
        context, msg.chat_id,
        "📎 This lead is already out — use the buttons above, or start a new tag to add files.",
        delay=8.0,
    )
    return None


async def handle_select_state_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
    """Typed/voice text while a button picker (dispatcher/driver/source) is on screen.

    With a live phase1 lead it is a review edit or spoken command ("driver John",
    "price 150"). While REASSIGNING an already-sent lead it is the name of the driver
    or dispatcher to hand it to — matched the same way the buttons resolve, so
    tapping, typing and speaking all reach the same code."""
    msg = update.effective_message
    text = ((msg.text if msg else "") or "").strip()
    if not text:
        return None
    _cr = _cancel_restart_kind(text)
    if _cr:
        return await _do_cancel_or_restart(update, context, _cr)
    st = None
    try:
        st = db.get_user_state(update.effective_user.id) if update.effective_user else None
    except Exception as e:
        logger.warning("select-state text: get_user_state failed: %s", e)
    state_name = (st or {}).get("state")
    lead_data = (st or {}).get("data") or {}
    if state_name == "phase1" and lead_data:
        return await handle_phase1_review_message(update, context)
    user_id = update.effective_user.id if update.effective_user else 0

    if state_name == "select_driver":
        active = [d for d in _get_all_drivers_cached() if record_is_active(d)]
        suspended = _get_suspended_driver_ids()
        eligible = [d for d in active if str(d.get("id")) not in suspended]
        if _ALL_SELECT_RE.match(text):
            picked = "select_driver_all"
        else:
            d = _match_name(text, eligible, "driver_name")
            if not d:
                susp = _match_name(text, [x for x in active if str(x.get("id")) in suspended],
                                   "driver_name")
                if susp and msg:
                    await msg.reply_text(
                        f"🚫 {susp.get('driver_name', 'That driver')} is suspended (PENALTY). "
                        "Pick someone else, or say another name.")
                elif msg:
                    await msg.reply_text(
                        f"🤔 No driver matched “{text}”. Tap one above, or say the name again.")
                return None
            picked = f"select_driver_{d.get('id')}"
        return await _handle_resend_to_drivers(
            _TypedAsTap(update, picked), context, lead_data, picked, user_id)

    if state_name == "select_group":
        g = ("all" if _ALL_SELECT_RE.match(text)
             else _match_name(text, [x for x in db.get_all_groups() if record_is_active(x)],
                              "group_name"))
        if not g:
            if msg:
                await msg.reply_text(
                    f"🤔 No dispatcher matched “{text}”. Tap one above, or say the name again.")
            return None
        picked = "select_group_all" if g == "all" else f"select_group_{g.get('id')}"
        return await handle_group_selection(_TypedAsTap(update, picked), context)

    if state_name == "select_contact_source":
        sources = db.get_contact_info_sources() or []
        src = _source_by_exact_label(text) or _match_name(text, sources, "label")
        if not src:
            if msg:
                await msg.reply_text(
                    f"🤔 No client source matched “{text}”. Tap one above, or say it again.")
            return None
        return await handle_contact_source_selection(
            _TypedAsTap(update, f"contact_source_{src.get('id')}"), context)

    if msg:
        await _send_vanishing(context, msg.chat_id, "☝️ Tap a button above to continue.")
    return None


# ── Commands without the slash ───────────────────────────────────────────────
# Saying or typing the bare word runs the command: "settings" == "/settings". The
# message is rewritten into a real command (text + bot_command entity) and allowed to
# flow on, so PTB routes it through the SAME handler as the typed slash — identical
# behaviour everywhere, with no per-handler wiring to drift.
# Only the whole message counts, so a value that merely contains one of these words
# is untouched.
_BARE_COMMANDS = {
    "start": "start", "begin": "start", "menu": "start",
    "leaderboard": "leaderboard", "stats": "leaderboard", "board": "leaderboard",
    "ranking": "leaderboard", "scoreboard": "leaderboard", "who is winning": "leaderboard",
    "help": "help", "commands": "help", "how do i use this": "help",
    "settings": "settings", "setting": "settings",
    "receipt": "receipt", "receipts": "receipt", "recipts": "receipt",
    "whoami": "whoami", "who am i": "whoami", "my id": "whoami", "me": "whoami",
    "followup": "followup", "follow up": "followup", "prospect": "followup",
    "followups": "followups", "follow ups": "followups", "my clients": "followups",
    "all followups": "allfollowups", "allfollowups": "allfollowups",
    "announce": "announce", "announcement": "announce", "broadcast": "announce",
    "driverblock": "driverblock", "driver block": "driverblock",
    "appeal": "appeal",
    "test": "test",
}
# Filler that speech puts on either end ("open settings", "settings, please.").
_BARE_CMD_LEAD_RE = re.compile(r"^(?:please|open|show\s+me|show|go\s+to|take\s+me\s+to)\s+", re.I)
_BARE_CMD_TAIL_RE = re.compile(r"[\s,]*please$", re.I)


def _bare_command_for(text: str):
    """The command a bare word means, or None. Whole-message match only."""
    phrase = re.sub(r"\s+", " ", (text or "").strip().lower()).strip(" .,!?")
    phrase = _BARE_CMD_LEAD_RE.sub("", phrase).strip()
    phrase = _BARE_CMD_TAIL_RE.sub("", phrase).strip(" .,!?")
    if not phrase:
        return None
    return _BARE_COMMANDS.get(phrase)


async def _bare_command_to_slash(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Rewrite a bare command word into the real command, then let it flow on.

    Runs after voice transcription, so a SPOKEN command works the same as a typed
    one. Never consumes the update: PTB's own CommandHandlers do the work."""
    msg = update.effective_message
    text = ((msg.text if msg else "") or "").strip()
    if not msg or not text or text.startswith("/"):
        return
    # Is a prompt explicitly waiting for a typed VALUE? That question has to be
    # asked FIRST. "Temp Tag" — the product this business sells — matches the
    # new-lead family, so asking about commands first meant typing it as a client
    # source or a note silently wiped the whole card.
    #
    # But the operator still has to be able to leave. `strict` is the line: the
    # exact cancel/restart words get through, the family made of ordinary nouns
    # (temp tag, new lead, the order, a tag) does not.
    awaiting_value = bool(context.user_data and (
        context.user_data.get("tset_await")
        or context.user_data.get("phase1_pending_edit_key")
        or context.user_data.get("missing_fields")
        or (context.user_data.get("fu") or {}).get("pending")))
    if awaiting_value:
        cmd = _cancel_restart_kind(text, strict=True)
        if not cmd:
            return                       # it is a value; leave it alone
    else:
        # cancel / restart (and "new lead", "temp tag", …) already have a tested
        # word family — reuse it so those phrases behave identically to their
        # slash command.
        cmd = _cancel_restart_kind(text) or _bare_command_for(text)
    if not cmd:
        return
    slash = f"/{cmd}"
    object.__setattr__(msg, "text", slash)
    object.__setattr__(msg, "entities", (
        MessageEntity(type=MessageEntity.BOT_COMMAND, offset=0, length=len(slash)),
    ))
    logger.info("bare command %r -> %s", text[:40], slash)


async def _begin_lead_flow(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    username: str,
    reply_message,
    send_welcome: bool = True,
) -> None:
    """Shared Phase 1 reset + welcome (used by /lead, /client, and Add new lead callback).
    Pass send_welcome=False when the caller already has the details to parse."""
    db.clear_user_state(user_id)
    if context.user_data:
        context.user_data.pop("phase1_attached_files", None)
        context.user_data.pop("phase1_extra_attachments", None)
        context.user_data.pop("phase1_attach_mode", None)
        context.user_data.pop("phase1_vision_batch", None)
        context.user_data.pop("phase1_pending_media", None)
        context.user_data.pop("phase1_pending_edit_key", None)
        context.user_data.pop("phase1_recent_edits", None)
        for _k in ("receipt_lead_id", "receipt_reference_id", "receipt_monday_item_id"):
            context.user_data.pop(_k, None)

    db.set_user_state(user_id, "phase1", {})

    phase1_instruction = (
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
        f"{motivation.get_random_quote()}\n\n"
        "🏁Automated🏎Automotive"
    )
    if send_welcome:
        await reply_message.reply_text(f"Welcome, @{username}! 👋\n\n{phase1_instruction}")


async def _begin_lead_flow_with_review(context, user_id, username, msg) -> int:
    """Start an empty lead but show the interactive review card right away, so the
    issuer sees the field checklist and fills it by typing/voice — friendlier than the
    plain text prompt. Used when someone just says 'new client' / 'start' / taps /lead."""
    await _begin_lead_flow(context, user_id, username, msg, send_welcome=False)
    await _send_phase1_ai_review(msg, {}, context, user_id)
    return STATE_AI_REVIEW


# Bare-word "cancel" / "restart" (typed or spoken) work at any point in the lead flow.
# Whole-message match only, so a field value is never mistaken for a command.
_CANCEL_RE = re.compile(
    r"^\s*(?:cancel|stop|never\s*mind|nvm|scrap(?:\s+it|\s+this)?|forget\s+it|"
    r"abort|quit|exit|discard)\s*[.!]*\s*$",
    re.I,
)
_RESTART_RE = re.compile(
    r"^\s*(?:re-?start|start\s*over|start\s*again|start\s*fresh|begin\s*again|"
    r"redo|reset|do\s*over|over\s*again|scratch\s*that|start\s*from\s*scratch)\s*[.!]*\s*$",
    re.I,
)


# "New lead" / "New client" / "New tag" / "Temp tag" — spoken or typed, in ANY state —
# mean the same as /lead: drop whatever is in progress and open a fresh review card.
# A qualifier ("new", "another", "start a", "temp") is REQUIRED so a bare "tag" or
# "client" inside a value can never wipe a card the issuer is filling in.
# The object a cancel can carry: "cancel it", "scrap that one", "forget the lead".
_OBJ_TAIL_RE = re.compile(
    r"\s+(?:it|this|that|that\s+one|this\s+one|the\s+lead|the\s+tag|the\s+card|"
    r"the\s+client|the\s+order|everything|all\s+of\s+it)\s*[.!]*$",
    re.I,
)
_NEW_LEAD_RE = re.compile(
    r"^\s*(?:(?:new|another|next|start|create|add|a|the)\s+)+"
    r"(?:temp(?:orary)?\s+)?(?:lead|client|customer|tag|sale|entry|order)s?\s*[.!]*\s*$"
    r"|^\s*temp(?:orary)?\s+tags?\s*[.!]*\s*$",
    re.I,
)


def _cancel_restart_kind(text: str, *, strict: bool = False):
    """'restart' | 'cancel' | None for a whole-message cancel/restart phrase.

    ``strict`` is for the handlers that have a prompt open and are waiting for a
    literal value. Cancel and restart are the same destructive action here, with
    no confirmation anywhere, so a prompt expecting a value gets the exact word
    or nothing — a driver note reading "never mind that" must not wipe a card.
    """
    t = (text or "").strip()
    if not t:
        return None
    kind = _cancel_restart_kind_once(t, strict=strict)
    if kind or strict:
        return kind
    # "cancel it", "scrap that one", "never mind this" — the same instruction
    # carrying an object the whole-message patterns cannot see past.
    n = _OBJ_TAIL_RE.sub("", _norm_command_text(t)).strip()
    return _cancel_restart_kind_once(n) if n and n != t else None


def _cancel_restart_kind_once(t: str, *, strict: bool = False):
    if _RESTART_RE.match(t):
        return "restart"
    # Asking for a new lead IS a restart: wipe the current card, open a fresh one.
    # Handled here so it works in every state that already honours cancel/restart —
    # the review card, the pickers, phase 1/2 and idle chat — instead of being
    # filed as a field value (it used to become the client's NAME).
    #
    # NOT under `strict`, because unlike cancel/restart this family is made of
    # ordinary nouns: "temp tag" is the product, "the order" is a plausible note,
    # "another client" is a plausible source. With a prompt open and a value
    # expected, matching those costs the operator the entire card.
    if not strict and _NEW_LEAD_RE.match(t):
        return "restart"
    if _CANCEL_RE.match(t):
        return "cancel"
    return None


def _cancel_restart_kind_from_update(update: Update, *, strict: bool = False):
    msg = update.effective_message
    # A photo CAPTIONED "cancel" carries the same intent as a message saying it,
    # and handle_media_in_any_state already reads the image itself.
    raw = ((msg.text if msg else "") or (msg.caption if msg else "") or "")
    return _cancel_restart_kind(raw, strict=strict)


async def _do_cancel_or_restart(update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str) -> int:
    """Wipe the current lead flow and open a fresh review card.

    CANCEL AND RESTART ARE THE SAME ACTION — by request. Whether it arrives as
    /cancel, /restart, or the spoken/typed word, the result is identical: drop
    whatever is in progress and hand back an empty card, ready for the next tag.
    ``kind`` only decides the wording of the confirmation."""
    msg = update.effective_message
    user_id = update.effective_user.id
    username = update.effective_user.username or "Unknown"
    try:
        await _clear_phase1_vision_upload_state(context, msg.chat_id)
    except Exception:
        pass
    _clear_lead_conversation_user_data(context)
    db.clear_user_state(user_id)
    return await _begin_lead_flow_with_review(context, user_id, username, msg)


async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/restart — abort whatever's in progress and open a fresh tag review."""
    return await _do_cancel_or_restart(update, context, "restart")


async def _autoclean_user_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete the issuer's typed text during the lead flow so the chat stays clean —
    the review card + prompts are the single source of truth. Private chats only, TYPED
    text only (photos/PDFs/voice keep their own handling); best-effort. Safe to call
    before the handler's replies: in a DM, reply_text does not quote the message."""
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat or getattr(chat, "type", None) != "private":
        return
    if (
        getattr(msg, "voice", None) or getattr(msg, "audio", None)
        or getattr(msg, "photo", None) or getattr(msg, "document", None)
    ):
        return  # keep media + let the voice pipeline manage its own echo cleanup
    if not (msg.text or "").strip():
        return
    try:
        await msg.delete()
    except Exception:
        pass


async def begin_lead_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the issuer lead flow (Phase 1). Used by /lead /client /newclient /newsale
    /enterlead /enterclient /newtag /tag. Args are pre-filled: '/lead William Smith'
    starts the lead and AI-parses the name straight into the review."""
    msg = update.effective_message
    if not msg:
        return ConversationHandler.END
    user_id = update.effective_user.id
    username = update.effective_user.username or "Unknown"
    prefill = " ".join(context.args).strip() if getattr(context, "args", None) else ""
    if prefill:
        await _begin_lead_flow(context, user_id, username, msg, send_welcome=False)
        object.__setattr__(msg, "text", prefill)
        return await handle_phase1(update, context)
    return await _begin_lead_flow_with_review(context, user_id, username, msg)


# DB lead states with a live BUTTON PICKER (dispatcher/driver pick after the lead
# was saved). A stray text while the in-memory conversation is gone (redeploy) must
# never fall through to a NEW lead — _begin_lead_flow would clear the saved row and
# destroy the in-flight pick. The pickers' buttons re-enter on their own. NOT here:
# "special_request_drivers" (no lead row yet → safely re-enters the review card) and
# "await_group_accept" (lead fully dispatched → the issuer's next text should start
# their next lead, as before).
_LEAD_MID_DISPATCH_STATES = frozenset({"select_group", "select_driver"})

# DB states whose data blob is still the editable phase1 card: the plain review
# window plus the driver-notes step (set on Accept BEFORE the lead row is created —
# also where an OTS-encryption failure strands the flow). Restoring the review card
# from these is always safe: nothing was dispatched yet.
_LEAD_REVIEWABLE_DB_STATES = ("phase1", "special_request_drivers")

# Words a bare idle message can be without starting a lead (greetings/acks).
_IDLE_CHATTER = frozenset({
    "hi", "hey", "hello", "yo", "sup", "ok", "okay", "k", "kk", "thanks", "thank you",
    "ty", "yes", "no", "yep", "nope", "hola", "gm", "gn", "good morning", "good night",
    "👍", "🙏", "👌", "test",
})
# The whole message IS just a trigger word ("start"/"lead"/"new client"/"new entry"/…)
# → open a fresh empty lead and wait for the details.
_PURE_TRIGGER_RE = re.compile(
    r"^\s*(?:(?:new|add|another)\s+)?(?:lead|client|sale|tag|entry|order)s?\s*$"
    # "start" is NOT here: bare "start" runs /start (see _BARE_COMMANDS), which is
    # what someone typing it expects — the same screen the slash gives them.
    r"|^\s*new\s*$",
    re.I,
)
# Optional leading trigger to strip when the message ALSO carries lead info.
_LEAD_TRIGGER_RE = re.compile(
    r"^\s*(?:new\s+|enter\s+|add\s+|create\s+|start\s+|another\s+|a\s+)?(?:lead|client|sale|tag|order|entry)s?\b[\s:.,\-]*",
    re.I,
)


async def handle_idle_lead_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
    """Idle-chat entry point: ANY substantive typed/spoken text starts a lead and
    auto-fills whatever was given. Very short chatter (hi/ok/thanks) is ignored.
    Voice already arrives here as text via the group -1 pre-processor."""
    msg = update.effective_message
    if not msg or not update.effective_user:
        return None
    # Lead entry is a DM activity — never react to (untagged) group/channel text.
    if update.effective_chat is not None and update.effective_chat.type != "private":
        return None
    text = (msg.text or "").strip()
    low = text.lower().strip(" .!?,")
    if not text or len(text) < 3 or low in _IDLE_CHATTER:
        return None  # ignore — don't start a lead from greetings/acks
    user_id = update.effective_user.id
    username = update.effective_user.username or "Unknown"
    try:
        _dbg = db.get_user_state(user_id)
        logger.info("🔎DIAG idle_lead_start uid=%s text=%r db_state=%s data_keys=%s",
                    user_id, text[:60], (_dbg or {}).get("state"),
                    list((_dbg or {}).get("data") or {})[:6])
    except Exception as _e:
        logger.info("🔎DIAG idle_lead_start uid=%s text=%r db_state=ERR %s", user_id, text[:60], _e)
    # RE-ENTRY (restart-safe): if the DB still holds an active "phase1" lead but PTB's
    # in-memory conversation was lost (redeploy / process restart / multi-worker), this
    # text/voice is an inline REVIEW EDIT on an orphaned card — NOT a new lead and NOT a
    # supervisor command. Restore the card from the saved data and route the edit into the
    # review handler, re-establishing STATE_AI_REVIEW. Without this, control fell through to
    # _begin_lead_flow which wiped the card to empty (the "it does nothing / stays -" bug).
    if not _cancel_restart_kind(text):  # let "restart"/"cancel" keep their own handling below
        try:
            _active = db.get_user_state(user_id)
        except Exception as _e:
            # Storage down must not crash the entry point (the message would die
            # silently) — treat as "no active lead" and fall through.
            logger.warning("idle re-entry: get_user_state failed: %s", _e)
            _active = None
        if _active and _active.get("state") in _LEAD_REVIEWABLE_DB_STATES:
            logger.info("🔎DIAG re-entry FIRED uid=%s text=%r -> restoring review card", user_id, text[:60])
            try:
                await _repost_review_card(msg, dict(_active.get("data") or {}), context, user_id)
                await handle_phase1_review_message(update, context)
            except Exception as _e:
                logger.exception("🔎DIAG re-entry CRASHED: %s", _e)
                await msg.reply_text(f"⚠️ (diag) re-entry error: {type(_e).__name__}: {_e}")
            return STATE_AI_REVIEW
        if _active:
            context.user_data.pop("state_miss_once", None)
        elif context.user_data.get("review_message_id"):
            # RAM still shows a live review card but the DB read came back empty —
            # more likely a storage miss than a real wipe. Never destroy the lead
            # on a first miss: ask to retry. A SECOND consecutive miss means the
            # row really is gone — drop the stale ids and route normally.
            if not context.user_data.get("state_miss_once"):
                context.user_data["state_miss_once"] = True
                await msg.reply_text("⚠️ Storage hiccup — please send that again in a few seconds.")
                return None
            context.user_data.pop("state_miss_once", None)
            context.user_data.pop("review_message_id", None)
            context.user_data.pop("review_chat_id", None)
        logger.info("🔎DIAG re-entry SKIPPED uid=%s db_state=%s (not phase1) -> falling through",
                    user_id, (_active or {}).get("state"))
    # A global supervisor can talk to the bot (ask about data, toggle settings,
    # enable/disable a group or driver, broadcast) instead of starting a lead.
    if _user_is_global_supervisor(user_id):
        handled = False
        try:
            handled = await _route_supervisor_message(update, context, user_id, text)
        except Exception as e:
            logger.warning("supervisor router failed: %s", e)
            handled = False
        if handled:
            raise ApplicationHandlerStop
    # "cancel" and "restart" are the SAME action: clear any saved lead (an orphaned
    # card survives a redeploy in the DB) and hand back a fresh review card.
    _cr = _cancel_restart_kind(text)
    if _cr:
        _clear_lead_conversation_user_data(context)
        db.clear_user_state(user_id)
        return await _begin_lead_flow_with_review(context, user_id, username, msg)
    # A lead is MID-DISPATCH per the DB but the in-memory conversation is gone
    # (redeploy). Starting a new lead here would WIPE it — protect it and point
    # back to the buttons (which re-enter via entry points on their own).
    if _active and _active.get("state") in _LEAD_MID_DISPATCH_STATES:
        await msg.reply_text(
            "⏳ Your current tag is mid-dispatch — use the buttons on the messages "
            "above to continue, or say “restart” to drop it and start a new one."
        )
        return None
    # Bare trigger word ("start", "lead", "new client", "new entry", "tag") → empty
    # lead shown as the review card, so they immediately see what's needed.
    if _PURE_TRIGGER_RE.match(text):
        return await _begin_lead_flow_with_review(context, user_id, username, msg)
    # Trigger + info ("new lead William Smith") or just info ("William Smith") → parse it.
    body = _LEAD_TRIGGER_RE.sub("", text).strip()
    await _begin_lead_flow(context, user_id, username, msg, send_welcome=not body)
    if body:
        object.__setattr__(msg, "text", body)
        return await handle_phase1(update, context)
    return STATE_PHASE1


# ── Supervisor voice/text router ─────────────────────────────────────────────
# A global supervisor can just talk to the bot — ask about drivers / groups /
# receipts, toggle settings, enable/disable a group or driver, or broadcast — by
# voice or plain typing. We only spend an AI classification call when the text
# smells like a command or a question (so dictating a lead stays on the fast
# path). Destructive actions (disable a group/driver, broadcast) are staged behind
# an inline Confirm button; reads and enables apply immediately.
_ROUTER_HINT_RE = re.compile(
    r"\?\s*$"
    r"|^\s*(?:who|what|which|when|where|how\s+many|how\s+much|is|are|do|does|can|show|list|tell)\b"
    r"|\b(?:disable|enable|deactivate|activate|reactivate|suspend|unsuspend|"
    r"turn\s+(?:on|off)|switch\s+(?:on|off)|broadcast|announce|announcement|pause|"
    r"resume|block|unblock|redact(?:ion)?|driver\s*block|driverblock|pending|owes?|"
    r"owed|suspended|status|look\s*up|reference|refs?|plate|tag\s*number|"
    r"control\s*number|temp\s*tag|resident)\b",
    re.I,
)
# Which plate/control counter a spoken "which" maps to (column on tag_plate_settings).
_ROUTER_PLATE_COL = {
    "resident_plate": "nj_plate_next_number",
    "nonresident_plate": "non_nj_plate_next_number",
    "resident_control": "nj_car_next_number",
    "nonresident_control": "non_nj_car_next_number",
}
# A staged clarification ("what should the announcement say?") is only honored if
# answered promptly — otherwise a forgotten prompt could later swallow an unrelated
# lead dictation. Real answers arrive in seconds; abandoned ones auto-discard.
_ROUTER_FOLLOWUP_TTL_SEC = 180
# Clearly a command, never lead info — used to avoid starting a spurious lead when
# the AI router is unavailable (no OpenAI key / quota).
_ROUTER_STRONG_RE = re.compile(
    r"\b(?:disable|enable|deactivate|activate|reactivate|suspend|unsuspend|"
    r"turn\s+(?:on|off)|broadcast|announce|driver\s*block|driverblock|"
    r"tag\s*number|plate\s*number|control\s*number|temp\s*tag)\b",
    re.I,
)


def _fmt_router_groups() -> str:
    groups = db.get_all_groups() or []
    if not groups:
        return "🏢 No groups configured."
    active = [g for g in groups if record_is_active(g)]
    lines = [f"🏢 Groups — {len(active)} active / {len(groups)} total:"]
    for g in groups:
        flag = "" if record_is_active(g) else " (disabled)"
        lines.append(f"• {g.get('group_name') or '—'}{flag}")
    return "\n".join(lines)


def _fmt_router_drivers() -> str:
    drivers = _get_all_drivers_cached() or []
    if not drivers:
        return "🚗 No drivers configured."
    suspended = _get_suspended_driver_ids()
    active = [d for d in drivers if record_is_active(d)]
    lines = [f"🚗 Drivers — {len(active)} active / {len(drivers)} total:"]
    for d in drivers:
        if not record_is_active(d):
            flag = " (inactive)"
        elif str(d.get("id")) in suspended:
            flag = " ⛔ suspended"
        else:
            flag = ""
        # Names only — contact details are read in /settings, not here.
        lines.append(f"• {d.get('driver_name') or '—'}{flag}")
    return "\n".join(lines)


def _fmt_router_suspended() -> str:
    suspended = _get_suspended_driver_ids()
    if not suspended:
        return "✅ No drivers are suspended (nobody is at the receipt-debt threshold)."
    drivers = {str(d.get("id")): d for d in (_get_all_drivers_cached() or [])}
    lines = [f"⛔ Suspended drivers ({len(suspended)}):"]
    for did in suspended:
        d = drivers.get(str(did))
        lines.append(f"• {(d.get('driver_name') if d else None) or did}")
    return "\n".join(lines)


def _fmt_router_pending_receipts() -> str:
    try:
        owed = db.get_drivers_owed_receipts_over_24h() or []
    except Exception as e:
        logger.warning("router pending_receipts lookup failed: %s", e)
        owed = []
    if not owed:
        return "✅ No drivers owe receipts past 24h."
    lines = [f"🧾 Drivers owing receipts ({len(owed)}):"]
    for row in owed:
        refs = row.get("refs") or []
        lines.append(f"• {row.get('driver_name') or row.get('driver_id') or '—'} — {len(refs)} owed")
    return "\n".join(lines)


def _fmt_router_usage() -> str:
    try:
        stats = db.get_lead_sender_stats() or []
    except Exception as e:
        logger.warning("router usage lookup failed: %s", e)
        stats = []
    if not stats:
        return "📊 No lead activity recorded yet."
    total_7d = sum(int(s.get("leads_count_7d") or 0) for s in stats)
    return (
        f"📊 Lead activity: {len(stats)} sender(s) on record, "
        f"{total_7d} lead(s) in the last 7 days."
    )


def _fmt_router_lead_lookup(reference: str) -> str:
    ref = (reference or "").strip()
    if not ref:
        return "Tell me the reference id to look up (e.g. “look up ABC12345”)."
    try:
        lead = db.get_lead_by_reference_id(ref)
    except Exception as e:
        logger.warning("router lead_lookup failed: %s", e)
        lead = None
    if not lead:
        return f"🔎 No lead found for reference {ref}."
    name = _client_display_name_from_lead(lead)
    created = (lead.get("created_at") or "")[:16].replace("T", " ")
    status = ""
    try:
        st = db.get_lead_assignment_status(str(lead.get("id")))
        if isinstance(st, dict):
            status = str(st.get("status") or "").strip()
        elif st:
            status = str(st).strip()
    except Exception:
        status = ""
    out = [f"🔎 Reference {ref}", f"👤 {name}"]
    if created:
        out.append(f"🕒 {created}")
    if status:
        out.append(f"📌 Status: {status}")
    return "\n".join(out)


_ROUTER_HELP = (
    "🤖 You can just tell me things like:\n"
    "• “which groups are active?” / “list drivers” / “who's suspended?”\n"
    "• “who owes receipts?” / “how many leads this week?”\n"
    "• “look up ABC12345”\n"
    "• “turn driver phone redaction on/off”\n"
    "• “disable group HighKage” / “activate driver Arman” (I'll confirm first)\n"
    "• “broadcast: trucks roll out at 9” (I'll confirm first)\n"
    "• “update resident tag number 553300” / “set non-resident control to 12345”\n"
    "• …or just send / forward a tag photo or PDF and I'll read the number\n"
    "Or just start dictating a client and I'll build the tag."
)


def _set_group_active(group_id, want_active: bool) -> bool:
    """Toggle a group to the desired active state (no-op if already there)."""
    g = db.get_group_by_id(group_id)
    if not g:
        return False
    if bool(record_is_active(g)) == bool(want_active):
        return True
    return bool(db.toggle_group_status(group_id))


def _set_driver_active(driver_id, want_active: bool) -> bool:
    """Toggle a driver to the desired active state (no-op if already there)."""
    cur = next((d for d in (db.get_all_drivers() or []) if str(d.get("id")) == str(driver_id)), None)
    if not cur:
        return False
    if bool(record_is_active(cur)) == bool(want_active):
        return True
    return bool(db.toggle_driver_status(driver_id))


async def _router_stage_confirm(msg, context, action: dict) -> None:
    """Stash a destructive action and ask for an inline Confirm/Cancel. A per-stage
    token rides in the callback data, so if a second action is staged before the
    first is resolved, the older card's buttons no longer match and can't fire the
    newer action (they report 'expired' instead)."""
    token = generate_reference_id()
    action["token"] = token
    context.user_data["router_pending"] = action
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm", callback_data=f"route_do:{token}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"route_no:{token}"),
    ]])
    await msg.reply_text(action.get("prompt") or "Confirm this action?", reply_markup=kb)


def _looks_like_plate_answer(text: str) -> bool:
    """True only if the message is just a plate/control number (optionally H-prefixed
    or V-suffixed, spaces/dashes ok) — NOT prose, a lead dictation, or another command.
    Lets a pending 'what's the new number?' consume '553300' / 'H553300' but never
    swallow 'William Smith apt 5', 'cancel', or 'look up ABC12345'."""
    t = (text or "").strip()
    if not re.fullmatch(r"[Hh]?\d[\d\s\-]*[Vv]?", t):
        return False
    return len(re.sub(r"\D", "", t)) >= 3


async def _stage_plate_confirm(msg, context, col: str, digits: str) -> None:
    """Stage a one-tap Confirm to set a plate/control counter to ``digits``."""
    cur = await asyncio.to_thread(db.get_plate_settings) or {}
    label = _PLATE_SET_LABELS.get(col, col)
    await _router_stage_confirm(msg, context, {
        "kind": "set_plate", "col": col, "value": int(digits),
        "prompt": (
            f"Set {label} to {int(digits)} "
            f"(currently {cur.get(col, '—')})?\nThe H/V letter is kept automatically."
        ),
    })


# The operator is told the model is down, but not on every message — an outage
# lasting an afternoon would otherwise become its own kind of noise.
_AI_WARN_INTERVAL_SEC = 3600
_ai_warned_at: dict = {}


async def _warn_ai_unavailable(update, context) -> None:
    """Say once an hour that AI understanding is off, so someone tops up.

    Deliberately not silent: the deterministic layer still understands labelled
    phrasings, so the bot keeps working — but nobody would know to fix the
    account, and the fancy phrasings would just quietly stop being understood.
    """
    chat = update.effective_chat.id if update.effective_chat else 0
    now = time.time()
    if now - float(_ai_warned_at.get(chat) or 0) < _AI_WARN_INTERVAL_SEC:
        return
    _ai_warned_at[chat] = now
    try:
        await update.effective_message.reply_text(
            "🤖 AI understanding is unavailable right now (the OpenAI account is "
            "out of credit or rate limited). Everything still works — use a "
            "labelled phrase like “price 150” or “driver Susan”, or the buttons."
        )
    except Exception:
        pass


class PerChatUpdateProcessor(BaseUpdateProcessor):
    """Run different chats concurrently; run one chat's updates in order.

    PTB warns against concurrent updates with ConversationHandler, and it is
    right — the conversation state, context.user_data and the review card are all
    written as though a chat's messages arrive one after another. Two interleaved
    updates from the same person would read a card the other is halfway through
    rewriting.

    But that ordering only ever mattered WITHIN a chat. Holding a per-chat lock
    keeps the guarantee the handlers actually rely on, and drops the one that was
    only ever an accident of the default — that everybody queues behind everybody
    else, which is what makes a one-second model call look like a dead bot.
    """

    def __init__(self, max_concurrent_updates: int = 32):
        super().__init__(max_concurrent_updates)
        self._locks: dict = {}

    def _lock_for(self, update) -> asyncio.Lock:
        """One lock per chat, created on demand.

        Never evicted, on purpose: an asyncio.Lock is a few bytes, the number of
        chats is bounded by the number of people who use this bot, and evicting
        one while somebody holds it is a race with no upside.
        """
        chat = getattr(getattr(update, "effective_chat", None), "id", None)
        key = str(chat if chat is not None else id(update))
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock

    async def do_process_update(self, update, coroutine) -> None:
        async with self._lock_for(update):
            await coroutine

    async def initialize(self) -> None:
        return None

    async def shutdown(self) -> None:
        self._locks.clear()


async def _route_supervisor_message(update, context, user_id, text: str) -> bool:
    """Interpret a supervisor's freeform message. Returns True if handled (caller
    then stops the update so no lead is started); False = not a router command."""
    msg = update.effective_message
    # A pending clarification (broadcast text / lookup reference) consumes the very
    # next idle message — even if it doesn't look like a command — so the answer is
    # never mistaken for a new lead. It's always cleared here (single-use); a stale
    # one (abandoned via a command/photo/chatter, then answered much later) is
    # discarded so it can't swallow an unrelated lead dictation.
    followup = context.user_data.pop("router_followup", None)
    if followup and (time.time() - float(followup.get("ts") or 0)) <= _ROUTER_FOLLOWUP_TTL_SEC:
        answer = (text or "").strip()
        fkind = followup.get("kind")
        if fkind == "broadcast" and answer:
            preview = answer if len(answer) <= 200 else answer[:200] + "…"
            await _router_stage_confirm(msg, context, {
                "kind": "broadcast", "payload": answer,
                "prompt": f"Broadcast to every group, driver, and lead sender?\n\n“{preview}”",
            })
            return True
        if fkind == "lead_lookup" and answer:
            await msg.reply_text(_fmt_router_lead_lookup(answer))
            return True
        # Unusable answer — fall through to normal routing below.

    # A pending plate clarification ("change resident plate number" → "what's the
    # update?") consumes the next TEXT/VOICE number for that specific counter — but
    # ONLY when the reply is actually a number. Anything else (prose, a lead, cancel/
    # restart, another command) drops the pending ask and routes normally, so the
    # supervisor is never trapped. (An image/PDF answer is handled by
    # handle_supervisor_plate_image instead.)
    pfu = context.user_data.pop("router_plate_followup", None)
    if (
        pfu
        and (time.time() - float(pfu.get("ts") or 0)) <= _ROUTER_FOLLOWUP_TTL_SEC
        and _looks_like_plate_answer(text)
    ):
        digits = re.sub(r"\D", "", text or "")
        if digits:
            await _stage_plate_confirm(msg, context, pfu["col"], digits)
            return True

    if not _ROUTER_HINT_RE.search(text):
        return False
    try:
        # Function calling rather than prompt-coaxed JSON: the model gets a typed
        # contract, so a missing argument is a validation result we can ask about
        # instead of a guess written into a record.
        #
        # On a thread because this is a network call in an async handler — the
        # version it replaces blocked the event loop for its whole duration.
        from utils import nl_router
        cls = await asyncio.to_thread(nl_router.classify, text)
    except ai_vision.AIVisionQuotaError:
        cls = None
        await _warn_ai_unavailable(update, context)
    except Exception as e:
        logger.warning("router classify failed: %s", e)
        cls = None
    if not cls:
        # AI unavailable — only intercept if it's unmistakably a command, so we
        # never turn a real lead into a no-op. Otherwise let the lead flow have it.
        if _ROUTER_STRONG_RE.search(text):
            await msg.reply_text(
                "🤖 I couldn't reach the AI router right now. Use /settings, "
                "/announce, or /driverblock directly."
            )
            return True
        return False

    intent = cls.get("intent") or "none"
    args = cls.get("args") or {}

    if intent == "lead":
        return False  # real client info — hand it to the lead flow

    # From here the message is a recognized command/question — handle it and never
    # fall through to the lead flow, even if a reply or DB call fails.
    try:
        if intent == "list_groups":
            await msg.reply_text(_fmt_router_groups())
        elif intent == "list_drivers":
            await msg.reply_text(_fmt_router_drivers())
        elif intent == "list_suspended":
            await msg.reply_text(_fmt_router_suspended())
        elif intent == "pending_receipts":
            await msg.reply_text(_fmt_router_pending_receipts())
        elif intent == "usage":
            await msg.reply_text(_fmt_router_usage())
        elif intent == "lead_lookup":
            ref = str(args.get("reference") or "").strip()
            if not ref:
                context.user_data["router_followup"] = {"kind": "lead_lookup", "ts": time.time()}
                await msg.reply_text("🔎 What's the reference id? Send it in your next message.")
            else:
                await msg.reply_text(_fmt_router_lead_lookup(ref))
        elif intent == "help":
            await msg.reply_text(_ROUTER_HELP)
        elif intent == "driverblock":
            want = bool(args.get("enable"))
            await asyncio.to_thread(_set_driverblock_enabled, want)
            state = "ON — driver messages hide the client phone" if want else "OFF — drivers can see the client phone"
            await msg.reply_text(f"🔐 Phone redaction is now {state}.")
        elif intent == "group_status":
            groups = db.get_all_groups() or []
            g = _match_name(str(args.get("name") or ""), groups, "group_name")
            if not g:
                await msg.reply_text(f"🏢 I couldn't find a group matching “{args.get('name') or ''}”.")
            else:
                want = bool(args.get("enable"))
                gname = g.get("group_name") or "group"
                if want:  # enabling is non-destructive → do it now
                    ok = await asyncio.to_thread(_set_group_active, g.get("id"), True)
                    await msg.reply_text(f"🏢 Group “{gname}” is now enabled." if ok else f"⚠️ Couldn't enable “{gname}”.")
                else:  # disabling stops dispatch → confirm
                    await _router_stage_confirm(msg, context, {
                        "kind": "group_status", "id": g.get("id"), "want_active": False,
                        "prompt": f"Disable group “{gname}”? It will stop receiving leads.",
                    })
        elif intent == "driver_status":
            drivers = _get_all_drivers_cached() or []
            d = _match_name(str(args.get("name") or ""), drivers, "driver_name")
            if not d:
                await msg.reply_text(f"🚗 I couldn't find a driver matching “{args.get('name') or ''}”.")
            else:
                want = bool(args.get("active"))
                dname = d.get("driver_name") or "driver"
                if want:  # activating is non-destructive → do it now
                    ok = await asyncio.to_thread(_set_driver_active, d.get("id"), True)
                    await msg.reply_text(f"🚗 Driver “{dname}” is now active." if ok else f"⚠️ Couldn't activate “{dname}”.")
                else:  # deactivating pulls them from dispatch → confirm
                    await _router_stage_confirm(msg, context, {
                        "kind": "driver_status", "id": d.get("id"), "want_active": False,
                        "prompt": f"Deactivate driver “{dname}”? They'll stop receiving leads.",
                    })
        elif intent == "broadcast":
            payload = str(args.get("message") or "").strip()
            if not payload:
                context.user_data["router_followup"] = {"kind": "broadcast", "ts": time.time()}
                await msg.reply_text("📢 What should the announcement say? Send it in your next message.")
            else:
                preview = payload if len(payload) <= 200 else payload[:200] + "…"
                await _router_stage_confirm(msg, context, {
                    "kind": "broadcast", "payload": payload,
                    "prompt": f"Broadcast to every group, driver, and lead sender?\n\n“{preview}”",
                })
        elif intent == "set_plate":
            col = _ROUTER_PLATE_COL.get(str(args.get("which") or "").strip().lower())
            digits = re.sub(r"\D", "", str(args.get("number") or ""))
            if col and digits:
                await _stage_plate_confirm(msg, context, col, digits)
            elif col:
                # Counter named but no number ("change resident plate number") → ask,
                # and accept the answer as text, a tag photo/PDF, or a voice note.
                context.user_data["router_plate_followup"] = {"col": col, "ts": time.time()}
                await msg.reply_text(
                    f"🔢 What's the new {_PLATE_SET_LABELS.get(col, col)}? "
                    "Send it as text, a tag photo/PDF, or a voice note."
                )
            else:
                await msg.reply_text(
                    "🔢 Which counter? e.g. “change resident plate number” (then send the "
                    "number), or in one go “update resident tag number 553300”."
                )
        elif intent == "set_plate_from_image":
            context.user_data["router_image_followup"] = {"ts": time.time()}
            await msg.reply_text(
                "📷 Send the temp-tag photo or PDF now — I'll read the number and confirm. "
                "The H/V letter is kept automatically."
            )
        else:  # "none" — matched a command hint but isn't actionable; don't start a lead
            await msg.reply_text(_ROUTER_HELP)
    except Exception as e:
        logger.warning("router dispatch failed (%s): %s", intent, e)
        try:
            await msg.reply_text("⚠️ That didn't go through — try again, or use the matching /command.")
        except Exception:
            pass
    return True


async def handle_router_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Execute (or cancel) a staged destructive router action."""
    query = update.callback_query
    if not query:
        return
    await _safe_answer_callback_query(query)
    if not _user_is_global_supervisor(query.from_user.id):
        return
    verb, _, token = (query.data or "").partition(":")
    pending = context.user_data.get("router_pending")
    # Only the card whose token matches the currently-staged action may act; an
    # older (superseded) card reports expired without touching what's pending.
    if not pending or pending.get("token") != token:
        try:
            await query.edit_message_text("⌛ This confirmation expired — please repeat the request.")
        except Exception:
            pass
        return
    context.user_data.pop("router_pending", None)
    if verb == "route_no":
        try:
            await query.edit_message_text("❌ Cancelled.")
        except Exception:
            pass
        return
    kind = pending.get("kind")
    try:
        if kind == "group_status":
            ok = await asyncio.to_thread(_set_group_active, pending.get("id"), bool(pending.get("want_active")))
            await query.edit_message_text("🏢 Group disabled." if ok else "⚠️ Couldn't update the group.")
        elif kind == "driver_status":
            ok = await asyncio.to_thread(_set_driver_active, pending.get("id"), bool(pending.get("want_active")))
            await query.edit_message_text("🚗 Driver deactivated." if ok else "⚠️ Couldn't update the driver.")
        elif kind == "broadcast":
            sent_n, failed_n = await _broadcast_announcement(context, query.from_user.id, pending.get("payload") or "")
            await query.edit_message_text(
                f"📢 Announcement delivered to {sent_n} chat(s)"
                + (f" — {failed_n} failed." if failed_n else ".")
            )
        elif kind == "set_plate":
            col = pending.get("col")
            val = int(pending.get("value"))
            ok = await asyncio.to_thread(db.update_plate_settings, {col: val})
            label = _PLATE_SET_LABELS.get(col, col)
            await query.edit_message_text(
                f"🔢 {label} set to {val}. The H/V letter is applied automatically."
                if ok else "⚠️ Couldn't update the plate number."
            )
        else:
            await query.edit_message_text("⚠️ Unknown action.")
    except Exception as e:
        logger.warning("router confirm execute failed: %s", e)
        try:
            await query.edit_message_text("⚠️ That action failed — check logs.")
        except Exception:
            pass


async def _download_update_image_bytes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Return (bytes, mime) for a photo or document on the update, or (None, None)."""
    msg = update.effective_message
    file_id, mime = None, "image/jpeg"
    if getattr(msg, "photo", None):
        file_id = msg.photo[-1].file_id
        mime = "image/jpeg"
    elif getattr(msg, "document", None):
        file_id = msg.document.file_id
        mime = (getattr(msg.document, "mime_type", "") or "").lower() or "application/octet-stream"
    if not file_id:
        return None, None
    try:
        f = await context.bot.get_file(file_id)
        bio = io.BytesIO()
        await f.download_to_memory(out=bio)
        return bio.getvalue(), mime
    except Exception as e:
        logger.warning("plate image download failed: %s", e)
        return None, None


# Which plate counter a read tag maps to (H → resident, V → non-resident).
_PLATE_IMAGE_KIND_COL = {
    "resident": "nj_plate_next_number",
    "nonresident": "non_nj_plate_next_number",
}


def _user_in_active_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                 ignore=None) -> bool:
    """True when this user/chat is inside any registered ConversationHandler's active
    state, using PTB's in-memory conversation map (the authoritative truth — cleared on
    END, so never stale unlike the DB ``states`` shadow). Lets the idle plate-image
    reader stand down so it never grabs a photo that belongs to an in-progress flow
    (a text-only sub-state like 'send me the missing field' would otherwise fall
    through to it). Degrades to False if PTB internals change, so behavior is safe.

    ``ignore`` skips one handler — used for /settings, which has no image handling of
    its own and is precisely where a plate photo IS the intended input."""
    try:
        groups = context.application.handlers
    except Exception:
        return False
    for group in groups.values():
        for h in group:
            if isinstance(h, ConversationHandler) and h is not ignore:
                try:
                    if h._conversations.get(h._get_key(update)) is not None:
                        return True
                except Exception:
                    continue
    return False


def _in_settings_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """True while this user is inside /settings — where a photo is always meant for a
    plate counter, never for a lead."""
    h = _SETTINGS_CONV_HANDLER
    if h is None:
        return False
    try:
        return h._conversations.get(h._get_key(update)) is not None
    except Exception:
        return False


# A photo of the newest tag issued sets the counter this far past it, so the next
# lead cannot land on a number already printed in the same block.
PLATE_IMAGE_JUMP = 10_000
# Six digits, so a jump off the end wraps rather than printing a 7-digit plate.
_PLATE_MODULUS = 1_000_000


def _plate_col_from_text(plate: str):
    """Which counter a tag belongs to, read off the tag: H###### is resident,
    ######V is not. The AI usually says so itself; this is for when it does not."""
    p = re.sub(r"[^A-Za-z0-9]", "", str(plate or "")).upper()
    if not p:
        return None
    if p.startswith("H") and p[1:].isdigit():
        return "nj_plate_next_number"
    if p.endswith("V") and p[:-1].isdigit():
        return "non_nj_plate_next_number"
    return None


def _plate_after_image(number: int) -> int:
    """The counter value to set from a tag read off a photo."""
    return (int(number) + PLATE_IMAGE_JUMP) % _PLATE_MODULUS


async def handle_supervisor_plate_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Supervisor photo/PDF → read a temp-tag number and stage a plate-counter update —
    works in ANY state. Registered at group -1 (before every conversation), it reads the
    image FIRST: if the AI finds a temp-tag number it stages the update and stops; if it's
    NOT a tag (e.g. a lead's title/license image sent mid-lead) it returns so the update
    flows on to the active conversation's own image handler. Nothing is written until the
    supervisor taps Confirm."""
    msg = update.effective_message
    if not msg or not update.effective_user:
        return
    if getattr(msg, "chat", None) is not None and msg.chat.type != "private":
        return
    if not (getattr(msg, "photo", None) or getattr(msg, "document", None)):
        return
    user_id = update.effective_user.id
    if not _user_is_global_supervisor(user_id):
        return
    # Peek (don't consume yet) at an armed "update <counter> from image" request.
    _pfu = context.user_data.get("router_plate_followup")
    forced_col = None
    if _pfu and (time.time() - float(_pfu.get("ts") or 0)) <= _ROUTER_FOLLOWUP_TTL_SEC:
        forced_col = _pfu.get("col")
    # DEFER a photo sent inside a LIVE lead/receipt conversation to that flow's own image
    # handler (title/license attach, receipt) — BEFORE any download or AI call, so a legit
    # lead image is never read as a tag or hijacked. Unless a plate update was explicitly
    # armed (forced_col). After a restart the in-memory conversation is gone, so an
    # orphaned-lead / idle plate photo is still read here — "no matter the state".
    # /settings is deliberately NOT deferred to: it has no image handler, so a plate
    # photo sent there used to be swallowed and nothing happened. Reading the tag is
    # exactly what the supervisor wants in that screen.
    # Standing on the Plate Numbers screen settles it: the photo is a tag, whatever
    # else is half-open. Ignoring the settings conversation was not enough — an
    # unfinished LEAD conversation still counted here, so the photo deferred to the
    # lead flow and started a new lead instead of updating the counter.
    if (not forced_col
            and not _in_settings_conversation(update, context)
            and _user_in_active_conversation(
                update, context, ignore=_SETTINGS_CONV_HANDLER)):
        return
    # Reading it as a tag now → consume the arming.
    context.user_data.pop("router_plate_followup", None)
    context.user_data.pop("router_image_followup", None)

    img_bytes, mime = await _download_update_image_bytes(update, context)
    if not img_bytes:
        await msg.reply_text("⚠️ Couldn't read that file. Send a clear photo or PDF of the tag.")
        raise ApplicationHandlerStop
    if mime == "application/pdf":
        png = await asyncio.to_thread(ai_vision.pdf_first_page_to_png_bytes, img_bytes)
        if not png:
            await msg.reply_text("⚠️ Couldn't render that PDF. Try a photo instead.")
            raise ApplicationHandlerStop
        img_bytes, mime = png, "image/png"

    note = await msg.reply_text("🔎 Reading the tag number…")
    try:
        result = await asyncio.to_thread(ai_vision.extract_plate_number_from_image, img_bytes, mime)
    except ai_vision.AIVisionQuotaError:
        result = None
    except Exception as e:
        logger.warning("plate image read failed: %s", e)
        result = None
    await _safe_delete_chat_message(context, note.chat_id, note.message_id)

    number = re.sub(r"\D", "", str((result or {}).get("number") or ""))
    if not result or not number:
        extra = ""
        if forced_col:
            # Keep the pinned counter armed so the next photo/text still targets it.
            context.user_data["router_plate_followup"] = {"col": forced_col, "ts": time.time()}
            extra = f" (still updating {_PLATE_SET_LABELS.get(forced_col, 'that counter')})"
        if not forced_col and not _in_settings_conversation(update, context):
            # Not a tag, no counter update requested, and not inside /settings — it is
            # a lead image. Fall through silently so the lead flow reads it; replying
            # here used to eat every forwarded screenshot.
            return
        # Inside /settings the picture was plainly meant for a counter, so a failed
        # read must say so — falling through would start a LEAD from a tag photo.
        await msg.reply_text(
            "⚠️ I couldn't read a tag number. Send a clearer photo, or type it — "
            f"e.g. “update resident tag number 553300”.{extra}"
        )
        raise ApplicationHandlerStop
    read_label = result.get("plate") or number
    # A pending clarification pinned the exact counter — use it, skip H/V auto-detect.
    if forced_col:
        cur = await asyncio.to_thread(db.get_plate_settings) or {}
        label = _PLATE_SET_LABELS.get(forced_col, forced_col)
        jumped = _plate_after_image(number)
        await _router_stage_confirm(msg, context, {
            "kind": "set_plate", "col": forced_col, "value": jumped,
            "prompt": (
                f"Read {read_label} from your image.\nSet {label} to {jumped} "
                f"— that tag plus {PLATE_IMAGE_JUMP:,} "
                f"(currently {cur.get(forced_col, '—')})? The H/V letter is kept automatically."
            ),
        })
        raise ApplicationHandlerStop
    col = _PLATE_IMAGE_KIND_COL.get(result.get("kind")) or _plate_col_from_text(read_label)
    if not col:
        await msg.reply_text(
            f"🔢 I read {read_label} but couldn't tell resident (H) vs non-resident (V). "
            f"Type e.g. “update resident tag number {number}”."
        )
        raise ApplicationHandlerStop
    cur = await asyncio.to_thread(db.get_plate_settings) or {}
    label = _PLATE_SET_LABELS.get(col, col)
    jumped = _plate_after_image(number)
    await _router_stage_confirm(msg, context, {
        "kind": "set_plate", "col": col, "value": jumped,
        "prompt": (
            f"Read {read_label} from your image.\nSet {label} to {jumped} "
            f"— that tag plus {PLATE_IMAGE_JUMP:,} "
            f"(currently {cur.get(col, '—')})? The H/V letter is kept automatically."
        ),
    })
    raise ApplicationHandlerStop


async def handle_driver_add_lead_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Inline: same as /lead or /client (lead ConversationHandler entry)."""
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await _safe_answer_callback_query(query)
    msg = update.effective_message
    if not msg:
        return ConversationHandler.END
    user_id = query.from_user.id
    username = query.from_user.username or "Unknown"
    return await _begin_lead_flow_with_review(context, user_id, username, msg)


async def handle_another_tag_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Inline '➕ Another tag (same client)': re-seat the just-saved client's contact +
       delivery into a fresh Phase 1, blank the vehicle, and reopen review. Same client,
       new vehicle → a fresh reference + plate get minted at Submit."""
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await _safe_answer_callback_query(query)
    msg = update.effective_message
    if not msg:
        return ConversationHandler.END
    user_id = query.from_user.id
    lead_id = (query.data or "").replace("another_tag_", "").strip()
    lead = db.get_lead_by_id(lead_id) if lead_id else None
    if not lead:
        await msg.reply_text("⚠️ Couldn't find that client. Use /lead to start a new one.")
        return ConversationHandler.END

    # Same client & delivery; fresh vehicle.
    p1 = _phase1_from_stored_lead(lead)
    # This is a NEW transaction for the same person, so the previous lead's extra
    # cars must not come with it — their tags were already issued.
    p1.pop(EXTRA_VEHICLES_KEY, None)
    p1["vin"] = ""
    p1["car"] = ""
    p1["color"] = ""
    # These contact fields live on the lead row, not in the details blob — carry them.
    p1["pending_phone_number"] = lead.get("phone_number") or ""
    p1["pending_price"] = lead.get("price") or ""
    p1["email"] = lead.get("email") or ""
    p1["driver_license_id"] = lead.get("driver_license_id") or ""

    _clear_lead_conversation_user_data(context)
    db.clear_user_state(user_id)
    await msg.reply_text(
        "➕ *Same client* — just add the new vehicle (VIN / car / color), then Submit.",
        parse_mode="Markdown",
    )
    await _send_phase1_ai_review(msg, p1, context, user_id)
    return STATE_AI_REVIEW


def _clear_lead_conversation_user_data(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Drop ConversationHandler scratch keys so /cancel leaves no stale UI/state."""
    if not context.user_data:
        return
    for key in (
        "phase1_pending_edit_key",
        "phase1_vision_batch",
        "phase1_vision_reply_chat_id",
        "phase1_vision_extracting",
        "phase1_batch_status_msg_id",
        "phase1_send_another_msg_id",
        "phase1_pending_media",
        "phase1_attached_files",
        "phase1_extra_attachments",
        "phase1_attach_mode",
        "phase1_recent_edits",
        "review_message_id",
        "review_chat_id",
        "vin_choice_api_car",
        "vin_choice_stated_car",
        "vin_conflict_msg_id",
        "vin_flow_msg_ids",
        "missing_fields",
        "missing_field_state_data",
        "add_files_prompt_msg_id",
        "phase2_before_files",
        "send_file_prompt_msg_id",
        "another_file_prompt_msg_id",
        "edit_prompt_msg_id",
        "receipt_lead_id",
        "receipt_reference_id",
        "receipt_monday_item_id",
        "receipt_on_behalf_driver_id",
        "receipt_uploaded_by_supervisor",
    ):
        context.user_data.pop(key, None)


async def _restart_bot_from_top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Driver hub (/start-style) or Phase 1 welcome — shared by /start and /cancel restart."""
    msg = update.effective_message
    if not msg:
        return ConversationHandler.END
    user = update.effective_user
    user_id = user.id
    username = user.username or "Unknown"

    driver = _driver_row_for_telegram_user(user_id)
    if driver:
        driver_nm = driver.get("driver_name", username)
        pending = db.get_driver_pending_receipts(driver["id"])
        n = len(pending)
        lines = [f"Welcome back, {driver_nm}! 🚗"]
        if n >= SUSPENSION_THRESHOLD:
            lines.append(
                f"\n⛔ You are currently suspended — you owe {n} receipt(s).\n"
                "Upload all outstanding receipts to resume receiving leads."
            )
        elif n > 0:
            lines.append(
                f"\n⚠️ You owe {n} receipt(s). At {SUSPENSION_THRESHOLD} unpaid you will be temporarily suspended."
            )
        lines.append("\nTo add a lead, type /lead or /client.")
        lines.append("\nTo view all receipts type /receipts.")
        lines.append("\nTap ❓ Help below or type /help for a full guide.")
        lines.append(f"\n{motivation.get_random_quote()}")
        lines.append("\n🏁Automated🏎Automotive")
        await msg.reply_text(
            "\n".join(lines),
            reply_markup=_driver_add_lead_keyboard_only(),
        )
        return ConversationHandler.END

    await _begin_lead_flow(context, user_id, username, msg)
    return STATE_PHASE1


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the conversation and initialize state."""
    if not update.message:
        return ConversationHandler.END
    return await _restart_bot_from_top(update, context)


# When a picture holds nothing the extractor recognises, some models answer in prose
# ("I'm unable to extract any personal details from this image…") instead of the field
# block. That sentence was being split across the name lines and shown as the client.
_AI_REFUSAL_RE = re.compile(
    r"\b(?:i'?m\s+sorry|i\s+am\s+sorry|i\s+apologi[sz]e|as\s+an\s+ai|"
    r"i'?m\s+unable|i\s+am\s+unable|unable\s+to\s+(?:extract|read|determine|identify|assist|help)|"
    r"cannot\s+(?:extract|read|determine|identify|assist|help|provide)|"
    r"can'?t\s+(?:extract|read|determine|identify|assist|help|provide)|"
    r"no\s+(?:personal\s+)?details?\s+(?:are\s+)?(?:visible|found|present)|"
    r"does\s+not\s+contain\s+(?:any\s+)?(?:personal|required)|"
    r"please\s+provide\s+(?:a\s+)?(?:document|image|clearer))\b",
    re.I,
)


def _value_is_refusal(value) -> bool:
    """True when ONE extracted field is an apology rather than a value.

    Separate from _looks_like_ai_refusal, which judges the whole reply: a model can
    refuse a single line and read the rest of the document perfectly, and throwing
    away a good address, VIN and carrier over one bad line would be worse than the
    bug. Length-capped so a genuine note that happens to say "sorry" survives."""
    v = str(value or "").strip()
    if not v or v == "-":
        return False
    return len(v) <= 120 and bool(_AI_REFUSAL_RE.search(v))


def _looks_like_ai_refusal(text: str) -> bool:
    """True when the model answered in prose instead of the field block.

    Checked on the FIRST few lines only: a genuine extraction can legitimately carry
    such words inside a note, but never as the name/address lines."""
    head = "\n".join((text or "").strip().splitlines()[:3])
    return bool(head) and bool(_AI_REFUSAL_RE.search(head))


def _normalize_ai_phase1_text(text: str) -> str:
    """Strip optional leading 'N) ' and any markdown code fences so parse_phase1_structured
    gets clean lines (models sometimes wrap the 17-line block in ```...``` — otherwise the
    fence leaks into the first field, e.g. name '```')."""
    lines = []
    for line in text.splitlines():
        line = line.strip()
        # Drop a whole-line markdown fence (``` or ```json / ```text).
        if re.fullmatch(r"`{3,}[a-zA-Z]*", line):
            continue
        # Remove leading "1) ", "2) ", ... "11) "
        line = re.sub(r"^\d{1,2}\)\s*", "", line).strip()
        lines.append(line)
    return "\n".join(lines)


def _sanitize_phones_for_send(text: str) -> str:
    """Replace any phone numbers in user content with OneTimeSecret links (no raw numbers)."""
    if not text or not str(text).strip():
        return text or ""
    return phone_redact.replace_phones_with_ots_links(str(text).strip(), ots)


def _telegram_md1_escape(text: str) -> str:
    """Escape text for Telegram legacy Markdown (entity parsing breaks on _ * ` [)."""
    s = str(text or "")
    out = []
    for ch in s:
        if ch in ("\\", "_", "*", "[", "`"):
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def _authoritative_group_id_for_lead(lead: dict | None) -> Optional[str]:
    """Winning group for this lead: accepted ``group_lead_offers`` row beats ``leads.group_id``.

    Broadcast flow initially stores the assistant's *primary* group on ``leads.group_id`` while
    offers are pending; the real winner is the offer with ``status='accepted'``. If those ever
    drift, prefer the offer and self-heal the lead row (forward-step validation).
    """
    if not lead:
        return None
    lid = lead.get("id")
    if lid:
        acc = db.get_accepted_group_for_lead(str(lid))
        if acc and acc.get("group_id"):
            offer_gid = str(acc.get("group_id")).strip()
            db_gid = lead.get("group_id")
            if db_gid is not None and str(db_gid).strip() and str(db_gid).strip() != offer_gid:
                try:
                    db.update_lead(str(lid), {"group_id": offer_gid})
                except Exception as e:
                    logger.warning(
                        "Could not align leads.group_id with accepted offer (lead=%s): %s",
                        lid,
                        e,
                    )
            return offer_gid
    raw = lead.get("group_id")
    if raw is None or str(raw).strip() == "":
        return None
    return str(raw).strip()


def _group_display_name_from_lead(lead: dict | None) -> str:
    """Human group name for UI / supervisory, from authoritative group id only."""
    gid = _authoritative_group_id_for_lead(lead)
    if not gid:
        return ""
    g = db.get_group_by_id(gid)
    if g and (g.get("group_name") or "").strip():
        return (g.get("group_name") or "").strip()
    return ""


def _resolve_selected_group(lead_data: dict, lead: Optional[dict] = None) -> Optional[dict]:
    """Resolve the group row for this lead.

    When ``lead`` is provided, use ``_authoritative_group_id_for_lead`` (accepted offer wins).
    Otherwise fall back to user state — state alone can still name the wrong group after broadcast.
    """
    gid = None
    if lead:
        gid = _authoritative_group_id_for_lead(lead)
    if gid:
        g = db.get_group_by_id(gid)
        if g:
            return g
    sg = lead_data.get("selected_group")
    if isinstance(sg, dict) and sg.get("id") is not None:
        return sg
    gid = lead_data.get("group_id")
    if gid is not None:
        g = db.get_group_by_id(gid)
        if g:
            return g
    return None


def _group_lead_copy_pre_html(phase1_data: dict, encrypted_link: str) -> str:
    """HTML <pre> block for the copy-paste section (shared with group notification + fallbacks)."""
    client_name = (_sanitize_phones_for_send(phase1_data.get("name") or "") or "").strip() or "—"
    d_street = (phase1_data.get("delivery_address") or "").strip()
    d_csz = (phase1_data.get("delivery_city_state_zip") or "").strip()
    delivery_combined = ", ".join(p for p in [d_street, d_csz] if p)
    if not delivery_combined:
        delivery_combined = _sanitize_phones_for_send(phase1_data.get("delivery_details") or "") or ""
    delivery_combined = (delivery_combined or "").strip() or "—"
    extra_time = (_sanitize_phones_for_send(phase1_data.get("extra_info") or "") or "").strip() or "—"
    link = (encrypted_link or "").strip()
    copy_plain = "\n".join([
        "- - - - - - copy & paste - - - - - -",
        f"Client Name: {client_name}",
        f"⏰ {extra_time}",
        f"📍Delivery address: {delivery_combined}",
        f"📞 Phone 🔗 Encrypted Link: {link}",
        "- - - - - - copy & paste - - - - - -",
    ])
    return f"<pre>{html.escape(copy_plain)}</pre>"


def _format_group_lead_message_html(
    reference_id: str,
    phase1_data: dict,
    encrypted_link: str,
    issue_dt,
    expiry_dt,
    special_request_issuers: str,
    *,
    header_text: str = "🏷NEW CLIENT❗️",
) -> str:
    """Telegram HTML for the detailed group lead: copy section in <pre> for tap-to-copy."""
    def _safe_raw(s: str) -> str:
        return (_sanitize_phones_for_send(s or "") or "").strip() or "-"

    def _h(s: str) -> str:
        return html.escape(s or "", quote=False)

    vin_raw = (phase1_data.get("vin") or "").strip() or "-"
    car_raw = (phase1_data.get("car") or "").strip() or "-"
    name_line = _h(_safe_raw(phase1_data.get("name")))
    # Labelled lines so every field stays visible even when partially filled,
    # and the policy / insurance company etc. can't be mistaken for each other.
    tail_lines = [
        f"🏠 Address: {_h(_safe_raw(phase1_data.get('address')))}",
        f"🏙 City/ST/ZIP: {_h(_safe_raw(phase1_data.get('city_state_zip')))}",
        f"🔢 VIN: {_h(vin_raw)}",
        f"🚘 Car: {_h(car_raw)}",
        f"🎨 Color: {_h(_safe_raw(phase1_data.get('color')))}",
        f"🛡 Insurance: {_h(_safe_raw(phase1_data.get('insurance_company')))}",
        f"📄 Policy #: {_h((phase1_data.get('insurance_policy_number') or '').strip() or '-')}",
        f"🕒 Extra: {_h(_safe_raw(phase1_data.get('extra_info')))}",
    ]
    note_i = (special_request_issuers or "").strip()
    if note_i:
        tail_lines.append(f"📝 Issuer note: {_h(_safe_raw(note_i))}")
    else:
        tail_lines.append("📝 Issuer note: —")
    vehicle_block = f"🚗 Vehicle: {name_line}\n" + "\n".join(tail_lines)
    # Extra cars, each with its own owner name, address, VIN, colour and insurer.
    # One tag PDF follows per car; without this the team would see two documents
    # and only one car described.
    for i, v in enumerate(_extra_vehicles(phase1_data)):
        vehicle_block += (
            f"\n\n🚘 <b>{_h(_ordinal_tag_label(i + 2))}</b>: {_h(_safe_raw(v.get('name')))}\n"
            + "\n".join([
                f"🏠 Address: {_h(_safe_raw(v.get('address')))}",
                f"🏙 City/ST/ZIP: {_h(_safe_raw(v.get('city_state_zip')))}",
                f"🔢 VIN: {_h((v.get('vin') or '').strip() or '-')}",
                f"🚘 Car: {_h((v.get('car') or '').strip() or '-')}",
                f"🎨 Color: {_h(_safe_raw(v.get('color')))}",
                f"🛡 Insurance: {_h(_safe_raw(v.get('insurance_company')))}",
                f"📄 Policy #: {_h((v.get('insurance_policy_number') or '').strip() or '-')}",
            ])
        )

    issue_s = issue_dt.strftime("%Y-%m-%d %H:%M:%S %Z") if issue_dt else "N/A"
    expiry_s = expiry_dt.strftime("%Y-%m-%d %H:%M:%S %Z") if expiry_dt else "N/A"

    pre_wrapped = _group_lead_copy_pre_html(phase1_data, encrypted_link)
    return (
        f"{_h(header_text)}\n\n"
        f"📋 Reference ID: <code>{_h(reference_id)}</code>\n"
        f"{vehicle_block}\n\n"
        "Please use Krab Dispatch (@KrabIssuerBot) 📧🚘\n"
        "Enter:\n"
        "• Tag 🏷\n"
        "• Phone 📞\n"
        "• Delivery time ⏰\n"
        "• Delivery address 📍\n"
        "⸻\n"
        "📋 Copy & paste below into the bot 🤖\n"
        f"{pre_wrapped}\n\n"
        f"📅 Issue Date: {_h(issue_s)}\n"
        f"⏰ Expires: {_h(expiry_s)}"
    )


def _dt_from_lead_field(val) -> datetime | None:
    """Parse issue_date / expiration_date from DB (ISO string or datetime)."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    s = str(val).strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except ValueError:
        pass
    for candidate in (s, s.replace(" ", "T", 1)):
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            continue
    try:
        return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        pass
    try:
        return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        pass
    return None


def _issue_and_expiration_for_group_display(lead: dict) -> tuple[datetime | None, datetime | None]:
    """Issue/expiration for group HTML — use DB; if missing (race), NY now + 30 days."""
    from datetime import datetime, timedelta
    import pytz

    issue_dt = _dt_from_lead_field(lead.get("issue_date"))
    exp_dt = _dt_from_lead_field(lead.get("expiration_date"))
    if issue_dt and not exp_dt:
        exp_dt = issue_dt + timedelta(days=30)
    if issue_dt and exp_dt:
        return issue_dt, exp_dt
    if lead.get("id"):
        ny = pytz.timezone("America/New_York")
        issue_dt = datetime.now(ny)
        exp_dt = issue_dt + timedelta(days=30)
        return issue_dt, exp_dt
    return issue_dt, exp_dt


def _phase1_from_stored_lead(lead: dict) -> dict:
    """Rebuild phase1 field dict directly from the lead row.
       Always produces correct fields, even if stored string was misaligned."""
    vd = (lead.get("vehicle_details") or "").strip()
    dd = (lead.get("delivery_details") or "").strip()
    extra = (lead.get("extra_info") or "").strip()

    # Split into lines, pad to at least 11 lines
    lines = [ln.strip() for ln in vd.splitlines()]
    while len(lines) < 11:
        lines.append("-")

    # Force VIN into line index 5 (6th line) if we can find a real VIN in the raw string
    vin_found = _extract_vin_17(vd)
    if vin_found:
        lines[5] = vin_found

    # Force extra_info into line 10 if we have it separately
    if extra:
        lines[10] = extra

    # Now build the dict directly from the fixed lines
    phase1 = {
        "name": lines[0] if lines[0] != "-" else "",
        "address": lines[1] if lines[1] != "-" else "",
        "city_state_zip": lines[2] if lines[2] != "-" else "",
        "delivery_address": lines[3] if lines[3] != "-" else "",
        "delivery_city_state_zip": lines[4] if lines[4] != "-" else "",
        "vin": lines[5] if lines[5] != "-" else "",
        "car": lines[6] if lines[6] != "-" else "",
        "color": lines[7] if lines[7] != "-" else "",
        "insurance_company": lines[8] if lines[8] != "-" else "",
        "insurance_policy_number": lines[9] if lines[9] != "-" else "",
        "extra_info": extra or lines[10] if lines[10] != "-" else "",
    }

    # Extra cars ride along so every renderer downstream can show them without
    # each one needing the raw lead row.
    extra = _extra_vehicles(lead)
    if extra:
        phase1[EXTRA_VEHICLES_KEY] = extra

    # Override delivery details from the dedicated column
    if dd:
        phase1["delivery_details"] = dd
        dlines = [L.strip() for L in dd.splitlines() if L.strip()]
        if len(dlines) >= 1:
            phase1["delivery_address"] = dlines[0]
        if len(dlines) >= 2:
            phase1["delivery_city_state_zip"] = dlines[1]

    return phase1


def _client_display_name_from_lead(lead: dict) -> str:
    """Client / registrant name from stored Phase 1 (for supervisory receipt notices)."""
    try:
        p1 = _phase1_from_stored_lead(lead)
        n = (p1.get("name") or "").strip()
        return n if n else "—"
    except Exception:
        return "—"


def _lead_issue_expiry_supervisory_line(lead: dict) -> str:
    """Issue + expiry times on one line (America/New_York) for receipt supervisory text."""
    issue_dt, exp_dt = _issue_and_expiration_for_group_display(lead)
    ny = pytz.timezone("America/New_York")
    if issue_dt and exp_dt:
        i = issue_dt.astimezone(ny)
        e = exp_dt.astimezone(ny)
        return (
            f"Issue {i.strftime('%Y-%m-%d %H:%M %Z')} · "
            f"Expires {e.strftime('%Y-%m-%d %H:%M %Z')}"
        )
    return "—"


def _lead_issuer_display_from_lead(lead: dict) -> str:
    """Telegram @username of the lead submitter (for supervisory lines)."""
    un = (lead.get("telegram_username") or "").strip()
    # Some older rows stored a numeric Telegram user_id in telegram_username; don't show that as a username.
    if un and un.lower() != "unknown" and not un.isdigit():
        return un if un.startswith("@") else f"@{un}"
    return "Unknown"


def _validate_lead_data_ready_for_send(lead_data: dict) -> tuple[bool, str]:
    if not lead_data.get("phone_number"):
        return False, "Missing phone number."
    enc = lead_data.get("encrypted_data") or {}
    if not enc.get("link"):
        return False, "Missing encrypted link."
    if not lead_data.get("reference_id"):
        return False, "Missing reference ID."
    return True, ""


def _issuer_state_data_from_lead(lead: dict) -> dict:
    """Rebuild issuer conversation state from a persisted lead (e.g. reassign to another group)."""
    enc = {
        "secret_key": lead.get("onetimesecret_token"),
        "metadata_key": lead.get("onetimesecret_secret_key"),
        "link": lead.get("encrypted_link"),
    }
    iss = (lead.get("special_request_issuers") or lead.get("special_request_note") or "") or ""
    out = {
        "vehicle_details": lead.get("vehicle_details") or "",
        "delivery_details": lead.get("delivery_details") or "",
        "phone_number": lead.get("phone_number"),
        "price": lead.get("price"),
        "encrypted_data": enc,
        "reference_id": lead.get("reference_id"),
        "extra_info": lead.get("extra_info") or "",
        "special_request_issuers": iss,
        "special_request_drivers": lead.get("special_request_drivers") or "",
        "special_request_note": iss,
        "username": lead.get("telegram_username") or "Unknown",
    }
    att = lead.get("phase1_attached_files")
    if isinstance(att, list) and att:
        out["attached_files"] = att
    # Extra cars must survive a reassignment. The tags themselves are built from
    # the lead row so they always go out, but without this the group getting the
    # lead would read ONE car and receive two PDFs.
    extra = _extra_vehicles(lead)
    if extra:
        out[EXTRA_VEHICLES_KEY] = extra
    return out


def _resolve_lead_row_for_resend(lead: dict | None) -> dict | None:
    """Copy of ``lead`` with canonical ``group_id`` (accepted offer first), or single-offer fallback."""
    if not lead:
        return None
    out = dict(lead)
    lid = out.get("id")
    auth = _authoritative_group_id_for_lead(out)
    if auth:
        out["group_id"] = auth
        return out
    if not lid or out.get("group_id"):
        return out
    gid = None
    offers = db.get_group_lead_offers(str(lid))
    if len(offers) == 1:
        o = offers[0]
        st = (o.get("status") or "").lower()
        if st in ("pending", "accepted") and o.get("group_id"):
            gid = o.get("group_id")
    if not gid:
        return out
    out["group_id"] = gid
    return out


def _lead_for_resend(lead_id: str) -> dict | None:
    """Load lead for Pick new driver / resend; persist ``group_id`` from offers if the row was missing it."""
    lead = db.get_lead_by_id(lead_id)
    if not lead:
        return None
    merged = _resolve_lead_row_for_resend(lead)
    if merged and merged.get("group_id") and not lead.get("group_id"):
        try:
            db.update_lead(str(lead_id), {"group_id": merged["group_id"]})
        except Exception as e:
            logger.warning("_lead_for_resend: could not persist group_id for lead %s: %s", lead_id, e)
        return db.get_lead_by_id(lead_id) or merged
    return merged or lead


def _validate_lead_row_for_resend(lead: dict | None, *, issuer_user_id: int | None = None) -> tuple[bool, str]:
    """Forward-step check: persisted lead row is complete before Pick new driver / Pick another group."""
    if not lead:
        return False, "Lead not found."
    if issuer_user_id is not None and int(lead.get("user_id") or 0) != int(issuer_user_id):
        return False, "Not your lead."
    if not (lead.get("reference_id") or "").strip():
        return False, "Missing reference ID."
    if not (lead.get("phone_number") or "").strip():
        return False, "Missing phone number."
    if not (lead.get("encrypted_link") or "").strip():
        return False, "Missing encrypted link."
    if not lead.get("group_id"):
        return False, "Missing group assignment."
    vd = (lead.get("vehicle_details") or "").strip()
    dd = (lead.get("delivery_details") or "").strip()
    ei = (lead.get("extra_info") or "").strip()
    if not vd and not dd and not ei:
        return False, "Missing vehicle/delivery details."
    return True, ""


def _build_driver_resend_request_message(lead: dict) -> str:
    """Same driver DM shape as the main send (city/state/zip line + ref + extra + special request)."""
    reference_id = lead.get("reference_id", "N/A")
    phase1 = _phase1_from_stored_lead(lead)
    extra_safe = _sanitize_phones_for_send(lead.get("extra_info") or "")
    spec = _lead_driver_note(lead)
    d_csz_esc = _telegram_md1_escape(phase1.get("delivery_city_state_zip", "") or "")
    extra_esc = _telegram_md1_escape(extra_safe)
    driver_request_message = (
        f"👋Hi! New client 💸 available📈❗️\n\n"
        f"📍 Delivery (City, State, Zip): {d_csz_esc}\n"
        f"📋 Reference ID: `{reference_id}`\n"
        f" Delivery Time 🏷️: {extra_esc}\n"
        f"Please have Car, Driver License, and Laser Printer Ready✅"
    )
    if spec:
        driver_request_message += (
            "\n\n📝 Special request (driver): "
            + _telegram_md1_escape(_sanitize_phones_for_send(spec))
        )
    return driver_request_message


async def _send_full_group_lead_to_chat(
    context: ContextTypes.DEFAULT_TYPE,
    group: dict,
    lead: dict,
    *,
    html_prefix: str | None = None,
    header_text: str = "🏷NEW CLIENT❗️",
    mirror_supervisory: bool = False,
    renewal: bool = False,
    accepted_by: str | None = None,
) -> None:
    """Post the same detailed HTML lead as the issuer flow; optionally mirror to supervisory chat(s)."""
    reference_id = (lead.get("reference_id") or "N/A").strip()
    phase1 = _phase1_from_stored_lead(lead)
    link = (lead.get("encrypted_link") or "").strip()
    issuer_note = _lead_issuer_note(lead)
    issue_dt, exp_dt = _issue_and_expiration_for_group_display(lead)
    body = _format_group_lead_message_html(
        reference_id, phase1, link, issue_dt, exp_dt, issuer_note, header_text=header_text,
    )
    accept_line = (
        f"✅ <b>Accepted by {html.escape(accepted_by, quote=False)}</b>\n\n"
        if accepted_by else ""
    )
    full_html = f"{html_prefix or ''}{accept_line}{body}"
    chat_id = _parse_chat_id(group.get("group_telegram_id"))
    group_name = group.get("group_name", "")
    sup_ids = (
        _supervisory_delivery_chat_ids(group.get("supervisory_telegram_id"))
        if mirror_supervisory
        else []
    )

    targets: list[tuple] = []
    if chat_id:
        targets.append((chat_id, group_name or "group"))
    for sid in sup_ids:
        targets.append((sid, f"supervisory {sid}"))
    if not targets:
        logger.warning(
            "Cannot post full lead: group %s missing group_telegram_id and no supervisory targets",
            group_name,
        )
        return

    async def _post_one(target_cid, label: str) -> None:
        try:
            try:
                await context.bot.send_message(chat_id=target_cid, text=full_html, parse_mode="HTML")
            except Exception as html_err:
                logger.warning("Full lead HTML failed for %s: %s", label, html_err)
                try:
                    await context.bot.send_message(chat_id=target_cid, text=body, parse_mode="HTML")
                except Exception as e2:
                    logger.error("Could not send full lead to %s (retry body fallback): %s", label, e2)
        except Exception as e:
            logger.error("Could not send full lead to %s: %s", label, e)

    await asyncio.gather(
        *(_post_one(tid, label) for tid, label in targets),
        return_exceptions=True,
    )

    # Second message: the generated NJ temp-tag PDF (+ insurance ride-along) to the
    # same targets. This runs only from accept handlers, so the tag goes out for
    # EVERY lead type — including website leads — only after the lead is ACCEPTED.
    # Either side's Accept releases it: handle_accept_lead does the same send for a
    # DRIVER, and the two check each other so exactly one tag is ever built.
    try:
        if not renewal and _driver_already_released_the_tag(lead):
            logger.info("Tag for ref %s already released when the driver accepted; "
                        "the team accept sends none.", reference_id)
        else:
            await _send_all_tag_pdfs(
                context, lead, [tid for tid, _ in targets], renewal=renewal, accepted_by=accepted_by,
            )
    except Exception as e:
        logger.warning("Tag PDF send failed for ref %s: %s", reference_id, e)


def _extra_vehicle_phase1(lead: dict, vehicle: int) -> dict:
    """An extra car shaped like ``_phase1_from_stored_lead`` output, so the tag
    builder needs no other change.

    The delivery lines come from the LEAD: there is one delivery, one phone and
    one price however many cars are on it.
    """
    vehicles = _extra_vehicles(lead)
    idx = vehicle - 2
    v = dict(vehicles[idx]) if 0 <= idx < len(vehicles) else {}
    base = _phase1_from_stored_lead(lead)
    return {
        "name": v.get("name") or "",
        "address": v.get("address") or "",
        "city_state_zip": v.get("city_state_zip") or "",
        "delivery_address": base.get("delivery_address") or "",
        "delivery_city_state_zip": base.get("delivery_city_state_zip") or "",
        "vin": v.get("vin") or "",
        "car": v.get("car") or "",
        "color": v.get("color") or "",
        "insurance_company": v.get("insurance_company") or "",
        "insurance_policy_number": v.get("insurance_policy_number") or "",
        "extra_info": base.get("extra_info") or "",
        "plate": v.get("plate") or "",
        "tag_control_number": v.get("tag_control_number") or "",
    }


def _persist_extra_vehicle_plate(lead: dict, vehicle: int, plate: str, control: str) -> None:
    """Write one extra car's plate back onto the lead, and into the in-memory
    ``lead`` dict too so a re-read in the same request sees it.

    Read-modify-write on a JSON array is only safe because plates are normally
    minted at submit; this is the fallback for a lead created some other way
    (HTTP ingest, an older row) or a renewal.
    """
    vehicles = _extra_vehicles(lead)
    idx = vehicle - 2
    if not (0 <= idx < len(vehicles)):
        return
    vehicles[idx]["plate"] = plate
    vehicles[idx]["tag_control_number"] = control
    lead[EXTRA_VEHICLES_KEY] = vehicles
    db.update_lead(str(lead.get("id")), {EXTRA_VEHICLES_KEY: vehicles})


async def _tag_fields_from_lead(lead: dict, *, renewal: bool = False,
                                vehicle: int = 1) -> dict:
    """Resolve a stored lead into the field dict tag_pdf.build_tag_pdf expects.

    Allocates (and persists) the plate + control number once per car, decodes
    the VIN for year/make/model/body (falling back to the typed vehicle line),
    and sets the registration state that picks the NJ vs non-NJ template. On a
    ``renewal`` the tag gets a FRESH plate + control and a new 30-day window
    (issued today) instead of the original, now-expired values.

    ``vehicle`` is 1 for the lead's own car — the path every existing lead takes,
    unchanged — and 2+ for an extra car, which carries its own name, address,
    registration state, VIN, colour, insurer and plate. The state is read from
    THAT car's city/state/ZIP, so a Florida second car cannot inherit a New York
    first car's plate format or PDF template.
    """
    from utils import tag_pdf

    if vehicle <= 1:
        phase1 = _phase1_from_stored_lead(lead)
    else:
        phase1 = _extra_vehicle_phase1(lead, vehicle)
    first, last = tag_pdf.split_name(phase1.get("name", ""))
    csz = phase1.get("city_state_zip", "")
    state = tag_pdf.parse_state(csz)
    city, zipc = tag_pdf.parse_city_zip(csz, state)
    city, state, zipc = tag_pdf.normalize_city_state_zip(city, state, zipc)
    vin = phase1.get("vin", "")

    decoded = await asyncio.to_thread(tag_pdf.decode_vin_for_tag, vin) if vin else None
    if decoded and decoded.get("make"):
        year, make, model, body = decoded["year"], decoded["make"], decoded["model"], decoded["body"]
    else:
        year, make, model = tag_pdf.parse_car_line(phase1.get("car", ""))
        body = ""
    body = tag_pdf.normalize_body_heuristic(body)

    # Plate + control number. A renewal always mints fresh ones (the old tag
    # expired); otherwise reuse the assigned values so re-sends are identical.
    plate = "" if renewal else (phase1.get("plate") or "").strip()
    control = "" if renewal else (phase1.get("tag_control_number") or "").strip()
    if not plate or not control:
        alloc = await asyncio.to_thread(db.allocate_temp_plate, state == "NJ")
        plate = plate or alloc["plate"]
        control = control or alloc["control_number"]
        try:
            if vehicle <= 1:
                db.update_lead(str(lead.get("id")),
                               {"plate": plate, "tag_control_number": control})
            else:
                _persist_extra_vehicle_plate(lead, vehicle, plate, control)
        except Exception as e:
            logger.warning("Could not persist plate for lead %s: %s", lead.get("id"), e)

    if renewal:
        issued = datetime.now(pytz.timezone("America/New_York")).date()  # fresh 30-day window
    else:
        issue_dt, _exp_dt = _issue_and_expiration_for_group_display(lead)
        issued = issue_dt.date() if issue_dt else None  # expires defaults to issue + 29d

    return {
        "is_nj": state == "NJ",
        "plate": plate,
        "control_number": control,
        "vin": vin,
        "year": year,
        "make": make,
        "model": model,
        "color": phase1.get("color", ""),
        "body": body,
        "first": first,
        "last": last,
        "address": phase1.get("address", ""),
        "city": city,
        "state": state,
        "zip": zipc,
        "insurance_company": phase1.get("insurance_company", ""),
        "policy": phase1.get("insurance_policy_number", ""),
        "issued": issued,
    }


async def _build_and_send_tag_pdf(
    context: ContextTypes.DEFAULT_TYPE, lead: dict, target_chat_ids: list,
    *, renewal: bool = False, accepted_by: str | None = None,
    vehicle: int = 1, ride_insurance: bool = True,
) -> int:
    """Generate ONE car's NJ temp-tag PDF and send it to each chat.

    Returns the number of chats that actually received the PDF (0 if every
    send failed) so callers can avoid marking the tag delivered when it wasn't.

    ``ride_insurance`` exists because the insurance hand-off is idempotent per
    LEAD: on a multi-car lead only one of the calls can ever do anything, so
    ``_send_all_tag_pdfs`` makes it exactly once instead of hoping the ordering
    works out.
    """
    from utils import tag_pdf

    if not target_chat_ids:
        return 0
    fields = await _tag_fields_from_lead(lead, renewal=renewal, vehicle=vehicle)
    pdf = await asyncio.to_thread(tag_pdf.build_tag_pdf, fields)
    reference_id = (lead.get("reference_id") or "N/A").strip()
    plate = fields.get("plate") or "tag"
    total = _vehicle_count(lead)
    # "Car 1 of 2" only when there IS more than one — a single-car caption is
    # exactly the string it has always been.
    which = f" (Car {vehicle} of {total})" if total > 1 else ""
    caption = f"🧾 NJ 30-Day Temp Tag — {plate}{which}\nReference: {reference_id}"
    if accepted_by:
        caption += f"\n✅ Accepted by {accepted_by}"
    filename = f"tag_{re.sub(r'[^A-Za-z0-9]+', '', plate) or 'tag'}.pdf"
    seen: set = set()
    sent = 0
    for cid in target_chat_ids:
        if cid is None or cid in seen:
            continue
        seen.add(cid)
        try:
            await context.bot.send_document(
                chat_id=cid,
                document=InputFile(io.BytesIO(pdf), filename=filename),
                caption=caption,
            )
            sent += 1
        except Exception as e:
            logger.warning("Could not send tag PDF to %s: %s", cid, e)
    # If the issuer opted into insurance, issue + drop the card right next to the tag.
    if ride_insurance:
        await _maybe_ride_insurance_with_tag(context, lead, list(seen))
    return sent


def _driver_already_released_the_tag(lead: dict) -> bool:
    """True when a driver accepted this lead, which now sends the tag itself.

    Deliberately fails OPEN: if the assignment cannot be read we return False and
    the tag goes out. The bug this pair of guards was written for is a tag that
    never arrives, and a duplicate is a nuisance next to a delivery that stalls.
    """
    lead_id = str(lead.get("id") or "").strip()
    if not lead_id:
        return False
    try:
        st = db.get_lead_assignment_status(lead_id)
    except Exception as e:
        logger.warning("Could not read assignment for %s, sending tag anyway: %s", lead_id, e)
        return False
    return bool(st) and (st.get("status") or "").lower() == "accepted"


async def _send_all_tag_pdfs(
    context: ContextTypes.DEFAULT_TYPE, lead: dict, target_chat_ids: list,
    *, renewal: bool = False, accepted_by: str | None = None,
) -> list:
    """Every car on this lead gets its own tag. Returns per-car send counts.

    A single-car lead loops exactly once, so it makes the identical call it
    always made. The list (not a bool) is what lets the paid instant-PDF path
    refuse to mark a lead delivered when the SECOND tag was the one that failed.
    """
    counts = []
    for n in _lead_vehicle_indices(lead):
        try:
            counts.append(await _build_and_send_tag_pdf(
                context, lead, target_chat_ids, renewal=renewal,
                accepted_by=accepted_by, vehicle=n,
                # Insurance is a per-lead hand-off; make it once, on the last car,
                # so every car's details are already persisted when it runs.
                ride_insurance=(n == _lead_vehicle_indices(lead)[-1]),
            ))
        except Exception as e:
            logger.error("Tag PDF for car %s of lead %s failed: %s",
                         n, lead.get("id"), e)
            counts.append(0)
    return counts


async def _attach_extra_vehicles_for_create(payload: dict, source) -> dict:
    """Put the card's extra cars, plates and all, onto a lead payload.

    Called at EVERY ``db.create_lead`` site. There are five, only one of which is
    the review card's own Submit — the rest are reached whenever the phone or
    price still has to be asked for, and each of them would otherwise drop the
    extra cars without a word and issue one tag.
    """
    extra = _extra_vehicles(source)
    if extra:
        payload[EXTRA_VEHICLES_KEY] = await _allocate_extra_vehicle_plates(extra)
    return payload


async def _warn_if_extra_vehicles_were_dropped(message, sent_payload: dict, lead: dict) -> None:
    """Say so when the database could not store the extra cars.

    ``create_lead`` drops columns the schema does not have and retries, which
    keeps the lead saveable — and would otherwise mean the extra cars vanished
    while the issuer was told it all worked.
    """
    wanted = sent_payload.get(EXTRA_VEHICLES_KEY) or []
    if not wanted or _extra_vehicles(lead):
        return
    logger.error(
        "Lead %s saved WITHOUT its %d extra vehicle(s) — extra_vehicles column missing",
        (lead or {}).get("id"), len(wanted),
    )
    if message is None:
        return
    try:
        await message.reply_text(
            f"⚠️ This lead saved, but its {len(wanted)} extra car(s) did NOT — the "
            "database is missing the extra_vehicles column, so only one tag will be "
            "issued.\n\nRun database/migration_extra_vehicles.sql, then add the extra "
            "car(s) again."
        )
    except Exception:
        pass


async def _allocate_extra_vehicle_plates(vehicles: list) -> list:
    """Give every extra car its own plate and control number.

    The NJ-vs-non-NJ format is read from EACH car's own registration city/state/
    ZIP, not the lead's. In the case that prompted this feature car 1 is New York
    and car 2 is Florida; using car 1's state would print an NJ ``H######`` plate
    on a non-NJ template, or vice versa — a legally wrong document, silently.

    Already-allocated plates are left alone so a re-send produces the identical
    tag, exactly as car 1 behaves.
    """
    from utils import tag_pdf

    out = []
    for v in vehicles or []:
        v = dict(v)
        plate = str(v.get("plate") or "").strip()
        control = str(v.get("tag_control_number") or "").strip()
        if not plate or not control:
            state = tag_pdf.parse_state(v.get("city_state_zip") or "")
            alloc = await asyncio.to_thread(db.allocate_temp_plate, state == "NJ")
            plate = plate or alloc["plate"]
            control = control or alloc["control_number"]
        v["plate"] = plate
        v["tag_control_number"] = control
        out.append(v)
    return out


def _multi_tag_notice_lines(lead: dict) -> list:
    """["🏷 2 TAGS ON THIS JOB …"] for a multi-car lead, [] for an ordinary one.

    A list rather than a string so it splices into an existing message with
    ``*``: an empty string would add a blank line to every single-car message
    that already ships today.
    """
    total = _vehicle_count(lead)
    if total <= 1:
        return []
    return [f"🏷 {total} TAGS ON THIS JOB — one per car. "
            f"Check you received all {total} PDFs."]


def _insurance_chat_targets(lead: dict, target_chat_ids) -> list:
    """Everyone who should see a policy: whoever was asked for, plus the dispatch
    team that owns the client and the main team chat.

    The card used to go only to the caller's list — usually just the accepting
    driver — so the team holding the client had no record of the policy and nobody
    but the client ever saw the portal login."""
    out = list(target_chat_ids or [])
    # The team this lead belongs to.
    try:
        gid = (lead or {}).get("group_id")
        if gid:
            group = db.get_group_by_id(str(gid))
            cid = _parse_chat_id((group or {}).get("group_telegram_id"))
            if cid is not None:
                out.append(cid)
    except Exception as e:
        logger.warning("insurance targets: group lookup failed: %s", e)
    # The standing dispatch chat, wherever follow-ups already go.
    try:
        team = followup_team_chat_id()
        if team:
            out.append(team)
    except Exception as e:
        logger.warning("insurance targets: team chat lookup failed: %s", e)
    return out


def _insurance_login_block(policy, portal_email, portal_pw,
                           password_unchanged: bool = False) -> str:
    """Plain-text portal login details to post next to the tag + card.

    `password_unchanged` means this email ALREADY had an account, so the portal
    kept its existing password rather than taking ours. Printing the one we sent
    would hand the client a password that fails at the login screen."""
    base = (Config.TRISTATECOVERAGE_API_BASE or "https://tristatecoverage.com").rstrip("/")
    lines = ["🔐 Insurance portal login (also emailed to the client):"]
    if policy:
        lines.append(f"📋 Policy: {policy}")
    lines.append(f"🌐 {base}/login")
    if portal_email:
        lines.append(f"✉️ Email: {portal_email}")
    if password_unchanged:
        lines.append("🔑 Password: unchanged — this email already had an account.")
        lines.append(f"   Forgotten it? Reset at {base}/login")
    elif portal_pw:
        lines.append(f"🔑 Password: {portal_pw}")
    return "\n".join(lines)


def _synthetic_lead_for_vehicle(lead: dict, vehicle: int) -> dict:
    """One extra car dressed as a lead, so the insurance pipeline needs no change.

    ``_build_and_send_insurance_card`` (and ``detect_card_state``, and
    ``infer_car_and_color_from_vehicle_lines``) all read the 11 positional lines
    of ``vehicle_details``. Handing them a blob containing ONLY this car means
    they resolve this car's name, address, state, VIN, make and colour — with no
    chance of picking up car 1's by index.
    """
    p1 = _extra_vehicle_phase1(lead, vehicle)
    vd = "\n".join([
        p1.get("name") or "-", p1.get("address") or "-", p1.get("city_state_zip") or "-",
        p1.get("delivery_address") or "-", p1.get("delivery_city_state_zip") or "-",
        p1.get("vin") or "-", p1.get("car") or "-", p1.get("color") or "-",
        p1.get("insurance_company") or "-", p1.get("insurance_policy_number") or "-",
        p1.get("extra_info") or "-",
    ])
    out = dict(lead)
    out["vehicle_details"] = vd
    # The extra cars must not travel with the copy — a synthetic single-car lead
    # is the whole point, and leaving them on would recurse.
    out.pop(EXTRA_VEHICLES_KEY, None)
    return out


async def _ride_insurance_for_extra_vehicles(context, lead: dict, chats: list) -> None:
    """Coverage for each extra car that arrived without an insurer of its own.

    Separate from the car-1 path on purpose: that path is idempotent on the
    lead-level ``insurance_card_sent_at``, so a second call there could only ever
    be a no-op. Each car stamps its own result inside its own entry.
    """
    vehicles = _extra_vehicles(lead)
    if not vehicles:
        return
    email = (lead.get("email") or "").strip()
    changed = False
    for i, v in enumerate(vehicles):
        n = i + 2
        if not _vehicle_needs_coverage(v):
            continue                      # already has Geico/Progressive/etc.
        if str(v.get("insurance_card_sent_at") or "").strip():
            continue                      # this car is already done
        if not email:
            for cid in chats:
                try:
                    await context.bot.send_message(
                        chat_id=cid,
                        text=f"🛡 The {_ordinal_tag_label(n)} needs coverage, but no "
                             "client email is on file — card not issued.",
                    )
                except Exception:
                    pass
            return
        try:
            ok, policy, err, portal_email, portal_pw, pdf_bytes = (
                await _build_and_send_insurance_card(_synthetic_lead_for_vehicle(lead, n)))
        except Exception as e:
            logger.warning("Insurance for car %s of lead %s failed: %s", n, lead.get("id"), e)
            continue
        now_iso = datetime.now(pytz.timezone("America/New_York")).isoformat()
        if ok:
            v["insurance_card_policy_number"] = policy
            v["insurance_card_sent_at"] = now_iso
            v["insurance_card_error"] = None
            # The policy now belongs to this car, so the card shows it as insured.
            v["insurance_company"] = v.get("insurance_company") or "TriState Coverage"
            v["insurance_policy_number"] = v.get("insurance_policy_number") or (policy or "")
        else:
            v["insurance_card_error"] = (err or "Unknown error")[:500]
        changed = True
        for cid in chats:
            if ok:
                await _drop_insurance_pdf_in_chat(
                    context, cid, pdf_bytes, policy,
                    caption=f"🛡 Insurance card — {_ordinal_tag_label(n)}. "
                            "Also emailed to the client.",
                )
                if portal_pw:
                    try:
                        await context.bot.send_message(
                            chat_id=cid,
                            text=_insurance_login_block(
                                policy, portal_email, portal_pw,
                                bool(lead.get("portal_password_unchanged"))),
                        )
                    except Exception:
                        pass
            else:
                try:
                    await context.bot.send_message(
                        chat_id=cid,
                        text=f"🛡 Couldn't issue the {_ordinal_tag_label(n)}'s insurance "
                             f"card: {err or 'unknown error'}",
                    )
                except Exception:
                    pass
    if changed:
        try:
            await asyncio.to_thread(
                db.update_lead, str(lead.get("id")), {EXTRA_VEHICLES_KEY: vehicles})
            lead[EXTRA_VEHICLES_KEY] = vehicles
        except Exception as e:
            logger.warning("Could not persist extra-car insurance for lead %s: %s",
                           lead.get("id"), e)


async def _maybe_ride_insurance_with_tag(context, lead: dict, target_chat_ids: list) -> None:
    """Issuer opted into insurance → issue the card when the tag goes out: email +
    portal to the client (existing pipeline) and drop the PDF next to the tag. Idempotent
    via insurance_card_sent_at; fully best-effort so it never blocks tag delivery."""
    try:
        if not lead or not lead.get("wants_insurance") or not lead.get("id"):
            return  # fast path: no DB read for the common (no-insurance) case
        fresh = db.get_lead_by_id(lead.get("id")) or lead
        if not fresh.get("wants_insurance"):
            return
        chats, seen = [], set()
        for cid in (_insurance_chat_targets(fresh, target_chat_ids)):
            key = _norm_chat_id(cid)
            if cid is None or key in seen:
                continue
            seen.add(key)
            chats.append(cid)
        # Extra cars first: car 1's guard below returns early once its card exists,
        # which on a re-send would leave an uninsured second car untouched forever.
        await _ride_insurance_for_extra_vehicles(context, fresh, chats)
        if (fresh.get("insurance_card_sent_at") or "").strip():
            return  # already issued for this lead
        email = (fresh.get("email") or "").strip()
        if not email:
            for cid in chats:
                try:
                    await context.bot.send_message(
                        chat_id=cid,
                        text="🛡 Insurance was requested, but no client email is on file — card not issued.",
                    )
                except Exception:
                    pass
            return
        ok, policy, err, portal_email, portal_pw, pdf_bytes = await _build_and_send_insurance_card(fresh)
        now_iso = datetime.now(pytz.timezone("America/New_York")).isoformat()
        if ok:
            payload = {
                "insurance_card_policy_number": policy,
                "insurance_card_sent_to_email": email,
                "insurance_card_sent_at": now_iso,
                "insurance_card_error": None,
                "portal_email": portal_email or email,
                "portal_password": portal_pw,
                "portal_password_unchanged": bool(fresh.get("portal_password_unchanged")),
            }
        else:
            payload = {
                "insurance_card_policy_number": policy,
                "insurance_card_sent_to_email": email,
                "insurance_card_error": (err or "Unknown error")[:500],
            }
            if portal_email and portal_pw:
                payload["portal_email"] = portal_email
                payload["portal_password"] = portal_pw
        try:
            await asyncio.to_thread(db.update_lead, fresh["id"], payload)
        except Exception as e:
            logger.warning("Could not persist insurance result for lead %s: %s", fresh.get("id"), e)
        if ok:
            # The portal keeps an existing account's password, so a repeat client
            # must not be handed the one we sent — it would fail at the login.
            _unchanged = bool((fresh.get("portal_password_unchanged")))
            login_txt = (_insurance_login_block(policy, portal_email, portal_pw, _unchanged)
                         if (portal_pw or _unchanged) else None)
            for cid in chats:
                await _drop_insurance_pdf_in_chat(
                    context, cid, pdf_bytes, policy,
                    caption="🛡 Insurance card — also emailed to the client.",
                )
                if login_txt:
                    try:
                        await context.bot.send_message(chat_id=cid, text=login_txt)
                    except Exception:
                        pass
        else:
            for cid in chats:
                try:
                    await context.bot.send_message(
                        chat_id=cid,
                        text=f"🛡 Couldn't issue the insurance card: {err or 'unknown error'}",
                    )
                except Exception:
                    pass
    except Exception as e:
        logger.warning("Insurance ride-along failed for lead %s: %s", (lead or {}).get("id"), e)


def _lead_issuer_note(lead: dict) -> str:
    """Note for group / issuers; falls back to legacy special_request_note."""
    v = (lead.get("special_request_issuers") or "").strip()
    if v:
        return v
    return (lead.get("special_request_note") or "").strip()


def _lead_driver_note(lead: dict) -> str:
    return (lead.get("special_request_drivers") or "").strip()


def _delivery_block_plain(lead: dict) -> str:
    raw = (lead.get("delivery_details") or "").strip()
    if not raw:
        return "N/A"
    return raw.replace("\r\n", "\n")


def _build_driver_lead_accepted_message_html(lead: dict) -> str:
    """Full post-accept DM for drivers (HTML): tap-to-copy reference in <code>, safe escapes."""
    def esc(s: str) -> str:
        return html.escape(str(s or ""), quote=False)

    client_name = esc(_client_display_name_from_lead(lead))
    link_raw = _driver_phone_display(lead)
    if link_raw.startswith("http://") or link_raw.startswith("https://"):
        link_line = f"📞Phone open link ({esc(link_raw)})"
    else:
        link_line = f"📞Phone {esc(link_raw)}"
    price = esc((lead.get("price") or "").strip() or "N/A")
    ref = esc((lead.get("reference_id") or "").strip() or "N/A")
    extra = esc(_sanitize_phones_for_send(lead.get("extra_info") or "") or "—")
    delivery = esc(_delivery_block_plain(lead))
    spec_d = _lead_driver_note(lead)
    lines = [
        "✅ LEAD ACCEPTED — 🕊LET'S FLY 💸",
        "",
        f"Client name: {client_name}",
        *_multi_tag_notice_lines(lead),
        "📍 Delivery Address",
        delivery,
        "",
        f"📝Extra info: {extra}",
        "📞 Call Client Now Confirm: 💰 Price • ⏱️ Time • 📍 Location • 🏷 Tag",
        link_line,
        "📞 Click link 🔗 enter password “ callclient “ to view",
        f"💰 Price: {price}",
        f"🆔 Reference ID: <code>{ref}</code>",
    ]
    if spec_d:
        lines.extend(["", f"📝 Special request (driver): {esc(_sanitize_phones_for_send(spec_d))}"])
    lines.extend([
        "",
        "🚨Client must pay dealership directly🚨",
        "💳 We Accept all electronic payment methods:",
        f"CashApp: {esc(Config.DRIVER_PAYMENT_CASHAPP)}",
        f"Venmo: {esc(Config.DRIVER_PAYMENT_VENMO)}",
        f"Zelle: {esc(Config.DRIVER_PAYMENT_ZELLE)}",
        f"PayPal: {esc(Config.DRIVER_PAYMENT_PAYPAL)}",
        "🌐 Payment Page",
        esc(Config.DRIVER_PAYMENT_PAGE_URL or ""),
        "🏦ask client to pay⚡️ electronically🏦",
        "",
        "⚠️ Important Message ‼️",
        "• DO NOT HAND TAG TO CLIENT WITHOUT PAYMENT FIRST✋❌🏷️🧾1️⃣",
        "• Be fast, polite, professional🤵",
        "• Double-check all info ℹ️",
        "• Drive safely 🚘",
        "• Upload receipt 🧾 within 1 hour ⚡️",
        "",
        "👇 Upload Payment Receipt Below 📸",
    ])
    return "\n".join(lines)


async def _phase1_finish_vision_extraction(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    raw_text: Optional[str],
    msg,
    *,
    source_label: str = "image",
    typed_text: Optional[str] = None,
    pdf_vin: Optional[str] = None,
) -> int:
    """Normalize AI vision output, validate, then AI review — shared by photo and PDF.

    ``typed_text`` is the text the user typed alongside the files (photo
    captions) — it gets the same fill-missing regex fallbacks the pure-text
    path applies to the raw message.
    """
    if not msg:
        return STATE_PHASE1
    if not raw_text or not raw_text.strip() or _looks_like_ai_refusal(raw_text):
        # A prose refusal is the same as nothing read — never build a card from it,
        # or the apology itself ends up shown as the client's name.
        await msg.reply_text(
            f"❌ Could not read any lead details from that {source_label}. "
            "Send a clearer picture, or type the details."
        )
        return STATE_PHASE1
    normalized = _normalize_ai_phase1_text(raw_text)
    lines = [ln.strip() for ln in normalized.splitlines() if ln.strip()]
    normalized_11 = "\n".join(lines[: ai_vision.PHASE1_LINE_COUNT]) if len(lines) >= ai_vision.PHASE1_LINE_COUNT else normalized
    state_data = parse_phase1_structured(normalized_11)
    _apply_single_address_as_both(state_data)
    # Same as the typed path: no completeness gate. Whatever the picture gave goes to
    # the review card, where a gap is visible and one edit away from fixed — rejecting
    # it here also threw away the fields that WERE read.
    # Parse extra fields (phone, price, notes, email, driver-license id) from lines 12+
    phone = price = issuer_note = driver_note = None
    email_val = dl_val = None
    extra_lines = lines[ai_vision.PHASE1_LINE_COUNT:]
    for line in extra_lines:
        l = line.strip()
        if not l or l == "-":
            continue
        low = l.lower()
        if low.startswith("phone:"):
            phone = l.split(":", 1)[1].strip()
        elif low.startswith("price:"):
            price = l.split(":", 1)[1].strip()
        elif low.startswith("issuer note:"):
            issuer_note = l.split(":", 1)[1].strip()
            if issuer_note.lower() in ("-", "none", "n/a", "na"):
                issuer_note = None
        elif low.startswith("driver note:"):
            driver_note = l.split(":", 1)[1].strip()
            if driver_note.lower() in ("-", "none", "n/a", "na"):
                driver_note = None
        elif low.startswith("email:"):
            email_val = ai_vision.normalize_email(l.split(":", 1)[1].strip())
        elif low.startswith("driverlicenseid:") or low.startswith("driver license id:") or low.startswith("driver license:") or low.startswith("dl id:") or low.startswith("dl:") or low.startswith("daq:"):
            dl_val = ai_vision.normalize_driver_license_id(l.split(":", 1)[1].strip())

    if phone and price:
        norm_phone = _normalize_ai_phone(phone)
        norm_price = _normalize_ai_price(price)
        if norm_phone:
            state_data["pending_phone_number"] = norm_phone
        if norm_price:
            state_data["pending_price"] = norm_price
        if issuer_note:
            state_data["special_request_issuers"] = issuer_note
        if driver_note:
            state_data["special_request_drivers"] = driver_note
    if email_val:
        state_data["email"] = email_val
    if dl_val:
        state_data["driver_license_id"] = dl_val

    # Fallbacks against the typed caption text — fill-missing only, mirroring
    # the pure-text path's raw-message fallbacks.
    if typed_text:
        if not state_data.get("pending_phone_number") or not state_data.get("pending_price"):
            phone_fb, price_fb, _, _ = _extract_phone_price_notes_from_text(typed_text)
            if not state_data.get("pending_phone_number") and phone_fb:
                state_data["pending_phone_number"] = phone_fb
            if not state_data.get("pending_price") and price_fb:
                state_data["pending_price"] = price_fb
        if not (state_data.get("email") or "").strip() or not (state_data.get("driver_license_id") or "").strip():
            raw_email, raw_dl = _extract_email_and_dl_from_text(typed_text)
            if raw_email and not (state_data.get("email") or "").strip():
                state_data["email"] = raw_email
            if raw_dl and not (state_data.get("driver_license_id") or "").strip():
                state_data["driver_license_id"] = raw_dl

    # The captions typed alongside the pictures are part of the same message: apply
    # anything labeled by hand so it lands even when vision did not repeat it.
    if typed_text:
        _apply_caption_to_lead(state_data, typed_text)

    # Robust VIN extraction from whole raw output
    vin_from_raw = _extract_vin_17(raw_text)
    if vin_from_raw:
        state_data["vin"] = vin_from_raw
    # A VIN lifted from the PDF's own text layer is exact, so it OVERRIDES whatever
    # vision made of the page render — reading 17 small characters off an image is
    # what was missing/mangling the VIN on PDF uploads.
    if pdf_vin:
        state_data["vin"] = pdf_vin

    # Rebuild vehicle_details with correct VIN
    state_data["vehicle_details"] = "\n".join([
        state_data.get("name", "-"),
        state_data.get("address", "-"),
        state_data.get("city_state_zip", "-"),
        state_data.get("delivery_address", "-"),
        state_data.get("delivery_city_state_zip", "-"),
        state_data.get("vin", "-"),
        state_data.get("car", "-"),
        state_data.get("color", "-"),
        state_data.get("insurance_company", "-"),
        state_data.get("insurance_policy_number", "-"),
        state_data.get("extra_info", "-"),
    ])

    _sanitize_phase1_pending_phone_price(state_data)
    db.set_user_state(user_id, "phase1", state_data)
    await _send_phase1_ai_review(msg, state_data, context, user_id)
    return STATE_AI_REVIEW


def _phase1_batch_count_text(batch: list) -> str:
    """User-facing count while queueing photos/PDFs for AI parsing."""
    n = len(batch)
    return f"Received {n} photo(s)."


def _phase1_batch_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("❌ Cancel", callback_data=PHASE1_VISION_CANCEL_CB),
            InlineKeyboardButton("📸➕Photo", callback_data=PHASE1_VISION_PHOTO_CB),
            InlineKeyboardButton("✅ Done", callback_data=PHASE1_VISION_DONE_CB),
        ],
    ])


async def _delete_phase1_transient_prompts(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int | None,
) -> None:
    """Remove the live 'Received N photo(s)' status and any 'Send another photo' nudge."""
    if not context.user_data or not chat_id:
        return
    for key in ("phase1_batch_status_msg_id", "phase1_send_another_msg_id"):
        mid = context.user_data.pop(key, None)
        if mid:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=mid)
            except Exception:
                pass


async def _clear_phase1_vision_upload_state(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int | None = None,
) -> None:
    """Drop queued files and remove any phase1 prompt messages."""
    if not context.user_data:
        return
    await _delete_phase1_transient_prompts(context, chat_id)
    context.user_data.pop("phase1_vision_batch", None)
    context.user_data.pop("phase1_vision_reply_chat_id", None)
    context.user_data.pop("phase1_vision_extracting", None)
    context.user_data.pop("phase1_pending_media", None)
    context.user_data.pop("phase1_typed_notes", None)   # text queued with the uploads


async def _refresh_phase1_batch_status_message(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    batch: list,
) -> None:
    """Drop the prior status / 'send another photo' messages and post a fresh count.

    Deleting + resending (rather than editing) keeps the count visible right
    after the user's latest upload, so it never looks like stale text scrolled
    away from the new photo.
    """
    await _delete_phase1_transient_prompts(context, chat_id)
    sent = await context.bot.send_message(
        chat_id=chat_id,
        text=_phase1_batch_count_text(batch),
        reply_markup=_phase1_batch_keyboard(),
    )
    if context.user_data:
        context.user_data["phase1_batch_status_msg_id"] = sent.message_id


async def handle_phase1_vision_batch_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Cancel upload batch, prompt for more photos, or run AI extraction (Done)."""
    query = update.callback_query
    await _safe_answer_callback_query(query)
    user_id = query.from_user.id
    msg = query.message
    if not msg or not context.user_data:
        return STATE_PHASE1
    chat_id = msg.chat_id
    data = query.data or ""

    if data == PHASE1_VISION_CANCEL_CB:
        await _clear_phase1_vision_upload_state(context, chat_id)
        db.clear_user_state(user_id)
        await msg.reply_text("❌ Cancelled — restarting from the top.")
        return await _restart_bot_from_top(update, context)

    if data == PHASE1_VISION_PHOTO_CB:
        # Drop the "Received N photo(s)" message that hosted the buttons, then
        # nudge the user. The nudge itself is cleared when the next upload
        # posts a fresh cumulative count.
        await _delete_phase1_transient_prompts(context, chat_id)
        sent = await context.bot.send_message(
            chat_id=chat_id,
            text="📸 Send another Photo",
        )
        context.user_data["phase1_send_another_msg_id"] = sent.message_id
        return STATE_PHASE1

    if data != PHASE1_VISION_DONE_CB:
        return STATE_PHASE1

    batch = context.user_data.get("phase1_vision_batch") or []
    if not batch:
        await query.answer("Send at least one photo or PDF first.", show_alert=True)
        return STATE_PHASE1

    context.user_data["phase1_vision_reply_chat_id"] = chat_id
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    return await _execute_phase1_vision_batch_extraction(context, user_id)


async def _execute_phase1_vision_batch_extraction(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
) -> int:
    """Run merged vision extraction on all queued Phase 1 photos/PDFs."""
    if not context.user_data or context.user_data.get("phase1_vision_extracting"):
        return STATE_PHASE1

    batch = list(context.user_data.pop("phase1_vision_batch", None) or [])
    if not batch:
        return STATE_PHASE1

    chat_id = context.user_data.get("phase1_vision_reply_chat_id")
    if not chat_id:
        return STATE_PHASE1

    if not Config.is_ai_vision_configured():
        await context.bot.send_message(
            chat_id=chat_id,
            text="❌ Image extraction is not configured. Please send the details as text.",
        )
        return STATE_PHASE1

    context.user_data["phase1_vision_extracting"] = True
    try:
        # Photo captions typed alongside the files feed the extraction too.
        # Captions AND any text typed alongside the uploads — the whole message is
        # one submission, so the extraction reads the pictures and the words together.
        typed_text = "\n".join(
            [(it.get("caption") or "").strip() for it in batch if (it.get("caption") or "").strip()]
            + [t for t in (context.user_data.get("phase1_typed_notes") or []) if t]
        ) or None
        parts: list[tuple[bytes, str]] = []
        pending_media: list[dict] = []
        pdf_vin = None                  # exact VIN from a PDF text layer, if any
        for item in batch:
            if item.get("kind") == "image":
                img_bytes = item["bytes"]
                img_mime = item.get("mime") or "image/jpeg"
                parts.append((img_bytes, img_mime))
                pending_media.append({"kind": "image", "bytes": img_bytes, "mime": img_mime})
            elif item.get("kind") == "pdf":
                # The PDF's text layer gives the VIN exactly; a page render makes
                # vision guess at 17 small characters, which is what missed it.
                if not pdf_vin:
                    pdf_vin = await asyncio.to_thread(ai_vision.vin_from_pdf, item["bytes"])
                png = await asyncio.to_thread(ai_vision.pdf_first_page_to_png_bytes, item["bytes"])
                if png:
                    parts.append((png, "image/png"))
                    pending_media.append({"kind": "image", "bytes": png, "mime": "image/png"})

        if pending_media:
            context.user_data["phase1_pending_media"] = pending_media

        if not parts:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "❌ Could not read images from those files (PDF render may have failed). "
                    "Try screenshots or type the details."
                ),
            )
            return STATE_PHASE1

        n = len(parts)
        status = await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏳ Reading **{n}** file{'s' if n != 1 else ''}…",
            parse_mode="Markdown",
        )
        try:
            raw_text = await asyncio.to_thread(
                lambda: ai_vision.extract_structured_from_media_parts(parts, typed_text=typed_text)
            )
        except ai_vision.AIVisionQuotaError:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "❌ Extraction is temporarily unavailable (API quota exceeded). "
                    "Please send the details as text."
                ),
            )
            return STATE_PHASE1

        label = "files" if n > 1 else "photo"
        return await _phase1_finish_vision_extraction(
            context, user_id, raw_text, status, source_label=label,
            typed_text=typed_text, pdf_vin=pdf_vin,
        )
    finally:
        if context.user_data:
            context.user_data.pop("phase1_vision_extracting", None)
            context.user_data.pop("phase1_batch_status_msg_id", None)


async def handle_idle_media_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """A photo/PDF sent (or forwarded) with no lead in progress STARTS one and queues
    the file for extraction — the image is the lead details.

    Text arriving on its own already starts a lead; an image did not, so a forwarded
    screenshot or a photo with a caption produced nothing at all."""
    msg = update.effective_message
    if not msg or not update.effective_user:
        return ConversationHandler.END
    if update.effective_chat is not None and update.effective_chat.type != "private":
        return ConversationHandler.END
    user_id = update.effective_user.id
    username = update.effective_user.username or "Unknown"
    # A caption rides along as typed context for the extraction, so "text + image"
    # is read as one thing.
    await _begin_lead_flow(context, user_id, username, msg, send_welcome=False)
    if msg.photo:
        return await handle_phase1_photo(update, context)
    return await handle_phase1_document(update, context)


async def handle_phase1_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Phase 1: queue screenshot(s); user taps Done to run vision on the batch."""
    msg = update.message
    if not msg or not msg.photo:
        return STATE_PHASE1
    if not Config.is_ai_vision_configured():
        await msg.reply_text(
            "❌ Image extraction is not configured. Please send the details as text in the required structure."
        )
        return STATE_PHASE1

    batch = context.user_data.setdefault("phase1_vision_batch", [])
    if len(batch) >= PHASE1_VISION_MAX_FILES:
        await msg.reply_text(f"❌ Maximum {PHASE1_VISION_MAX_FILES} files per lead.")
        return STATE_PHASE1

    photo = msg.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    bio = io.BytesIO()
    await file.download_to_memory(out=bio)
    image_bytes = bio.getvalue()
    mime = "image/jpeg"
    if file.file_path and file.file_path.lower().endswith(".png"):
        mime = "image/png"
    # Photo + text in one message: the caption feeds the AI extraction too.
    batch.append({
        "kind": "image",
        "bytes": image_bytes,
        "mime": mime,
        "caption": (msg.caption or "").strip() or None,
    })
    context.user_data["phase1_vision_reply_chat_id"] = msg.chat_id
    await _refresh_phase1_batch_status_message(context, msg.chat_id, batch)
    return STATE_PHASE1


async def handle_phase1_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Phase 1: queue PDF (first page per file); user taps Done to extract."""
    msg = update.message
    if not msg:
        return STATE_PHASE1
    if not Config.is_ai_vision_configured():
        await msg.reply_text(
            "❌ Document extraction is not configured. Please send the details as text in the required structure."
        )
        return STATE_PHASE1
    doc = msg.document
    if not doc:
        await msg.reply_text("❌ No document received.")
        return STATE_PHASE1
    mime = (doc.mime_type or "").lower()
    fname = (doc.file_name or "").lower()
    pdf_mimes = ("application/pdf", "application/x-pdf")
    if mime not in pdf_mimes and not fname.endswith(".pdf"):
        await msg.reply_text(
            "📄 In Phase 1, send **text**, **photo(s)/screenshot(s)**, or **PDF(s)** with vehicle and delivery details.\n\n"
            "Other file types are not supported for auto-extraction — use a PDF or type the details.",
            parse_mode="Markdown",
        )
        return STATE_PHASE1
    sz = doc.file_size
    if sz is not None and sz > 20 * 1024 * 1024:
        await msg.reply_text(
            "❌ This PDF is too large (max ~20 MB). Please send a smaller file or a screenshot."
        )
        return STATE_PHASE1

    batch = context.user_data.setdefault("phase1_vision_batch", [])
    if len(batch) >= PHASE1_VISION_MAX_FILES:
        await msg.reply_text(f"❌ Maximum {PHASE1_VISION_MAX_FILES} files per lead.")
        return STATE_PHASE1

    file = await context.bot.get_file(doc.file_id)
    bio = io.BytesIO()
    await file.download_to_memory(out=bio)
    pdf_bytes = bio.getvalue()
    batch.append({"kind": "pdf", "bytes": pdf_bytes, "caption": (msg.caption or "").strip() or None})
    context.user_data["phase1_vision_reply_chat_id"] = msg.chat_id
    await _refresh_phase1_batch_status_message(context, msg.chat_id, batch)
    return STATE_PHASE1


async def handle_phase1(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle Phase 1: Vehicle and delivery details. If AI is configured, accept any format and let model rearrange."""
    user_id = update.effective_user.id
    message_text = (update.message.text or "").strip()
    if not message_text:
        await update.message.reply_text(
            "Please send the client/vehicle and delivery details as **text**, or send **photo(s)/PDF(s)** "
            "and tap **Done** when finished.",
            parse_mode="Markdown",
        )
        return STATE_PHASE1

    _cr = _cancel_restart_kind(message_text)
    if _cr:
        return await _do_cancel_or_restart(update, context, _cr)

    # Photos already queued? Then this text belongs WITH them — a bundle of images
    # followed by the details is how leads actually arrive. Read them together
    # instead of discarding the uploads, which is what clearing the batch here did.
    if (context.user_data or {}).get("phase1_vision_batch"):
        context.user_data.setdefault("phase1_typed_notes", []).append(message_text)
        return await _execute_phase1_vision_batch_extraction(context, user_id)

    await _clear_phase1_vision_upload_state(
        context, update.effective_chat.id if update.effective_chat else None
    )

    if Config.is_ai_vision_configured():
        await update.message.reply_text("⏳ Processing…")
        try:
            raw_text = await asyncio.to_thread(
                lambda: ai_vision.extract_structured_from_text(message_text)
            )
        except ai_vision.AIVisionQuotaError:
            await update.message.reply_text(
                "❌ Processing is temporarily unavailable (API quota). "
                "Please try again later or send in the 11-line structure."
            )
            return STATE_PHASE1
        if not raw_text or not raw_text.strip():
            await update.message.reply_text(
                "❌ I couldn't extract the fields from that message. "
                "Try rephrasing or send name, address, delivery, VIN, car, and delivery time."
            )
            return STATE_PHASE1
        normalized = _normalize_ai_phase1_text(raw_text)
        lines = [ln.strip() for ln in normalized.splitlines() if ln.strip()]
        normalized_11 = "\n".join(lines[: ai_vision.PHASE1_LINE_COUNT]) if len(lines) >= ai_vision.PHASE1_LINE_COUNT else normalized
        state_data = parse_phase1_structured(normalized_11)
        _apply_single_address_as_both(state_data)
        # No completeness gate here any more. It rejected the message AND threw away
        # everything that had been extracted, so a lead with a gap could not even get
        # to the card — where the gap is visible and one edit away from fixed.
        db.set_user_state(user_id, "phase1", state_data)
        message_text = update.message.text or update.message.caption or ""

        # ═════════════════════════════════════════════════
        #  1. Always use the AI's own labels for phone, price, and notes (lines 12–15)
        #     These are authoritative – no guesswork needed.
        # ═════════════════════════════════════════════════
        extra_lines = lines[ai_vision.PHASE1_LINE_COUNT:]   # index 11..end
        for line in extra_lines:
            l = line.strip()
            if not l or l == "-":
                continue
            low = l.lower()
            if low.startswith("phone:"):
                state_data["pending_phone_number"] = l.split(":", 1)[1].strip()
            elif low.startswith("price:"):
                state_data["pending_price"] = l.split(":", 1)[1].strip()
            elif low.startswith("issuer note:"):
                note = l.split(":", 1)[1].strip()
                if note.lower() not in ("-", "none", "n/a", "na"):
                    state_data["special_request_issuers"] = note
                else:
                    state_data["special_request_issuers"] = ""   # explicitly clear
            elif low.startswith("driver note:"):
                note = l.split(":", 1)[1].strip()
                if note.lower() not in ("-", "none", "n/a", "na"):
                    state_data["special_request_drivers"] = note
                else:
                    state_data["special_request_drivers"] = ""
            elif low.startswith("email:"):
                e = ai_vision.normalize_email(l.split(":", 1)[1].strip())
                state_data["email"] = e  # always set (may be "")
            elif low.startswith("driverlicenseid:") or low.startswith("driver license id:") or low.startswith("driver license:") or low.startswith("dl id:") or low.startswith("dl:") or low.startswith("daq:"):
                d = ai_vision.normalize_driver_license_id(l.split(":", 1)[1].strip())
                state_data["driver_license_id"] = d

        # ═════════════════════════════════════════════════
        #  2. Fallback: if the AI did NOT give us a phone or price,
        #     try the raw text parser (but NEVER for notes).
        # ═════════════════════════════════════════════════
        if not state_data.get("pending_phone_number") or not state_data.get("pending_price"):
            phone, price, _, _ = _extract_phone_price_notes_from_text(message_text)
            if not state_data.get("pending_phone_number") and phone:
                state_data["pending_phone_number"] = phone
            if not state_data.get("pending_price") and price:
                state_data["pending_price"] = price

        # Fallback for email + driver-license id from the user's raw message
        if not (state_data.get("email") or "").strip() or not (state_data.get("driver_license_id") or "").strip():
            raw_email, raw_dl = _extract_email_and_dl_from_text(message_text)
            if raw_email and not (state_data.get("email") or "").strip():
                state_data["email"] = raw_email
            if raw_dl and not (state_data.get("driver_license_id") or "").strip():
                state_data["driver_license_id"] = raw_dl

        _sanitize_phase1_pending_phone_price(state_data)
        db.set_user_state(user_id, "phase1", state_data)
        await _autoclean_user_msg(update, context)  # parse OK — safe to clear the input now
        await _send_phase1_ai_review(update.message, state_data, context, user_id)
        return STATE_AI_REVIEW
    else:
        # No AI: require the 11-line structure
        state_data = parse_phase1_structured(message_text)
        _apply_single_address_as_both(state_data)
        db.set_user_state(user_id, "phase1", state_data)
        message_text = update.message.text or update.message.caption or ""
        phone, price, issuer_note, driver_note = _extract_phone_price_notes_from_text(message_text)
        if phone and price:
            state_data["pending_phone_number"] = phone
            state_data["pending_price"] = price
            if issuer_note:
                state_data["special_request_issuers"] = issuer_note
            if driver_note:
                state_data["special_request_drivers"] = driver_note
            db.set_user_state(user_id, "phase1", state_data)
        raw_email, raw_dl = _extract_email_and_dl_from_text(message_text)
        if raw_email:
            state_data["email"] = raw_email
        if raw_dl:
            state_data["driver_license_id"] = raw_dl
        _sanitize_phase1_pending_phone_price(state_data)
        db.set_user_state(user_id, "phase1", state_data)
        await _autoclean_user_msg(update, context)  # structured parse OK — clear the input now
        await _send_phase1_ai_review(update.message, state_data, context, user_id)
        # VIN decode is opt-in via the review screen's "🔍 VIN" button.
        missing = ai_vision.detect_missing_fields(state_data, message_text)
        missing = [f for f in missing if f not in PHASE1_OPTIONAL_FIELDS
                   and not _field_already_filled(state_data, f)]
        asked = await _ask_next_missing(
            update.message, context, update.effective_user.id, missing, state_data)
        if asked is not None:
            return asked
        return await _ensure_phone_price_before_files(update.message, context, update.effective_user.id)


# A spoken colour arrives as a sentence ("the color is dark blue", "it's white") —
# strip the lead-in so the field gets the colour itself.
_SPOKEN_COLOR_LEAD_RE = re.compile(
    r"^\s*(?:the\s+)?(?:car(?:'s)?\s+)?(?:colou?r\s*)?(?:is\s+|it'?s\s+|its\s+)?", re.I
)


def _clean_spoken_color(text: str) -> str:
    """'the color is dark blue' -> 'Dark Blue'. Returns '' when nothing is left."""
    v = _SPOKEN_COLOR_LEAD_RE.sub("", (text or "").strip(), count=1).strip(" .,!")
    if not v:
        return ""
    # The palette writes "Blue - Dark"; speech says "dark blue". Keep both readable.
    if len(v.split()) > 1:
        return v.title()
    # A spoken colour word reads as "Red", not the DMV code "RED" the normalizer
    # produces for three-letter tokens — the palette writes "Red" too.
    if v.lower() in _COMMON_COLORS:
        return v.title()
    return ai_vision.normalize_phase1_color(v)


async def _finish_field_edit(context, user_id, state_data, chat_id) -> None:
    """Shared tail of a field edit: save, drop the prompt, re-render the picker."""
    db.set_user_state(user_id, "phase1", state_data)
    prompt_id = context.user_data.pop("edit_prompt_msg_id", None)
    if prompt_id:
        await _safe_delete_chat_message(context, chat_id, prompt_id)
    await _show_edit_picker(context, state_data)


async def handle_phase1_color_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """A colour tapped from the palette — apply it and return to the field picker."""
    query = update.callback_query
    await _safe_answer_callback_query(query)
    _adopt_review_message(context, query)
    user_id = query.from_user.id
    color = (query.data or "").replace(PH1_COLOR_CB, "", 1).strip()
    state = db.get_user_state(user_id)
    if not state or state.get("data") is None or not color:
        return STATE_AI_REVIEW
    state_data = state["data"]
    # WHICH car's colour prompt is open. This used to be hardcoded to car 1, so
    # tapping a colour for the 2nd Tag silently repainted the first car.
    pending = context.user_data.get("phase1_pending_edit_key")
    vehicle_parts = _vehicle_edit_key_parts(pending) if _edit_key_base(pending) == "col" else None
    if vehicle_parts:
        _apply_vehicle_edit(state_data, pending, color)
    else:
        _apply_single_phase1_edit(state_data, "col", color)
        _clean_vin_and_car(state_data)
    context.user_data.pop("phase1_pending_edit_key", None)
    try:
        await query.message.delete()          # the palette itself
    except Exception:
        pass
    context.user_data.pop("edit_prompt_msg_id", None)
    db.set_user_state(user_id, "phase1", state_data)
    chat_id = query.message.chat_id if query.message else None
    if vehicle_parts:
        await _show_vehicle_edit_picker(
            context, state_data, vehicle_parts[0], fallback_chat_id=chat_id)
    else:
        await _show_edit_picker(context, state_data, fallback_chat_id=chat_id)
    if chat_id:
        where = f"{_ordinal_tag_label(vehicle_parts[0])} " if vehicle_parts else ""
        await _send_vanishing(context, chat_id, f"✅ Updated: {where}color → {color}")
    return STATE_AI_REVIEW


async def handle_phase1_price_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """A price tapped from the picker — apply it and return to the field picker.

    The toll toggle rides the same callback: with an amount already on the card it
    completes the edit (add/remove the toll and close); with nothing picked yet it
    just arms itself and leaves the picker open for the amount.
    """
    query = update.callback_query
    await _safe_answer_callback_query(query)
    _adopt_review_message(context, query)
    user_id = query.from_user.id
    raw = (query.data or "").replace(PH1_PRICE_CB, "", 1).strip()
    state = db.get_user_state(user_id)
    if not state or state.get("data") is None:
        return STATE_AI_REVIEW
    state_data = state["data"]

    if raw == PH1_PRICE_TOLL:
        current = state_data.get("pending_price") or ""
        want_toll = not (_price_has_toll(current) or context.user_data.get("phase1_price_toll"))
        if _is_valid_pending_price(current):
            price = _price_with_toll(current, want_toll)
            state_data["pending_price"] = price
            context.user_data.pop("phase1_price_toll", None)
            context.user_data.pop("phase1_pending_edit_key", None)
            try:
                await query.message.delete()
            except Exception:
                pass
            context.user_data.pop("edit_prompt_msg_id", None)
            db.set_user_state(user_id, "phase1", state_data)
            await _show_edit_picker(context, state_data)
            if query.message:
                await _send_vanishing(context, query.message.chat_id,
                                      f"✅ Updated: price → {price}")
            return STATE_AI_REVIEW
        # No amount yet — remember the toll and keep the picker up for the number.
        context.user_data["phase1_price_toll"] = want_toll
        try:
            await query.edit_message_reply_markup(
                reply_markup=_price_picker_keyboard(want_toll))
        except Exception:
            pass
        return None

    # Through the same cleaner the typed path uses, so it gains the "$" that
    # _is_valid_pending_price requires — without it the sanitizer would drop it.
    price = _clean_inline_value("price", raw)
    if not price:
        return STATE_AI_REVIEW
    # Toll carries over from the toggle, or from an amount that already had one.
    if context.user_data.pop("phase1_price_toll", False) or _price_has_toll(
            state_data.get("pending_price")):
        price = _price_with_toll(price, True)
    _apply_single_phase1_edit(state_data, "price", price)
    _sanitize_phase1_pending_phone_price(state_data)
    context.user_data.pop("phase1_pending_edit_key", None)
    try:
        await query.message.delete()          # the picker itself
    except Exception:
        pass
    context.user_data.pop("edit_prompt_msg_id", None)
    db.set_user_state(user_id, "phase1", state_data)
    chat_id = query.message.chat_id if query.message else None
    # Tapped at the "price missing" gate rather than on the review card: the lead is
    # mid-dispatch, so carry on with it instead of reopening the edit picker.
    if context.user_data.pop("phase2_awaiting_price", None):
        if _phase1_has_phone_and_price(state_data):
            db.set_user_state(user_id, "special_request_drivers", state_data)
            if chat_id:
                await _send_vanishing(context, chat_id, f"✅ Price: {price}")
            return await _prompt_issuer_special_request(query.message, context, user_id)
        db.set_user_state(user_id, "phase1", state_data)
        return await _phase2_ask(query.message, context, state_data)
    await _show_edit_picker(context, state_data, fallback_chat_id=chat_id)
    if chat_id:
        await _send_vanishing(context, chat_id, f"✅ Updated: price → {price}")
    return STATE_AI_REVIEW


async def handle_edit_field_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """A photo sent while a field prompt is open. For Color, the AI reads the car's
    colour straight off the picture; for any other field the image goes through the
    normal review parser so nothing sent here is ever lost."""
    message = update.message
    user_id = update.effective_user.id
    ek = context.user_data.get("phase1_pending_edit_key")
    if ek != "col":
        result = await handle_phase1_adjust_input(update, context)
        return STATE_AI_REVIEW if result == STATE_ADJUST_INPUT else result
    state = db.get_user_state(user_id)
    if not state or not state.get("data"):
        return STATE_AI_REVIEW
    state_data = state["data"]
    note = await message.reply_text("🎨 Reading the colour…")
    raw, mime = await _download_update_image_bytes(update, context)
    color = None
    if raw:
        # Every file sent for parsing also rides along to whoever accepts the lead.
        _add_extra_attachment(
            context,
            "document" if (mime or "").endswith("pdf") else "photo",
            mime or "image/jpeg",
            getattr(getattr(message, "document", None), "file_name", None) or "color.jpg",
            raw,
            "🎨 Colour reference",
        )
        try:
            color = await asyncio.to_thread(ai_vision.read_color_from_image, raw, mime)
        except Exception as e:
            logger.warning("colour read failed: %s", e)
    await _safe_delete_chat_message(context, note.chat_id, note.message_id)
    if not color:
        await _send_vanishing(
            context, message.chat_id,
            "⚠️ Couldn't tell the colour from that picture — tap one below or type it.",
        )
        return STATE_EDIT_FIELD_PROMPT
    _apply_single_phase1_edit(state_data, "col", color)
    _clean_vin_and_car(state_data)
    context.user_data.pop("phase1_pending_edit_key", None)
    await _finish_field_edit(context, user_id, state_data, message.chat_id)
    await _send_vanishing(context, message.chat_id, f"✅ Updated: color → {color}")
    return STATE_AI_REVIEW


# Fields whose whole purpose is prose. A note reading "color the car black" is a
# note, so these keep what you type instead of being re-read as labels.
_PROSE_EKS = frozenset({"issuer", "driver", "xtra"})


async def _selection_command_at_a_field_prompt(update, context, user_id: int,
                                              state_data: dict, text: str,
                                              ek: str | None = None):
    """"change the driver Susan" typed with a field prompt open — the next state,
    or None when the text really is a value for the open field.

    An open prompt is where you ARE, not what you MEANT. Without this the
    sentence is written into whichever field is waiting: a car's colour became
    "Change The Driver Susan", and a VIN prompt took the whole sentence as a VIN.
    Both print on the tag.

    Restricted to the three selections on purpose. Labelled field edits already
    route correctly below, and the VIN verbs match loosely enough — _VIN_KEEP_RE
    finds a bare "same" or "keep" anywhere in the line — that a genuine value like
    "Same Day Delivery" typed at the notes prompt would be read as a command.
    """
    # A field prompt is open, so no DMV question is: the VIN verbs are excluded
    # here by design anyway, and asking for them can only produce a false hit.
    kind, _payload = _classify_review_command(text, vin_pending=False)
    if kind not in ("SELECT_DRIVER", "SELECT_GROUP", "SELECT_SOURCE"):
        return None
    # A line can classify as a pick and still be prose: "the drivers all called
    # already" opens with the noun. Ask before dismantling the open prompt —
    # otherwise the prompt is gone and the sentence is still just a value.
    if _payload_is_prose(_payload):
        return None
    # At a NAME prompt a name is a name. "Dispatch Solutions LLC" and "Team
    # Rubicon" are real registrants, and the group regex matches anything opening
    # with dispatch/team/group/crew. Driver selection survives even here: it needs
    # the literal word "driver", which no person or company is called.
    if _edit_key_base(ek) in ("fn", "ln") and kind != "SELECT_DRIVER":
        return None
    chat_id = update.effective_chat.id if update.effective_chat else None
    await _close_open_field_prompt(context, chat_id)
    await _clear_missing_prompts(context)
    context.user_data.pop("phase1_pending_edit_key", None)
    # The interpreter applies the selection, re-renders the card so the button
    # shows the new name, and reports what it did.
    return await _interpret_review_command(update, context, user_id, state_data, text)


async def _place_text_at_field_prompt(state_data: dict, ek: str, text: str):
    """Words typed or spoken while a field prompt is open.

    A prompt is where you are, not a cage: say "color black" at the Price prompt
    and the colour is what changes. Order — an explicit label for another field
    wins, then the open field takes the value if it fits, then the AI decides.

    Returns (handled, changed_labels). handled=False means nothing could be made
    of it, which is the only case worth warning about."""
    text = (text or "").strip()
    if not text:
        return False, []
    parts = _vehicle_edit_key_parts(ek)
    if parts:
        # An extra car's prompt is not a suggestion. Everything below routes by
        # CONTENT, so "Progressive" or "grey" typed here would be filed against
        # car 1 — the exact bug class this feature must not reintroduce.
        n, base = parts
        if _apply_vehicle_edit(state_data, ek, text):
            return True, [f"{_ordinal_tag_label(n)} {PH1_EDIT_PROMPT_LABEL.get(base, base)}"]
        return False, []
    if ek not in _PROSE_EKS:
        labelled = _parse_multi_field_line(text)
        if labelled and len(labelled) == 1 and labelled[0][0] == ek:
            # "price 150" AT the price prompt: same field, so use the value the
            # parser already stripped the label off. Falling through to the raw text
            # wrote "price 150" into the field.
            return True, _apply_ek_value(state_data, ek, labelled[0][1])
        if labelled:
            changed = []
            for p_ek, p_val in labelled:
                for lbl in _apply_ek_value(state_data, p_ek, p_val):
                    if lbl not in changed:
                        changed.append(lbl)
            if changed:
                return True, changed
    value = (_clean_spoken_color(text) or text) if ek == "col" else text
    cleaned = _clean_inline_value(ek, value)
    if cleaned:
        changed = _apply_ek_value(state_data, ek, cleaned)
        if changed:
            return True, changed
        # Accepted by the field but nothing actually moved — "2019 Honda Accord"
        # survives the loose VIN check and is then scrubbed. Let the AI have it
        # before calling this handled.
    # Not a value for this field and not labelled — let the AI find its home.
    changed = await _smart_place_single_value(state_data, text, guess_name=False)
    if changed:
        return True, changed
    return bool(cleaned), []


async def handle_edit_field_text(update, context):
    user_id = update.effective_user.id
    _cr = _cancel_restart_kind_from_update(update, strict=True)
    if _cr:
        return await _do_cancel_or_restart(update, context, _cr)
    await _autoclean_user_msg(update, context)
    ek = context.user_data.get("phase1_pending_edit_key")
    if not ek:
        # Which field the prompt was for lives in memory a restart wipes, and a
        # conversation re-entered from a stale button never had it. The card is
        # still on screen and the words are still an edit — apply them as one
        # rather than returning without a sound, which is what "price 150 did
        # nothing" was.
        return await handle_phase1_review_message(update, context)

    state = db.get_user_state(user_id)
    if not state or not state.get("data"):
        return ConversationHandler.END
    state_data = state["data"]

    text = (update.message.text or "").strip()
    redirected = await _selection_command_at_a_field_prompt(
        update, context, user_id, state_data, text, ek)
    if redirected is not None:
        return redirected
    if text == "-":
        _apply_single_phase1_edit(state_data, ek, "")
    else:
        handled, _ = await _place_text_at_field_prompt(state_data, ek, text)
        if not handled:
            await _send_vanishing(
                context, update.effective_chat.id,
                f"⚠️ I couldn't read that as a {PH1_EDIT_PROMPT_LABEL.get(ek, ek)}. "
                "Try again, or name the field — 'color black', 'price 150'.",
            )
            return STATE_EDIT_FIELD_PROMPT
    _apply_single_address_as_both(state_data)
    _clean_vin_and_car(state_data)
    _sanitize_phase1_pending_phone_price(state_data)
    db.set_user_state(user_id, "phase1", state_data)

    # Delete user message and prompt
    try:
        await update.message.delete()
    except:
        pass
    prompt_id = context.user_data.pop("edit_prompt_msg_id", None)
    if prompt_id:
        try:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=prompt_id)
        except:
            pass

    # Stay in the field-edit picker (values now updated) so they can edit another
    # field or tap ✅ Submit — no need to re-open Edit for each field.
    await _show_edit_picker(context, state_data)
    return STATE_AI_REVIEW

_PHONE_PRICE_PLACEHOLDERS = frozenset(
    ("-", "—", "–", "n/a", "na", "none", "null", "?", "unknown", "tbd", "pending")
)


def _normalize_ai_phone(raw) -> str:
    """Coerce a vision-extracted phone into the +1XXXXXXXXXX shape Phase 2 produces.

    Returns ``""`` when the string clearly isn't a phone number; otherwise the
    same canonical form ``_is_valid_pending_phone`` accepts so the AI output
    flows through without re-prompting the user.
    """
    p = (raw or "").strip()
    if not p or p.lower() in _PHONE_PRICE_PLACEHOLDERS:
        return ""
    digits = re.sub(r"\D", "", p)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) not in (9, 10) or not digits.isdigit():
        return ""
    return "+1" + digits


def _normalize_ai_price(raw) -> str:
    """Coerce a vision-extracted price into the ``$N`` shape Phase 2 expects.

    Strips currency words and stray punctuation, keeps digits + decimal,
    re-adds a leading ``$`` if the AI omitted it.
    """
    p = (raw or "").strip()
    if not p or p.lower() in _PHONE_PRICE_PLACEHOLDERS:
        return ""
    p = p.replace("USD", "").replace("usd", "").replace("Usd", "").strip()
    has_digit = bool(re.search(r"\d", p))
    if not has_digit:
        return ""
    # Keep $ + digits + decimal/comma; drop everything else.
    cleaned = re.sub(r"[^0-9.$,]", "", p).strip()
    if not cleaned:
        return ""
    if "$" not in cleaned:
        cleaned = "$" + cleaned.lstrip("$")
    # The toll is part of the quote, and stripping non-digits would have eaten it.
    return cleaned + _PH1_TOLL_SUFFIX if _price_has_toll(p) else cleaned


def _is_valid_pending_phone(raw) -> bool:
    """Real US-style phone for encryption (+1 and 10 digits); rejects placeholders and junk."""
    p = (raw or "").strip()
    if not p or p.lower() in _PHONE_PRICE_PLACEHOLDERS:
        return False
    d = re.sub(r"\D", "", p)
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    # Match Phase 2: 9–10 digit local numbers
    if len(d) not in (9, 10) or not d.isdigit():
        return False
    return True


def _is_valid_pending_price(raw) -> bool:
    """Require a dollar amount (same expectation as Phase 2 text parsing)."""
    p = (raw or "").strip()
    if not p or p.lower() in _PHONE_PRICE_PLACEHOLDERS:
        return False
    return bool("$" in p and re.search(r"\d", p))


def _sanitize_phase1_pending_phone_price(state_data: dict) -> None:
    """Drop invalid phone/price so the user is prompted in Phase 2 / before files."""
    if not _is_valid_pending_phone(state_data.get("pending_phone_number")):
        state_data.pop("pending_phone_number", None)
    if not _is_valid_pending_price(state_data.get("pending_price")):
        state_data.pop("pending_price", None)


def _phase1_has_phone_and_price(state_data: dict) -> bool:
    return _is_valid_pending_phone(state_data.get("pending_phone_number")) and _is_valid_pending_price(
        state_data.get("pending_price")
    )


def _phase2_missing(state_data: dict) -> tuple:
    """(phone_missing, price_missing) — so we only ever ask for what is absent."""
    return (
        not _is_valid_pending_phone((state_data or {}).get("pending_phone_number")),
        not _is_valid_pending_price((state_data or {}).get("pending_price")),
    )


def _phase2_prompt(state_data: dict) -> str:
    """Ask for the missing piece ONLY. Asking for both when one was already given
    reads like the bot ignored what was sent, and invites re-typing a good value."""
    needs_phone, needs_price = _phase2_missing(state_data)
    if needs_phone and needs_price:
        return ("📞💲 Phone number and price missing: please enter the client's phone "
                "number then the price\n(example: +1234567890 $150)")
    if needs_phone:
        return "📞 Client phone number missing: please enter the client's phone number"
    if needs_price:
        return "💲 Price missing: tap one below, or say/type it (150 or $150)"
    return ""


async def _phase2_ask(message, context, state_data) -> int:
    """Ask for whatever is still missing, with the price buttons when it is the
    price. The amounts are the same handful here as under Edit -> Price, so the
    same picker serves both; the flag tells the tap handler to carry on with the
    dispatch instead of dropping back to the review card."""
    needs_phone, needs_price = _phase2_missing(state_data or {})
    markup = None
    if needs_price and not needs_phone:
        context.user_data["phase2_awaiting_price"] = True
        markup = _fresh_price_picker(context, state_data or {})
    else:
        context.user_data.pop("phase2_awaiting_price", None)
    await message.reply_text(_phase2_prompt(state_data or {}), reply_markup=markup)
    return STATE_PHASE2


async def _safe_delete_chat_message(context: ContextTypes.DEFAULT_TYPE, chat_id, message_id) -> None:
    """Best-effort delete; ignore Telegram restrictions (already gone, too old, etc.)."""
    if not chat_id or not message_id:
        return
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


async def _delete_pending_file_prompts(context: ContextTypes.DEFAULT_TYPE, chat_id) -> None:
    """Remove the "Do you want to add files?" / "Send the file" / "Send another?" prompt
    messages so the chat ends up clean once the user uploads or declines."""
    for key in (
        "add_files_prompt_msg_id",
        "send_file_prompt_msg_id",
        "another_file_prompt_msg_id",
    ):
        mid = context.user_data.pop(key, None)
        await _safe_delete_chat_message(context, chat_id, mid)


def _has_special_request(value) -> bool:
    """Treat any non-blank, non-placeholder string as a real note from the AI extract."""
    if value is None:
        return False
    s = str(value).strip()
    return bool(s) and s.lower() not in ("-", "—", "–", "none", "n/a", "na")


async def _prompt_issuer_special_request(message, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> int:
    """Notes are optional and already editable from the AI review screen; never
    re-prompt the user for them once phone + price are known. Whatever the AI
    extracted (or didn't) flows straight through to dispatch.
    """
    state = db.get_user_state(user_id)
    if not state or not state.get("data"):
        await message.reply_text("❌ Phase 1 data not found. Please start over with /start")
        return ConversationHandler.END
    state_data = state["data"].copy()
    # Normalize note placeholders so empty values stay clean downstream.
    for key in ("special_request_issuers", "special_request_drivers"):
        val = state_data.get(key)
        if val is not None and not _has_special_request(val):
            state_data[key] = ""
    db.set_user_state(user_id, "special_request_drivers", state_data)
    return await _finalize_lead_after_notes(message, context, user_id, state_data)


async def _ensure_phone_price_before_files(message, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> int:
    """Gate the lead on phone + price before the issuer/driver-note flow.

    Phase 1 used to ask the user to attach files here. Files are now reused
    from the photos/PDFs that were uploaded for AI parsing, so we go straight
    into the special-request notes path instead.
    """
    state = db.get_user_state(user_id)
    if not state or not state.get("data"):
        await message.reply_text("❌ Phase 1 data not found. Please start over with /start")
        return ConversationHandler.END
    state_data = state["data"].copy()
    # Phone is the only hard requirement; a missing price just shows as "-" on
    # the review instead of blocking with a question.
    if _is_valid_pending_phone(state_data.get("pending_phone_number")):
        return await _prompt_issuer_special_request(message, context, user_id)
    context.user_data.pop("phase2_before_files", None)
    db.set_user_state(user_id, "phase1", state_data)
    return await _phase2_ask(message, context, state_data)


async def handle_add_files_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle add_files_yes / add_files_no."""
    query = update.callback_query
    await _safe_answer_callback_query(query)
    user_id = update.effective_user.id
    chat_id = query.message.chat_id if query.message else None
    # We delete the prompt *after* downstream code finishes using `query.message`
    # for replies — deleting it first can make `reply_text` fail silently.
    prompt_msg_id = context.user_data.pop("add_files_prompt_msg_id", None) or (
        query.message.message_id if query.message else None
    )

    if query.data == "add_files_no":
        state = db.get_user_state(user_id)
        if state and state.get("data"):
            data = state["data"]
            if _phase1_has_phone_and_price(data):
                try:
                    return await _submit_lead_from_review(
                        query.message, context, user_id, data
                    )
                finally:
                    await _safe_delete_chat_message(context, chat_id, prompt_msg_id)
        await _safe_delete_chat_message(context, chat_id, prompt_msg_id)
        _ask = _phase2_prompt((state or {}).get("data") or {}) or PHASE2_INTRO_MESSAGE
        if chat_id:
            await context.bot.send_message(chat_id=chat_id, text=_ask)
        else:
            await query.message.reply_text(_ask)
        return STATE_PHASE2

    # add_files_yes
    await _safe_delete_chat_message(context, chat_id, prompt_msg_id)
    if chat_id:
        sent = await context.bot.send_message(
            chat_id=chat_id, text="📎 Send the file (photo or document)."
        )
    else:
        sent = await query.message.reply_text("📎 Send the file (photo or document).")
    if sent is not None:
        context.user_data["send_file_prompt_msg_id"] = sent.message_id
    return STATE_WAITING_FILE


async def handle_add_files_stray_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    In STATE_ADD_FILES only inline buttons were handled, so sending a PDF/photo first
    matched no handler and the bot looked stuck. Accept document/photo as implicit Yes,
    and nudge for plain text.
    """
    msg = update.effective_message
    if not msg:
        return STATE_ADD_FILES
    files = context.user_data.get("phase1_attached_files")
    if files is None:
        files = []
        context.user_data["phase1_attached_files"] = files
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Yes", callback_data="another_file_yes")],
        [InlineKeyboardButton("No", callback_data="another_file_no")],
    ])
    chat_id = msg.chat_id

    if msg.document or msg.photo:
        # User uploaded directly — drop the now-redundant prompt so the chat
        # only shows the user's file + the next confirmation.
        await _delete_pending_file_prompts(context, chat_id)
        if msg.document:
            files.append({"type": "document", "file_id": msg.document.file_id})
        else:
            files.append({"type": "photo", "file_id": msg.photo[-1].file_id})
        sent = await msg.reply_text(
            "✅ File received. Send another if needed, or tap **No** to continue.",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
        if sent is not None:
            context.user_data["another_file_prompt_msg_id"] = sent.message_id
        return STATE_WAITING_FILE

    await msg.reply_text(
        "Please tap **Yes** to attach files (then send your PDF or photo), or **No** to continue without files.",
        parse_mode="Markdown",
    )
    return STATE_ADD_FILES


async def handle_waiting_file_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User sent text instead of file; remind them."""
    await update.message.reply_text(
        "Please send a photo or document to attach. If you're done, tap No on the previous message."
    )
    return STATE_WAITING_FILE


async def handle_file_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle file (photo/document) when in STATE_WAITING_FILE."""
    msg = update.message
    if not msg:
        return STATE_WAITING_FILE
    files = context.user_data.get("phase1_attached_files") or []
    if msg.photo:
        file_id = msg.photo[-1].file_id
        files.append({"type": "photo", "file_id": file_id})
    elif msg.document:
        sz = msg.document.file_size
        if sz is not None and sz > 20 * 1024 * 1024:
            await msg.reply_text(
                "❌ This file is too large for the bot (max ~20 MB). Please send a smaller file."
            )
            return STATE_WAITING_FILE
        files.append({"type": "document", "file_id": msg.document.file_id})
    else:
        await msg.reply_text("Please send a photo or document file.")
        return STATE_WAITING_FILE
    context.user_data["phase1_attached_files"] = files

    # Now that the user has uploaded, drop any leftover "Send the file" /
    # "Send another?" prompt + buttons so only the document remains visible.
    await _delete_pending_file_prompts(context, msg.chat_id)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Yes", callback_data="another_file_yes")],
        [InlineKeyboardButton("No", callback_data="another_file_no")],
    ])
    try:
        sent = await msg.reply_text("Do you want to send another file?", reply_markup=keyboard)
        if sent is not None:
            context.user_data["another_file_prompt_msg_id"] = sent.message_id
    except Exception as e:
        logger.error("handle_file_upload reply failed: %s", e, exc_info=True)
        await msg.reply_text("File saved. Tap Yes/No on the previous keyboard if you still see it, or send /cancel to restart from the top.")
    return STATE_WAITING_FILE


async def handle_another_file_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle another_file_yes / another_file_no."""
    query = update.callback_query
    await _safe_answer_callback_query(query)
    user_id = update.effective_user.id
    chat_id = query.message.chat_id if query.message else None
    # Defer deletion until *after* downstream replies so reply_text on
    # query.message keeps working even though the prompt vanishes visually.
    prompt_msg_id = context.user_data.pop("another_file_prompt_msg_id", None) or (
        query.message.message_id if query.message else None
    )

    if query.data == "another_file_no":
        state = db.get_user_state(user_id)
        if state and state.get("data"):
            d = state["data"].copy()
            d["attached_files"] = context.user_data.get("phase1_attached_files") or []
            db.set_user_state(user_id, "phase1", d)
            if _phase1_has_phone_and_price(d):
                try:
                    return await _submit_lead_from_review(
                        query.message, context, user_id, d
                    )
                finally:
                    await _safe_delete_chat_message(context, chat_id, prompt_msg_id)
            await _safe_delete_chat_message(context, chat_id, prompt_msg_id)
            _ask = _phase2_prompt(d) or PHASE2_INTRO_MESSAGE
            if chat_id:
                await context.bot.send_message(chat_id=chat_id, text=_ask)
            else:
                await query.message.reply_text(_ask)
            return STATE_PHASE2

    await _safe_delete_chat_message(context, chat_id, prompt_msg_id)
    if chat_id:
        sent = await context.bot.send_message(
            chat_id=chat_id, text="📎 Send the file (photo or document)."
        )
    else:
        sent = await query.message.reply_text("📎 Send the file (photo or document).")
    if sent is not None:
        context.user_data["send_file_prompt_msg_id"] = sent.message_id
    return STATE_WAITING_FILE


async def handle_phase1_ai_review_callback(update, context):
    query = update.callback_query
    await _safe_answer_callback_query(query)
    _adopt_review_message(context, query)
    user_id = query.from_user.id
    data = query.data

    state = db.get_user_state(user_id)
    if not state or not state.get("data"):
        await query.message.reply_text("❌ Data lost.")
        return ConversationHandler.END

    state_data = state["data"]

    if data == PH1_REVIEW_ACCEPT:
        context.user_data.pop("phase1_recent_edits", None)
        return await _continue_phase1_after_ai_review(query.message, context, user_id)

    elif data == PH1_REVIEW_VIN_CHECK:
        return await _handle_phase1_vin_check_button(context, query, user_id, state_data)

    elif data == "edit_cancel":
        try:
            await query.message.delete()
        except:
            pass
        chat_id = context.user_data.get("review_chat_id")
        mid = context.user_data.get("review_message_id")
        if chat_id and mid:
            await _edit_message_keyboard(context, chat_id, mid, _phase1_edit_fields_keyboard(state_data))
        return STATE_AI_REVIEW

    elif data == PH1_REVIEW_EDIT:
        context.user_data["phase1_recent_edits"] = []
        await _show_edit_picker(context, state_data)
        return STATE_AI_REVIEW

    elif data in ("ph1_add_image", "ph1_adjust", "ph1_attach"):
        # Single "Add image" action. The old Attach/Adjust callbacks alias here too so any
        # card sent before this change still works. It's just a hint — the issuer can send
        # a photo/PDF any time; EVERY upload is read for fields, kept visible, and included
        # with the dispatch. Vanishing so the review card stays put.
        n = len(context.user_data.get("phase1_extra_attachments") or [])
        chat_id = update.effective_chat.id if update.effective_chat else query.message.chat_id
        await _send_vanishing(
            context, chat_id,
            "🖼 Send a photo or PDF — title, license, anything. I'll read it, keep it, and "
            "include it with the dispatch to the team.\n\n"
            + (f"({n} already added.) " if n else "")
            + "Send as many as you like.",
            delay=10,
        )
        return STATE_AI_REVIEW

    elif data == "adjust_cancel":
        try:
            await query.message.delete()
        except Exception:
            pass
        context.user_data.pop("adjust_prompt_msg_id", None)
        return STATE_AI_REVIEW

    elif data == "ph1_ins_toggle":
        state_data["wants_insurance"] = not state_data.get("wants_insurance")
        db.set_user_state(user_id, "phase1", state_data)
        # Just flip the button in place ("🛡 Add insurance" ⇄ "🛡 Insurance: ON").
        # No separate on/off message — keep the review card clean and visible.
        await _update_review_message_text(context, state_data)
        return STATE_AI_REVIEW

    elif (data == PH1_ADD_CAR_CB
          or data.startswith(PH1_CAR_MENU_CB)
          or data.startswith(PH1_CAR_REMOVE_CB)):
        handled = await _handle_vehicle_menu_action(update, context, data, state_data)
        return handled if handled is not None else STATE_AI_REVIEW

    elif data.startswith("ph1edit_"):
        edit_key = data.replace("ph1edit_", "", 1)
        # Switching away from a field mid-edit: take its prompt down first.
        await _close_open_field_prompt(context, query.message.chat_id if query.message else None)
        context.user_data["phase1_pending_edit_key"] = edit_key
        label = _edit_prompt_label(edit_key)
        if _edit_key_base(edit_key) == "col":
            # Colour gets the tap-to-pick palette; typing, a voice note and a photo
            # of the car all still work while this prompt is open.
            prompt = await query.message.reply_text(
                _PH1_COLOR_PROMPT, reply_markup=_color_picker_keyboard()
            )
        elif edit_key == "price":
            prompt = await query.message.reply_text(
                _PH1_PRICE_PROMPT,
                reply_markup=_fresh_price_picker(context, state_data),
            )
        else:
            prompt = await query.message.reply_text(
                f"✏️ Send new text for: {label}\n\n"
                "Type minus (-) to clear.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Cancel", callback_data="edit_cancel")
                ]])
            )
        context.user_data["edit_prompt_msg_id"] = prompt.message_id
        return STATE_EDIT_FIELD_PROMPT

    elif data == PH1_EDIT_BACK:
        await _update_review_message_text(context, state_data)
        return STATE_AI_REVIEW

    elif data == "ph1_pick_group":
        groups = db.get_all_groups()
        active = [g for g in groups if record_is_active(g)]
        if not active:
            if query.message:
                await query.message.reply_text("⚠️ No active dispatchers configured.")
            return STATE_AI_REVIEW
        buttons = [[InlineKeyboardButton(g.get("group_name", str(g["id"])), callback_data=f"selgrp_{g['id']}")] for g in active]
        buttons.append([InlineKeyboardButton("📢 Send to All Dispatchers", callback_data="selgrp_all")])
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="ph1_sel_back")])
        await _edit_message_keyboard(
            context,
            context.user_data["review_chat_id"],
            context.user_data["review_message_id"],
            InlineKeyboardMarkup(buttons)
        )
        return STATE_AI_REVIEW

    elif data == "ph1_pick_driver":
        chat_id = context.user_data.get("review_chat_id")
        mid = context.user_data.get("review_message_id")
        if not chat_id or not mid:
            return STATE_AI_REVIEW
        all_drivers = _get_all_drivers_cached()
        active = [d for d in all_drivers if record_is_active(d)]
        suspended = _get_suspended_driver_ids()
        buttons = []
        for d in active:
            did = d.get("id")
            name = d.get("driver_name", "Unknown")
            if str(did) in suspended:
                buttons.append([InlineKeyboardButton(f"🚫 {name} (PENALTY)", callback_data=f"driver_suspended_{did}")])
            else:
                buttons.append([InlineKeyboardButton(f"🚗 {name}", callback_data=f"seldrv_{did}")])
        elig = [d for d in active if str(d.get("id")) not in suspended]
        if elig:
            buttons.append([InlineKeyboardButton("📢 Send to All Drivers", callback_data="seldrv_all")])
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="ph1_sel_back")])
        await _edit_message_keyboard(context, chat_id, mid, InlineKeyboardMarkup(buttons))
        return STATE_AI_REVIEW

    elif data == "ph1_pick_source":
        sources = db.get_contact_info_sources()
        if not sources:
            return STATE_AI_REVIEW
        buttons = [[InlineKeyboardButton(s.get("label", str(s["id"])), callback_data=f"selsrc_{s['id']}")] for s in sources]
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="ph1_sel_back")])
        await _edit_message_keyboard(
            context,
            context.user_data["review_chat_id"],
            context.user_data["review_message_id"],
            InlineKeyboardMarkup(buttons)
        )
        return STATE_AI_REVIEW

    elif data == "ph1_sel_back":
        state = db.get_user_state(user_id)
        if state and state.get("data"):
            await _update_review_message_text(context, state_data)
        return STATE_AI_REVIEW

    elif data.startswith("driver_suspended_"):
        driver_id = data.replace("driver_suspended_", "")
        driver = next((d for d in _get_all_drivers_cached() if str(d.get("id")) == driver_id), None)
        name = driver.get("driver_name", "Driver") if driver else "Driver"
        if query.message:
            await query.message.reply_text(f"🚫 {name} is suspended (PENALTY).")
        return STATE_AI_REVIEW

    elif data == "selgrp_all" or data.startswith("selgrp_"):
        if data == "selgrp_all":
            state_data["selected_group_id"] = "all"
            state_data["selected_group_name"] = "All Dispatchers"
            db.set_user_state(user_id, "phase1", state_data)
            await _update_review_message_text(context, state_data)
            return STATE_AI_REVIEW
        group_id = data.replace("selgrp_", "")
        group = db.get_group_by_id(group_id)
        if group:
            state_data["selected_group_id"] = group_id
            state_data["selected_group_name"] = group.get("group_name", "?")
            db.set_user_state(user_id, "phase1", state_data)
            await _update_review_message_text(context, state_data)
        return STATE_AI_REVIEW

    elif data.startswith("seldrv_") or data == "seldrv_all":
        drivers = _get_all_drivers_cached()
        active = [d for d in drivers if record_is_active(d)]
        suspended = _get_suspended_driver_ids()
        if data == "seldrv_all":
            selected = [d for d in active if str(d["id"]) not in suspended]
            names = "All Drivers"
            ids = [d["id"] for d in selected]
        else:
            driver_id = data.replace("seldrv_", "")
            d = next((d for d in active if str(d.get("id")) == driver_id), None)
            if not d:
                return STATE_AI_REVIEW
            selected = [d]
            names = d.get("driver_name", "?")
            ids = [d["id"]]
        state_data["selected_driver_ids"] = ids
        state_data["selected_driver_names"] = names
        db.set_user_state(user_id, "phase1", state_data)
        await _update_review_message_text(context, state_data)
        return STATE_AI_REVIEW

    elif data.startswith("selsrc_"):
        source_id = data.replace("selsrc_", "")
        source = db.get_contact_info_source_by_id(source_id)
        label = source.get("label", "") if source else ""
        state_data["selected_source_label"] = label
        db.set_user_state(user_id, "phase1", state_data)
        await _update_review_message_text(context, state_data)
        return STATE_AI_REVIEW

    return STATE_AI_REVIEW


async def handle_phase1_edit_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Choose field to edit or go back to summary."""
    query = update.callback_query
    await _safe_answer_callback_query(query)
    _adopt_review_message(context, query)
    user_id = query.from_user.id
    if query.data == PH1_REVIEW_ACCEPT:
        state = db.get_user_state(user_id)
        if not state or not state.get("data"):
            await query.message.reply_text("❌ Lead data not found. Please start over with /start")
            return ConversationHandler.END
        context.user_data.pop("phase1_recent_edits", None)
        return await _continue_phase1_after_ai_review(query.message, context, user_id)
    if query.data == PH1_EDIT_BACK:
        state = db.get_user_state(user_id)
        if not state or not state.get("data"):
            await query.message.reply_text("❌ Lead data not found. Please start over with /start")
            return ConversationHandler.END
        context.user_data.pop("phase1_recent_edits", None)
        # Restore the review keyboard with current selections
        await _edit_message_keyboard(
            context,
            context.user_data.get("review_chat_id"),
            context.user_data.get("review_message_id"),
            _build_review_keyboard_with_selections(state["data"]),
        )
        return STATE_AI_REVIEW
    if (query.data == PH1_ADD_CAR_CB
            or query.data.startswith(PH1_CAR_MENU_CB)
            or query.data.startswith(PH1_CAR_REMOVE_CB)):
        # This state runs its own handler FIRST, so the extra-car buttons have to
        # be answered here too — not only on the review card.
        state = db.get_user_state(user_id)
        if not state or state.get("data") is None:
            await query.message.reply_text("❌ Lead data not found. Please start over with /start")
            return ConversationHandler.END
        handled = await _handle_vehicle_menu_action(update, context, query.data, state["data"])
        return handled if handled is not None else STATE_AI_EDIT_MENU
    if not query.data.startswith("ph1edit_"):
        return STATE_AI_EDIT_MENU
    edit_key = query.data.replace("ph1edit_", "", 1)
    if not _is_known_edit_key(edit_key):
        return STATE_AI_EDIT_MENU
    await _close_open_field_prompt(context, query.message.chat_id if query.message else None)
    context.user_data["phase1_pending_edit_key"] = edit_key
    label = _edit_prompt_label(edit_key)
    if _edit_key_base(edit_key) == "col":
        await query.message.reply_text(_PH1_COLOR_PROMPT, reply_markup=_color_picker_keyboard())
    elif edit_key == "price":
        card = (db.get_user_state(user_id) or {}).get("data") or {}
        await query.message.reply_text(
            _PH1_PRICE_PROMPT, reply_markup=_fresh_price_picker(context, card))
    else:
        await query.message.reply_text(
            f"✏️ Send new text for: {label}\n\n"
            "Type minus (-) to clear that field.",
        )
    return STATE_AI_EDIT_INPUT


async def handle_phase1_edit_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Apply new text for the selected Phase 1 field."""
    user_id = update.effective_user.id
    ek = context.user_data.get("phase1_pending_edit_key")
    if not ek:
        # Same as above: the text is an edit even when we have forgotten which
        # button opened the prompt. Telling them to use the buttons loses the value
        # they just typed.
        return await handle_phase1_review_message(update, context)
    text = (update.message.text or "").strip()
    state = db.get_user_state(user_id)
    if not state or not state.get("data"):
        context.user_data.pop("phase1_pending_edit_key", None)
        await update.message.reply_text("❌ Lead data not found. Please start over with /start")
        return ConversationHandler.END
    state_data = state["data"].copy()
    redirected = await _selection_command_at_a_field_prompt(
        update, context, user_id, state_data, text, ek)
    if redirected is not None:
        return redirected
    placed = []
    if text == "-":
        _apply_single_phase1_edit(state_data, ek, "")
    else:
        handled, placed = await _place_text_at_field_prompt(state_data, ek, text)
        if not handled:
            await update.message.reply_text(
                f"⚠️ I couldn't read that as a {PH1_EDIT_PROMPT_LABEL.get(ek, ek)}. "
                "Try again, or name the field — 'color black', 'price 150'.",
            )
            return STATE_AI_EDIT_INPUT
    _apply_single_address_as_both(state_data)
    _clean_vin_and_car(state_data)
    _sanitize_phase1_pending_phone_price(state_data)
    db.set_user_state(user_id, "phase1", state_data)
    context.user_data.pop("phase1_pending_edit_key", None)
    # Report where it actually landed — it may not be the field that was open.
    label = ", ".join(placed) if placed else PH1_EDIT_PROMPT_LABEL.get(ek, ek)
    preview = _preview_value_after_phase1_edit(state_data, ek)
    context.user_data.setdefault("phase1_recent_edits", [])
    re_list = context.user_data["phase1_recent_edits"]
    re_list.append({"label": label, "value": preview})
    if len(re_list) > 15:
        context.user_data["phase1_recent_edits"] = re_list[-15:]
    await update.message.reply_text(
        f"✅ Updated: {label}.\n\n"
        "Need another Edit, or Done with edits?",
        reply_markup=_phase1_after_edit_keyboard(),
    )
    return STATE_AI_EDIT_INPUT


async def handle_phase1_edit_followup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """After an edit: more fields, final review + confirm, or run VIN / files flow."""
    query = update.callback_query
    await _safe_answer_callback_query(query)
    user_id = query.from_user.id
    if query.data == PH1_EDIT_MORE:
        state = db.get_user_state(user_id)
        if not state or not state.get("data"):
            await query.message.reply_text("❌ Lead data not found. Please start over with /start")
            return ConversationHandler.END
        await query.message.reply_text(
            "Pick a field to edit:",
            reply_markup=_phase1_edit_fields_keyboard(state["data"]),
        )
        return STATE_AI_EDIT_MENU
    if query.data == PH1_FINAL_CONFIRM:
        context.user_data.pop("phase1_recent_edits", None)
        return await _continue_phase1_after_ai_review(query.message, context, user_id)
    if query.data == PH1_EDIT_DONE:
        state = db.get_user_state(user_id)
        if not state or not state.get("data"):
            await query.message.reply_text("❌ Lead data not found. Please start over with /start")
            return ConversationHandler.END
        recent = context.user_data.get("phase1_recent_edits") or []
        await query.message.reply_text(
            _format_phase1_final_review_text(state["data"], recent),
            reply_markup=_phase1_final_confirm_keyboard(),
        )
        return STATE_AI_EDIT_INPUT
    return STATE_AI_EDIT_INPUT


# Maps API field names to state_data keys (e.g. delivery_date -> extra_info)
MISSING_FIELD_TO_STATE_KEY = {"delivery_date": "extra_info"}


# state-key -> inline edit-key, so a missing-field answer goes through the same
# parser as every other prompt.
_STATE_KEY_TO_INLINE_EK = {v: k for k, v in _INLINE_EK_STATE_KEY.items()}


def _field_already_filled(state_data: dict, field: str) -> bool:
    """True when the card already answers this question. Asked before re-prompting,
    because the card stays editable while the question is on screen."""
    key = MISSING_FIELD_TO_STATE_KEY.get(field, field)
    val = str((state_data or {}).get(key) or "").strip()
    if not val or val == "-":
        return False
    if field == "color":
        return ai_vision._has_valid_color(val)
    return True


async def _ask_next_missing(message, context, user_id: int, fields: list, state_data: dict):
    """Ask for the first thing still genuinely absent, or return None if nothing is.

    The queue is re-checked against the LIVE card here rather than against whatever
    dict the caller is holding: a value can arrive while the queue waits — typed on
    the card, tapped from a picker — and being asked for a colour that is printed
    right there is the complaint this exists to prevent."""
    try:
        live = (db.get_user_state(user_id) or {}).get("data")
    except Exception:
        live = None
    card = live if isinstance(live, dict) else (state_data or {})
    still = [f for f in (fields or [])
             if f not in PHASE1_OPTIONAL_FIELDS and not _field_already_filled(card, f)]
    context.user_data["missing_fields"] = still
    context.user_data["missing_field_state_data"] = dict(card)
    # Persist the live card before the question goes up. handle_missing_field
    # answers from the stashed copy and saves it, so without this anything the
    # operator changed WHILE the question was on screen is silently reverted —
    # ask for a colour, get "price 150" typed on the card, answer the colour, and
    # the price is gone.
    if live is not card or card is not state_data:
        try:
            db.set_user_state(user_id, "phase1", card)
        except Exception as e:
            logger.warning("could not persist the card before asking: %s", e)
    if not still:
        return None
    prompts = ai_vision.MISSING_FIELD_PROMPTS
    text = prompts.get(still[0], (f"You missed out {still[0]}. Can you add it?", still[0]))[0]
    sent = await message.reply_text(text)
    _track_missing_prompt(context, sent)
    return STATE_MISSING_FIELD


async def handle_missing_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle user reply when we asked for a missing field (e.g. color)."""
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()
    if not text:
        sent = await update.message.reply_text("Please send the missing value.")
        _track_missing_prompt(context, sent)
        return STATE_MISSING_FIELD
    _cr = _cancel_restart_kind(text, strict=True)
    if _cr:
        return await _do_cancel_or_restart(update, context, _cr)
    await _autoclean_user_msg(update, context)
    missing_fields = context.user_data.get("missing_fields") or []
    # The LIVE card, not the snapshot taken when the question was asked — the card
    # stays editable while this question is open, and answering used to save the
    # snapshot back over anything changed in between.
    live = db.get_user_state(user_id)
    state_data = (live or {}).get("data")
    if state_data is None:
        state_data = context.user_data.get("missing_field_state_data") or {}
    field = missing_fields[0] if missing_fields else "color"
    state_key = MISSING_FIELD_TO_STATE_KEY.get(field, field)
    # Parse the answer the way every other prompt does, so "color white" stores
    # White (and "price 150" said here still reaches the price).
    redirected = await _selection_command_at_a_field_prompt(
        update, context, user_id, state_data, text)
    if redirected is not None:
        return redirected
    ek = _STATE_KEY_TO_INLINE_EK.get(state_key)
    placed = False
    if ek:
        placed, _ = await _place_text_at_field_prompt(state_data, ek, text)
    if not placed:
        # A command-shaped answer is not a value. Without this, "cancel" or
        # "driver Susan" typed at "You missed out the vehicle color" became the
        # COLOUR, and it prints on the tag.
        if _COMMAND_LIKE_RE.search(text):
            sent = await update.message.reply_text(
                f"That looked like a command, so I did not store it as the "
                f"{field.replace('_', ' ')}. Send just the value, or tap ✏️ Edit."
            )
            _track_missing_prompt(context, sent)
            return STATE_MISSING_FIELD
        state_data[state_key] = text
    missing_fields = missing_fields[1:]
    # Whatever else the card gained in the meantime is no longer missing.
    missing_fields = [f for f in missing_fields
                      if not _field_already_filled(state_data, f)]
    context.user_data["missing_fields"] = missing_fields
    context.user_data["missing_field_state_data"] = state_data
    # The question (and the answer) disappear — the value lands on the review.
    await _clear_missing_prompts(context)
    try:
        await update.message.delete()
    except Exception:
        pass
    asked = await _ask_next_missing(
        update.message, context, user_id, missing_fields, state_data)
    if asked is not None:
        return asked

    # Safety net: re-scan in case initial detection short-circuited on one field
    # (e.g. early return on color) and missed insurance/VIN/etc.
    blob = "\n".join(
        str(state_data.get(k) or "")
        for k in (
            "name",
            "address",
            "city_state_zip",
            "delivery_address",
            "delivery_city_state_zip",
            "vin",
            "car",
            "color",
            "insurance_company",
            "insurance_policy_number",
            "extra_info",
        )
    )
    still_missing = ai_vision.detect_missing_fields(state_data, blob)
    still_missing = [f for f in still_missing if f not in PHASE1_OPTIONAL_FIELDS
                     and not _field_already_filled(state_data, f)]
    asked = await _ask_next_missing(
        update.message, context, user_id, still_missing, state_data)
    if asked is not None:
        return asked

    _sanitize_phase1_pending_phone_price(state_data)
    db.set_user_state(user_id, "phase1", state_data)
    context.user_data.pop("missing_fields", None)
    context.user_data.pop("missing_field_state_data", None)
    # VIN decode is opt-in via the review screen's "🔍 Check VIN" button.
    return await _ensure_phone_price_before_files(update.message, context, user_id)


async def _apply_vin_choice(context, reply_msg, chat_id, user_id, choice: str) -> int:
    """Apply a VIN-conflict decision. choice ∈ {use, keep, retype}. Shared by the
    inline buttons and the voice/text path. reply_msg = a Message to reply on."""
    if choice == "retype":
        # The "choose which to use" card is now stale — drop it, then show (and track)
        # the retype prompt so it too gets wiped once the new VIN is captured.
        await _clear_vin_flow_msgs(context)
        prompt = await reply_msg.reply_text("Please type the correct VIN (17 characters):")
        _track_vin_flow_msg(context, prompt)
        return STATE_VIN_RETYPE
    api_car = context.user_data.get("vin_choice_api_car")
    if choice == "use":
        if not api_car:
            await reply_msg.reply_text("Run the VIN check first — say “run vin”.")
            return STATE_AI_REVIEW
        state = db.get_user_state(user_id)
        if state and state.get("data"):
            d = state["data"]
            d["car"] = api_car
            vehicle_lines = [
                d.get("name"), d.get("address"), d.get("city_state_zip"),
                d.get("vin"), d.get("car"), d.get("color"),
                d.get("insurance_company"), d.get("insurance_policy_number"), d.get("extra_info"),
            ]
            d["vehicle_details"] = "\n".join([l for l in vehicle_lines if l])
            db.set_user_state(user_id, "phase1", d)
    context.user_data.pop("vin_choice_api_car", None)
    context.user_data.pop("vin_choice_stated_car", None)
    # Wipe the whole VIN exchange (lookup card + any retype prompts/repeat cards).
    await _clear_vin_flow_msgs(context)
    # Return to the AI review screen — Submit is the only path forward.
    state = db.get_user_state(user_id)
    if state and state.get("data"):
        await _update_review_message_text(context, state["data"])
    return STATE_AI_REVIEW


async def handle_vin_choice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle VIN conflict choice button: use API result, keep stated car, or retype."""
    query = update.callback_query
    await query.answer()
    _adopt_review_message(context, query)
    choice = {"vin_use": "use", "vin_keep": "keep", "vin_retype": "retype"}.get(query.data, "keep")
    return await _apply_vin_choice(context, query.message, update.effective_chat.id, update.effective_user.id, choice)


async def handle_vin_choice_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """STATE_VIN_CHOICE: answer the VIN conflict by voice/text ('use the new',
    'keep the same', 'retype vin') instead of tapping a button."""
    text = ((update.message.text if update.message else "") or "").strip()
    _cr = _cancel_restart_kind(text, strict=True)
    if _cr:
        return await _do_cancel_or_restart(update, context, _cr)
    await _autoclean_user_msg(update, context)
    # The question on screen is "Would you like to use DMV system?", so plain yes/no
    # answers it — checked before the older phrasing, which stays supported.
    if _YES_RE.match(text):
        kind = "VIN_USE"
    elif _NO_RE.match(text):
        kind = "VIN_KEEP"
    else:
        kind, _ = _classify_review_command(text)
    mapping = {"VIN_USE": "use", "VIN_KEEP": "keep", "VIN_RETYPE": "retype"}
    if kind in mapping:
        result = await _apply_vin_choice(
            context, update.message, update.effective_chat.id if update.effective_chat else None,
            update.effective_user.id, mapping[kind],
        )
        try:
            await update.message.delete()
        except Exception:
            pass
        await _cleanup_voice_echo(context, update.effective_chat.id if update.effective_chat else None)
        return result
    # Not an answer to the VIN question — so it is an ordinary edit. The DMV check is
    # OPTIONAL: it must never block "price 150" or any other change. Apply it and
    # leave the question on screen, still answerable by button or word.
    result = await handle_phase1_review_message(update, context)
    return STATE_VIN_CHOICE if result == STATE_AI_REVIEW else result


async def handle_vin_retype(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle user's new VIN input; re-run lookup and either show choice again or return to review."""
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()
    vin_new = _extract_vin_17(text)
    if not vin_new or len(vin_new) != 17:
        await _autoclean_user_msg(update, context)  # drop the bad attempt
        warn = await update.message.reply_text(
            "Please send a valid 17-character VIN (letters and numbers only)."
        )
        _track_vin_flow_msg(context, warn)  # cleared once a good VIN lands
        return STATE_VIN_RETYPE
    await _autoclean_user_msg(update, context)  # valid VIN captured — clear the input
    state = db.get_user_state(user_id)
    if not state or not state.get("data"):
        await update.message.reply_text("❌ Error: Phase 1 data not found. Please start over with /start")
        return ConversationHandler.END
    state_data = state["data"].copy()
    state_data["vin"] = vin_new
    _clean_vin_and_car(state_data)
    db.set_user_state(user_id, "phase1", state_data)
    alert_msg, conflict = _vin_check_after_phase1(state_data)
    if conflict:
        api_car, stated_car = conflict
        context.user_data["vin_choice_api_car"] = api_car
        context.user_data["vin_choice_stated_car"] = stated_car
        # Wipe the prior prompt/card before showing the fresh conflict card.
        await _clear_vin_flow_msgs(context)
        vin_msg = await update.message.reply_text(
            _vin_conflict_body(stated_car, api_car),
            reply_markup=_vin_choice_keyboard(api_car, stated_car),
        )
        context.user_data["vin_conflict_msg_id"] = vin_msg.message_id
        _track_vin_flow_msg(context, vin_msg)
        return STATE_VIN_CHOICE
    # VIN resolved — wipe every VIN message so only the review card remains.
    await _clear_vin_flow_msgs(context)
    if alert_msg:
        chat_id = update.effective_chat.id if update.effective_chat else update.message.chat_id
        await _send_vanishing(context, chat_id, alert_msg)
    await _update_review_message_text(context, state_data)
    return STATE_AI_REVIEW


async def _run_vin_check_for_review(update, context: ContextTypes.DEFAULT_TYPE,
                                    user_id: int, state_data: dict) -> int:
    """Run the DMV decode automatically after a VIN arrives from a picture or PDF.

    Silent no-op unless it can actually succeed — a 17-character VIN and a configured
    lookup — so an upload never produces a stray "not configured" warning."""
    vin = (state_data.get("vin") or "").strip()
    if len(vin) != 17:
        return STATE_AI_REVIEW
    try:
        if not Config.is_vin_lookup_configured():
            return STATE_AI_REVIEW
        return await _handle_phase1_vin_check_button(context, update, user_id, state_data)
    except Exception as e:
        logger.warning("auto VIN check failed: %s", e)
        return STATE_AI_REVIEW


async def _handle_phase1_vin_check_button(
    context: ContextTypes.DEFAULT_TYPE, query, user_id: int, state_data: dict,
) -> int:
    """Run the DMV VIN decode on demand from the review screen.

    Asks "Would you like to use DMV system?" with Yes/No, the same prompt the
    auto-decode flow uses; after the user picks one, we return to the AI review
    rather than continuing to phone/notes — Submit is the only path forward.
    """
    vin = (state_data.get("vin") or "").strip()
    if not vin or vin == "-" or len(vin) != 17:
        await query.message.reply_text(
            "⚠️ VIN is not 17 characters; cannot run DMV check. Edit the VIN and try again."
        )
        return STATE_AI_REVIEW
    if not Config.is_vin_lookup_configured():
        await query.message.reply_text("⚠️ VIN lookup is not configured.")
        return STATE_AI_REVIEW
    result = vin_lookup.vin_lookup(
        vin,
        provider=Config.VIN_PROVIDER,
        api_key=Config.API_NINJAS_API_KEY,
    )
    if not result:
        await query.message.reply_text(
            "⚠️ VIN returned no result. Ensure it's 17 characters and try again."
        )
        return STATE_AI_REVIEW
    api_car = (result.get("car_line") or "").strip()
    if not api_car:
        await query.message.reply_text("⚠️ DMV did not return a car for this VIN.")
        return STATE_AI_REVIEW
    stated = (state_data.get("car") or "").strip()
    context.user_data["vin_choice_api_car"] = api_car
    context.user_data["vin_choice_stated_car"] = stated
    vin_msg = await query.message.reply_text(
        _vin_conflict_body(stated, api_car),
        reply_markup=_vin_choice_keyboard(api_car, stated),
    )
    context.user_data["vin_conflict_msg_id"] = vin_msg.message_id
    _track_vin_flow_msg(context, vin_msg)
    return STATE_VIN_CHOICE


async def handle_phase2(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle Phase 2: Phone number and price, then issuer note, then driver-only note, then encrypt."""
    user_id = update.effective_user.id
    msg = update.effective_message
    if not msg:
        return STATE_PHASE2
    # Text message OR caption on photo/document (avoids silent no-op when user sends media + caption)
    message_text = ((msg.text or msg.caption) or "").strip()
    if not message_text:
        if msg.photo or msg.document or getattr(msg, "video", None) or getattr(msg, "voice", None):
            await msg.reply_text(
                "⚠️ Add phone and price in the **caption**, or send a **plain text** message.\n\n"
                + PHASE2_INTRO_MESSAGE,
                parse_mode="Markdown",
            )
        else:
            await msg.reply_text(
                "❌ Please send your phone number and price as text.\n\n" + PHASE2_INTRO_MESSAGE
            )
        return STATE_PHASE2
    _cr = _cancel_restart_kind(message_text)
    if _cr:
        return await _do_cancel_or_restart(update, context, _cr)
    await _autoclean_user_msg(update, context)

    # Get phase 1 data
    state = db.get_user_state(user_id)
    # `is None`, not falsy: an EMPTY card is a real state and ending the lead over
    # it is the same silent-drop shape that once ate typed edits.
    if not state or state.get("data") is None:
        await msg.reply_text("❌ Error: Phase 1 data not found. Please start over with /start")
        return ConversationHandler.END

    phase1_data = state.get("data", {})

    # Only ever ask for what is absent: if the phone already came in with the lead,
    # the reply here is just the price (and vice versa). Requiring both meant a good
    # value had to be re-typed.
    needs_phone, needs_price = _phase2_missing(phase1_data)

    # Parse phone and price: any token containing $ is the price; phone = any format accepted (digits only)
    parts = message_text.split()
    price = next((p for p in parts if "$" in p), None)
    # Build text without price so we don't take digits from "$500" etc.
    non_price_text = " ".join(p for p in parts if "$" not in p)
    digits_only = re.sub(r"\D", "", non_price_text)
    if len(digits_only) == 11 and digits_only.startswith("1"):
        digits_only = digits_only[1:]
    # NOTE: Do NOT strip leading "1" from 10-digit numbers.
    # Example: "+1234567890" is already a valid 10-digit number (area code can start with 1),
    # and stripping would corrupt it and break encryption/unlock downstream.
    phone_number = "+1" + digits_only if len(digits_only) in (9, 10) else None
    # When the PRICE is the only thing outstanding, a bare number is the price — no
    # "$" needed. Capped at six digits so a mistakenly pasted phone number is not
    # accepted as a price.
    if needs_price and not price:
        # No "$" needed. Capped at six digits so a pasted phone is never taken as a
        # price — which also makes this safe when BOTH are still missing: a number
        # that short cannot be the phone we are also waiting for.
        bare = re.sub(r"[^\d.]", "", message_text)
        if bare and len(re.sub(r"\D", "", bare)) <= 6 and not phone_number:
            price = _clean_inline_value("price", bare) or None

    state_data = phase1_data.copy()
    if phone_number and needs_phone:
        state_data["pending_phone_number"] = phone_number
    if price and needs_price:
        state_data["pending_price"] = price
    still_phone, still_price = _phase2_missing(state_data)
    if still_phone or still_price:
        db.set_user_state(user_id, "phase1", state_data)
        return await _phase2_ask(msg, context, state_data)
    context.user_data.pop("phase2_before_files", None)
    # Notes are optional and editable from the AI review screen — never block
    # dispatch on them. Save state then jump straight into finalize/review.
    db.set_user_state(user_id, "special_request_drivers", state_data)
    return await _prompt_issuer_special_request(msg, context, user_id)


async def handle_special_request_issuers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Save note for group/issuers, then ask for driver-only note (still before encrypt)."""
    user_id = update.effective_user.id
    state = db.get_user_state(user_id)
    if not state or not state.get("data"):
        await update.message.reply_text("❌ Error: Phase 1 data not found. Please start over with /start")
        return ConversationHandler.END

    state_data = state["data"].copy()
    if not _phase1_has_phone_and_price(state_data):
        db.set_user_state(user_id, "phase1", state_data)
        return await _phase2_ask(update.message, context, state_data)

    raw = (update.message.text or "").strip()
    _cr = _cancel_restart_kind(raw, strict=True)
    if _cr:
        return await _do_cancel_or_restart(update, context, _cr)
    await _autoclean_user_msg(update, context)
    skip_tokens = frozenset(("-", "—", "–", "none", "n/a", "na"))
    issuers_note = "" if not raw or raw.lower() in skip_tokens else raw
    state_data["special_request_issuers"] = issuers_note
    if _has_special_request(state_data.get("special_request_drivers")):
        db.set_user_state(user_id, "special_request_drivers", state_data)
        return await _finalize_lead_after_notes(update.message, context, user_id, state_data)
    db.set_user_state(user_id, "special_request_drivers", state_data)
    await update.message.reply_text(
        "📝 Would you like to say any Special Requests to the delivery drivers? (optional)"
    )
    return STATE_SPECIAL_REQUEST_DRIVERS


async def handle_special_request_drivers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """After issuer + driver notes: encrypt phone and continue to group/driver selection."""
    user_id = update.effective_user.id
    state = db.get_user_state(user_id)
    if not state or not state.get("data"):
        await update.message.reply_text("❌ Error: Phase 1 data not found. Please start over with /start")
        return ConversationHandler.END

    state_data = state["data"].copy()
    if not _phase1_has_phone_and_price(state_data):
        db.set_user_state(user_id, "phase1", state_data)
        return await _phase2_ask(update.message, context, state_data)

    raw_d = (update.message.text or "").strip()
    _cr = _cancel_restart_kind(raw_d, strict=True)
    if _cr:
        return await _do_cancel_or_restart(update, context, _cr)
    await _autoclean_user_msg(update, context)
    skip_tokens = frozenset(("-", "—", "–", "none", "n/a", "na"))
    drivers_note = "" if not raw_d or raw_d.lower() in skip_tokens else raw_d
    state_data["special_request_drivers"] = drivers_note
    return await _finalize_lead_after_notes(update.message, context, user_id, state_data)


async def _finalize_lead_after_notes(
    message,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    state_data: dict,
) -> int:
    """Encrypt phone, persist lead, post group approval, and continue dispatch.

    Reusable so notes flows can skip prompts when the AI already extracted them.
    """
    msg_user = getattr(message, "from_user", None) if message else None
    username = (
        (state_data.get("username") if state_data.get("username") not in (None, "", "Unknown") else None)
        or (msg_user.username if msg_user and msg_user.username else None)
        or "Unknown"
    )

    if not _phase1_has_phone_and_price(state_data):
        db.set_user_state(user_id, "phase1", state_data)
        return await _phase2_ask(message, context, state_data)

    phone_number = state_data.pop("pending_phone_number", None)
    price = state_data.pop("pending_price", None)
    issuers_note = (state_data.get("special_request_issuers") or "").strip()
    drivers_note = (state_data.get("special_request_drivers") or "").strip()

    encrypted_data = await asyncio.to_thread(ots.encrypt_phone, phone_number)
    if not encrypted_data:
        state_data["pending_phone_number"] = phone_number
        state_data["pending_price"] = price
        db.set_user_state(user_id, "special_request_drivers", state_data)
        reason = (getattr(ots, "last_error", "") or "").strip()
        if reason:
            await message.reply_text(
                "❌ Error encrypting phone number.\n\n"
                f"Reason: {reason}\n\n"
                "If this keeps happening, the `clientsphonenumber` service is usually missing env vars "
                "(SUPABASE_URL/SUPABASE_KEY and ONETIMESECRET_USERNAME/ONETIMESECRET_API_KEY) on Vercel "
                "or the bot has wrong credentials.\n\n"
                "Send your reply again when ready (or **-** for none).",
                parse_mode="Markdown",
            )
        else:
            await message.reply_text(
                "❌ Error encrypting phone number. Please try again.\n\n"
                "Send your reply again (or **-** for none).",
                parse_mode="Markdown",
            )
        return STATE_SPECIAL_REQUEST_DRIVERS

    reference_id = generate_reference_id()
    state_data["special_request_issuers"] = issuers_note
    state_data["special_request_drivers"] = drivers_note
    state_data["special_request_note"] = issuers_note
    state_data["phone_number"] = phone_number
    state_data["price"] = price
    state_data["encrypted_data"] = encrypted_data
    state_data["reference_id"] = reference_id
    state_data["username"] = username

    groups = db.get_all_groups()
    active_groups = [g for g in groups if record_is_active(g)]
    if not active_groups:
        await message.reply_text("❌ Error: No active groups configured. Please contact admin.")
        return ConversationHandler.END

    preferred_group_id = str(state_data.get("selected_group_id") or "").strip()
    assistants_choose_group = (db.get_setting("assistants_choose_group") or "").lower() in ("true", "1", "yes")
    if assistants_choose_group and not preferred_group_id:
        db.set_user_state(user_id, "select_group", state_data)
        group_keyboard = _build_group_keyboard(active_groups, include_all=True)
        await message.reply_text(
            "✅ Ready.\n\n**Select which group to send this lead to:**",
            parse_mode="Markdown",
            reply_markup=group_keyboard,
        )
        return STATE_SELECT_GROUP

    is_all_groups = preferred_group_id == "all"
    selected_group = None
    if not is_all_groups and preferred_group_id:
        picked = db.get_group_by_id(preferred_group_id)
        if picked and record_is_active(picked):
            selected_group = picked

    if not selected_group:
        user_telegram_id = str(user_id)
        assistant_group = db.get_group_by_assistant_telegram_id(user_telegram_id)
        if assistant_group and record_is_active(assistant_group):
            selected_group = assistant_group
            logger.info(
                "User is assistant for group '%s'; using that group for lead",
                selected_group.get("group_name"),
            )
        else:
            selected_group = active_groups[0]
    group_id = selected_group["id"]
    logger.info(
        "Finalize flow routing lead via group '%s' (id=%s, is_all_groups=%s)",
        selected_group.get("group_name"),
        group_id,
        is_all_groups,
    )

    phase1_data = {k: v for k, v in state_data.items() if k not in _PHASE1_STATE_EXCLUDE}
    phase1_data["vehicle_details"] = "\n".join([
    phase1_data.get("name", "-"),
    phase1_data.get("address", "-"),
    phase1_data.get("city_state_zip", "-"),
    phase1_data.get("delivery_address", "-"),
    phase1_data.get("delivery_city_state_zip", "-"),
    phase1_data.get("vin", "-"),
    phase1_data.get("car", "-"),
    phase1_data.get("color", "-"),
    phase1_data.get("insurance_company", "-"),
    phase1_data.get("insurance_policy_number", "-"),
    phase1_data.get("extra_info", "-"),
])
    attached_for_dispatch = await _finalize_phase1_media_for_dispatch(
        context,
        getattr(message, "chat_id", None),
        known_phone=state_data.get("phone_number"),
    )
    if not attached_for_dispatch:
        attached_for_dispatch = state_data.get("attached_files") or []
    final_lead_data = {
        "user_id": user_id,
        "telegram_username": username,
        "vehicle_details": phase1_data.get("vehicle_details", ""),
        "delivery_details": phase1_data.get("delivery_details", ""),
        "phone_number": state_data.get("phone_number"),
        "price": state_data.get("price"),
        "onetimesecret_token": (state_data.get("encrypted_data") or {}).get("secret_key"),
        "onetimesecret_secret_key": (state_data.get("encrypted_data") or {}).get("metadata_key"),
        "encrypted_link": (state_data.get("encrypted_data") or {}).get("link"),
        "reference_id": state_data.get("reference_id"),
        "group_id": group_id,
        "extra_info": state_data.get("extra_info", ""),
        "special_request_issuers": state_data.get("special_request_issuers", "") or "",
        "special_request_drivers": state_data.get("special_request_drivers", "") or "",
        "special_request_note": state_data.get("special_request_issuers", "") or "",
        "email": (state_data.get("email") or "") or None,
        "driver_license_id": (state_data.get("driver_license_id") or "") or None,
        "contact_info_source": _resolve_contact_source_label(state_data),
        "phase1_attached_files": attached_for_dispatch,
    }
    final_lead_data = await _attach_extra_vehicles_for_create(final_lead_data, state_data)
    lead = db.create_lead(final_lead_data)
    if not lead:
        await message.reply_text("❌ Error saving lead to database.")
        return ConversationHandler.END
    await _on_lead_created(context, lead)

    reference_id = lead.get("reference_id", "N/A")

    # Honour the drivers picked on the review card. A pick that resolves to nobody
    # is reported, never quietly widened into a broadcast.
    drivers_list: list = []
    dropped: list = []
    try:
        ids, dropped = _dispatch_drivers_with_reasons(
            state_data, group_id=group_id, is_all_groups=is_all_groups)
        keep = {str(x) for x in ids}
        drivers_list = [d for d in (_get_all_drivers_cached() or []) if str(d.get("id")) in keep]
    except Exception as e:
        logger.warning("finalize_lead_after_notes: resolving selected drivers failed: %s", e)
        drivers_list, dropped = [], []
    if not drivers_list and dropped:
        await message.reply_text(
            "⚠️ That lead could not be sent to the driver you picked:\n\n"
            + _dropped_drivers_note(dropped)
            + "\n\nFix it in /settings, or pick another driver and submit again."
        )
        db.set_user_state(user_id, "phase1", state_data)
        return STATE_AI_REVIEW

    if is_all_groups:
        await _post_lead_to_all_groups_for_approval(context, lead, active_groups)
    else:
        await _post_single_group_approval(context, lead, selected_group)

    # The temp-tag PDF is sent later, when a group ACCEPTS the lead
    # (see _send_full_group_lead_to_chat), not at creation.

    # Drivers are told NOW, alongside the group — neither waits on the other. They
    # used to get nothing until a group tapped Accept, so a lead the group had not
    # picked up yet was invisible to every driver, while the issuer's confirmation
    # below already claimed it had been sent to them.
    if drivers_list:
        vehicle_safe, extra_safe = _dispatch_display_parts(phase1_data, issuers_note)
        _fire_driver_dispatch(
            context,
            issuer_notify_chat_id=getattr(message, "chat_id", user_id),
            user_id=user_id,
            username=username,
            lead=lead,
            lead_data=dict(state_data),
            phase1_data=phase1_data,
            selected_drivers=drivers_list,
            selected_group=selected_group,
            skip_duplicate_full_group_post=True,   # the approval post just went out
            phone_number=phone_number,
            price=price,
            encrypted_data=encrypted_data,
            reference_id=reference_id,
            issuer_note_disp=issuers_note,
            driver_note_disp=drivers_note,
            group_id=group_id,
            vehicle_safe=vehicle_safe,
            extra_safe=extra_safe,
            driver_names=", ".join(d.get("driver_name", "?") for d in drivers_list),
        )
    context.user_data.pop("phase1_pending_media", None)
    context.user_data.pop("phase1_attached_files", None)
    ref_h = html.escape(str(reference_id), quote=False)
    drivers_label = html.escape(_drivers_sent_label(state_data, drivers_list), quote=False)
    source_label = html.escape((state_data.get("selected_source_label") or "—"), quote=False)
    _another = _after_send_keyboard(str(lead["id"]))
    if is_all_groups:
        await message.reply_text(
            "✅ Lead saved.\n\n"
            f"📋 Reference ID: <code>{ref_h}</code>\n"
            f"📣 Approval sent to <b>{len(active_groups)}</b> group(s)\n"
            f"🚗 Approval sent to <b>{drivers_label}</b>\n"
            f"📊 Lead source: <b>{source_label}</b>",
            parse_mode="HTML",
            reply_markup=_another,
        )
    else:
        await message.reply_text(
            "✅ Lead saved.\n\n"
            f"📋 Reference ID: <code>{ref_h}</code>\n"
            f"Approval sent to <b>{html.escape(selected_group.get('group_name') or 'group', quote=False)}</b>\n"
            f"🚗 Approval sent to <b>{drivers_label}</b>\n"
            f"📊 Lead source: <b>{source_label}</b>",
            parse_mode="HTML",
            reply_markup=_another,
        )
    await _maybe_offer_insurance_card(
        context, message, lead_id=str(lead["id"]), reference_id=str(reference_id),
    )
    return ConversationHandler.END

async def _submit_lead_from_review(message, context, user_id, data):
    # Last chance to mirror a single address across both lines, whatever route the
    # lead took to get here — the driver must never receive a blank delivery address.
    _apply_single_address_as_both(data)
    # Phone is the only hard requirement at submit; price may be "-".
    if not _is_valid_pending_phone(data.get("pending_phone_number")):
        _sanitize_phase1_pending_phone_price(data)
        db.set_user_state(user_id, "phase1", data)
        await message.reply_text(_phase2_prompt(data))
        return STATE_PHASE2
    phone = data.pop("pending_phone_number", None)
    price = data.pop("pending_price", None)

    group_id = data.get("selected_group_id")
    groups = db.get_all_groups()
    active_groups = [g for g in groups if record_is_active(g)]
    is_all_groups = str(group_id) == "all"
    if is_all_groups:
        primary_group = db.get_group_by_assistant_telegram_id(str(user_id))
        if not primary_group or not record_is_active(primary_group):
            primary_group = active_groups[0] if active_groups else None
        group = primary_group
        group_id = primary_group.get("id") if primary_group else None
    else:
        group = db.get_group_by_id(group_id) if group_id else None
    if not group:
        await message.reply_text("❌ No group selected. Please select a group in the review screen.")
        return STATE_AI_REVIEW

    # Encrypt
    enc = await asyncio.to_thread(ots.encrypt_phone, phone)
    if not enc:
        await message.reply_text("❌ Encryption failed.")
        return ConversationHandler.END

    ref_id = generate_reference_id()
    username = data.get("username", "Unknown")

    # Build vehicle_details as 11 lines
    vd = "\n".join([
        data.get("name", "-"), data.get("address", "-"), data.get("city_state_zip", "-"),
        data.get("delivery_address", "-"), data.get("delivery_city_state_zip", "-"),
        data.get("vin", "-"), data.get("car", "-"), data.get("color", "-"),
        data.get("insurance_company", "-"), data.get("insurance_policy_number", "-"),
        data.get("extra_info", "-"),
    ])

    attached_for_dispatch = await _finalize_phase1_media_for_dispatch(
        context,
        getattr(message, "chat_id", None),
        known_phone=phone,
    )
    if not attached_for_dispatch:
        attached_for_dispatch = data.get("attached_files") or []
    lead_payload = await _attach_extra_vehicles_for_create({}, data)
    lead = db.create_lead({
        **lead_payload,
        "user_id": user_id, "telegram_username": username,
        "vehicle_details": vd,
        "delivery_details": data.get("delivery_details", ""),
        "phone_number": phone, "price": price,
        "encrypted_link": enc.get("link"),
        "onetimesecret_token": enc.get("secret_key"),
        "onetimesecret_secret_key": enc.get("metadata_key"),
        "reference_id": ref_id, "group_id": group_id,
        "extra_info": data.get("extra_info", ""),
        "special_request_issuers": data.get("special_request_issuers", ""),
        "special_request_drivers": data.get("special_request_drivers", ""),
        "email": (data.get("email") or "") or None,
        "driver_license_id": (data.get("driver_license_id") or "") or None,
        "contact_info_source": _resolve_contact_source_label(data),
        "phase1_attached_files": attached_for_dispatch,
    })
    if not lead:
        await message.reply_text("❌ Could not save lead.")
        return ConversationHandler.END
    await _warn_if_extra_vehicles_were_dropped(message, lead_payload, lead)
    await _on_lead_created(context, lead)

    drivers_list: list = []
    try:
        raw_driver_ids = data.get("selected_driver_ids") or []
        driver_id_set = {str(x).strip() for x in raw_driver_ids if str(x).strip()}
        all_drivers_now = _get_all_drivers_cached() or []
        drivers_list = [d for d in all_drivers_now if str(d.get("id")) in driver_id_set]
    except Exception as e:
        logger.warning("submit_lead_from_review: resolving picked drivers failed: %s", e)
        drivers_list = []

    logger.info(
        "submit_lead_from_review: lead=%s ref=%s group=%s picked=%s",
        lead.get("id"),
        ref_id,
        (group or {}).get("group_name"),
        [d.get("driver_name") for d in drivers_list],
    )

    # Send group approval (single group) or broadcast offers (all groups).
    if is_all_groups:
        group_offer_message = (
            "🏷 NEW CLIENT\n"
            f"📋 Ref ID: `{ref_id}`\n\n"
            "✅ Double-check the tag for mistakes\n"
            "📲 Send tag with Krab Dispatch (@KrabIssuerBot)\n"
            "📋 Copy/paste client phone, address, and delivery time"
        )
        offer_kb_by_group: dict[str, InlineKeyboardMarkup] = {}
        short_lead = _short_uuid(lead["id"])
        for g in active_groups:
            gid = g.get("id")
            if not gid:
                continue
            short_gid = _short_uuid(gid)
            offer_kb_by_group[gid] = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Accept", callback_data=f"ag_{short_lead}{short_gid}"),
                InlineKeyboardButton("🔄 Different Team", callback_data=f"dg_{short_lead}{short_gid}"),
            ]])
        for g in active_groups:
            gid = g.get("id")
            chat_id = _parse_chat_id(g.get("group_telegram_id"))
            if not gid or not chat_id:
                continue
            db.create_group_lead_offer(lead["id"], gid, group_chat_id=str(chat_id), group_message_id=None)
            try:
                msg = await context.bot.send_message(
                    chat_id=chat_id,
                    text=group_offer_message,
                    parse_mode="Markdown",
                    reply_markup=offer_kb_by_group.get(gid),
                )
                db.update_group_lead_offer_message(lead["id"], gid, str(chat_id), msg.message_id)
            except RetryAfter as e:
                wait_s = int(getattr(e, "retry_after", 1) or 1)
                await asyncio.sleep(wait_s)
                try:
                    msg = await context.bot.send_message(
                        chat_id=chat_id,
                        text=group_offer_message,
                        parse_mode="Markdown",
                        reply_markup=offer_kb_by_group.get(gid),
                    )
                    db.update_group_lead_offer_message(lead["id"], gid, str(chat_id), msg.message_id)
                except Exception:
                    pass
            except Exception:
                pass
    else:
        await _post_single_group_approval(context, lead, group)

    # The temp-tag PDF is sent later, when a group ACCEPTS the lead
    # (see _send_full_group_lead_to_chat), not at creation.

    # Safety net: only run AFTER the group approval has been posted, so any
    # failure here can never block the group from receiving the lead.
    if not drivers_list:
        try:
            try:
                suspended = _get_suspended_driver_ids()
            except Exception:
                suspended = set()
            fallback_pool: list = []
            if not is_all_groups and group:
                try:
                    linked = db.get_group_driver_rows_for_group(group.get("id"))
                except Exception:
                    linked = []
                fallback_pool = linked or _get_all_drivers_cached() or []
            else:
                fallback_pool = _get_all_drivers_cached() or []
            drivers_list = [
                d
                for d in fallback_pool
                if d
                and record_is_active(d)
                and str(d.get("id")) not in suspended
            ]
            logger.info(
                "submit_lead_from_review: empty selected drivers; fallback resolved %d eligible drivers (group=%s)",
                len(drivers_list),
                (group or {}).get("group_name"),
            )
        except Exception as e:
            logger.warning("submit_lead_from_review: driver fallback failed: %s", e)
            drivers_list = []

    _store_issuer_await_group_accept(
        user_id,
        lead_id=str(lead["id"]),
        await_mode="dispatch_pending",
        selected_driver_ids=[str(d.get("id")) for d in drivers_list if d.get("id")],
    )
    context.user_data.pop("phase1_attached_files", None)
    context.user_data.pop("phase1_pending_media", None)
    context.user_data.pop("review_message_id", None)
    success_label = "broadcast sent" if is_all_groups else "sent"
    await message.reply_text(
        f"✅ **Lead created & {success_label}!**\n📋 Reference: `{ref_id}`\n\n"
        "Approval was posted to the selected team chat.\n\n"
        f"In **Krab Dispatch**, name the tag PDF **similar to this client’s name** (first line of details) so it auto‑links to reference `{ref_id}`.\n\n"
        f"Use /lead to add another.",
        parse_mode="Markdown",
        reply_markup=_after_send_keyboard(str(lead["id"])),
    )
    await _maybe_offer_insurance_card(
        context, message, lead_id=str(lead["id"]), reference_id=str(ref_id),
    )
    return ConversationHandler.END

async def handle_group_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle group selection when assistants_choose_group is on; then show driver picker."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    state = db.get_user_state(user_id)
    if not state or not state.get("data"):
        await query.message.reply_text("❌ Error: Lead data not found. Please start over with /start")
        return ConversationHandler.END
    lead_data = state.get("data", {}).copy()
    if query.data == "select_group_all":
        # Broadcast: notify groups, then immediately continue to driver selection (no waiting).
        phase1_data = {k: v for k, v in lead_data.items() if k not in _PHASE1_STATE_EXCLUDE}
        groups = db.get_all_groups()
        active_groups = [g for g in groups if record_is_active(g)]
        # group_id is NOT NULL for driver assignments, so pick a primary group for the lead record.
        primary_group = db.get_group_by_assistant_telegram_id(str(user_id))
        if not primary_group or not record_is_active(primary_group):
            primary_group = active_groups[0] if active_groups else None
        if not primary_group:
            await query.message.reply_text("❌ Error: No active groups configured. Please contact admin.")
            return ConversationHandler.END
        final_lead_data = {
            "user_id": user_id,
            "telegram_username": (query.from_user.username or "Unknown"),
            "vehicle_details": phase1_data.get("vehicle_details", ""),
            "delivery_details": phase1_data.get("delivery_details", ""),
            "phone_number": lead_data.get("phone_number"),
            "price": lead_data.get("price"),
            "onetimesecret_token": (lead_data.get("encrypted_data") or {}).get("secret_key"),
            "onetimesecret_secret_key": (lead_data.get("encrypted_data") or {}).get("metadata_key"),
            "encrypted_link": (lead_data.get("encrypted_data") or {}).get("link"),
            "reference_id": lead_data.get("reference_id"),
            "group_id": primary_group.get("id"),
            "extra_info": lead_data.get("extra_info", ""),
            "special_request_issuers": lead_data.get("special_request_issuers", "") or "",
            "special_request_drivers": lead_data.get("special_request_drivers", "") or "",
            "special_request_note": lead_data.get("special_request_issuers", "") or "",
            "email": (lead_data.get("email") or "") or None,
            "driver_license_id": (lead_data.get("driver_license_id") or "") or None,
            "contact_info_source": _resolve_contact_source_label(lead_data),
            "phase1_attached_files": _dispatch_attach_files(context, lead_data),
        }
        final_lead_data = await _attach_extra_vehicles_for_create(final_lead_data, lead_data)
        lead = db.create_lead(final_lead_data)
        if not lead:
            await query.message.reply_text("❌ Error saving lead to database.")
            return ConversationHandler.END
        await _on_lead_created(context, lead)

        reference_id = lead.get("reference_id", "N/A")

        group_offer_message = (
            "🏷 NEW CLIENT\n"
            f"📋 Ref ID: `{reference_id}`\n\n"
            "✅ Double-check the tag for mistakes\n"
            "📲 Send tag with Krab Dispatch (@KrabIssuerBot)\n"
            "📋 Copy/paste client phone, address, and delivery time"
        )
        offer_kb_by_group: dict[str, InlineKeyboardMarkup] = {}
        short_lead = _short_uuid(lead["id"])
        for g in active_groups:
            gid = g.get("id")
            if not gid:
                continue
            short_gid = _short_uuid(gid)
            offer_kb_by_group[gid] = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Accept", callback_data=f"ag_{short_lead}{short_gid}"),
                InlineKeyboardButton("🔄 Different Team", callback_data=f"dg_{short_lead}{short_gid}"),
            ]])
        sent_count = 0
        failures: list[tuple[str, str]] = []
        for g in active_groups:
            gid = g.get("id")
            chat_id = _parse_chat_id(g.get("group_telegram_id"))
            if not gid or not chat_id:
                logger.warning(
                    "Broadcast skipped group %s: missing group id or telegram chat id (telegram_id=%r)",
                    g.get("group_name"),
                    g.get("group_telegram_id"),
                )
                failures.append((g.get("group_name") or str(gid) or "Unknown group", "missing group_telegram_id"))
                continue
            # Create offer row first; we'll fill message IDs after sending.
            db.create_group_lead_offer(lead["id"], gid, group_chat_id=str(chat_id), group_message_id=None)
            try:
                msg = await context.bot.send_message(
                    chat_id=chat_id,
                    text=group_offer_message,
                    parse_mode="Markdown",
                    reply_markup=offer_kb_by_group.get(gid),
                )
                db.update_group_lead_offer_message(lead["id"], gid, str(chat_id), msg.message_id)
                sent_count += 1
            except RetryAfter as e:
                # Telegram is rate limiting. Wait the requested time and retry once.
                wait_s = int(getattr(e, "retry_after", 1) or 1)
                logger.warning("Broadcast rate-limited for %s; retrying in %ss", g.get("group_name"), wait_s)
                await asyncio.sleep(wait_s)
                try:
                    msg = await context.bot.send_message(
                        chat_id=chat_id,
                        text=group_offer_message,
                        parse_mode="Markdown",
                        reply_markup=offer_kb_by_group.get(gid),
                    )
                    db.update_group_lead_offer_message(lead["id"], gid, str(chat_id), msg.message_id)
                    sent_count += 1
                except Exception as e2:
                    logger.error("Error sending group offer to %s after retry: %s", g.get("group_name"), e2)
                    failures.append((g.get("group_name") or str(gid) or "Unknown group", f"{type(e2).__name__}: {e2}"))
            except Exception as e:
                logger.error("Error sending group offer to %s: %s", g.get("group_name"), e)
                failures.append((g.get("group_name") or str(gid) or "Unknown group", f"{type(e).__name__}: {e}"))

        # Phase-1 attachments stay on the lead row until a group taps Accept (handle_accept_group_offer).

        continue_data = lead_data.copy()
        continue_data["lead_id"] = lead["id"]
        continue_data["group_id"] = primary_group.get("id")
        continue_data["selected_group"] = primary_group
        continue_data["follow_after_broadcast"] = True
        continue_data["broadcast"] = True
        continue_data.pop("resend", None)
        _store_issuer_await_group_accept(
            user_id,
            lead_id=str(lead["id"]),
            await_mode="dispatch_pending",
            selected_driver_ids=_resolve_dispatch_driver_ids(
                lead_data,
                group_id=primary_group.get("id"),
                is_all_groups=True,
            ),
        )
        try:
            summary = ""
            if failures:
                top = failures[:8]
                summary_lines = [f"- {name}: {reason}" for (name, reason) in top]
                more = f"\n- (+{len(failures) - len(top)} more)" if len(failures) > len(top) else ""
                summary = "\n\nFailed group(s):\n" + "\n".join(summary_lines) + more
            ref_h = html.escape(str(reference_id), quote=False)
            body = (
                "📣 **Approval requests sent**\n\n"
                f"📋 Reference ID: <code>{ref_h}</code>\n"
                f"Sent to {sent_count} group(s)."
                f"{html.escape(summary, quote=False)}"
            )
            await query.message.reply_text(body, parse_mode="HTML")
        except Exception as e:
            logger.error("Broadcast: could not reply to issuer: %s", e)
            await query.message.reply_text(
                "📣 Approval requests sent.",
                parse_mode="Markdown",
            )
        await _maybe_offer_insurance_card(
            context, query.message, lead_id=str(lead["id"]), reference_id=str(reference_id),
        )
        return ConversationHandler.END

    group_id = query.data.replace("select_group_", "")
    selected_group = db.get_group_by_id(group_id)
    if not selected_group or not record_is_active(selected_group):
        await query.message.reply_text("❌ Group not found or inactive. Please start over with /start")
        return ConversationHandler.END

    rid = lead_data.get("reassign_lead_id")
    if rid:
        lead = _lead_for_resend(rid)
        ok_row, err_row = _validate_lead_row_for_resend(lead, issuer_user_id=user_id)
        if not ok_row:
            await query.message.reply_text(f"❌ {err_row}")
            return ConversationHandler.END
        db.delete_group_lead_offers_for_lead(rid)
        db.update_lead(rid, {
            "group_id": group_id,
            # Reassign re-dispatches an EXISTING lead: keep its own stored files
            # (carried in lead_data by _issuer_state_data_from_lead). Do NOT pull the
            # in-memory phase1_extra_attachments here — those belong to whatever lead the
            # user is currently reviewing and would leak that client's title/license photo
            # to this lead's newly chosen team.
            "phase1_attached_files": lead_data.get("attached_files") or [],
        })
        lead = db.get_lead_by_id(rid) or lead
        await _post_single_group_approval(context, lead, selected_group)
        continue_data = _issuer_state_data_from_lead(lead)
        continue_data["lead_id"] = rid
        continue_data["group_id"] = group_id
        continue_data["selected_group"] = selected_group
        continue_data["follow_after_broadcast"] = True
        _store_issuer_await_group_accept(
            user_id,
            lead_id=str(rid),
            await_mode="dispatch_pending",
            selected_driver_ids=_resolve_dispatch_driver_ids(
                continue_data,
                group_id=group_id,
                is_all_groups=False,
            ),
        )
        reference_id = lead.get("reference_id", "N/A")
        ref_h = html.escape(str(reference_id), quote=False)
        await query.message.reply_text(
            "✅ Group updated.\n\n"
            f"📋 Reference ID: <code>{ref_h}</code>\n"
            f"Approval sent to <b>{html.escape(selected_group.get('group_name') or 'group', quote=False)}</b>.",
            parse_mode="HTML",
        )
        return ConversationHandler.END

    ok, err = _validate_lead_data_ready_for_send(lead_data)
    if not ok:
        await query.message.reply_text(f"❌ {err} Use /start to begin again.")
        return ConversationHandler.END

    phase1_data = {k: v for k, v in lead_data.items() if k not in _PHASE1_STATE_EXCLUDE}
    phase1_data["vehicle_details"] = "\n".join([
    phase1_data.get("name", "-"),
    phase1_data.get("address", "-"),
    phase1_data.get("city_state_zip", "-"),
    phase1_data.get("delivery_address", "-"),
    phase1_data.get("delivery_city_state_zip", "-"),
    phase1_data.get("vin", "-"),
    phase1_data.get("car", "-"),
    phase1_data.get("color", "-"),
    phase1_data.get("insurance_company", "-"),
    phase1_data.get("insurance_policy_number", "-"),
    phase1_data.get("extra_info", "-"),
    ])
    final_lead_data = {
        "user_id": user_id,
        "telegram_username": (query.from_user.username or "Unknown"),
        "vehicle_details": phase1_data.get("vehicle_details", ""),
        "delivery_details": phase1_data.get("delivery_details", ""),
        "phone_number": lead_data.get("phone_number"),
        "price": lead_data.get("price"),
        "onetimesecret_token": (lead_data.get("encrypted_data") or {}).get("secret_key"),
        "onetimesecret_secret_key": (lead_data.get("encrypted_data") or {}).get("metadata_key"),
        "encrypted_link": (lead_data.get("encrypted_data") or {}).get("link"),
        "reference_id": lead_data.get("reference_id"),
        "group_id": group_id,
        "extra_info": lead_data.get("extra_info", ""),
        "special_request_issuers": lead_data.get("special_request_issuers", "") or "",
        "special_request_drivers": lead_data.get("special_request_drivers", "") or "",
        "special_request_note": lead_data.get("special_request_issuers", "") or "",
        "email": (lead_data.get("email") or "") or None,
        "driver_license_id": (lead_data.get("driver_license_id") or "") or None,
        "contact_info_source": _resolve_contact_source_label(lead_data),
        "phase1_attached_files": _dispatch_attach_files(context, lead_data),
    }
    final_lead_data = await _attach_extra_vehicles_for_create(final_lead_data, lead_data)
    lead = db.create_lead(final_lead_data)
    if not lead:
        await query.message.reply_text("❌ Error saving lead to database.")
        return ConversationHandler.END
    await _on_lead_created(context, lead)

    reference_id = lead.get("reference_id", "N/A")

    await _post_single_group_approval(context, lead, selected_group)

    continue_data = lead_data.copy()
    continue_data["lead_id"] = lead["id"]
    continue_data["group_id"] = group_id
    continue_data["selected_group"] = selected_group
    continue_data["follow_after_broadcast"] = True
    _store_issuer_await_group_accept(
        user_id,
        lead_id=str(lead["id"]),
        await_mode="dispatch_pending",
        selected_driver_ids=_resolve_dispatch_driver_ids(
            lead_data,
            group_id=group_id,
            is_all_groups=False,
        ),
    )
    ref_h = html.escape(str(reference_id), quote=False)
    await query.message.reply_text(
        f"✅ Group selected: <b>{html.escape(selected_group.get('group_name', 'N/A'), quote=False)}</b>\n\n"
        f"📋 Reference ID: <code>{ref_h}</code>\n"
        "Approval sent.",
        parse_mode="HTML",
    )
    await _maybe_offer_insurance_card(
        context, query.message, lead_id=str(lead["id"]), reference_id=str(reference_id),
    )
    return ConversationHandler.END


async def handle_reassign_group_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Issuer taps *Pick another group* after a team chose Different team (re-entry to group picker)."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lead_id = query.data.replace("reassign_group_", "", 1).strip()
    if not lead_id:
        await query.message.reply_text("❌ Invalid request.")
        return ConversationHandler.END
    lead = _lead_for_resend(lead_id)
    if not lead or int(lead.get("user_id") or 0) != int(user_id):
        await query.message.reply_text("❌ Not allowed.")
        return ConversationHandler.END
    ok_r, err_r = _validate_lead_row_for_resend(lead, issuer_user_id=user_id)
    if not ok_r:
        await query.message.reply_text(f"❌ {err_r}")
        return ConversationHandler.END
    data = _issuer_state_data_from_lead(lead)
    data["reassign_lead_id"] = lead_id
    db.set_user_state(user_id, "select_group", data)
    groups = db.get_all_groups()
    active_groups = [g for g in groups if record_is_active(g)]
    if not active_groups:
        await query.message.reply_text("❌ No active groups configured.")
        return ConversationHandler.END
    kb = _build_group_keyboard(active_groups, include_all=False)
    ref = lead.get("reference_id", "N/A")
    await query.message.reply_text(
        f"🔄 *Pick another group* for this lead.\n\n"
        f"Reference: `{ref}`\n\n"
        "Choose a group — the same approval message will be sent there.",
        parse_mode="Markdown",
        reply_markup=kb,
    )
    return STATE_SELECT_GROUP


async def handle_driver_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle driver selection after Phase 2 (or after group selection, or after timeout resend)."""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    
    # Get stored lead data
    state = db.get_user_state(user_id)
    if not state or not state.get("data"):
        await query.message.reply_text("❌ Error: Lead data not found. Please start over with /start")
        return ConversationHandler.END
    
    lead_data = state.get("data", {})

    lid_gate = lead_data.get("lead_id")
    if lid_gate and not lead_data.get("resend"):
        offers_gate = db.get_group_lead_offers(str(lid_gate))
        if offers_gate and not db.get_accepted_group_for_lead(str(lid_gate)):
            await query.message.reply_text(
                "⏳ **Wait for a team to accept first.**\n\n"
                "A group must tap **Accept** on the approval message in their team chat before you can notify drivers.",
                parse_mode="Markdown",
            )
            return STATE_SELECT_DRIVER

    # Resend flow: lead exists, just send to new drivers (ignore follow_after_broadcast — stale state breaks Pick new driver)
    if lead_data.get("resend") and lead_data.get("lead_id"):
        return await _handle_resend_to_drivers(
            update, context, lead_data, query.data, user_id,
        )
    
    phase1_data = {k: v for k, v in lead_data.items() if k not in _PHASE1_STATE_EXCLUDE}
    phase1_data["vehicle_details"] = "\n".join([
    phase1_data.get("name", "-"),
    phase1_data.get("address", "-"),
    phase1_data.get("city_state_zip", "-"),
    phase1_data.get("delivery_address", "-"),
    phase1_data.get("delivery_city_state_zip", "-"),
    phase1_data.get("vin", "-"),
    phase1_data.get("car", "-"),
    phase1_data.get("color", "-"),
    phase1_data.get("insurance_company", "-"),
    phase1_data.get("insurance_policy_number", "-"),
    phase1_data.get("extra_info", "-"),
    ])
    phone_number = lead_data.get('phone_number')
    price = lead_data.get('price')
    encrypted_data = lead_data.get('encrypted_data', {})
    reference_id = lead_data.get('reference_id')
    group_id = lead_data.get('group_id')
    username = query.from_user.username or "Unknown"
    
    # Determine which drivers to notify
    # Drivers work for all groups, so get all active drivers
    callback_data = query.data
    all_drivers = _get_all_drivers_cached()
    active_drivers = [d for d in all_drivers if record_is_active(d)]
    
    suspended = _get_suspended_driver_ids()
    if callback_data == "select_driver_all":
        selected_drivers = [d for d in active_drivers if str(d.get("id")) not in suspended]
        selected_driver_ids = [d['id'] for d in selected_drivers]
        if not selected_drivers:
            await query.message.reply_text("❌ No eligible drivers (all suspended). Please select a driver individually.")
            return STATE_SELECT_DRIVER
    elif callback_data.startswith("driver_suspended_"):
        driver_id = callback_data.replace("driver_suspended_", "")
        driver = next((d for d in all_drivers if str(d.get("id")) == str(driver_id)), None)
        name = driver.get("driver_name", "Driver") if driver else "Driver"
        pending = db.get_driver_pending_receipts(driver_id) if driver_id else []
        count = len(pending)
        await query.message.reply_text(
            f"⚠️ **{_telegram_md1_escape(name)}** is temporarily suspended (PENALTY).\n\n"
            f"They owe {count} receipt(s). No leads will be sent until all receipts are uploaded.",
            parse_mode="Markdown",
        )
        # Notify driver that dispatcher tried to send lead
        tid = driver.get("driver_telegram_id") if driver else None
        if tid and pending:
            try:
                cid = int(str(tid).strip())
                ref_buttons = [
                    [InlineKeyboardButton(f"📋 {p['reference_id']}", callback_data=f"receipt_for_{p['reference_id']}")]
                    for p in pending[:10]
                ]
                await context.bot.send_message(
                    chat_id=cid,
                    text=(
                        f"⛔ **Temporary suspension**\n\n"
                        f"Dispatcher tried to send you a lead, but you owe **{count}** receipt(s).\n\n"
                        f"Upload all receipts below to resume receiving leads:"
                    ),
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(ref_buttons),
                )
            except Exception as e:
                logger.warning("Could not notify suspended driver: %s", e)
        return STATE_SELECT_DRIVER
    else:
        # Send to selected driver
        driver_id = callback_data.replace("select_driver_", "")
        selected_driver_ids = [driver_id]
        selected_drivers = [d for d in active_drivers if str(d.get("id")) == str(driver_id)]
        if not selected_drivers:
            await query.message.reply_text("❌ Error: Driver not found.")
            return ConversationHandler.END
        if str(driver_id) in suspended:
            await query.message.reply_text("❌ This driver is suspended. Please select another.")
            return STATE_SELECT_DRIVER

    # Create lead in database
    final_lead_data = {
        "user_id": user_id,
        "telegram_username": username,
        "vehicle_details": phase1_data.get("vehicle_details", ""),
        "delivery_details": phase1_data.get("delivery_details", ""),
        "phone_number": phone_number,
        "price": price,
        "onetimesecret_token": encrypted_data.get("secret_key"),
        "onetimesecret_secret_key": encrypted_data.get("metadata_key"),
        "encrypted_link": encrypted_data.get("link"),
        "reference_id": reference_id,
        "group_id": group_id,
        # Store extra_info explicitly so we can show it to drivers/supervisors later
        "extra_info": lead_data.get("extra_info", ""),
        "special_request_issuers": lead_data.get("special_request_issuers", "") or "",
        "special_request_drivers": lead_data.get("special_request_drivers", "") or "",
        "special_request_note": lead_data.get("special_request_issuers", "") or "",
        "email": (lead_data.get("email") or "") or None,
        "driver_license_id": (lead_data.get("driver_license_id") or "") or None,
        "contact_info_source": _resolve_contact_source_label(lead_data),
        "phase1_attached_files": _dispatch_attach_files(context, lead_data),
    }

    if lead_data.get("follow_after_broadcast") and lead_data.get("lead_id"):
        lead = db.get_lead_by_id(lead_data["lead_id"])
        if not lead:
            await query.message.reply_text("❌ Error: lead not found. Use /start to begin again.")
            return ConversationHandler.END
        reference_id = lead.get("reference_id") or reference_id
    else:
        final_lead_data = await _attach_extra_vehicles_for_create(final_lead_data, lead_data)
        lead = db.create_lead(final_lead_data)
        if not lead:
            await query.message.reply_text("❌ Error saving lead to database.")
            return ConversationHandler.END
        await _on_lead_created(context, lead)

    had_broadcast_offers = bool(db.get_group_lead_offers(lead["id"]))
    if lead_data.get("follow_after_broadcast") and lead.get("group_id"):
        group_id = lead["group_id"]
    skip_duplicate_full_group_post = bool(lead_data.get("follow_after_broadcast") and had_broadcast_offers)

    # Fresh DB row so winning group (broadcast accept) is visible before Monday + messaging
    lead = db.get_lead_by_id(lead["id"]) or lead
    selected_group = _resolve_selected_group(lead_data, lead)
    if lead_data.get("follow_after_broadcast") and selected_group:
        lead_data["selected_group"] = selected_group
        lead_data["group_id"] = selected_group.get("id")
    if not selected_group:
        await query.message.reply_text(
            "❌ Error: could not resolve the group for this lead. Please start over with /start."
        )
        return ConversationHandler.END

    group_id = selected_group.get("id")

    issuer_note_disp = (
        (lead_data.get("special_request_issuers") or lead.get("special_request_issuers")
         or lead_data.get("special_request_note") or lead.get("special_request_note") or "").strip()
    )
    driver_note_disp = (lead_data.get("special_request_drivers") or lead.get("special_request_drivers") or "").strip()

    # Build vehicle block from individual fields so VIN and car are NEVER sanitized (no link in those lines)
    def _safe(s: str) -> str:
        return _sanitize_phones_for_send(s or "") or "-"
    vin_only = (phase1_data.get("vin") or "").strip() or "-"
    car_only = (phase1_data.get("car") or "").strip() or "-"
    name_line_safe = _safe(phase1_data.get("name"))
    vehicle_lines_display = [
        _safe(phase1_data.get("address")),
        _safe(phase1_data.get("city_state_zip")),
        vin_only,
        car_only,
        _safe(phase1_data.get("color")),
        _safe(phase1_data.get("insurance_company")),
        (phase1_data.get("insurance_policy_number") or "").strip() or "-",
        _safe(phase1_data.get("extra_info")),
    ]
    if issuer_note_disp:
        vehicle_lines_display.append(_safe("📝 " + issuer_note_disp))
    else:
        vehicle_lines_display.append("📝 No")
    vehicle_safe = f"🚗 Vehicle: {name_line_safe}\n" + "\n".join(vehicle_lines_display)
    extra_safe = _sanitize_phones_for_send(phase1_data.get('extra_info', '') or '')
    
    driver_names = ", ".join(d.get("driver_name", "?") for d in selected_drivers)

    def _bg_task_done(t: asyncio.Task) -> None:
        try:
            exc = t.exception()
        except asyncio.CancelledError:
            return
        if exc:
            logger.error("Background lead dispatch failed: %s", exc, exc_info=exc)

    asyncio.create_task(
        _background_dispatch_lead_after_driver_pick(
            context,
            issuer_notify_chat_id=user_id,
            user_id=user_id,
            username=username,
            lead=lead,
            lead_data=lead_data,
            phase1_data=phase1_data,
            selected_drivers=selected_drivers,
            selected_group=selected_group,
            skip_duplicate_full_group_post=skip_duplicate_full_group_post,
            phone_number=phone_number,
            price=price,
            encrypted_data=encrypted_data,
            reference_id=reference_id,
            issuer_note_disp=issuer_note_disp,
            driver_note_disp=driver_note_disp,
            group_id=group_id,
            vehicle_safe=vehicle_safe,
            extra_safe=extra_safe,
            driver_names=driver_names,
        )
    ).add_done_callback(_bg_task_done)

    # Source is already chosen in the main lead flow; do not ask again here.
    lead_fresh = db.get_lead_by_id(lead["id"]) if lead and lead.get("id") else None
    existing_source = (lead_fresh.get("contact_info_source") or "").strip() if lead_fresh else ""
    preselected_source = (lead_data.get("selected_source_label") or "").strip()
    if not existing_source and preselected_source:
        try:
            db.update_lead(lead["id"], {"contact_info_source": preselected_source})
        except Exception as e:
            logger.warning("Could not persist preselected contact source on lead %s: %s", lead.get("id"), e)

    await _issuer_lead_success_and_motivation(
        query.message, user_id, username, reference_id, driver_names, selected_group.get("group_name", "N/A"),
    )
    await _maybe_offer_insurance_card(
        context, query.message, lead_id=lead["id"], reference_id=reference_id,
    )
    return ConversationHandler.END


def _dispatch_display_parts(phase1_data: dict, issuer_note: str) -> tuple:
    """(vehicle_safe, extra_safe) — the vehicle block drivers and groups both see."""
    def _safe(v) -> str:
        return _sanitize_phones_for_send(v or "") or "-"

    lines = [
        _safe(phase1_data.get("address")),
        _safe(phase1_data.get("city_state_zip")),
        (phase1_data.get("vin") or "").strip() or "-",
        (phase1_data.get("car") or "").strip() or "-",
        _safe(phase1_data.get("color")),
        _safe(phase1_data.get("insurance_company")),
        (phase1_data.get("insurance_policy_number") or "").strip() or "-",
        _safe(phase1_data.get("extra_info")),
        _safe("📝 " + issuer_note) if (issuer_note or "").strip() else "📝 No",
    ]
    vehicle_safe = (f"🚗 Vehicle: {_safe(phase1_data.get('name'))}\n"
                    + "\n".join(lines))
    extra_safe = _sanitize_phones_for_send(phase1_data.get("extra_info", "") or "")
    return vehicle_safe, extra_safe


def _fire_driver_dispatch(context, **kw) -> None:
    """Start the driver DMs now, in the background, without waiting on anyone."""
    def _done(t) -> None:
        try:
            exc = t.exception()
        except asyncio.CancelledError:
            return
        if exc:
            logger.error("Driver dispatch failed: %s", exc, exc_info=exc)

    asyncio.create_task(
        _background_dispatch_lead_after_driver_pick(context, **kw)
    ).add_done_callback(_done)


async def _background_dispatch_lead_after_driver_pick(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    issuer_notify_chat_id: int,
    user_id: int,
    username: str,
    lead: dict,
    lead_data: dict,
    phase1_data: dict,
    selected_drivers: list,
    selected_group: dict,
    skip_duplicate_full_group_post: bool,
    phone_number,
    price,
    encrypted_data: dict,
    reference_id: str,
    issuer_note_disp: str,
    driver_note_disp: str,
    group_id,
    vehicle_safe: str,
    extra_safe: str,
    driver_names: str,
) -> None:
    """Monday (non-blocking), driver DMs, group post, supervisory/ST, usage — runs after issuer continues."""
    lead_id = lead["id"]
    monday_result = None
    if monday:
        monday_lead_data = {
            "name": phase1_data.get("name", ""),
            "phone_number": phone_number,
            "price": price,
            "delivery_address": phase1_data.get("delivery_address", ""),
            "delivery_city_state_zip": phase1_data.get("delivery_city_state_zip", ""),
            "group_message": (
                "🏷NEW CLIENT❗️\n\n"
                f"📋 Reference ID: {reference_id}\n"
                f"{vehicle_safe}\n\n"
                "Please use Krab Dispatch (@KrabIssuerBot) 📧🚘 — Enter: Tag, Phone, Delivery time, Delivery address.\n"
                f"🔗 Encrypted Link: {encrypted_data.get('link')}"
                + (f"\n\n📝 Driver-only note:\n{driver_note_disp}" if driver_note_disp else "")
            ),
            "supervisor_name": selected_group.get("group_name", ""),
        }
        try:
            monday_result = await asyncio.to_thread(monday.create_item, monday_lead_data, username)
        except Exception as e:
            logger.error("Monday.com create_item failed: %s", e, exc_info=True)
            monday_result = None

        if monday_result:
            db.update_lead(lead_id, {
                "monday_item_id": monday_result["item_id"],
                "issue_date": monday_result["issue_date"].isoformat(),
                "expiration_date": monday_result["expiration_date"].isoformat(),
            })
        else:
            from datetime import datetime, timedelta
            ny_tz = pytz.timezone("America/New_York")
            issue_date = datetime.now(ny_tz)
            expiration_date = issue_date + timedelta(days=30)
            db.update_lead(lead_id, {
                "issue_date": issue_date.isoformat(),
                "expiration_date": expiration_date.isoformat(),
            })
            monday_result = {"issue_date": issue_date, "expiration_date": expiration_date}
    else:
        from datetime import datetime, timedelta
        ny_tz = pytz.timezone("America/New_York")
        issue_date = datetime.now(ny_tz)
        expiration_date = issue_date + timedelta(days=30)
        db.update_lead(lead_id, {
            "issue_date": issue_date.isoformat(),
            "expiration_date": expiration_date.isoformat(),
        })
        monday_result = {"issue_date": issue_date, "expiration_date": expiration_date}

    lead = db.get_lead_by_id(lead_id) or lead
    selected_group = _resolve_selected_group(lead_data, lead)
    if selected_group:
        lead_data["selected_group"] = selected_group
        lead_data["group_id"] = selected_group.get("id")

    issue_s = (
        monday_result["issue_date"].strftime("%Y-%m-%d %H:%M:%S %Z") if monday_result else "N/A"
    )
    exp_s = (
        monday_result["expiration_date"].strftime("%Y-%m-%d %H:%M:%S %Z") if monday_result else "N/A"
    )

    group_message = _format_group_lead_message_html(
        reference_id,
        phase1_data,
        encrypted_data.get("link") or "",
        monday_result["issue_date"] if monday_result else None,
        monday_result["expiration_date"] if monday_result else None,
        issuer_note_disp,
    )

    d_csz_esc = _telegram_md1_escape(phase1_data.get("delivery_city_state_zip", "") or "")
    extra_esc = _telegram_md1_escape(extra_safe)
    driver_request_message = (
        f"👋Hi! New client 💸 available📈❗️\n\n"
        f"📍 Delivery (City, State, Zip): {d_csz_esc}\n"
        f"📋 Reference ID: `{reference_id}`\n"
        f" Delivery Time 🏷️: {extra_esc}\n"
        f"Please have Car, Driver License, and Laser Printer Ready✅"
    )
    if driver_note_disp:
        driver_request_message += (
            "\n\n📝 Special request (driver): "
            + _telegram_md1_escape(_sanitize_phones_for_send(driver_note_disp))
        )

    # The receipt link travels WITH the offer, so a driver has it in hand before the
    # delivery rather than hunting for it after. It is a web page, not Telegram, so
    # it still works tomorrow.
    accept_keyboard = _keyboard_lead_accept_decline(str(lead_id))
    try:
        accept_keyboard = InlineKeyboardMarkup(
            list(accept_keyboard.inline_keyboard)
            + [[InlineKeyboardButton("🧾 Upload receipt", url=receipt_portal_url(lead_id))]]
        )
    except Exception as e:
        logger.warning("could not attach the receipt link to the offer: %s", e)

    async def _notify_one_driver(driver: dict) -> bool:
        """Deliver offer + optional receipt strike to one driver. Returns True if primary DM sent."""
        driver_telegram_id_raw = driver.get("driver_telegram_id")
        if not driver_telegram_id_raw:
            return False
        try:
            driver_chat_id = int(str(driver_telegram_id_raw).strip())
        except (ValueError, TypeError):
            driver_chat_id = driver_telegram_id_raw
        try:
            db.create_lead_assignment(lead_id, driver["id"], group_id)
            try:
                await context.bot.send_message(
                    chat_id=driver_chat_id,
                    text=driver_request_message,
                    parse_mode="Markdown",
                    reply_markup=accept_keyboard,
                )
            except BadRequest as e:
                if "parse" in str(e).lower():
                    await context.bot.send_message(
                        chat_id=driver_chat_id,
                        text=driver_request_message.replace("`", ""),
                        reply_markup=accept_keyboard,
                    )
                else:
                    raise
            pending = db.get_driver_pending_receipts(driver["id"])
            if pending and len(pending) < SUSPENSION_THRESHOLD:
                ref_buttons = [
                    [InlineKeyboardButton(f"📤 Upload {p['reference_id']}", callback_data=f"receipt_for_{p['reference_id']}")]
                    for p in pending
                ]
                strike_txt = (
                    f"⚠️ You owe **{len(pending)}** receipt(s):\n\n"
                    + "\n".join(f"• Ref `{p['reference_id']}`" for p in pending)
                    + f"\n\nAt **{SUSPENSION_THRESHOLD}** unpaid you will be **temporarily suspended** from new leads."
                    + "\n\nTo view all receipts type /receipts"
                )
                try:
                    await context.bot.send_message(
                        chat_id=driver_chat_id,
                        text=strike_txt,
                        parse_mode="Markdown",
                        reply_markup=_keyboard_receipt_plus_rows(ref_buttons),
                    )
                except BadRequest:
                    await context.bot.send_message(
                        chat_id=driver_chat_id,
                        text=strike_txt.replace("`", "").replace("*", ""),
                        reply_markup=_keyboard_receipt_plus_rows(ref_buttons),
                    )
            return True
        except Exception as e:
            logger.error(
                "Error sending to driver %s (chat_id=%s): %r",
                driver.get("driver_name"),
                driver_telegram_id_raw,
                e,
            )
            return False

    results = await asyncio.gather(
        *(_notify_one_driver(d) for d in selected_drivers),
        return_exceptions=True,
    )
    assigned_count = sum(1 for r in results if r is True)

    logger.info("Sent lead request to %s drivers (background)", assigned_count)
    if assigned_count == 0:
        ref_h = html.escape(str(reference_id or "N/A"), quote=False)
        try:
            await context.bot.send_message(
                chat_id=issuer_notify_chat_id,
                text=(
                    "⚠️ No driver received the Telegram notification (check driver chat IDs in admin or logs). "
                    "The lead was still saved.\n\n"
                    f"📋 Reference ID: <code>{ref_h}</code>"
                ),
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning("Could not notify issuer about zero driver delivery: %s", e)

    group_telegram_id_raw = selected_group.get("group_telegram_id") if selected_group else None
    if skip_duplicate_full_group_post:
        logger.info(
            "Skipping full group HTML post: broadcast lead %s — winner already notified on accept.",
            lead_id,
        )
    elif selected_group:
        group_name = selected_group.get("group_name", "N/A")
        if not group_telegram_id_raw:
            logger.warning(
                "No group_telegram_id for group '%s' (id=%s). Lead not sent to group.",
                group_name,
                selected_group.get("id"),
            )
        else:
            group_chat_id = _parse_chat_id(group_telegram_id_raw)
            try:
                logger.info("Sending lead to group '%s' (chat_id=%s)", group_name, group_chat_id)
                try:
                    await context.bot.send_message(
                        chat_id=group_chat_id, text=group_message, parse_mode="HTML",
                    )
                except Exception as html_err:
                    logger.warning(
                        "Group lead HTML send failed for %s, retrying plain: %s",
                        group_name,
                        html_err,
                    )
                    ref_h = html.escape(str(reference_id or "N/A"), quote=False)
                    vehicle_pre = f"<pre>{html.escape(vehicle_safe)}</pre>"
                    plain_fallback = (
                        "🏷NEW CLIENT❗️\n\n"
                        f"📋 Reference ID: <code>{ref_h}</code>\n"
                        f"{vehicle_pre}\n\n"
                        "Please use Krab Dispatch (@KrabIssuerBot) 📧🚘\n"
                        "Enter:\n"
                        "• Tag 🏷\n"
                        "• Phone 📞\n"
                        "• Delivery time ⏰\n"
                        "• Delivery address 📍\n"
                        "⸻\n"
                        "📋 Copy & paste below into the bot 🤖\n"
                        f"{_group_lead_copy_pre_html(phase1_data, encrypted_data.get('link') or '')}\n\n"
                        f"📅 Issue Date: {html.escape(issue_s, quote=False)}\n"
                        f"⏰ Expires: {html.escape(exp_s, quote=False)}"
                    )
                    await context.bot.send_message(
                        chat_id=group_chat_id, text=plain_fallback, parse_mode="HTML",
                    )
                logger.info("Lead sent to group '%s' successfully", group_name)
            except Exception as e:
                logger.error(
                    "Error sending to group '%s' (chat_id=%s): %r",
                    group_name,
                    group_chat_id,
                    e,
                )

    lead = db.get_lead_by_id(lead_id) or lead
    gn = _group_display_name_from_lead(lead) or (selected_group or {}).get("group_name", "N/A")
    # Supervisory "new lead" DMs go out when a driver accepts (see handle_accept_lead).
    db.record_bot_usage(user_id, username or "Unknown", lead_id, gn, driver_names)


def _cancel_contact_source_timeout_job(application, user_id: int, lead_id) -> None:
    """Remove scheduled auto-complete for lead source picker (user tapped a source)."""
    jq = application.job_queue if application else None
    if not jq:
        return
    name = f"contact_source_timeout_{user_id}_{lead_id}"
    getter = getattr(jq, "get_jobs_by_name", None)
    if callable(getter):
        try:
            for j in getter(name) or ():
                j.schedule_removal()
        except Exception:
            pass
        return
    try:
        for j in jq.jobs():
            if getattr(j, "name", None) == name:
                j.schedule_removal()
                break
    except Exception:
        pass


async def _contact_source_timeout_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """3 minutes without tapping a lead source: clear state silently."""
    job = context.job
    if not job or not job.data:
        return
    user_id = job.data.get("user_id")
    expected_lead_id = job.data.get("lead_id")
    if user_id is None or expected_lead_id is None:
        return
    st = db.get_user_state(user_id)
    if not st or st.get("state") != "select_contact_source":
        return
    data = st.get("data") or {}
    if str(data.get("lead_id")) != str(expected_lead_id):
        return
    db.clear_user_state(user_id)

    # One 📬 supervisory (Source: —) if a driver already accepted and source still empty
    try:
        lead_row = db.get_lead_by_id(str(expected_lead_id))
        if (
            lead_row
            and not (lead_row.get("contact_info_source") or "").strip()
            and db.get_lead_assignment_status(str(expected_lead_id))
        ):
            await _send_supervisory_new_lead_notices_from_lead(context, lead_row)
    except Exception as e:
        logger.warning("Supervisory new-lead after source timeout: %s", e)


def _should_defer_supervisory_until_source(lead: dict) -> bool:
    """When lead-source buttons are configured and this lead has no source yet — do not send supervisory on accept."""
    if not db.get_contact_info_sources():
        return False
    return not (lead.get("contact_info_source") or "").strip()


async def _send_supervisory_new_lead_notices_from_lead(
    context: ContextTypes.DEFAULT_TYPE,
    lead: dict,
) -> None:
    """Single supervisory 📬 block from current DB row (source line reflects contact_info_source)."""
    lid = str(lead.get("id") or "").strip()
    if not lid:
        return
    st = db.get_lead_assignment_status(lid)
    acc_name = "Driver"
    if st and st.get("driver_id"):
        did = st.get("driver_id")
        drow = next(
            (d for d in _get_all_drivers_cached() if str(d.get("id")) == str(did)),
            None,
        )
        if drow:
            acc_name = str(drow.get("driver_name") or "Driver")
    ref_sup = lead.get("reference_id") or "N/A"
    gn_sup = _group_display_name_from_lead(lead) or "N/A"
    issuer_un = lead.get("telegram_username") or "Unknown"
    await _send_supervisory_new_lead_notices(
        context,
        username=issuer_un,
        lead_id=lid,
        reference_id=str(ref_sup),
        driver_names=acc_name,
        group_name=gn_sup,
        driver_count=1,
    )


async def _send_supervisory_new_lead_notices(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    username: str,
    lead_id: str,
    reference_id: str,
    driver_names: str,
    group_name: str,
    driver_count: Optional[int] = None,
) -> None:
    """SUPERVISORY MESSAGE new-lead template to per-group + global supervisory + ST (not usage row)."""
    uname = username or "Unknown"
    lead_row = db.get_lead_by_id(lead_id)
    client_nm = _client_display_name_from_lead(lead_row) if lead_row else "—"
    src_raw = (lead_row.get("contact_info_source") or "").strip() if lead_row else ""
    lead = lead_row or {}
    group_row = None
    if lead and lead.get("group_id"):
        group_row = db.get_group_by_id(lead["group_id"])

    group_label = (group_name or "").strip() or "N/A"
    group_chat_id = group_row.get("group_telegram_id") if group_row else None
    group_display = _telegram_chat_link_html(group_chat_id, group_label)

    driver_display = html.escape((driver_names or "").strip() or "N/A", quote=False)
    st = db.get_lead_assignment_status(lead_id) if lead_id else None
    if st and st.get("driver_id"):
        drow = next(
            (d for d in _get_all_drivers_cached() if str(d.get("id")) == str(st.get("driver_id"))),
            None,
        )
        if drow:
            driver_display = _telegram_user_link_html(
                drow.get("telegram_id"),
                str(drow.get("driver_name") or driver_names or "Driver"),
            )

    issuer_display = _issuer_display_html_from_lead(lead) if lead else (
        html.escape(uname if uname.startswith("@") else f"@{uname}", quote=False)
        if uname and uname.lower() != "unknown"
        else "Unknown"
    )

    body_supervisory = _new_lead_supervisory_notice_text(
        reference_id,
        group_display,
        driver_display,
        issuer_display,
        client_name=client_nm,
        source_label=src_raw or None,
        include_lead_issuer=True,
        driver_count=driver_count,
    )
    body_st_only = _new_lead_supervisory_notice_text(
        reference_id,
        group_display,
        driver_display,
        issuer_display,
        client_name=client_nm,
        source_label=src_raw or None,
        include_lead_issuer=False,
        driver_count=driver_count,
    )
    sup_text_supervisory = _prefix_supervisory_html(body_supervisory)
    sup_text_st = _prefix_supervisory_html(body_st_only)
    sup_raw = group_row.get("supervisory_telegram_id") if group_row else None
    sup_targets = _supervisory_delivery_chat_ids(sup_raw)
    seen_norm: set = set()
    for sup_cid in sup_targets:
        nk = _norm_chat_id(sup_cid)
        if nk is not None:
            seen_norm.add(nk)
        try:
            await context.bot.send_message(
                chat_id=sup_cid,
                text=sup_text_supervisory,
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning("Could not send new-lead notice to supervisory chat %s: %s", sup_cid, e)
    st_raw = (db.get_setting("st_telegram_id") or "").strip()
    if st_raw:
        st_cid = _parse_chat_id(st_raw)
        stk = _norm_chat_id(st_cid) if st_cid is not None else None
        if st_cid is not None and stk is not None and stk not in seen_norm:
            try:
                await context.bot.send_message(
                    chat_id=st_cid,
                    text=sup_text_st,
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.warning("Could not send new-lead notice to ST chat %s: %s", st_cid, e)


async def _issuer_lead_success_and_motivation(
    message,
    user_id: int,
    username: str,
    reference_id: str,
    driver_names: str,
    group_name: str,
) -> None:
    """End issuer flow after dispatch; summary DM goes when a driver accepts (not here)."""
    db.clear_user_state(user_id)


async def _finish_lead_send(
    context: ContextTypes.DEFAULT_TYPE,
    message,
    user_id: int,
    username: str,
    lead_id: str,
    reference_id: str,
    driver_names: str,
    group_name: str,
    contact_source_label: Optional[str] = None,
) -> None:
    """After lead source callback: save source, clear state immediately; Monday + follow-up in background."""
    lead = db.get_lead_by_id(lead_id)
    resolved_gn = _group_display_name_from_lead(lead)
    if resolved_gn:
        group_name = resolved_gn
    if contact_source_label and lead:
        db.update_lead(lead_id, {"contact_info_source": contact_source_label})
    db.clear_user_state(user_id)

    if contact_source_label and lead:
        lid = str(lead_id)
        label = contact_source_label
        lead_fresh = db.get_lead_by_id(lid) or lead
        if db.get_lead_assignment_status(lid):
            try:
                await _send_supervisory_new_lead_notices_from_lead(context, lead_fresh)
            except Exception as e:
                logger.warning("Supervisory after lead source: %s", e)

        async def _bg_contact_source_sync() -> None:
            try:
                l2 = db.get_lead_by_id(lid) or lead
                if not monday:
                    return
                for _ in range(40):
                    mid = l2.get("monday_item_id") if l2 else None
                    if mid:
                        break
                    await asyncio.sleep(0.05)
                    l2 = db.get_lead_by_id(lid) or l2
                monday_item_id = l2.get("monday_item_id") if l2 else None
                if monday_item_id:
                    try:
                        await asyncio.to_thread(
                            monday.update_item_contact_source,
                            int(monday_item_id),
                            label,
                        )
                    except Exception as e:
                        logger.error("Error updating Monday contact source: %s", e)
            except Exception as e:
                logger.warning("Background contact source sync failed: %s", e)

        asyncio.create_task(_bg_contact_source_sync())


async def _maybe_offer_insurance_card(
    context: ContextTypes.DEFAULT_TYPE,
    chat,
    *,
    lead_id: str,
    reference_id: str,
) -> bool:
    """Post a Yes/No prompt to send a NY FS-20 insurance-card PDF to the lead's
    email. Returns ``True`` when a prompt was sent.

    Quietly skips when the lead has no email or Resend is not configured.
    """
    lead = db.get_lead_by_id(lead_id) if lead_id else None
    if not lead:
        logger.warning(
            "Insurance card: lead %s not found; cannot offer card", lead_id
        )
        return False
    email = (lead.get("email") or "").strip()
    if not email:
        logger.info(
            "Insurance card: lead %s has no email; skipping offer", lead_id
        )
        return False
    # Don't re-offer if a card was already sent for this lead.
    if (lead.get("insurance_card_sent_at") or "").strip():
        return False
    # Leads that opted into insurance during review are served by the accept-time
    # ride-along (card issued together with the tag) — don't also show the manual button.
    if lead.get("wants_insurance"):
        return False
    # The client already told us their carrier — offering to issue them a policy is
    # noise on a lead that plainly does not need one.
    if _lead_already_insured(lead):
        logger.info("Insurance card: lead %s already carries insurance; no offer", lead_id)
        return False
    if not Config.is_resend_configured():
        logger.warning(
            "Insurance card: lead %s has email %s but RESEND_API_KEY/RESEND_FROM "
            "are not configured — prompt skipped",
            lead_id,
            email,
        )
        try:
            await chat.reply_text(
                "ℹ️ Email detected on this lead but insurance-card sending is not "
                "configured.\n\nSet <code>RESEND_API_KEY</code> and "
                "<code>RESEND_FROM</code> in the bot's environment to enable the "
                "NY FS-20 PDF email flow.",
                parse_mode="HTML",
            )
        except Exception:
            pass
        return False
    if not Config.is_portal_integration_configured():
        logger.warning(
            "Insurance card: lead %s — INTEGRATIONS_API_KEY not configured",
            lead_id,
        )
        try:
            await chat.reply_text(
                "ℹ️ Email detected but <b>portal integration</b> is not configured.\n\n"
                "Set <code>INTEGRATIONS_API_KEY</code> on the bot (same secret as "
                "TriStateCoverage Vercel) to enable insurance card + portal account flow.",
                parse_mode="HTML",
            )
        except Exception:
            pass
        return False
    # Telegram callback_data has a 64-byte limit; full UUID (36) + prefix fits.
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Yes, send card", callback_data=f"ins_card_yes_{lead_id}"),
            InlineKeyboardButton("❌ No, skip", callback_data=f"ins_card_no_{lead_id}"),
        ],
    ])
    safe_email = html.escape(email, quote=False)
    safe_ref = html.escape(str(reference_id or "N/A"), quote=False)
    try:
        await chat.reply_text(
            (
                "📧 <b>Email detected on this lead.</b>\n\n"
                f"📋 Reference: <code>{safe_ref}</code>\n"
                f"✉️ Send an <b>NY FS-20 insurance card</b> PDF to "
                f"<code>{safe_email}</code>?"
            ),
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        return True
    except Exception as e:
        logger.warning("Could not post insurance-card offer: %s", e)
        return False


def _lead_already_insured(lead: dict) -> bool:
    """True when the lead came in with a carrier (or a policy number) of its own."""
    try:
        phase1 = _phase1_from_stored_lead(lead or {}) or {}
    except Exception:
        phase1 = {}
    for key in ("insurance_company", "insurance_policy_number"):
        val = phase1.get(key) or (lead or {}).get(key) or ""
        # One definition, shared with the per-car rule. The literal tuple this
        # replaces compared case-SENSITIVELY and listed only five spellings, so
        # "none", "na", "null" and "unknown" all counted as INSURED — and a car
        # whose insurer field read "none" never got the policy it needed.
        if not _is_blank_field(val):
            return True
    return False


PORTAL_DEFAULT_PASSWORD = "Temp#A9"


def _generate_portal_password() -> str:
    """Fixed portal password for all new TriStateCoverage accounts."""
    return PORTAL_DEFAULT_PASSWORD


def _price_amount_str(price) -> str:
    """Just the number out of a price string: "$150 + toll" -> "150".

    Prices are written for people ("$150", "$150 + toll", "1,500"), and every
    numeric consumer — the premium maths, Monday's number column — needs the digits
    on their own."""
    m = re.search(r"\d[\d,]*(?:\.\d+)?", str(price or ""))
    return m.group(0).replace(",", "") if m else ""


def _parse_annual_premium(lead: dict) -> float:
    # The price is a human string — "$150", "$150 + toll". Take the NUMBER out of it
    # rather than stripping two characters and hoping: "+ toll" made float() raise
    # and the premium silently became 0.
    raw = _price_amount_str(lead.get("price"))
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


async def _build_and_send_insurance_card(
    lead: dict,
) -> tuple[bool, Optional[str], Optional[str], Optional[str], Optional[str], Optional[bytes]]:
    """Generate insurance card (NY FS-20 or NJ TEI), email, provision portal.

    Returns ``(ok, policy_number, error_message, portal_email, portal_password,
    pdf_bytes)``. ``pdf_bytes`` is the built NY FS-20 card (for dropping into chat)
    on success, else None (NJ path and all failures).
    """
    from utils import insurance_card as ic
    from utils import nj_card_api as nj
    from utils import resend_client as rc
    from utils import state_detection as sd
    from utils import tristatecoverage_api as tsc

    # Carries the built NY FS-20 PDF back to callers that want to drop it in chat.
    # Stays None on every failure path and for the NJ (remote, no local PDF) path.
    pdf_bytes = None

    email = (lead.get("email") or "").strip()
    if not email:
        return (False, None, "Lead has no email on file.", None, None, pdf_bytes)

    card_state = sd.detect_card_state(lead)

    raw_vehicle = (lead.get("vehicle_details") or "").splitlines()
    # vehicle_details layout (per parse_phase1_structured/_clean_vin_and_car):
    #   [name, address, city_state_zip, delivery_address, delivery_city_state_zip,
    #    vin, car, color, insurance_company, insurance_policy_number, extra_info]
    # Older / edited leads may omit lines — resolve VIN by scanning the full blob.
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
            "No valid 17-character VIN found on this lead (searched vehicle details, "
            "delivery details, and notes). Update the lead with the correct VIN and try again.",
            None,
            None,
            pdf_bytes,
        )

    car_raw, color = ic.infer_car_and_color_from_vehicle_lines(
        raw_vehicle, vin_clean=vin_clean
    )

    decoded = await asyncio.to_thread(ic.decode_vin_from_nhtsa, vin_clean)
    if decoded:
        vehicle_year = (decoded.get("modelYear") or "").strip()
        vehicle_make_full = (decoded.get("vehicleMake") or "").strip()
        vehicle_model = (decoded.get("vehicleModel") or "").strip()
    else:
        # Fall back to "car" line (e.g. "2020 Toyota Camry")
        parts = car_raw.split()
        vehicle_year = parts[0] if parts and parts[0].isdigit() else ""
        vehicle_make_full = parts[1] if len(parts) > 1 else ""
        vehicle_model = " ".join(parts[2:]) if len(parts) > 2 else ""

    vehicle_make_short = (
        re.sub(r"[^A-Za-z0-9]", "", vehicle_make_full).upper()[:5] or "MAKE"
    )
    if not (vehicle_year and vehicle_year.isdigit() and len(vehicle_year) == 4):
        vehicle_year = "0000"

    today = datetime.now(pytz.timezone("America/New_York")).date()
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

    if card_state == "NJ":
        if not Config.is_nj_configured():
            return (
                False,
                None,
                "NJ insurance card not configured (BARCODE_APP_BASE_URL).",
                None,
                None,
                pdf_bytes,
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
            email=email,
            first_name=rc.first_name_from_full(name),
            phone=(lead.get("phone_number") or "").strip() or None,
            annual_premium=_parse_annual_premium(lead) or None,
        )
        nj_result = await asyncio.to_thread(nj.send_nj_insurance_email, nj_payload)
        if not nj_result.ok:
            err = nj_result.error or "NJ card API failed."
            if nj_result.status_code:
                err = f"{err} (HTTP {nj_result.status_code})"
            return (False, policy_number, err, None, None, pdf_bytes)
        return (
            True,
            nj_result.policy_number or policy_number,
            None,
            nj_result.email or email,
            None,
            pdf_bytes,
        )

    if not Config.is_portal_integration_configured():
        return (False, None, "Portal integration not configured (INTEGRATIONS_API_KEY).", None, None, pdf_bytes)
    if not Config.is_resend_configured():
        return (False, None, "Email not configured (RESEND_API_KEY and RESEND_FROM).", None, None, pdf_bytes)

    issuer = ic.CardIssuer(
        carrier_name=Config.INSURANCE_CARRIER_NAME,
        agency_phone=Config.INSURANCE_ISSUER_PHONE,
        agency_name=Config.INSURANCE_ISSUER_NAME,
        agency_address_lines=[ln.strip() for ln in (Config.INSURANCE_ISSUER_ADDRESS or "").split("|") if ln.strip()],
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
        logger.exception("Failed to build FS-20 PDF for lead %s: %s", lead.get("id"), e)
        return (False, policy_number, f"Could not build insurance card PDF: {e}", None, None, None)

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

    portal_result = await asyncio.to_thread(
        tsc.create_portal_client,
        portal_payload,
        pdf_bytes,
    )
    # The portal keeps an existing account's password rather than taking ours, and
    # says so. Recorded on the lead dict the caller already holds: threading a
    # seventh element through eleven return sites would be far more disruptive than
    # this one documented out-parameter, and the caller reads it straight back.
    try:
        lead["portal_password_unchanged"] = bool(
            (portal_result.payload or {}).get("passwordUnchanged")
            or (portal_result.payload or {}).get("added") == "vehicle"
            or (portal_result.payload or {}).get("alreadyExisted")
        )
    except Exception:
        lead["portal_password_unchanged"] = False

    if not portal_result.ok:
        err = portal_result.error or "Portal create failed."
        return (
            False,
            policy_number,
            f"Portal create failed ({portal_result.status_code}): {err}",
            None,
            None,
            pdf_bytes,
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
        return (
            False,
            policy_number,
            send_result.error or "Resend send failed.",
            email,
            portal_password,
            pdf_bytes,
        )
    return (True, policy_number, None, email, portal_password, pdf_bytes)


async def _drop_insurance_pdf_in_chat(context, chat_id, pdf_bytes, policy_number, *, caption=None) -> None:
    """Post the generated NY FS-20 insurance PDF into a Telegram chat (best-effort)."""
    if not pdf_bytes or not chat_id:
        return
    try:
        bio = io.BytesIO(pdf_bytes)
        bio.name = f"insurance-id-card-{policy_number or 'card'}.pdf"
        await context.bot.send_document(
            chat_id=chat_id,
            document=InputFile(bio, filename=bio.name),
            caption=caption or "🛡 Insurance card (also emailed to the client).",
        )
    except Exception as e:
        logger.warning("Could not drop insurance PDF in chat %s: %s", chat_id, e)


async def handle_insurance_card_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Top-level callback handler for the post-dispatch insurance card prompt.

    Pattern: ``ins_card_(yes|no)_<short_lead_id>``. Runs OUTSIDE the
    ConversationHandler so it works after ``ConversationHandler.END``.
    """
    query = update.callback_query
    if not query:
        return
    try:
        await query.answer()
    except Exception:
        pass
    raw = query.data or ""
    if raw.startswith("ins_card_yes_"):
        decision = "yes"
        lead_id = raw[len("ins_card_yes_"):]
    elif raw.startswith("ins_card_no_"):
        decision = "no"
        lead_id = raw[len("ins_card_no_"):]
    else:
        return
    if not lead_id:
        return
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    lead = db.get_lead_by_id(lead_id)
    if not lead:
        try:
            await query.message.reply_text("❌ Could not find this lead to issue the card.")
        except Exception:
            pass
        return

    if decision == "no":
        try:
            await query.message.reply_text("👍 Skipped insurance card email.")
        except Exception:
            pass
        return

    # Idempotency: if a card was already issued (e.g. by the accept-time ride-along),
    # don't issue a second card / portal / email from a stale button tap.
    if (lead.get("insurance_card_sent_at") or "").strip():
        try:
            await query.message.reply_text("✅ Insurance card was already issued for this lead.")
        except Exception:
            pass
        return

    email = (lead.get("email") or "").strip()
    safe_email = html.escape(email or "—", quote=False)
    try:
        await query.message.reply_text(
            f"⏳ Building NY FS-20 insurance card and emailing <code>{safe_email}</code>…",
            parse_mode="HTML",
        )
    except Exception:
        pass

    ok, policy_number, err, portal_email, portal_password, pdf_bytes = await _build_and_send_insurance_card(lead)
    update_payload: dict = {}
    if ok:
        update_payload = {
            "insurance_card_policy_number": policy_number,
            "insurance_card_sent_to_email": email,
            "insurance_card_sent_at": datetime.now(pytz.timezone("America/New_York")).isoformat(),
            "insurance_card_error": None,
            "portal_email": portal_email or email,
            "portal_password": portal_password,
        }
    else:
        update_payload = {
            "insurance_card_policy_number": policy_number,
            "insurance_card_sent_to_email": email,
            "insurance_card_error": (err or "Unknown error")[:500],
        }
        if portal_email and portal_password:
            update_payload["portal_email"] = portal_email
            update_payload["portal_password"] = portal_password
    try:
        db.update_lead(lead["id"], update_payload)
    except Exception as e:
        logger.warning("Could not update insurance_card_* fields on lead %s: %s", lead.get("id"), e)

    if ok:
        await _drop_insurance_pdf_in_chat(
            context, query.message.chat_id, pdf_bytes, policy_number,
        )
        safe_policy = html.escape(policy_number or "—", quote=False)
        safe_portal_email = html.escape(portal_email or email or "—", quote=False)
        safe_portal_pw = html.escape(portal_password or "—", quote=False)
        try:
            safe_portal_url = html.escape(
                (Config.TRISTATECOVERAGE_API_BASE or "https://tristatecoverage.com").rstrip("/") + "/login",
                quote=False,
            )
            await query.message.reply_text(
                "✅ <b>Insurance card sent</b>\n\n"
                f"📋 Policy: <code>{safe_policy}</code>\n"
                f"📧 Delivered to <code>{safe_email}</code>\n\n"
                "🔐 <b>Portal login</b>\n"
                f"🌐 <code>{safe_portal_url}</code>\n"
                f"✉️ Email: <code>{safe_portal_email}</code>\n"
                f"🔑 Password: <code>{safe_portal_pw}</code>",
                parse_mode="HTML",
            )
        except Exception:
            pass
    else:
        safe_err = html.escape(err or "Unknown error", quote=False)
        try:
            await query.message.reply_text(
                "❌ <b>Could not send insurance card</b>\n\n"
                f"{safe_err}",
                parse_mode="HTML",
            )
        except Exception:
            pass


async def handle_contact_source_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle contact info source selection after lead was sent to drivers."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    username = query.from_user.username or "Unknown"
    raw = query.data.replace("contact_source_", "")
    source_id = raw.strip()
    state = db.get_user_state(user_id)
    if not state or state.get("state") != "select_contact_source":
        await query.message.reply_text("❌ Session expired. Use /start to begin again.")
        db.clear_user_state(user_id)
        return ConversationHandler.END
    data = state.get("data") or {}
    lead_id = data.get("lead_id")
    reference_id = data.get("reference_id", "")
    driver_names = data.get("driver_names", "")
    group_name = data.get("group_name", "N/A")
    lead_row = db.get_lead_by_id(lead_id) if lead_id else None
    gn_from_db = _group_display_name_from_lead(lead_row)
    if gn_from_db:
        group_name = gn_from_db
    source = db.get_contact_info_source_by_id(source_id)
    label = source.get("label", "") if source else ""
    _cancel_contact_source_timeout_job(context.application, user_id, lead_id)
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except BadRequest:
        pass
    await _finish_lead_send(
        context, query.message, user_id, username, lead_id, reference_id,
        driver_names, group_name, contact_source_label=label or None,
    )
    ref_h = html.escape(str(reference_id or "N/A"), quote=False)
    lbl_h = html.escape((label or "").strip() or "—", quote=False)
    q = html.escape(motivation.get_random_quote(), quote=False)
    await query.message.reply_text(
        "✅ <b>Lead sent successfully</b>\n\n"
        f"📋 Reference: <code>{ref_h}</code>\n"
        f"📊 Source: {lbl_h}\n\n"
        f"💡 <i>{q}</i>\n\n"
        "➕ Send another lead: /lead or /client",
        parse_mode="HTML",
    )
    if lead_id:
        await _maybe_offer_insurance_card(
            context, query.message, lead_id=lead_id, reference_id=reference_id,
        )
    return ConversationHandler.END


async def cancel_from_lead_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Lead flow /cancel — identical to /restart: wipe everything and open a fresh
    review card, so the two commands never behave differently."""
    if not update.effective_message:
        return ConversationHandler.END
    return await _do_cancel_or_restart(update, context, "cancel")


async def cancel_from_receipt_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receipt-upload /cancel: full restart; always end receipt ConversationHandler."""
    msg = update.effective_message
    if not msg:
        return ConversationHandler.END
    user_id = update.effective_user.id
    _clear_lead_conversation_user_data(context)
    db.clear_user_state(user_id)
    await msg.reply_text("❌ Cancelled — restarting from the top.")
    await _restart_bot_from_top(update, context)
    return ConversationHandler.END


async def _handle_resend_to_drivers(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    lead_data: dict, callback_data: str, user_id: int,
) -> int:
    """Resend lead to newly selected drivers after timeout."""
    lead_id = lead_data.get("lead_id")
    lead = _lead_for_resend(lead_id) if lead_id else None
    ok, err = _validate_lead_row_for_resend(lead, issuer_user_id=user_id)
    if not ok:
        await update.callback_query.message.reply_text(f"❌ {err} Use /start if this persists.")
        db.clear_user_state(user_id)
        return ConversationHandler.END

    reference_id = lead.get("reference_id") or lead_data.get("reference_id", "N/A")
    group_id = lead.get("group_id")
    selected_group = db.get_group_by_id(group_id) if group_id else None
    if not selected_group:
        await update.callback_query.message.reply_text("❌ Group not found for this lead. Contact admin.")
        db.clear_user_state(user_id)
        return ConversationHandler.END

    all_drivers = _get_all_drivers_cached()
    active_drivers = [d for d in all_drivers if record_is_active(d)]
    suspended = _get_suspended_driver_ids()
    if callback_data == "select_driver_all":
        selected_drivers = [d for d in active_drivers if str(d.get("id")) not in suspended]
    else:
        driver_id = callback_data.replace("select_driver_", "")
        selected_drivers = [d for d in active_drivers if str(d.get("id")) == str(driver_id)]
        if not selected_drivers:
            await update.callback_query.message.reply_text("❌ Driver not found.")
            return STATE_SELECT_DRIVER

    driver_request_message = _build_driver_resend_request_message(lead)
    accept_keyboard = _keyboard_lead_accept_decline(str(lead_id))
    assigned_count = 0
    for driver in selected_drivers:
        tid = driver.get("driver_telegram_id")
        if not tid:
            continue
        try:
            driver_chat_id = int(str(tid).strip())
        except (ValueError, TypeError):
            driver_chat_id = tid
        try:
            db.create_lead_assignment(lead_id, driver["id"], group_id)
            try:
                await context.bot.send_message(
                    chat_id=driver_chat_id,
                    text=driver_request_message,
                    parse_mode="Markdown",
                    reply_markup=accept_keyboard,
                )
            except BadRequest as e:
                if "parse" in str(e).lower():
                    await context.bot.send_message(
                        chat_id=driver_chat_id,
                        text=driver_request_message.replace("`", ""),
                        reply_markup=accept_keyboard,
                    )
                else:
                    raise
            assigned_count += 1
            pending = db.get_driver_pending_receipts(driver["id"])
            if pending and len(pending) < SUSPENSION_THRESHOLD:
                ref_buttons = [
                    [InlineKeyboardButton(f"📤 Upload {p['reference_id']}", callback_data=f"receipt_for_{p['reference_id']}")]
                    for p in pending
                ]
                try:
                    await context.bot.send_message(
                        chat_id=driver_chat_id,
                        text=(
                            f"⚠️ You owe **{len(pending)}** receipt(s). "
                            f"At **{SUSPENSION_THRESHOLD}** unpaid you will be **temporarily suspended**.\n\n"
                            "To view all receipts type /receipts"
                        ),
                        parse_mode="Markdown",
                        reply_markup=_keyboard_receipt_plus_rows(ref_buttons),
                    )
                except BadRequest:
                    await context.bot.send_message(
                        chat_id=driver_chat_id,
                        text=(
                            f"⚠️ You owe {len(pending)} receipt(s). "
                            f"At {SUSPENSION_THRESHOLD} unpaid you will be temporarily suspended.\n\n"
                            "To view all receipts type /receipts"
                        ),
                        reply_markup=_keyboard_receipt_plus_rows(ref_buttons),
                    )
        except Exception as e:
            logger.error("Resend to driver %s: %s", driver.get("driver_name"), e)

    driver_names = ", ".join(d.get("driver_name", "?") for d in selected_drivers)
    group_telegram_id = selected_group.get("group_telegram_id")
    if group_telegram_id and assigned_count > 0:
        try:
            gcid = _parse_chat_id(group_telegram_id)
            await context.bot.send_message(
                chat_id=gcid,
                text=f"🔄 Reference ID `{reference_id}`: Reassigned to driver(s) **{driver_names}**",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.warning("Group reassign notify: %s", e)

    if assigned_count == 0:
        ref_h = html.escape(str(reference_id or "N/A"), quote=False)
        await update.callback_query.message.reply_text(
            "⚠️ **No driver received** the Telegram message (missing chat ID or blocked). "
            "Drivers must open a private chat with the bot and tap **Start**.\n\n"
            f"📋 Reference ID: <code>{ref_h}</code>\n\n"
            "Try **Pick new driver** again or contact admin.",
            parse_mode="HTML",
        )
        return STATE_SELECT_DRIVER

    await update.callback_query.message.reply_text(
        f"✅ **Lead resent successfully**\n\n"
        f"Reference ID: `{reference_id}`\n"
        f"Sent to driver(s): **{driver_names}**\n\n"
        "Use /start to create another lead.",
        parse_mode="Markdown",
    )
    db.clear_user_state(user_id)
    return ConversationHandler.END


# Driver assignment handlers
async def handle_resend_driver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle 'Pick new driver' after timeout – show driver picker for resend."""
    query = update.callback_query
    await query.answer()
    lead_id = query.data.replace("resend_driver_", "").strip()
    user_id = query.from_user.id
    lead = _lead_for_resend(lead_id) if lead_id else None
    ok, err = _validate_lead_row_for_resend(lead, issuer_user_id=user_id)
    if not ok:
        await query.message.reply_text(f"❌ {err} Use /start to create a new lead.")
        return ConversationHandler.END
    group_id = lead.get("group_id")
    selected_group = db.get_group_by_id(group_id)
    if not selected_group:
        await query.message.reply_text("❌ Group not found. Use /start to create a new lead.")
        return ConversationHandler.END
    reference_id = lead.get("reference_id") or "N/A"
    resend_data = {
        "lead_id": lead_id,
        "reference_id": reference_id,
        "group_id": group_id,
        "selected_group": selected_group,
        "resend": True,
    }
    db.set_user_state(user_id, "select_driver", resend_data)
    drivers = _get_all_drivers_cached()
    active_drivers = [d for d in drivers if record_is_active(d)]
    if not active_drivers:
        await query.message.reply_text("❌ No active drivers found. Please contact admin.")
        return ConversationHandler.END
    driver_keyboard = _build_driver_keyboard(active_drivers, exclude_suspended=True, include_all=True)
    await query.message.reply_text(
        f"🔄 **Pick new driver**\n\n"
        f"Reference ID: `{reference_id}`\n\n"
        f"Select which driver(s) to notify:",
        parse_mode="Markdown",
        reply_markup=driver_keyboard,
    )
    return STATE_SELECT_DRIVER


def _store_issuer_await_group_accept(
    user_id: int,
    *,
    lead_id: str,
    await_mode: str,
    pick_payload: dict | None = None,
    selected_driver_ids: list | None = None,
) -> None:
    """Issuer must wait for a team Accept before driver selection or deferred dispatch."""
    data: dict = {"lead_id": str(lead_id), "await_mode": await_mode}
    if await_mode == "dispatch_pending":
        data["selected_driver_ids"] = [str(x) for x in (selected_driver_ids or []) if str(x).strip()]
    elif pick_payload is not None:
        data["pick_payload"] = pick_payload
    db.set_user_state(user_id, USER_STATE_AWAIT_GROUP_ACCEPT, data)


async def _issuer_open_driver_selection_after_group_accept(
    context: ContextTypes.DEFAULT_TYPE,
    lead_id: str,
    lead_row: dict,
) -> None:
    """After a team taps Accept: notify the lead creator to pick drivers (or run deferred dispatch)."""
    issuer_uid = int(lead_row.get("user_id") or 0)
    if not issuer_uid:
        return
    st = db.get_user_state(issuer_uid)
    if not st or st.get("state") != USER_STATE_AWAIT_GROUP_ACCEPT:
        return
    inner = (st.get("data") or {}).copy()
    if str(inner.get("lead_id") or "") != str(lead_id):
        return
    mode = (inner.get("await_mode") or "pick_drivers").strip()
    lead_ref = db.get_lead_by_id(lead_id) or lead_row
    win_gid = lead_ref.get("group_id")
    winner_group = db.get_group_by_id(str(win_gid)) if win_gid else None

    if mode == "dispatch_pending":
        # Kept for leads created before drivers were told up-front, and for the
        # broadcast path that still defers. A lead already dispatched leaves no
        # dispatch_pending state behind, so this cannot double-send.
        db.clear_user_state(issuer_uid)
        raw_ids = inner.get("selected_driver_ids") or []
        id_set = {str(x).strip() for x in raw_ids if str(x).strip()}
        all_drivers = _get_all_drivers_cached()
        selected_drivers = [d for d in all_drivers if str(d.get("id")) in id_set]
        if not selected_drivers and not raw_ids:
            # Nothing was ever picked — the pool is the right answer. A pick that
            # merely resolved to nothing is NOT widened: that turned a one-driver
            # lead into a broadcast.
            fallback_ids = _resolve_dispatch_driver_ids(
                {"selected_driver_ids": []},
                group_id=str((winner_group or {}).get("id") or ""),
                is_all_groups=bool(db.get_group_lead_offers(str(lead_id))),
            )
            id_set = {str(x).strip() for x in fallback_ids if str(x).strip()}
            selected_drivers = [d for d in all_drivers if str(d.get("id")) in id_set]
        if not selected_drivers:
            try:
                ref_h = html.escape(str(lead_ref.get("reference_id") or "N/A"), quote=False)
                await context.bot.send_message(
                    chat_id=issuer_uid,
                    text=(
                        "⚠️ A team **accepted** your lead, but no drivers matched your earlier selection.\n\n"
                        f"📋 Reference: <code>{ref_h}</code>\n\n"
                        "Use /lead or contact admin."
                    ),
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.warning("issuer notify dispatch_pending empty drivers: %s", e)
            return

        phase1_data = _phase1_from_stored_lead(lead_ref)
        phase1_data["vehicle_details"] = "\n".join([
            phase1_data.get("name", "-"),
            phase1_data.get("address", "-"),
            phase1_data.get("city_state_zip", "-"),
            phase1_data.get("delivery_address", "-"),
            phase1_data.get("delivery_city_state_zip", "-"),
            phase1_data.get("vin", "-"),
            phase1_data.get("car", "-"),
            phase1_data.get("color", "-"),
            phase1_data.get("insurance_company", "-"),
            phase1_data.get("insurance_policy_number", "-"),
            phase1_data.get("extra_info", "-"),
        ])
        phone_number = lead_ref.get("phone_number")
        price = lead_ref.get("price")
        encrypted_data = {
            "secret_key": lead_ref.get("onetimesecret_token"),
            "metadata_key": lead_ref.get("onetimesecret_secret_key"),
            "link": lead_ref.get("encrypted_link"),
        }
        reference_id = lead_ref.get("reference_id")
        if not winner_group:
            logger.warning("dispatch_pending: no winner_group for lead %s", lead_id)
            return

        lead_data = _issuer_state_data_from_lead(lead_ref)
        att = lead_ref.get("phase1_attached_files")
        if isinstance(att, list) and att:
            lead_data["attached_files"] = att

        def _safe(s: str) -> str:
            return _sanitize_phones_for_send(s or "") or "-"

        vin_only = (phase1_data.get("vin") or "").strip() or "-"
        car_only = (phase1_data.get("car") or "").strip() or "-"
        name_line_safe = _safe(phase1_data.get("name"))
        vehicle_lines_display = [
            _safe(phase1_data.get("address")),
            _safe(phase1_data.get("city_state_zip")),
            vin_only,
            car_only,
            _safe(phase1_data.get("color")),
            _safe(phase1_data.get("insurance_company")),
            (phase1_data.get("insurance_policy_number") or "").strip() or "-",
            _safe(phase1_data.get("extra_info")),
        ]
        issuer_note_disp = (lead_data.get("special_request_issuers") or "").strip()
        if issuer_note_disp:
            vehicle_lines_display.append(_safe("📝 " + issuer_note_disp))
        else:
            vehicle_lines_display.append("📝 No")
        vehicle_safe = f"🚗 Vehicle: {name_line_safe}\n" + "\n".join(vehicle_lines_display)
        extra_safe = _sanitize_phones_for_send(phase1_data.get("extra_info", "") or "")
        driver_names = ", ".join(d.get("driver_name", "?") for d in selected_drivers)
        username = (lead_ref.get("telegram_username") or "Unknown").strip() or "Unknown"

        def _bg_task_done(t: asyncio.Task) -> None:
            try:
                exc = t.exception()
            except asyncio.CancelledError:
                return
            if exc:
                logger.error("Background lead dispatch failed: %s", exc, exc_info=exc)

        asyncio.create_task(
            _background_dispatch_lead_after_driver_pick(
                context,
                issuer_notify_chat_id=issuer_uid,
                user_id=issuer_uid,
                username=username,
                lead=lead_ref,
                lead_data=lead_data,
                phase1_data=phase1_data,
                selected_drivers=selected_drivers,
                selected_group=winner_group,
                skip_duplicate_full_group_post=True,
                phone_number=phone_number,
                price=price,
                encrypted_data=encrypted_data,
                reference_id=reference_id,
                issuer_note_disp=issuer_note_disp,
                driver_note_disp=(lead_data.get("special_request_drivers") or "").strip(),
                group_id=winner_group.get("id"),
                vehicle_safe=vehicle_safe,
                extra_safe=extra_safe,
                driver_names=driver_names,
            )
        ).add_done_callback(_bg_task_done)

        # Source is already chosen in the main lead flow; do not prompt here.
        lead_fresh = db.get_lead_by_id(lead_ref["id"]) if lead_ref and lead_ref.get("id") else None
        existing_source = (lead_fresh.get("contact_info_source") or "").strip() if lead_fresh else ""
        preselected_source = (lead_data.get("selected_source_label") or "").strip()
        if not existing_source and preselected_source:
            try:
                db.update_lead(lead_ref["id"], {"contact_info_source": preselected_source})
            except Exception as e:
                logger.warning(
                    "Could not persist preselected contact source on delayed dispatch lead %s: %s",
                    lead_ref.get("id"),
                    e,
                )
        db.clear_user_state(issuer_uid)
        return

    pick_payload = inner.get("pick_payload")
    if not isinstance(pick_payload, dict):
        db.clear_user_state(issuer_uid)
        return
    db.set_user_state(issuer_uid, "select_driver", pick_payload)
    drivers = _get_all_drivers_cached()
    active_drivers = [d for d in drivers if record_is_active(d)]
    if not active_drivers:
        try:
            await context.bot.send_message(chat_id=issuer_uid, text="❌ No active drivers found. Please contact admin.")
        except Exception:
            pass
        return
    driver_keyboard = _build_driver_keyboard(drivers, exclude_suspended=True, include_all=True)
    ref_h = html.escape(str(lead_ref.get("reference_id") or "N/A"), quote=False)
    gname = html.escape((winner_group or {}).get("group_name") or "group", quote=False)
    try:
        await context.bot.send_message(
            chat_id=issuer_uid,
            text=(
                "✅ **A team accepted this lead.**\n\n"
                f"📋 Reference ID: <code>{ref_h}</code>\n"
                f"Accepted by <b>{gname}</b>.\n\n"
                "Select which driver(s) to notify:"
            ),
            parse_mode="HTML",
            reply_markup=driver_keyboard,
        )
    except Exception as e:
        logger.warning("Could not DM issuer driver picker after group accept: %s", e)


# A driver answering the offer in words. Deliberately narrow: these are answers to
# a question that is on their screen, not general chat.
# Answering an offer that is ALREADY PROVEN OPEN for this driver. Read the note
# on handle_driver_word_answer before widening either of these: they are only
# this generous because the offer is confirmed first.
#
# Anchored at the start but NOT at the end, so a reason can ride along — a driver
# saying "no I'm in Newark till 6" is declining, and losing that to a blank lead
# form is the failure this exists to prevent.
_DRIVER_ACCEPT_RE = re.compile(
    r"^\s*(?:"
    r"accept(?:ed|ing)?|yes|yep|yeah|yup|ya|yea|y|ok|okay|k|kk|sure|"
    r"i'?l?l?\s*(?:take|grab|do|get|run|handle)\s*(?:it|this|that|the\s+\w+)?|"
    r"take\s+it|grab(?:bing)?\s+it|mine|got\s+it|on\s+it|i\s+got\s+(?:it|this)|"
    r"claim(?:ed|ing)?(?:\s+it)?|"
    r"on\s+my\s+way|omw|heading\s+(?:out|there)|going\s+now|"
    r"count\s+me\s+in|i'?m\s+(?:in|on\s+it|good|free|available|close|nearby)|"
    r"i\s+can\s+(?:take|do|grab|get)\s*(?:it|this|that)?|i'?ll\s+be\s+there|"
    r"10[\s-]?4|copy(?:\s+that)?|roger(?:\s+that)?|wilco|bet|say\s+less"
    r")\b[\s.!,]*"
    # Emoji sit OUTSIDE the \b group: a word boundary needs a word character
    # beside it, and an emoji is not one, so every emoji here used to be dead.
    r"|^\s*[\U0001f44d\U0001f44c\u2705\U0001f64b\U0001f919\U0001f4aa]+\s*[.!,]*$",
    re.IGNORECASE,
)
# Words that defer or condition an answer. An accept carrying one of these is
# not an acceptance of THIS offer, now — "accept the lead tomorrow", "yes but
# after this one", "ok if nobody else takes it". Declines are unaffected: a
# decline is already a no whatever the reason attached to it.
_DRIVER_QUALIFIER_RE = re.compile(
    r"\b(?:tomorrow|later|tonight|afterwards?|after|next\s+(?:week|one|run)|"
    r"in\s+(?:a|an|\d+)\s*\w*|if|unless|but|maybe|might|probably|possibly|"
    r"when|once|depend(?:s|ing)?|not\s+(?:now|yet)|another\s+time)\b",
    re.IGNORECASE,
)
# Words that mean refusal on their own, so whatever follows is just the reason.
_DRIVER_DECLINE_STRONG = (
    r"decline[ds]?|pass|skip|"
    r"can'?t|cannot|can\s+not|won'?t|unable|"
    r"not\s+(?:me|able|today|now|free|available|interested)|"
    r"i'?m\s+(?:busy|out|off|booked|tied\s+up|far|away|not\s+\w+)|"
    r"too\s+far|different\s+driver|someone\s+else|give\s+it\s+to\s+\w+"
)
# A bare "no" is just the word "no", and it starts plenty of sentences that are
# not answers — "no answer at the door", "no one is home", "no parking here".
# Accepted only when the message ENDS there, or when what follows reads like a
# reason rather than a noun.
_DRIVER_DECLINE_BARE = r"no|nope|nah|naw|negative"
_DRIVER_REASON_HEAD = (
    r"i|i'?m|im|we|it'?s|its|not|too|sorry|just|cant|can'?t|wont|won'?t|"
    r"unable|busy|far|out|off|booked|thanks|sir|man|bro"
)
_DRIVER_DECLINE_RE = re.compile(
    rf"^\s*(?:{_DRIVER_DECLINE_STRONG})\b[\s.!,]*"
    rf"|^\s*(?:{_DRIVER_DECLINE_BARE})\b\s*[.!]*\s*$"
    rf"|^\s*(?:{_DRIVER_DECLINE_BARE})\b\s*[,;-]+\s*\S"
    rf"|^\s*(?:{_DRIVER_DECLINE_BARE})\s+(?:{_DRIVER_REASON_HEAD})\b"
    rf"|^\s*[\u274c\U0001f645\U0001f44e\U0001f6ab]+\s*[.!,]*$",
    re.IGNORECASE,
)


async def handle_driver_word_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """"accept" / "yes" / "on my way" (or "no", with a reason) from a driver.

    Runs the button's own handler through _TypedAsTap rather than repeating the
    acceptance rules, so typing and tapping can never behave differently. Anything
    else — or no open offer — passes straight through untouched.

    THE ORDER HERE IS THE DESIGN. Whether an offer is open is settled BEFORE the
    text is read, which is what lets the vocabulary be generous: "bet", "10-4",
    "on my way" and a bare 👍 are unmistakable from a driver who is looking at an
    offer this second, and would be reckless to claim from anyone else.

    Getting this wrong is expensive in one direction only. Unrecognised text from
    a driver falls through to handle_idle_lead_start, so a miss does not just fail
    to accept — it loses the offer and opens a blank lead form at them.
    """
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return
    text = ((msg.text or "") or (msg.caption or "")).strip()
    if not text:
        return
    if getattr(msg, "chat", None) is not None and msg.chat.type != "private":
        return
    try:
        driver = await asyncio.to_thread(_driver_row_for_telegram_user, user.id)
    except Exception:
        driver = None
    if not driver:
        return                                  # not a driver — leave the text alone
    try:
        pending = await asyncio.to_thread(
            db.get_driver_pending_assignment, str(driver.get("id")))
    except Exception as e:
        logger.warning("driver word answer: pending lookup failed: %s", e)
        return
    lead_id = str((pending or {}).get("lead_id") or "").strip()
    if not lead_id:
        return                                  # nothing open — could be anything
    # Only now, with an offer proven open on this driver, read what they said.
    accept = bool(_DRIVER_ACCEPT_RE.match(text)) and not _DRIVER_QUALIFIER_RE.search(text)
    decline = bool(_DRIVER_DECLINE_RE.match(text))
    if accept and decline:
        return                                  # ambiguous — let them tap
    if not accept and not decline:
        return
    logger.info("driver %s answered %r -> %s", driver.get("driver_name"),
                text[:40], "accept" if accept else "decline")
    prefix = "accept_lead_" if accept else "decline_lead_"
    handler = handle_accept_lead if accept else handle_decline_lead
    await handler(_TypedAsTap(update, f"{prefix}{lead_id}"), context)
    raise ApplicationHandlerStop


async def handle_accept_lead(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle driver accepting a lead."""
    query = update.callback_query
    await query.answer()
    
    # Extract lead_id from callback_data (format: "accept_lead_{lead_id}")
    lead_id = query.data.replace("accept_lead_", "")
    
    driver = _driver_row_for_telegram_user(query.from_user.id)
    if not driver:
        await query.message.reply_text(
            "❌ Error: Driver not found in system.",
            reply_markup=_driver_add_lead_keyboard_only(),
        )
        return

    lead = db.get_lead_by_id(lead_id)
    if not lead:
        await query.message.edit_text(
            "❌ Error: Lead not found.",
            reply_markup=_EMPTY_INLINE_KB,
        )
        return

    accepted_row = db.accept_lead_assignment(lead_id, driver['id'])

    if not accepted_row:
        st = db.get_lead_assignment_status(lead_id)
        if st and st.get("status") == "accepted":
            await query.message.edit_text(
                "❌ Request Already Taken\n\n"
                "1. Turn on❗telegram notifications🔔\n"
                "2. Check ✅here ⏱️hourly\n"
                "3. Go the extra🛣️mile, post ads instead of doing nothing waiting ask us how.\n\n"
                "-Thank you 🙏\n"
                "🏁Automated🏎️Automotive",
                parse_mode="Markdown",
                reply_markup=_EMPTY_INLINE_KB,
            )
            return
        await query.message.edit_text(
            "❌ **Error accepting lead. Please try again.**",
            parse_mode="Markdown",
            reply_markup=_EMPTY_INLINE_KB,
        )
        return

    # Paper inventory (shared Paper Investigator tables): subtract one paper per accepted lead
    aid = accepted_row.get("id")
    ref = (lead.get("reference_id") or "") or ""
    new_paper_bal = db.apply_paper_on_lead_accept(str(driver["id"]), str(aid), str(ref))
    if new_paper_bal is not None and new_paper_bal < Config.LOW_PAPER_THRESHOLD:
        if not db.paper_was_low_alert_sent(driver["id"]):
            db.paper_mark_low_alert_sent(driver["id"])
            sup = Config.PAPER_SUPERVISOR_TELEGRAM_ID
            if sup:
                try:
                    dnm = driver.get("driver_name", "Driver")
                    await context.bot.send_message(
                        chat_id=int(sup),
                        text=(
                            f"🔴 Low paper: {dnm} has {new_paper_bal} paper(s) left.\n\n"
                            "Open the Paper Investigator bot (All Drivers) to approve resupply."
                        ),
                    )
                except Exception as e:
                    logger.warning("Could not notify paper supervisor (low paper): %s", e)

    # Get group info for forwarding
    group_id = lead.get('group_id')
    group = db.get_group_by_id(group_id) if group_id else None

    # Ledger: post the accepting driver onto this lead's PENDING row on
    # tristatetags.com/backend — no krab-sender involvement needed.
    try:
        from utils import ledger as _ledger
        if _ledger.is_configured():
            _accept_driver_name = str(driver.get("driver_name") or "").strip()
            _lead_for_ledger = dict(lead)

            async def _ledger_driver_update() -> None:
                try:
                    await asyncio.to_thread(
                        _ledger.record_driver_for_lead, _lead_for_ledger, _accept_driver_name
                    )
                except Exception as e:
                    logger.warning("Ledger driver update failed: %s", e)

            asyncio.create_task(_ledger_driver_update())
    except Exception as e:
        logger.warning("Ledger driver update setup failed: %s", e)

    # Monday driver column — off hot path (HTTP blocks other Telegram updates)
    raw_mid = lead.get("monday_item_id")
    if monday and raw_mid:
        try:
            _mid_int = int(raw_mid)
        except (TypeError, ValueError):
            _mid_int = None
        if _mid_int is not None:
            _dn = driver.get("driver_name", "")

            async def _monday_driver_update() -> None:
                try:
                    await asyncio.to_thread(monday.update_item_driver, _mid_int, _dn)
                except Exception as e:
                    logger.error("Error updating Monday.com driver column: %s", e)

            asyncio.create_task(_monday_driver_update())

    # The word "accept" reaches this via _TypedAsTap, where query.message is the
    # DRIVER's own message — editing someone else's message is a BadRequest, and it
    # landed here AFTER the acceptance was committed, aborting everything below it
    # (tracking gate, details, notices). Edit when we can, say it plainly when not.
    try:
        await query.message.edit_text(
            "✅ **You accepted this lead!**",
            parse_mode="Markdown",
            reply_markup=_EMPTY_INLINE_KB,
        )
    except Exception:
        try:
            await query.message.reply_text(
                "✅ **You accepted this lead!**", parse_mode="Markdown")
        except Exception as e:
            logger.warning("could not confirm the acceptance to the driver: %s", e)
    # Location gate: with tracking configured, the driver gets the tracking
    # link now and the full details only after their location ping arrives.
    await _start_tracking_gate_or_send_details(
        context,
        kind="lead",
        lead=lead,
        driver_id=str(driver.get("id")) if driver.get("id") else None,
        driver_name=driver.get("driver_name"),
        chat_id=query.message.chat_id,
    )

    # Issuer summary + supervisory "new lead" — before optional receipt strike / group posts so they always run
    lead = db.get_lead_by_id(lead_id) or lead
    acc_name = str(driver.get("driver_name") or "Driver")
    await _notify_initiator_lead_accepted_summary(
        context,
        lead,
        accepting_driver_name=acc_name,
    )
    # Supervisory 📬 once: on accept if source already set or no source picker; else after source tap or 3m timeout
    if not _should_defer_supervisory_until_source(lead):
        try:
            await _send_supervisory_new_lead_notices_from_lead(context, lead)
        except Exception as e:
            logger.error("Supervisory new-lead notice on accept failed: %s", e, exc_info=True)

    pending = db.get_driver_pending_receipts(driver["id"])
    if pending:
        ref_buttons = [
            [InlineKeyboardButton(f"📤 Upload {p['reference_id']}", callback_data=f"receipt_for_{p['reference_id']}")]
            for p in pending[:10]
        ]
        if len(pending) >= SUSPENSION_THRESHOLD:
            txt = (
                f"⛔ **You have been suspended**\n\n"
                f"Reason: You owe **{len(pending)}** receipt(s). "
                "You will not receive new leads until all outstanding receipts are uploaded.\n\n"
                "To view all receipts type /receipts"
            )
            driver_nm = driver.get("driver_name", "Unknown")
            ref_parts = []
            for p in pending:
                ref = (p.get("reference_id") or "").strip()
                if not ref or ref.upper() == "N/A":
                    continue
                ref_parts.append(_telegram_md1_escape(ref))
            refs_line = (
                f"\nReceipt references: {', '.join(ref_parts)}"
                if ref_parts
                else "\nReceipt references: (none on file)"
            )
            try:
                sup_txt = _prefix_supervisory_message(
                    f"⛔ **Driver Suspended**\n\n"
                    f"Driver: **{_telegram_md1_escape(driver_nm)}**\n"
                    f"Reason: {len(pending)} unpaid receipt(s)"
                    f"{refs_line}"
                )
                for sup_id in _global_supervisory_chat_ids():
                    try:
                        await context.bot.send_message(chat_id=sup_id, text=sup_txt, parse_mode="Markdown")
                    except BadRequest:
                        await context.bot.send_message(chat_id=sup_id, text=sup_txt.replace("*", ""))
            except Exception as e:
                logger.warning("Could not send suspension alert to supervisory: %s", e)
        else:
            txt = (
                f"⚠️ You owe **{len(pending)}** receipt(s).\n\n"
                f"At **{SUSPENSION_THRESHOLD}** unpaid you will be "
                "**temporarily suspended** from new leads.\n\n"
                "To view all receipts type /receipts"
            )
        try:
            await query.message.reply_text(
                txt,
                parse_mode="Markdown",
                reply_markup=_keyboard_receipt_plus_rows(ref_buttons),
            )
        except Exception as e:
            logger.warning("Could not send driver receipt-strike follow-up: %s", e)
    # Forward acceptance message to group chat only (not per-group / global supervisory — reduces duplicate spam).
    extra_safe = _sanitize_phones_for_send(lead.get("extra_info") or "")
    spec_grp = _lead_issuer_note(lead)
    acceptance_message = (
        "✅ **Lead Accepted**\n\n"
        f"🚗 Driver: {driver.get('driver_name', 'Unknown')}\n"
        f"📝 Extra info: {extra_safe}\n"
        f"📋 Reference ID: `{lead.get('reference_id', 'N/A')}`"
    )
    if spec_grp:
        acceptance_message += f"\n📝 Issuers note: {_sanitize_phones_for_send(spec_grp)}"
    if group:
        group_telegram_id = group.get("group_telegram_id")
        if group_telegram_id:
            try:
                await context.bot.send_message(
                    chat_id=group_telegram_id,
                    text=acceptance_message,
                    parse_mode="Markdown",
                )
            except Exception as e:
                logger.error(f"Error forwarding acceptance to group: {e}")

    # The tag itself. A team tapping ag_ sends it via _send_full_group_lead_to_chat;
    # a DRIVER tapping Accept used to send nothing at all, which on a manual lead
    # (team offer and driver offers posted together) meant whoever accepted first
    # decided whether a tag existed. Now either accept releases it, and it is still
    # released exactly once.
    try:
        already = None
        try:
            already = db.get_accepted_group_for_lead(lead_id)
        except Exception as e:
            # Unknown, not "no". Send: the failure this repairs is a MISSING tag,
            # and a duplicate is a nuisance where an absence is a stuck delivery.
            logger.warning("Could not check accepted group for %s, sending tag anyway: %s",
                           lead_id, e)
        if already:
            logger.info("Tag for %s already released by the accepting team; driver accept sends none.",
                        lead_id)
        else:
            tag_targets = [_parse_chat_id((group or {}).get("group_telegram_id"))]
            tag_targets = [c for c in tag_targets if c]
            if tag_targets:
                await _send_all_tag_pdfs(
                    context, lead, tag_targets,
                    accepted_by=driver.get("driver_name") or "driver",
                )
            else:
                logger.warning("Driver accepted %s but no group chat to send the tag to.", lead_id)
    except Exception as e:
        # Never abort the acceptance over the document: the renewal schedule and
        # the driver's own confirmation are below this and matter more.
        logger.error("Tag PDF on driver accept failed for %s: %s", lead_id, e)

    # Schedule 28-day renewal
    try:
        from datetime import datetime, timedelta, timezone as _tz
        renewal_due = datetime.now(_tz.utc) + timedelta(days=Config.RENEWAL_DAYS)
        existing_renewal = db.get_active_renewal_for_lead(lead_id)
        if not existing_renewal:
            renewal_group_id = group_id or db.resolve_renewal_group_id(lead_id, lead)
            db.schedule_renewal(
                lead_id=lead_id,
                group_id=renewal_group_id,
                driver_id=driver["id"],
                renewal_due_at=renewal_due.isoformat(),
            )
            logger.info("Renewal scheduled for lead %s in %d days", lead.get("reference_id", "?"), Config.RENEWAL_DAYS)
    except Exception as e:
        logger.warning("Could not schedule renewal: %s", e)


async def handle_decline_lead(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Driver chose *Different Driver* (same as pass/decline on assignment)."""
    query = update.callback_query
    await query.answer()
    
    # Extract lead_id from callback_data
    lead_id = query.data.replace("decline_lead_", "")
    
    driver = _driver_row_for_telegram_user(query.from_user.id)
    if not driver:
        await query.message.reply_text(
            "❌ Error: Driver not found in system.",
            reply_markup=_driver_add_lead_keyboard_only(),
        )
        return

    db.decline_lead_assignment(lead_id, driver['id'])
    
    await query.message.edit_text(
        "🔄 **Different driver**\n\n"
        "You passed on this lead.",
        parse_mode="Markdown",
        reply_markup=_EMPTY_INLINE_KB,
    )


async def handle_reassign_lead(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reassign an ACCEPTED lead: driver changed their mind, or dispatch pulls it.

    Allowed: the accepted driver, the lead's issuer (dispatcher), and global
    supervisors. Releases the assignment, cancels open tracking sessions, and
    re-offers the lead to the other drivers (issuer alerts fire again when the
    new driver accepts, via the normal accept flow).
    """
    query = update.callback_query
    await query.answer()
    lead_id = query.data.replace("reassign_lead_", "")
    lead = db.get_lead_by_id(lead_id)
    if not lead:
        await query.message.reply_text("❌ Lead not found or expired.")
        return
    ref = lead.get("reference_id", "N/A")

    assignment = db.get_lead_assignment_status(lead_id)
    if not assignment:
        await query.message.reply_text(
            f"ℹ️ `{ref}` has no accepted driver right now — nothing to reassign.",
            parse_mode="Markdown",
        )
        return
    old_driver = assignment.get("driver") or {}
    old_driver_id = assignment.get("driver_id")
    old_driver_name = old_driver.get("driver_name", "the driver")

    presser_id = update.effective_user.id
    is_the_driver = str(old_driver.get("driver_telegram_id") or "") == str(presser_id)
    is_issuer = str(lead.get("user_id") or "") == str(presser_id)
    if not (is_the_driver or is_issuer or _user_is_global_supervisor(presser_id)):
        await query.message.reply_text("⛔ Only the assigned driver, the dispatcher, or a supervisor can reassign.")
        return

    if not db.reopen_lead_for_reassign(lead_id, old_driver_id):
        await query.message.reply_text("❌ Could not reassign. Please try again.")
        return
    db.cancel_open_tracking_sessions_for_lead(lead_id)

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await query.message.reply_text(
        f"🔄 **Reassigning** `{ref}` — offering it to the other drivers now.",
        parse_mode="Markdown",
    )

    # Tell the old driver if someone else pulled it.
    if not is_the_driver:
        old_cid = _parse_chat_id(old_driver.get("driver_telegram_id"))
        if old_cid:
            try:
                await context.bot.send_message(
                    chat_id=old_cid,
                    text=(
                        f"🔄 Delivery `{ref}` was reassigned by dispatch — "
                        "you're no longer on it."
                    ),
                    parse_mode="Markdown",
                )
            except Exception as e:
                logger.warning("Could not notify old driver of reassign: %s", e)

    # Re-offer to the rest of the pool (same machinery as dispatch).
    group = db.get_group_by_id(lead.get("group_id")) if lead.get("group_id") else None
    assigned_count = 0
    if group:
        assigned_count, _names, reason, _scope = await _send_driver_requests_for_group(
            context, lead, group, exclude_driver_id=old_driver_id
        )
    else:
        offer_text = _driver_offer_message_text(lead)
        kb = _keyboard_lead_accept_decline(str(lead_id))
        suspended = _get_suspended_driver_ids()
        for d in _get_all_drivers_cached() or []:
            if not d or not record_is_active(d) or str(d.get("id")) in suspended:
                continue
            if old_driver_id and str(d.get("id")) == str(old_driver_id):
                continue
            cid = _parse_chat_id(d.get("driver_telegram_id"))
            if not cid:
                continue
            try:
                db.create_lead_assignment(lead_id, d["id"], lead.get("group_id"))
                await context.bot.send_message(
                    chat_id=cid, text=offer_text, parse_mode="Markdown", reply_markup=kb
                )
                assigned_count += 1
            except Exception as e:
                logger.error("Reassign offer to %s failed: %s", d.get("driver_name"), e)

    # Alert issuer + supervisors.
    presser_name = update.effective_user.full_name or "someone"
    note = (
        f"🔄 Lead `{ref}` was reassigned"
        + (f" by {old_driver_name}" if is_the_driver else f" by {presser_name}")
        + f" — re-offered to {assigned_count} driver(s)."
    )
    initiator_id = lead.get("user_id")
    if initiator_id and str(initiator_id) != str(presser_id):
        try:
            await context.bot.send_message(chat_id=int(initiator_id), text=note, parse_mode="Markdown")
        except Exception as e:
            logger.warning("Could not notify issuer of reassign: %s", e)
    for sup_id in _global_supervisory_chat_ids():
        if str(sup_id) == str(presser_id):
            continue
        try:
            await context.bot.send_message(chat_id=sup_id, text=note, parse_mode="Markdown")
        except Exception as e:
            logger.warning("Could not notify supervisor of reassign: %s", e)


async def handle_accept_group_offer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle a group member accepting a broadcast lead offer."""
    query = update.callback_query
    await query.answer()
    pair = _parse_paired_short_uuids(query.data, "ag_")
    if not pair:
        await query.message.reply_text("❌ Invalid request.")
        return
    short_lead, short_group = pair
    try:
        lead_id = _long_uuid(short_lead)
        group_id = _long_uuid(short_group)
    except (ValueError, Exception):
        await query.message.reply_text("❌ Invalid request.")
        return

    lead = db.get_lead_by_id(lead_id)
    group = db.get_group_by_id(group_id)
    if not lead or not group or not record_is_active(group):
        try:
            await query.message.edit_text(
                "❌ Offer not found or expired.",
                reply_markup=_EMPTY_INLINE_KB,
            )
        except Exception:
            pass
        return

    accepted = db.accept_group_lead_offer(lead_id, group_id, accepted_by_telegram_id=str(query.from_user.id))
    if not accepted:
        # Someone else already accepted — refresh every group's message so Accept is gone everywhere.
        accepted_row = db.get_accepted_group_for_lead(lead_id)
        win_gid = (accepted_row or {}).get("group_id")
        accepted_group = db.get_group_by_id(win_gid) if win_gid else None
        gname = accepted_group.get("group_name") if accepted_group else "another group"
        ref_show = lead.get("reference_id", "N/A")
        # "Accepted by" line should show the team member who actually tapped
        # the button (not the lead creator).
        acceptor_handle = (query.from_user.username or "").strip() or (
            query.from_user.full_name or "Unknown"
        )
        acceptor_esc = _telegram_md1_escape(acceptor_handle)
        for o in db.get_group_lead_offers(lead_id):
            ocid = _parse_chat_id(o.get("group_chat_id"))
            mid = o.get("group_message_id")
            ogid = o.get("group_id")
            if not ocid or not mid:
                continue
            try:
                if win_gid and str(ogid) == str(win_gid):
                    await context.bot.edit_message_text(
                        chat_id=ocid,
                        message_id=int(mid),
                        text=(
                            f"✅ **Accepted by {gname}**\n"
                            f"Issuer: @{acceptor_esc}\n"
                            f"Reference ID: `{ref_show}`"
                        ),
                        parse_mode="Markdown",
                        reply_markup=_EMPTY_INLINE_KB,
                    )
                else:
                    await context.bot.edit_message_text(
                        chat_id=ocid,
                        message_id=int(mid),
                        text=(
                            f"❌ **Taken by another group**\n\n"
                            f"Reference ID: `{ref_show}`"
                        ),
                        parse_mode="Markdown",
                        reply_markup=_EMPTY_INLINE_KB,
                    )
            except Exception as e:
                logger.warning("Could not refresh group offer message after late accept: %s", e)
        return

    # Set lead.group_id to winning group (single accepted group per lead — enforced in DB)
    db.update_lead(lead_id, {"group_id": group_id})
    lead = db.get_lead_by_id(lead_id) or lead
    acc_row = db.get_accepted_group_for_lead(lead_id)
    if not acc_row or str(acc_row.get("group_id")) != str(group_id):
        logger.error(
            "accept_group_offer: accepted offer row missing or mismatch (lead=%s group=%s row=%s)",
            lead_id,
            group_id,
            acc_row,
        )
    win_gid = str((acc_row or {}).get("group_id") or group_id).strip()
    winner_group = db.get_group_by_id(win_gid) or group
    if not lead or str(lead.get("group_id")) != str(win_gid):
        logger.error(
            "accept_group_offer: leads.group_id not set to winner (lead=%s expected=%s got=%s)",
            lead_id,
            win_gid,
            (lead or {}).get("group_id"),
        )

    reference_id = lead.get("reference_id", "N/A")
    winner_name = (winner_group.get("group_name") or "Group").strip() or "Group"
    # "Accepted by" line shows the team member who actually tapped Accept.
    acceptor_handle = (query.from_user.username or "").strip() or (
        query.from_user.full_name or "Unknown"
    )
    acceptor_esc = _telegram_md1_escape(acceptor_handle)
    accepted_by_label = f"{winner_name} (@{acceptor_handle})"

    # Update all group offer messages to reflect taken/accepted
    offers = db.get_group_lead_offers(lead_id)
    for o in offers:
        ocid = _parse_chat_id(o.get("group_chat_id"))
        mid = o.get("group_message_id")
        ogid = o.get("group_id")
        if not ocid or not mid:
            continue
        try:
            if str(ogid) == str(win_gid):
                await context.bot.edit_message_text(
                    chat_id=ocid,
                    message_id=int(mid),
                    text=(
                        f"✅ **Accepted by {winner_name}**\n"
                        f"Issuer: @{acceptor_esc}\n"
                        f"Reference ID: `{reference_id}`"
                    ),
                    parse_mode="Markdown",
                    reply_markup=_EMPTY_INLINE_KB,
                )
            else:
                await context.bot.edit_message_text(
                    chat_id=ocid,
                    message_id=int(mid),
                    text=(
                        f"❌ **Taken by another group**\n\n"
                        f"Reference ID: `{reference_id}`"
                    ),
                    parse_mode="Markdown",
                    reply_markup=_EMPTY_INLINE_KB,
                )
        except Exception as e:
            logger.warning("Could not edit group offer message: %s", e)

    lead_for_files = db.get_lead_by_id(lead_id) or lead
    att = lead_for_files.get("phase1_attached_files")
    if isinstance(att, list) and att:
        await _forward_phase1_attached_files_to_targets(
            context,
            att,
            winner_group.get("group_telegram_id"),
        )
        # Deliberately NOT cleared here any more: the driver who accepts next still
        # needs the same paperwork, and wiping it left them with nothing.

    # Lead adder: one summary DM when a driver accepts (handle_accept_lead), not on group tap.

    # If the sender already went through driver selection, do not DM drivers again.
    if db.lead_has_assignments(lead_id):
        try:
            lead_for_group = db.get_lead_by_id(lead_id) or lead
            if offers:
                await _send_full_group_lead_to_chat(
                    context,
                    winner_group,
                    lead_for_group,
                    html_prefix=(
                        "<b>✅ Your group claimed this client</b>\n"
                        "<i>Sender already notified driver(s).</i>\n\n"
                    ),
                    mirror_supervisory=False,
                    accepted_by=accepted_by_label,
                )
            else:
                _claimed_txt = (
                    f"✅ **Your group claimed this lead**\n\n"
                    f"Reference: `{reference_id}`\n\n"
                    f"The sender already notified driver(s). This group is now recorded as the accepting group."
                )
                await context.bot.send_message(
                    chat_id=_parse_chat_id(winner_group.get("group_telegram_id")),
                    text=_claimed_txt,
                    parse_mode="Markdown",
                )
        except Exception as e:
            logger.warning("Could not notify group after accept (assignments already exist): %s", e)
    else:
        # Multi-group broadcast: offers exist; issuer picks drivers only after a team accepts.
        if offers:
            lead_fresh = db.get_lead_by_id(lead_id) or lead
            if lead_fresh.get("external_order_id"):
                # Website lead — no human issuer: auto-dispatch drivers now.
                await _api_lead_auto_dispatch_after_group_accept(
                    context, lead_fresh, winner_group, accepted_by=accepted_by_label
                )
                return
            try:
                await _issuer_open_driver_selection_after_group_accept(
                    context, str(lead_id), lead_for_files
                )
            except Exception as e:
                logger.warning("Could not notify issuer after group accept: %s", e)
            try:
                await _send_full_group_lead_to_chat(
                    context,
                    winner_group,
                    lead_fresh,
                    html_prefix="<b>✅ Your group claimed this client</b>\n\n",
                    mirror_supervisory=False,
                    accepted_by=accepted_by_label,
                )
            except Exception as e:
                logger.warning("Could not post full lead to group after broadcast accept: %s", e)
        else:
            count, driver_names, fail_reason, driver_scope = await _send_driver_requests_for_group(
                context, lead, winner_group,
            )
            if count > 0:
                _drv_txt = f"🚗 Sent to driver(s): **{driver_names}**\nReference: `{reference_id}`"
                await context.bot.send_message(
                    chat_id=_parse_chat_id(winner_group.get("group_telegram_id")),
                    text=_drv_txt,
                    parse_mode="Markdown",
                )
            else:
                _fail_txt = _group_accept_notify_fail_text(reference_id, fail_reason, driver_scope)
                await context.bot.send_message(
                    chat_id=_parse_chat_id(winner_group.get("group_telegram_id")),
                    text=_fail_txt,
                    parse_mode="Markdown",
                )


async def handle_decline_group_offer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle a group member declining a broadcast lead offer (for that group only)."""
    query = update.callback_query
    await query.answer()
    pair = _parse_paired_short_uuids(query.data, "dg_")
    if not pair:
        await query.message.reply_text("❌ Invalid request.")
        return
    short_lead, short_group = pair
    try:
        lead_id = _long_uuid(short_lead)
        group_id = _long_uuid(short_group)
    except (ValueError, Exception):
        await query.message.reply_text("❌ Invalid request.")
        return
    db.decline_group_lead_offer(lead_id, group_id)
    try:
        await query.message.edit_text(
            "❌ **Declined**",
            parse_mode="Markdown",
            reply_markup=_EMPTY_INLINE_KB,
        )
    except Exception:
        pass


async def handle_different_team_offer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Single-group approval: team asks the lead creator to assign a different group."""
    query = update.callback_query
    await query.answer()
    pair = _parse_paired_short_uuids(query.data, "dt_")
    if not pair:
        await query.message.reply_text("❌ Invalid request.")
        return
    short_lead, short_group = pair
    try:
        lead_id = _long_uuid(short_lead)
        group_id = _long_uuid(short_group)
    except (ValueError, Exception):
        await query.message.reply_text("❌ Invalid request.")
        return

    lead = db.get_lead_by_id(lead_id)
    group = db.get_group_by_id(group_id)
    if not lead or not group or not record_is_active(group):
        try:
            await query.message.edit_text(
                "❌ Offer not found or expired.",
                reply_markup=_EMPTY_INLINE_KB,
            )
        except Exception:
            pass
        return

    db.decline_group_lead_offer(lead_id, group_id)
    try:
        await query.message.edit_text(
            "🔄 **Different team**\n\nThe lead creator will pick another group.",
            parse_mode="Markdown",
            reply_markup=_EMPTY_INLINE_KB,
        )
    except Exception:
        pass

    issuer_uid = lead.get("user_id")
    ref = lead.get("reference_id", "N/A")
    gname = group.get("group_name", "A group")
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Pick another group", callback_data=f"reassign_group_{lead_id}"),
    ]])
    try:
        await context.bot.send_message(
            chat_id=int(issuer_uid),
            text=(
                f"🔄 **Different team**\n\n"
                f"**{gname}** asked to pass this lead to another team.\n\n"
                f"Reference: `{ref}`\n\n"
                "Tap below to choose a group. You can keep picking drivers — no need to wait."
            ),
            parse_mode="Markdown",
            reply_markup=kb,
        )
    except Exception as e:
        logger.warning("Could not DM issuer for different team: %s", e)


# Receipt submission handlers
def _driver_row_for_telegram_user(telegram_user_id: int) -> dict | None:
    """Resolve driver by Telegram user id (indexed query — avoids loading all drivers per tap)."""
    return db.get_driver_by_telegram_id(str(telegram_user_id).strip())


def _driver_accepted_this_lead(driver_id, lead_id: str) -> bool:
    st = db.get_lead_assignment_status(lead_id)
    if not st or str(st.get("driver_id")) != str(driver_id):
        return False
    return (st.get("status") or "").lower() == "accepted"


def _driver_row_by_id(driver_id) -> dict | None:
    if not driver_id:
        return None
    return next(
        (d for d in _get_all_drivers_cached() if str(d.get("id")) == str(driver_id)),
        None,
    )


def _receipt_upload_allowed_for_user(
    user_id: int,
    lead_id: str,
    lead: dict,
    context: ContextTypes.DEFAULT_TYPE,
) -> tuple[bool, str | None, dict | None]:
    """Whether this user may upload a receipt for the lead; sets on-behalf driver in context.

    A lead that already has a receipt on file stays uploadable — receipts
    sometimes have to be corrected, so a new upload REPLACES the old one
    (flagged in context so the flow can say so).
    """
    if (lead.get("receipt_image_url") or "").strip():
        context.user_data["receipt_replacing_existing"] = True
    else:
        context.user_data.pop("receipt_replacing_existing", None)

    st = db.get_lead_assignment_status(lead_id)
    if not st or (st.get("status") or "").lower() != "accepted":
        return False, "❌ This lead has no accepted driver assignment.", None

    assigned_id = str(st.get("driver_id") or "").strip()
    if not assigned_id:
        return False, "❌ No driver assigned on this lead.", None

    if _user_is_global_supervisor(user_id):
        target = _driver_row_by_id(assigned_id)
        context.user_data["receipt_on_behalf_driver_id"] = assigned_id
        context.user_data["receipt_uploaded_by_supervisor"] = True
        return True, None, target

    drv = _driver_row_for_telegram_user(user_id)
    if not drv:
        return (
            False,
            "❌ This Telegram account is not registered as a driver.\n"
            "Ask an admin to add your Telegram user ID in the dashboard.",
            None,
        )
    if str(drv["id"]) != assigned_id:
        return False, "❌ You can only upload receipts for leads you accepted.", None

    context.user_data.pop("receipt_on_behalf_driver_id", None)
    context.user_data.pop("receipt_uploaded_by_supervisor", None)
    return True, None, drv


def _receipt_target_driver_row(
    user_id: int,
    lead_id: str,
    context: ContextTypes.DEFAULT_TYPE,
) -> dict | None:
    """Driver row receipt is credited to (from context or telegram user)."""
    on_behalf = context.user_data.get("receipt_on_behalf_driver_id")
    if on_behalf:
        return _driver_row_by_id(on_behalf)
    drv = _driver_row_for_telegram_user(user_id)
    if drv and _driver_accepted_this_lead(drv["id"], lead_id):
        return drv
    return None


def _merge_receipt_context_from_db(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    row = db.get_user_state(user_id)
    if not row or not row.get("data"):
        return
    st_name = (row.get("state") or "").strip()
    if st_name not in ("waiting_receipt_image", "waiting_receipt_confirm", "waiting_reference_id"):
        return
    data = row["data"]
    for key in (
        "receipt_lead_id",
        "receipt_reference_id",
        "receipt_monday_item_id",
        "receipt_on_behalf_driver_id",
        "receipt_uploaded_by_supervisor",
        "receipt_replacing_existing",
    ):
        if data.get(key) is not None and context.user_data.get(key) is None:
            context.user_data[key] = data[key]


# ── Supervisor receipt navigation: drill-down callback prefixes ──────────────
# ``recsup_dri_<short_uuid>`` (10 + 22 chars) → show that driver's pending refs.
# ``recsup_back``                              → return to the drivers list.
RECSUP_DRI_PREFIX = "recsup_dri_"
RECSUP_BACK = "recsup_back"


def _group_pending_receipts_by_driver(pending: list) -> list[tuple[str, str, list]]:
    """Group ``get_all_pending_receipts`` output by driver.

    Returns a list of ``(driver_id, driver_name, [{ref, lead_id, ...}, ...])``
    sorted by descending receipt count, then by driver name.
    """
    buckets: dict[str, dict] = {}
    for row in pending or []:
        did = str(row.get("driver_id") or "").strip()
        if not did:
            continue
        dname = (row.get("driver_name") or "Driver").strip() or "Driver"
        buckets.setdefault(did, {"id": did, "name": dname, "rows": []})
        buckets[did]["rows"].append(row)
    out = [(b["id"], b["name"], b["rows"]) for b in buckets.values()]
    out.sort(key=lambda t: (-len(t[2]), t[1].lower()))
    return out


def _supervisor_drivers_keyboard(grouped: list[tuple[str, str, list]]) -> InlineKeyboardMarkup:
    """Drivers list keyboard: one button per driver with their pending count."""
    rows: list[list[InlineKeyboardButton]] = []
    for driver_id, driver_name, items in grouped:
        n = len(items)
        try:
            short = _short_uuid(driver_id)
        except Exception:
            # If the driver_id isn't a UUID, fall back to a plain receipt menu.
            continue
        label = f"🚗 {driver_name[:24]} — {n} owed"
        rows.append([InlineKeyboardButton(label, callback_data=f"{RECSUP_DRI_PREFIX}{short}")])
    rows.append([InlineKeyboardButton("📋 Enter Reference ID", callback_data="driver_receipt")])
    return InlineKeyboardMarkup(rows)


def _supervisor_driver_refs_keyboard(rows_for_driver: list) -> InlineKeyboardMarkup:
    """Per-driver refs keyboard with a Back button."""
    rows: list[list[InlineKeyboardButton]] = []
    for r in rows_for_driver:
        ref = (r.get("reference_id") or "").strip()
        if not ref or ref.upper() == "N/A":
            continue
        rows.append([InlineKeyboardButton(f"📤 Upload {ref}", callback_data=f"receipt_for_{ref}")])
    rows.append([InlineKeyboardButton("⬅️ Back to drivers", callback_data=RECSUP_BACK)])
    return InlineKeyboardMarkup(rows)


async def _send_supervisor_pending_receipts_menu(
    context: ContextTypes.DEFAULT_TYPE,
    reply_to_message,
) -> None:
    """Supervisors: drivers-with-owed-receipts list → tap a driver to see refs.

    Per the user request ("see each driver then each reference for easy
    upload"), this two-tier menu replaces the older flat list.
    """
    pending = db.get_all_pending_receipts(500)
    grouped = _group_pending_receipts_by_driver(pending)

    if not grouped:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 Enter Reference ID", callback_data="driver_receipt")],
        ])
        await reply_to_message.reply_text(
            "✅ No outstanding receipts in the system.\n\n"
            "Tap below to upload by reference ID:",
            reply_markup=kb,
        )
        return

    total_refs = sum(len(items) for _, _, items in grouped)
    n_drivers = len(grouped)
    body = (
        "🧾 <b>Upload receipts for drivers</b>\n\n"
        f"<b>{total_refs}</b> owed receipt(s) across <b>{n_drivers}</b> driver(s).\n"
        "Tap a driver to see their pending references."
    )
    kb = _supervisor_drivers_keyboard(grouped)
    try:
        await reply_to_message.reply_text(body, parse_mode="HTML", reply_markup=kb)
    except BadRequest:
        await reply_to_message.reply_text(
            body.replace("<b>", "").replace("</b>", ""),
            reply_markup=kb,
        )


def _supervisor_driver_refs_body(driver_name: str, n: int) -> str:
    return (
        f"🚗 <b>{html.escape(driver_name, quote=False)}</b>\n"
        f"{n} pending receipt(s).\n\n"
        "Tap a reference to upload its receipt:"
    )


async def handle_supervisor_receipts_nav(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Top-level handler for supervisor receipts drill-down navigation.

    Pattern: ``recsup_dri_<short_uuid>`` (show driver's refs) or
    ``recsup_back`` (back to drivers list). Runs OUTSIDE the receipt
    ConversationHandler so navigation always works regardless of state.
    """
    query = update.callback_query
    if not query:
        return
    try:
        await query.answer()
    except Exception:
        pass

    user_id = query.from_user.id
    if not _user_is_global_supervisor(user_id):
        try:
            await query.message.reply_text("❌ Only supervisors can use this menu.")
        except Exception:
            pass
        return

    data = query.data or ""

    # Back to drivers list — re-fetch + re-render the top-level menu in place.
    if data == RECSUP_BACK:
        pending = db.get_all_pending_receipts(500)
        grouped = _group_pending_receipts_by_driver(pending)
        if not grouped:
            try:
                await query.edit_message_text(
                    "✅ No outstanding receipts in the system.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("📋 Enter Reference ID", callback_data="driver_receipt")],
                    ]),
                )
            except Exception:
                pass
            return
        total_refs = sum(len(items) for _, _, items in grouped)
        n_drivers = len(grouped)
        body = (
            "🧾 <b>Upload receipts for drivers</b>\n\n"
            f"<b>{total_refs}</b> owed receipt(s) across <b>{n_drivers}</b> driver(s).\n"
            "Tap a driver to see their pending references."
        )
        try:
            await query.edit_message_text(
                body, parse_mode="HTML",
                reply_markup=_supervisor_drivers_keyboard(grouped),
            )
        except Exception:
            pass
        return

    # Drill into one driver
    if data.startswith(RECSUP_DRI_PREFIX):
        token = data[len(RECSUP_DRI_PREFIX):]
        try:
            driver_id = _long_uuid(token)
        except Exception:
            try:
                await query.message.reply_text("❌ Invalid driver link.")
            except Exception:
                pass
            return
        driver_row = _driver_row_by_id(driver_id)
        driver_name = (driver_row.get("driver_name") or "Driver") if driver_row else "Driver"
        rows_for_driver = db.get_driver_pending_receipts(driver_id) or []
        if not rows_for_driver:
            try:
                await query.edit_message_text(
                    f"✅ {html.escape(driver_name, quote=False)} has no outstanding receipts.",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("⬅️ Back to drivers", callback_data=RECSUP_BACK)],
                    ]),
                )
            except Exception:
                pass
            return
        try:
            await query.edit_message_text(
                _supervisor_driver_refs_body(driver_name, len(rows_for_driver)),
                parse_mode="HTML",
                reply_markup=_supervisor_driver_refs_keyboard(rows_for_driver),
            )
        except Exception:
            # Fallback: send a fresh message if the original can't be edited.
            try:
                await query.message.reply_text(
                    _supervisor_driver_refs_body(driver_name, len(rows_for_driver)),
                    parse_mode="HTML",
                    reply_markup=_supervisor_driver_refs_keyboard(rows_for_driver),
                )
            except Exception:
                pass


async def _send_driver_pending_receipts_menu(
    context: ContextTypes.DEFAULT_TYPE,
    reply_to_message,
    driver: dict,
) -> None:
    """Show owed-receipt upload buttons, or a short message if none pending (same as /receipts)."""
    pending = db.get_driver_pending_receipts(driver["id"])
    if not pending:
        await reply_to_message.reply_text(
            "✅ You don't owe any receipts right now.",
            reply_markup=_driver_keyboard_lead_and_receipt(),
        )
        return
    max_show = 90
    n_total = len(pending)
    if n_total > max_show:
        await reply_to_message.reply_text(
            f"You owe {n_total} receipts. Showing the first {max_show} — upload those, then send /receipts again."
        )
        pending = pending[:max_show]
    rows = []
    for p in pending:
        ref = (p.get("reference_id") or "").strip()
        if not ref or ref.upper() == "N/A":
            continue
        rows.append(
            [InlineKeyboardButton(f"📤 Upload {ref}", callback_data=f"receipt_for_{ref}")]
        )
    if not rows:
        await reply_to_message.reply_text(
            "⚠️ You have pending receipts but no valid reference IDs. Contact support."
        )
        return
    parts = []
    if n_total >= SUSPENSION_THRESHOLD:
        parts.append(
            "⛔ <b>You are suspended</b>\n\n"
            f"Reason: You owe <b>{n_total}</b> receipt(s). "
            "You will not receive new leads until all outstanding receipts are uploaded."
        )
    elif n_total > 0:
        parts.append(
            f"⚠️ You owe <b>{n_total}</b> receipt(s). At <b>{SUSPENSION_THRESHOLD}</b> unpaid you will be "
            "<b>temporarily suspended</b> from new leads."
        )
    parts.append(f"🧾 <b>Upload these ({len(rows)})</b> — tap a reference:")
    body = "\n\n".join(parts)
    receipt_kb = _keyboard_receipt_plus_rows(rows)
    try:
        await reply_to_message.reply_text(
            body,
            parse_mode="HTML",
            reply_markup=receipt_kb,
        )
    except BadRequest:
        await reply_to_message.reply_text(
            body.replace("<b>", "").replace("</b>", ""),
            reply_markup=receipt_kb,
        )


async def handle_driver_add_receipt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Inline: same owed-receipt list as /receipts — short message if nothing pending."""
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await query.answer()
    msg = update.effective_message
    if not msg:
        return ConversationHandler.END
    uid = query.from_user.id
    if _user_is_global_supervisor(uid):
        await _send_supervisor_pending_receipts_menu(context, msg)
        return ConversationHandler.END
    driver = _driver_row_for_telegram_user(uid)
    if not driver:
        await msg.reply_text(
            "❌ This Telegram account is not registered as a driver.\n"
            "Ask an admin to add your Telegram user ID in the dashboard."
        )
        return ConversationHandler.END
    await _send_driver_pending_receipts_menu(context, msg, driver)
    return ConversationHandler.END


async def handle_driver_receipts_menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Drivers or supervisors: owed receipts + upload. Commands: /receipts, /receipt, /recipts."""
    user = update.effective_user
    if not user or not update.message:
        return ConversationHandler.END
    if _user_is_global_supervisor(user.id):
        await _send_supervisor_pending_receipts_menu(context, update.message)
        return ConversationHandler.END
    driver = _driver_row_for_telegram_user(user.id)
    if not driver:
        await update.message.reply_text(
            "❌ This Telegram account is not registered as a driver.\n"
            "Ask an admin to add your Telegram user ID in the dashboard."
        )
        return ConversationHandler.END
    await _send_driver_pending_receipts_menu(context, update.message, driver)
    return ConversationHandler.END


async def handle_receipt_for_ref_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """When driver clicks ref in strike message – show lead details and Upload button."""
    query = update.callback_query
    await query.answer()
    _merge_receipt_context_from_db(query.from_user.id, context)
    ref = query.data.partition("receipt_for_")[2].strip()
    lead = db.get_lead_by_reference_id(ref)
    if not lead:
        await query.message.reply_text(f"❌ Reference ID `{ref}` not found.")
        return ConversationHandler.END
    ok, err, _target_drv = _receipt_upload_allowed_for_user(
        query.from_user.id, lead["id"], lead, context
    )
    if not ok:
        await query.message.reply_text(err or "❌ Cannot upload receipt for this lead.")
        return ConversationHandler.END
    context.user_data["receipt_lead_id"] = lead["id"]
    context.user_data["receipt_reference_id"] = ref
    context.user_data["receipt_monday_item_id"] = lead.get("monday_item_id")
    delivery_safe = _sanitize_phones_for_send(lead.get("delivery_details") or "")
    driver_line = ""
    if context.user_data.get("receipt_uploaded_by_supervisor"):
        drow = _driver_row_by_id(context.user_data.get("receipt_on_behalf_driver_id"))
        dname = (drow.get("driver_name") or "Driver") if drow else "Driver"
        driver_line = f"\n🚗 Driver: **{dname}**"
    replace_line = (
        "\n♻️ This lead already has a receipt — the new upload will **replace** it.\n"
        if context.user_data.get("receipt_replacing_existing")
        else ""
    )
    msg = (
        f"📋 **Reference ID:** `{ref}`{driver_line}\n\n"
        f"📍 Delivery: {delivery_safe or 'N/A'}\n"
        f"🚗 Vehicle: {lead.get('vehicle_details', 'N/A')[:300]}\n"
        f"{replace_line}\n"
        "Upload receipt for this lead:"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Upload Receipt", callback_data="confirm_receipt")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_receipt")],
    ])
    db.set_user_state(query.from_user.id, "waiting_receipt_confirm", context.user_data)
    await query.message.reply_text(msg, parse_mode="Markdown", reply_markup=kb)
    return STATE_WAITING_RECEIPT_CONFIRM


async def handle_driver_receipt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle driver receipt button callback."""
    query = update.callback_query
    await query.answer()
    try:
        await query.edit_message_reply_markup(reply_markup=_EMPTY_INLINE_KB)
    except Exception:
        pass

    user_id = query.from_user.id

    # Set state to waiting for reference ID
    db.set_user_state(user_id, "waiting_reference_id", {})

    if _user_is_global_supervisor(user_id):
        title = "📋 **Supervisor Receipt Upload**\n\n"
        hint = "Enter the Reference ID for the driver's lead."
    else:
        title = "📋 **Driver Receipt Submission**\n\n"
        hint = "Please enter the Reference ID for the lead you want to submit a receipt for."

    await query.message.reply_text(
        title + hint,
        parse_mode="Markdown",
    )

    return STATE_WAITING_REFERENCE_ID


async def handle_reference_id_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle reference ID input."""
    user_id = update.effective_user.id
    msg = update.effective_message
    if not msg or not (getattr(msg, "text", None) or "").strip():
        if msg:
            await msg.reply_text("Please send the reference ID as text, or type /cancel to restart from the top.")
        return STATE_WAITING_REFERENCE_ID
    # Pull the id out of whatever they typed around it — "ref ABC12345",
    # "Reference: ABC12345", or the id read aloud and transcribed letter by letter.
    reference_id = extract_reference_id(msg.text, db.get_lead_by_reference_id)
    
    # Get lead by reference ID
    lead = db.get_lead_by_reference_id(reference_id)
    
    if not lead:
        await msg.reply_text(
            "❌ Reference ID not found. Please check and try again.\n"
            "Or type /cancel to restart from the top."
        )
        return STATE_WAITING_REFERENCE_ID

    ok, err, _target_drv = _receipt_upload_allowed_for_user(user_id, lead["id"], lead, context)
    if not ok:
        await msg.reply_text(err or "❌ Cannot upload receipt for this lead.")
        db.clear_user_state(user_id)
        return ConversationHandler.END

    context.user_data['receipt_lead_id'] = lead['id']
    context.user_data['receipt_reference_id'] = reference_id
    context.user_data['receipt_monday_item_id'] = lead.get('monday_item_id')
    db.set_user_state(user_id, "waiting_receipt_confirm", context.user_data)

    delivery_safe = _sanitize_phones_for_send(lead.get('delivery_details') or '')
    phone_display = _driver_phone_display(lead)
    phone_label = "📞 Phone (one-time link)" if _driverblock_enabled() else "📞 Phone"
    driver_line = ""
    if context.user_data.get("receipt_uploaded_by_supervisor"):
        drow = _driver_row_by_id(context.user_data.get("receipt_on_behalf_driver_id"))
        dname = (drow.get("driver_name") or "Driver") if drow else "Driver"
        driver_line = f"\n🚗 Driver: **{dname}**"
    replace_line = (
        "♻️ This lead already has a receipt — the new upload will **replace** it.\n\n"
        if context.user_data.get("receipt_replacing_existing")
        else ""
    )
    confirmation_message = (
        f"✅ **Lead Found**{driver_line}\n\n"
        f"📍 Delivery Address: {delivery_safe or 'N/A'}\n"
        f"{phone_label}: {phone_display}\n"
        f"📋 Reference ID: `{reference_id}`\n\n"
        f"{replace_line}"
        f"Please confirm this is the correct lead, then upload the receipt image."
    )
    
    # Create confirmation keyboard
    confirm_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirm & Upload Receipt", callback_data="confirm_receipt")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_receipt")]
    ])
    
    await msg.reply_text(
        confirmation_message,
        parse_mode="Markdown",
        reply_markup=confirm_keyboard
    )
    
    return STATE_WAITING_RECEIPT_CONFIRM


async def handle_reference_id_stray(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """A picture sent where a reference id was asked for. Keeps the receipt session
    rather than letting the image escape into the lead flow and wipe it."""
    msg = update.effective_message
    if msg:
        await msg.reply_text(
            "📋 I need the *reference ID* first (the code on the lead), then the "
            "receipt photo.",
            parse_mode="Markdown")
    return STATE_WAITING_REFERENCE_ID


async def handle_receipt_confirm_words(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """"yes" / "no" at the receipt confirmation.

    Runs the button's own handler through _TypedAsTap so typing and tapping can
    never drift apart. Anything else re-asks rather than dropping the message —
    silence at a question is how an operator ends up staring at a dead screen.
    """
    msg = update.effective_message
    text = ((msg.text if msg else "") or "").strip()
    if _YES_RE.match(text) or _DRIVER_ACCEPT_RE.match(text):
        return await handle_receipt_confirm_callback(
            _TypedAsTap(update, "confirm_receipt"), context)
    if _NO_RE.match(text) or _DRIVER_DECLINE_RE.match(text):
        return await handle_receipt_confirm_callback(
            _TypedAsTap(update, "cancel_receipt"), context)
    await msg.reply_text("Please tap ✅ Confirm or ❌ Cancel above — or say yes or no.")
    return STATE_WAITING_RECEIPT_CONFIRM


async def handle_receipt_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle receipt confirmation callback."""
    query = update.callback_query

    if query.data == "cancel_receipt":
        await query.answer("Cancelled")
        user_id = query.from_user.id
        db.clear_user_state(user_id)
        await query.message.reply_text("❌ Receipt submission cancelled.")
        return ConversationHandler.END

    await query.answer()

    user_id = query.from_user.id

    # Restore receipt context from DB in case the bot restarted between
    # showing the Confirm button and the user tapping it. Without this the
    # in-memory ``context.user_data`` is empty after a redeploy, gets
    # persisted back as empty, and the photo upload later fails with
    # "Lead information not found."
    _merge_receipt_context_from_db(user_id, context)

    if not context.user_data.get("receipt_lead_id"):
        await query.message.reply_text(
            "⚠️ This receipt session has expired (the bot may have restarted).\n\n"
            "Type /receipts to see your pending receipts again."
        )
        db.clear_user_state(user_id)
        return ConversationHandler.END

    db.set_user_state(user_id, "waiting_receipt_image", context.user_data)

    lead_id = context.user_data.get("receipt_lead_id")
    # The web page is offered alongside the photo. A picture sent here is mirrored
    # into the database anyway, but the page is what keeps working when Telegram
    # is awkward — and what the office opens later, since it never expires.
    portal = receipt_portal_url(lead_id) if lead_id else ""
    ask = "📸 **Upload Receipt**\n\nPlease upload the receipt image now🧾."
    kb = None
    if portal:
        ask += "\n\nOr upload it on the web — that link never expires:"
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🌐 Upload on the web", url=portal)]])
    await query.message.reply_text(ask, parse_mode="Markdown", reply_markup=kb)

    return STATE_WAITING_RECEIPT_IMAGE


async def _notify_supervisory_receipt_submission(
    context: ContextTypes.DEFAULT_TYPE,
    update: Update,
    lead: dict,
    reference_id: str,
    receipt_file_id: str | None,
    group: dict | None,
    driver_display_name: str,
    *,
    uploaded_by_supervisor: bool = False,
    supervisor_display_name: str | None = None,
    replacing_existing: bool = False,
) -> None:
    """Plain receipt summary + same image as caption (no SUPERVISORY prefix); fallback text + forward."""
    gn = (group.get("group_name") or "—") if group else "—"
    client_name = _client_display_name_from_lead(lead)
    ref_plain = str(reference_id or "N/A")
    issuer_line = _lead_issuer_display_from_lead(lead)
    caption = (
        ("🧾 Receipt REPLACED\n" if replacing_existing else "🧾 New receipt sent\n")
        + f"Reference: {ref_plain}\n"
        f"Group: {gn}\n"
        f"Driver(s): {driver_display_name}\n"
        f"Client name: {client_name}\n"
        f"Lead issued by: {issuer_line}"
    )
    if replacing_existing:
        caption += "\n♻️ Replaces the previous receipt for this reference."
    if uploaded_by_supervisor and supervisor_display_name:
        caption += f"\nUploaded by supervisor: {supervisor_display_name}"

    async def _send_caption_and_receipt(chat_id: int, label: str) -> None:
        msg = update.message
        try:
            if receipt_file_id and msg and msg.photo:
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=receipt_file_id,
                    caption=caption,
                )
            elif receipt_file_id and msg and msg.document:
                await context.bot.send_document(
                    chat_id=chat_id,
                    document=receipt_file_id,
                    caption=caption,
                )
            else:
                await context.bot.send_message(chat_id=chat_id, text=caption)
                if receipt_file_id:
                    await context.bot.send_photo(chat_id=chat_id, photo=receipt_file_id)
        except Exception as e:
            logger.warning("Receipt caption+file to %s (%s): %s", chat_id, label, e)
            try:
                await context.bot.send_message(chat_id=chat_id, text=caption)
            except Exception as e2:
                logger.warning("Receipt text to %s: %s", chat_id, e2)
            try:
                if msg:
                    await context.bot.forward_message(
                        chat_id=chat_id,
                        from_chat_id=update.effective_chat.id,
                        message_id=msg.message_id,
                    )
            except Exception as e3:
                logger.warning("Receipt forward to %s: %s", chat_id, e3)
                if receipt_file_id:
                    try:
                        await context.bot.send_photo(chat_id=chat_id, photo=receipt_file_id)
                    except Exception as e4:
                        logger.warning("Receipt photo fallback to %s: %s", chat_id, e4)

    sent_norm: set = set()
    sup_targets = _supervisory_delivery_chat_ids(
        group.get("supervisory_telegram_id") if group else None
    )
    for sup_chat_id in sup_targets:
        nk = _norm_chat_id(sup_chat_id)
        if nk is not None and nk in sent_norm:
            continue
        await _send_caption_and_receipt(sup_chat_id, "supervisory")
        if nk is not None:
            sent_norm.add(nk)

    st_raw = (db.get_setting("st_telegram_id") or "").strip()
    if st_raw:
        st_chat_id = _parse_chat_id(st_raw)
        stk = _norm_chat_id(st_chat_id) if st_chat_id is not None else None
        if st_chat_id is not None and stk is not None and stk not in sent_norm:
            await _send_caption_and_receipt(st_chat_id, "ST")
            sent_norm.add(stk)


async def handle_receipt_image_stray(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User sent text or non-image while waiting for receipt photo — nudge without leaving state."""
    msg = update.effective_message
    if msg:
        await msg.reply_text(
            "Please send a photo or an image file (JPG, PNG, or WebP) of the receipt.",
        )
    return STATE_WAITING_RECEIPT_IMAGE


async def handle_receipt_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle receipt image upload (photo or image document)."""
    user_id = update.effective_user.id
    _merge_receipt_context_from_db(user_id, context)

    import io

    receipt_file_id: str | None = None
    if update.message.photo:
        ph = update.message.photo[-1]
        receipt_file_id = ph.file_id
        file = await context.bot.get_file(receipt_file_id)
        telegram_file_url = _telegram_download_url_from_file_path(file.file_path or "")
        bio = io.BytesIO()
        await file.download_to_memory(out=bio)
        image_bytes = bio.getvalue()
        file_name = (file.file_path.split("/")[-1] if file.file_path else "receipt.jpg")
    elif update.message.document:
        doc = update.message.document
        mime = (doc.mime_type or "").lower()
        if not mime.startswith("image/"):
            await update.message.reply_text(
                "❌ Please send a photo or an image file (JPG, PNG, or WebP)."
            )
            return STATE_WAITING_RECEIPT_IMAGE
        receipt_file_id = doc.file_id
        file = await context.bot.get_file(receipt_file_id)
        telegram_file_url = _telegram_download_url_from_file_path(file.file_path or "")
        bio = io.BytesIO()
        await file.download_to_memory(out=bio)
        image_bytes = bio.getvalue()
        file_name = (doc.file_name or (file.file_path.split("/")[-1] if file.file_path else "receipt.jpg"))
    else:
        await update.message.reply_text(
            "❌ Please send a photo or an image file. Upload the receipt."
        )
        return STATE_WAITING_RECEIPT_IMAGE

    lead_id = context.user_data.get("receipt_lead_id")
    reference_id = context.user_data.get("receipt_reference_id")
    monday_item_id = context.user_data.get("receipt_monday_item_id")

    if not lead_id:
        await update.message.reply_text("❌ Error: Lead information not found. Please start over.")
        db.clear_user_state(user_id)
        return ConversationHandler.END
    
    # Get lead and driver info
    lead = db.get_lead_by_id(lead_id)
    if not lead:
        await update.message.reply_text("❌ Error: Lead not found.")
        db.clear_user_state(user_id)
        return ConversationHandler.END

    dr_check = _receipt_target_driver_row(user_id, lead_id, context)
    if not dr_check:
        await update.message.reply_text(
            "❌ You can only upload receipts for accepted leads assigned to the driver."
        )
        db.clear_user_state(user_id)
        return ConversationHandler.END

    uploaded_by_supervisor = bool(context.user_data.get("receipt_uploaded_by_supervisor"))

    mime_for_ai = "image/jpeg"
    if update.message.document:
        mime_for_ai = (update.message.document.mime_type or "image/jpeg").lower()
    if Config.is_ai_vision_configured():
        _rec_mode = _resolve_receipt_detection_mode()
        try:
            rv = await asyncio.to_thread(
                lambda: ai_vision.validate_driver_receipt_image(
                    image_bytes,
                    mime_type=mime_for_ai,
                    expected_price_text=(lead.get("price") or "").strip() or None,
                    detection_mode=_rec_mode,
                )
            )
        except ai_vision.AIVisionQuotaError:
            await update.message.reply_text(
                "❌ Receipt verification is temporarily unavailable (API limit). Please try again in a few minutes."
            )
            return STATE_WAITING_RECEIPT_IMAGE
        if not rv.accept:
            await update.message.reply_text(rv.message)
            return STATE_WAITING_RECEIPT_IMAGE

    assignment_status = db.get_lead_assignment_status(lead_id)
    driver_name = "Driver"
    if assignment_status:
        driver_id = assignment_status.get("driver_id")
        driver = next(
            (d for d in _get_all_drivers_cached() if str(d.get("id")) == str(driver_id)),
            None,
        )
        if driver:
            driver_name = driver.get("driver_name", "Driver")

    pending_before = db.get_driver_pending_receipts(dr_check["id"]) if dr_check else []
    was_suspended = len(pending_before) >= SUSPENSION_THRESHOLD

    # Into the DATABASE first — a row cannot expire the way a Telegram file does,
    # and it is what the dashboard reads. Storage stays as a second copy.
    in_db = await _store_receipt_bytes(
        lead_id, image_bytes,
        content_type=("image/png" if str(file_name or "").lower().endswith(".png")
                      else "image/jpeg"),
        reference_id=reference_id,
        driver_id=str((dr_check or {}).get("id") or ""))
    storage_url = db.upload_receipt_to_storage(lead_id, reference_id, image_bytes, file_name)
    if in_db:
        stored_url = f"{RECEIPT_PORTAL_BASE}/receipt/{lead_id}"
    else:
        stored_url = _normalize_receipt_image_url(
            ((storage_url or "").strip() or telegram_file_url).strip()
        )
    # Telegram file URLs expire after ~1h. When we fall back to one, append the
    # permanent file_id as a URL fragment (#tgfid=...) — fragments are invisible
    # to HTTP fetches, but the dispatch-api receipt viewer parses it to re-sign
    # a fresh download URL via getFile whenever the stored path has expired.
    if not (storage_url or "").strip() and receipt_file_id and "#" not in stored_url:
        stored_url = f"{stored_url}#tgfid={receipt_file_id}"
    if not (stored_url or "").strip():
        logger.error("Receipt upload: no durable URL (lead_id=%s)", lead_id)
        await update.message.reply_text(
            "❌ Could not save the receipt file URL. Please try again or contact support."
        )
        return STATE_WAITING_RECEIPT_IMAGE

    receipt_price: str | None = None
    if Config.is_ai_vision_configured():
        try:
            amounts = await asyncio.to_thread(
                lambda: ai_vision.extract_receipt_amounts_usd(image_bytes, mime_type=mime_for_ai)
            )
            if amounts:
                receipt_price = str(max(amounts))
        except Exception as e:
            logger.warning("receipt amount extraction failed (lead_id=%s): %s", lead_id, e)

    # Update lead with receipt URL (prefer durable Supabase Storage public URL)
    success = db.update_lead_receipt(lead_id, stored_url, receipt_price=receipt_price)
    if not success:
        logger.error("update_lead_receipt failed lead_id=%s ref=%s", lead_id, reference_id)

    if success:
        # Paper Investigator shared tables: idempotent catch-up if subtract-at-accept missed the row
        # (UUID formatting, API errors, or race with PI job). Receipt proves the delivery is real.
        try:
            st = db.get_lead_assignment_status(lead_id)
            if st and db._norm_uuid_str(st.get("driver_id")) == db._norm_uuid_str(dr_check.get("id")):
                aid = st.get("id")
                ref = (lead.get("reference_id") or "") or ""
                new_paper_bal = db.apply_paper_on_lead_accept(
                    str(dr_check["id"]), str(aid), str(ref)
                )
                if new_paper_bal is not None and new_paper_bal < Config.LOW_PAPER_THRESHOLD:
                    if not db.paper_was_low_alert_sent(dr_check["id"]):
                        db.paper_mark_low_alert_sent(dr_check["id"])
                        sup = Config.PAPER_SUPERVISOR_TELEGRAM_ID
                        if sup:
                            try:
                                dnm = dr_check.get("driver_name", "Driver")
                                await context.bot.send_message(
                                    chat_id=int(sup),
                                    text=(
                                        f"🔴 Low paper: {dnm} has {new_paper_bal} paper(s) left.\n\n"
                                        "Open the Paper Investigator bot (All Drivers) to approve resupply."
                                    ),
                                )
                            except Exception as e:
                                logger.warning(
                                    "Could not notify paper supervisor (low paper after receipt): %s", e
                                )
        except Exception as e:
            logger.warning("Paper inventory sync on receipt failed: %s", e)

    if success and monday and monday_item_id:
        # First, try to upload the actual image file to the Monday files4 column.
        upload_ok = False
        try:
            upload_ok = monday.update_item_receipt(monday_item_id, file_name, image_bytes)
        except Exception as e:
            logger.error(f"Error uploading receipt to Monday.com: {e}")
        
        # If the direct file upload failed, fall back to storing a public URL
        # (e.g. Supabase public URL or Telegram file URL) into a text column
        # so the team still has access to the receipt.
        if not upload_ok:
            try:
                monday.update_item_receipt_link(monday_item_id, stored_url)
            except Exception as e:
                logger.error(f"Error updating Monday.com with receipt URL fallback: {e}")
        
        # Always attempt to update status after trying to attach the receipt
        try:
            monday.update_item_status(monday_item_id, "PAID RECEIPT")
        except Exception as e:
            logger.error(f"Error updating Monday.com status: {e}")
    
    if success:
        ref_show = html.escape(str(reference_id or "N/A"), quote=False)
        replacing_existing = bool(context.user_data.get("receipt_replacing_existing"))
        context.user_data.pop("receipt_replacing_existing", None)
        replaced_html = (
            "\n♻️ Previous receipt was replaced with this one." if replacing_existing else ""
        )
        if uploaded_by_supervisor:
            dn_esc = html.escape(driver_name, quote=False)
            supervisor_confirm_html = (
                "✅ <b>Receipt saved for driver</b>\n"
                f"🚗 Driver: {dn_esc}\n"
                f"Reference ID: <code>{ref_show}</code>"
                f"{replaced_html}"
            )
            try:
                await update.message.reply_text(supervisor_confirm_html, parse_mode="HTML")
            except Exception as e:
                logger.error("Supervisor receipt confirmation reply failed: %s", e)
                await update.message.reply_text(
                    f"✅ Receipt saved for {driver_name}. Reference: {reference_id or 'N/A'}"
                )
        else:
            dq = html.escape(driver_motivation.get_random_driver_quote(), quote=False)
            driver_confirm_html = (
                "✅ <b>Receipt received successfully!</b>\n"
                "📂 Your Receipt🧾 is on file.\n"
                "🚗💨 Thank you &amp; 💪Great job — keep up the excellent work!\n"
                f"{replaced_html}\n"
                f"Reference ID: <code>{ref_show}</code>\n\n"
                f"💪 <i>{dq}</i>"
            )
            try:
                await update.message.reply_text(
                    driver_confirm_html,
                    parse_mode="HTML",
                    reply_markup=_driver_keyboard_lead_and_receipt(),
                )
            except Exception as e:
                logger.error("Driver receipt confirmation reply failed: %s", e)
                try:
                    dqp = driver_motivation.get_random_driver_quote()
                    await update.message.reply_text(
                        f"✅ Receipt received and saved. Reference: {reference_id or 'N/A'}\n\n{dqp}",
                        reply_markup=_driver_keyboard_lead_and_receipt(),
                    )
                except Exception as e2:
                    logger.error("Fallback driver receipt confirm failed: %s", e2)

        # Lead adder: single summary is sent on driver accept only (not receipt).

        group_id = lead.get("group_id")
        group = db.get_group_by_id(group_id) if group_id else None
        sup_name = None
        if update.effective_user:
            u = update.effective_user
            sup_name = (u.full_name or u.username or str(u.id)).strip()
        try:
            await _notify_supervisory_receipt_submission(
                context,
                update,
                lead,
                str(reference_id or ""),
                receipt_file_id,
                group,
                driver_name,
                uploaded_by_supervisor=uploaded_by_supervisor,
                supervisor_display_name=sup_name if uploaded_by_supervisor else None,
                replacing_existing=replacing_existing,
            )
        except Exception as e:
            logger.error("Supervisory receipt notification failed: %s", e, exc_info=True)

        if was_suspended and dr_check:
            pending_after = db.get_driver_pending_receipts(dr_check["id"])
            if len(pending_after) < SUSPENSION_THRESHOLD:
                await _notify_suspension_lifted(
                    context,
                    driver=dr_check,
                    pending_after=pending_after,
                    reply_message=update.message,
                )
    else:
        await update.message.reply_text(
            "❌ Error uploading receipt. Please try again or contact support."
        )
    
    db.clear_user_state(user_id)
    
    return ConversationHandler.END


def _wait_for_exclusive_polling(bot_token: str, max_wait: int = 120) -> bool:
    """
    Wait until no other process is polling this bot token.
    Render starts the new worker before killing the old one; this loop
    retries until the old process releases getUpdates.
    Returns True when the slot is free, False if timed out.
    """
    import requests as _req
    import time as _time
    api = f"https://api.telegram.org/bot{bot_token}"

    try:
        _req.post(f"{api}/deleteWebhook", json={"drop_pending_updates": True}, timeout=5)
    except Exception:
        pass

    waited = 0
    backoff = 3
    attempt = 0
    while waited < max_wait:
        attempt += 1
        # Re-clear webhook every few attempts (another deploy may have set one)
        if attempt % 5 == 0:
            try:
                _req.post(f"{api}/deleteWebhook", json={"drop_pending_updates": True}, timeout=5)
            except Exception:
                pass
        try:
            r = _req.post(f"{api}/getUpdates", json={"timeout": 1, "limit": 1}, timeout=10)
            if r.status_code == 200:
                logger.info("Polling slot is free — proceeding to start bot.")
                return True
            if r.status_code == 409:
                logger.info(
                    "Another instance still polling (409). Retrying in %ds… (%d/%ds elapsed)",
                    backoff, waited, max_wait,
                )
                _time.sleep(backoff)
                waited += backoff
                backoff = min(backoff + 2, 10)
                continue
            logger.warning("getUpdates probe returned HTTP %s, retrying…", r.status_code)
            _time.sleep(3)
            waited += 3
        except Exception as e:
            logger.warning("getUpdates probe failed: %s, retrying…", e)
            _time.sleep(3)
            waited += 3

    logger.error("Timed out waiting for exclusive polling slot after %ds.", max_wait)
    return False


# ── Renewal system ────────────────────────────────────────────────────────

def _build_renewal_group_message(renewal: dict) -> str:
    """Build the group-facing renewal notice (plain text)."""
    lead = renewal.get("lead") or {}
    ref = (lead.get("reference_id") or "N/A").strip() or "N/A"

    # Issue/expiration (best-effort; shown as blank when unknown to match requested shape)
    issue_dt = _dt_from_lead_field(lead.get("issue_date"))
    exp_dt = _dt_from_lead_field(lead.get("expiration_date"))
    issue_s = issue_dt.strftime("%Y-%m-%d %H:%M %Z") if issue_dt else ""
    exp_s = exp_dt.strftime("%Y-%m-%d %H:%M %Z") if exp_dt else ""

    # Last accepted driver (original assignment)
    last_driver_name = "—"
    original_did = renewal.get("original_driver_id")
    if original_did:
        try:
            all_drivers = _get_all_drivers_cached()
            d = next((x for x in all_drivers if str(x.get("id")) == str(original_did)), None)
            if d and (d.get("driver_name") or "").strip():
                last_driver_name = (d.get("driver_name") or "").strip()
        except Exception:
            pass

    vd = (lead.get("vehicle_details") or "").strip().replace("\r\n", "\n")
    dd = _sanitize_phones_for_send(lead.get("delivery_details") or "") or "N/A"
    extra = _sanitize_phones_for_send(lead.get("extra_info") or "") or "—"

    lines = [
        "🔄 RENEWAL DUE",
        f"📋 Ref ID: {ref}",
        "",
        f"Issue date: {issue_s}",
        f"Expiration date: {exp_s}",
        f"Which driver accepted last month: {last_driver_name}",
        "",
        f"🚗 Vehicle: {vd}" + _format_all_extra_vehicle_lines(lead),
        *_multi_tag_notice_lines(lead),
        f"📍 Delivery: {dd}",
        f"📝 Extra info: {extra}",
        "",
        "Tap Accept to keep this renewal.",
        "Tap Reassign to pass it to another team.",
    ]
    return "\n".join(lines)


def _build_renewal_driver_message(renewal: dict) -> str:
    """Build the driver-facing renewal notice (plain text).

    Shows the TAG INFO up front (client, vehicle, issue/expiration) so the
    driver sees exactly which tag this is BEFORE choosing Accept or Reassign.
    """
    lead = renewal.get("lead") or {}
    ref = lead.get("reference_id") or "N/A"
    delivery = _delivery_block_plain(lead)
    extra = _sanitize_phones_for_send(lead.get("extra_info") or "") or "—"
    link = (lead.get("encrypted_link") or "").strip() or "N/A"
    price = (lead.get("price") or "").strip() or "N/A"
    spec_d = _lead_driver_note(lead)
    client_name = _client_display_name_from_lead(lead) or "N/A"
    vd = (lead.get("vehicle_details") or "").strip().replace("\r\n", "\n") or "N/A"
    issue_dt = _dt_from_lead_field(lead.get("issue_date"))
    exp_dt = _dt_from_lead_field(lead.get("expiration_date"))
    issue_s = issue_dt.strftime("%Y-%m-%d") if issue_dt else "—"
    exp_s = exp_dt.strftime("%Y-%m-%d") if exp_dt else "—"
    lines = [
        "🔄 RENEWAL DELIVERY AVAILABLE",
        "",
        "🏷 TAG INFO",
        f"👤 Client: {client_name}",
        f"🚗 Vehicle: {vd}" + _format_all_extra_vehicle_lines(lead),
        *_multi_tag_notice_lines(lead),
        f"📅 Issued: {issue_s}",
        f"⌛️ Expires: {exp_s}",
        "",
        "📍 Delivery Address",
        delivery,
        f"📝 Extra info: {extra}",
        f"📞Phone {link}",
        "📞 Click link 🔗 enter password “ callclient “ to view",
        f"💰 Price: {price}",
        f"🆔 Reference ID: {ref}",
    ]
    if spec_d:
        lines.extend(["", f"📝 Special request (driver): {_sanitize_phones_for_send(spec_d)}"])
    lines.extend([
        "",
        "Tap Accept to take this renewal delivery.",
        "Tap Reassign to pass it to another driver.",
    ])
    return "\n".join(lines)


async def _send_renewal_to_group(context: ContextTypes.DEFAULT_TYPE, renewal: dict, group: dict) -> bool:
    """Send a renewal offer to a single group chat. Returns True if sent."""
    gid = group.get("group_telegram_id")
    chat_id = _parse_chat_id(gid)
    if not chat_id:
        return False
    short_r = _short_uuid(renewal["id"])
    short_g = _short_uuid(group["id"])
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Accept", callback_data=f"rga_{short_r}{short_g}"),
        InlineKeyboardButton("🔄 Reassign", callback_data=f"rgr_{short_r}{short_g}"),
    ]])
    text = _build_renewal_group_message(renewal)
    try:
        msg = await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
        db.update_renewal(renewal["id"], {
            "group_message_chat_id": str(chat_id),
            "group_message_id": msg.message_id,
        })
        return True
    except Exception as e:
        logger.warning("Could not send renewal to group %s: %s", group.get("group_name"), e)
        return False


async def _send_renewal_to_driver(context: ContextTypes.DEFAULT_TYPE, renewal: dict, driver: dict) -> bool:
    """Send a renewal offer to a single driver. Returns True if sent."""
    cid = _parse_chat_id(driver.get("driver_telegram_id"))
    if not cid:
        return False
    short_r = _short_uuid(renewal["id"])
    short_d = _short_uuid(driver["id"])
    kb = _keyboard_renewal_driver(short_r, short_d)
    text = _build_renewal_driver_message(renewal)
    try:
        msg = await context.bot.send_message(chat_id=cid, text=text, reply_markup=kb)
        db.update_renewal(renewal["id"], {
            "driver_message_chat_id": str(cid),
            "driver_message_id": msg.message_id,
        })
        return True
    except Exception as e:
        logger.warning("Could not send renewal to driver %s: %s", driver.get("driver_name"), e)
        return False


async def _offer_renewal_to_group_drivers(
    context: ContextTypes.DEFAULT_TYPE,
    renewal_id: str,
    renewal: dict,
    group_id: str | None,
    *,
    exclude_driver_id: str | None = None,
) -> bool:
    """Offer a renewal delivery to active drivers in the accepting group."""
    if not group_id:
        return False
    drivers = db.get_active_drivers_for_group(group_id) or []
    suspended = _get_suspended_driver_ids()
    refreshed = db.get_renewal_by_id(renewal_id) or renewal
    db.update_renewal(renewal_id, {
        "driver_status": "sent",
        "driver_sent_at": datetime.utcnow().isoformat(),
    })
    sent_any = False
    for d in drivers:
        did = str(d.get("id") or "")
        if not did:
            continue
        if exclude_driver_id and did == str(exclude_driver_id):
            continue
        if did in suspended:
            continue
        if await _send_renewal_to_driver(context, refreshed, d):
            sent_any = True
    return sent_any


async def _escalate_renewal_group(context: ContextTypes.DEFAULT_TYPE, renewal_id: str) -> None:
    """Timer callback: original group didn't accept within the escalation window — broadcast to all."""
    renewal = db.get_renewal_by_id(renewal_id)
    if not renewal:
        return
    renewal = db.ensure_renewal_original_group(renewal)
    if renewal.get("group_status") == "accepted":
        return  # already handled
    logger.info("Renewal %s: group escalation triggered", renewal_id)
    db.update_renewal(renewal_id, {
        "group_status": "escalated",
        "group_escalated_at": datetime.utcnow().isoformat(),
    })
    groups = db.get_all_groups()
    active = [g for g in groups if record_is_active(g)]
    original_gid = renewal.get("original_group_id")
    refreshed = db.get_renewal_by_id(renewal_id) or renewal
    for g in active:
        if g.get("id") == original_gid:
            continue
        await _send_renewal_to_group(context, refreshed, g)


async def _escalate_renewal_driver(
    context: ContextTypes.DEFAULT_TYPE,
    renewal_id: str,
    *,
    exclude_driver_id: str | None = None,
) -> None:
    """Timer / reassign callback: offer renewal to other drivers in the accepted group."""
    renewal = db.get_renewal_by_id(renewal_id)
    if not renewal:
        return
    if renewal.get("driver_status") == "accepted":
        return  # already handled
    logger.info("Renewal %s: driver escalation triggered", renewal_id)
    db.update_renewal(renewal_id, {
        "driver_status": "escalated",
        "driver_escalated_at": datetime.utcnow().isoformat(),
    })
    group_id = renewal.get("group_accepted_by_id") or renewal.get("original_group_id")
    await _offer_renewal_to_group_drivers(
        context,
        renewal_id,
        renewal,
        group_id,
        exclude_driver_id=exclude_driver_id,
    )


async def _escalate_renewal_driver_all(
    context: ContextTypes.DEFAULT_TYPE,
    renewal_id: str,
    *,
    exclude_driver_id: str | None = None,
) -> None:
    """Fallback: the original driver couldn't take the renewal — offer it to ALL active drivers everywhere (FCFS)."""
    # Atomic claim: never overwrite a concurrent Accept (reopening a completed
    # renewal), never re-broadcast when timer + Reassign both fire.
    if not db.claim_renewal_driver_escalation(renewal_id):
        logger.info(
            "Renewal %s: all-drivers escalation skipped (already accepted or escalated)",
            renewal_id,
        )
        return
    logger.info("Renewal %s: escalating to all drivers", renewal_id)
    refreshed = db.get_renewal_by_id(renewal_id)
    if not refreshed:
        return
    suspended = _get_suspended_driver_ids()
    sent_any = False
    for d in _get_all_drivers_cached() or []:
        did = str(d.get("id") or "")
        if not did:
            continue
        if not record_is_active(d):
            continue
        if exclude_driver_id and did == str(exclude_driver_id):
            continue
        if did in suspended:
            continue
        if await _send_renewal_to_driver(context, refreshed, d):
            sent_any = True
    if not sent_any:
        logger.warning("Renewal %s: no drivers available for all-driver escalation", renewal_id)


async def handle_renewal_group_accept(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Group member taps Accept on a renewal offer."""
    query = update.callback_query
    await query.answer()
    pair = _parse_paired_short_uuids(query.data, "rga_")
    if not pair:
        await query.message.reply_text("❌ Invalid request.")
        return
    short_r, short_g = pair
    try:
        renewal_id = _long_uuid(short_r)
        group_id = _long_uuid(short_g)
    except (ValueError, Exception):
        await query.message.reply_text("❌ Invalid request.")
        return

    renewal = db.get_renewal_by_id(renewal_id)
    if not renewal:
        try:
            await query.message.edit_text("❌ Renewal not found or expired.")
        except Exception:
            pass
        return
    renewal = db.ensure_renewal_original_group(renewal)
    group = db.get_group_by_id(group_id)
    if not group:
        try:
            await query.message.edit_text("❌ Renewal not found or expired.")
        except Exception:
            pass
        return

    result = db.accept_renewal_group(renewal_id, group_id)
    if result == "wrong_team":
        try:
            await query.message.edit_text(
                "❌ This renewal is reserved for the team that originally issued this client.\n\n"
                "Wait for them to accept, or use **Reassign** on their renewal message to send it to other teams.",
                parse_mode="Markdown",
            )
        except Exception:
            pass
        return
    if result != "ok":
        err_msg = {
            "already_accepted": "❌ This renewal was already accepted by another team.",
            "not_found": "❌ Renewal not found or expired.",
            "error": "❌ Could not accept this renewal. Please try again or contact support.",
        }.get(result, "❌ Could not accept this renewal.")
        try:
            await query.message.edit_text(err_msg)
        except Exception:
            pass
        return

    ref = (renewal.get("lead") or {}).get("reference_id", "N/A")
    gname = group.get("group_name", "Group")
    try:
        await query.message.edit_text(
            f"✅ **Renewal accepted by {gname}**\n\n"
            f"Reference ID: `{ref}`\n\n"
            "Now sending to your drivers…",
            parse_mode="Markdown",
        )
    except Exception:
        pass

    refreshed = db.get_renewal_by_id(renewal_id) or renewal

    # Re-send the lead to the accepting group with a RENEWAL header (not NEW CLIENT).
    try:
        lead_full = db.get_lead_by_id(refreshed.get("lead_id")) or (refreshed.get("lead") or {})
        _renew_acceptor = (query.from_user.username or "").strip() or (query.from_user.full_name or "Unknown")
        await _send_full_group_lead_to_chat(
            context,
            group,
            lead_full,
            header_text="🏷RENEWAL CLIENT❗️",
            mirror_supervisory=False,
            renewal=True,
            accepted_by=f"{gname} (@{_renew_acceptor})",
        )
    except Exception as e:
        logger.warning("Could not re-send renewal lead to accepting group: %s", e)

    # Phase 2: all active drivers in this group choose whether they can take it.
    accepting_gid = str(group_id)
    await _offer_renewal_to_group_drivers(
        context, renewal_id, refreshed, accepting_gid
    )


async def handle_renewal_group_reassign(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Group member taps Reassign — immediately escalate to other groups."""
    query = update.callback_query
    await query.answer()
    pair = _parse_paired_short_uuids(query.data, "rgr_")
    if not pair:
        await query.message.reply_text("❌ Invalid request.")
        return
    short_r, short_g = pair
    try:
        renewal_id = _long_uuid(short_r)
        _long_uuid(short_g)  # validate group id in payload
    except (ValueError, Exception):
        await query.message.reply_text("❌ Invalid request.")
        return

    renewal = db.get_renewal_by_id(renewal_id)
    if not renewal:
        return
    if renewal.get("group_status") == "accepted":
        try:
            await query.message.edit_text("❌ Already accepted by a team.")
        except Exception:
            pass
        return

    ref = (renewal.get("lead") or {}).get("reference_id", "N/A")
    try:
        await query.message.edit_text(
            f"🔄 **Reassigned** — this renewal (`{ref}`) has been sent to other teams.",
            parse_mode="Markdown",
        )
    except Exception:
        pass
    await _escalate_renewal_group(context, renewal_id)


async def handle_renewal_driver_accept(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Driver taps Accept on a renewal delivery."""
    query = update.callback_query
    await query.answer()
    pair = _parse_paired_short_uuids(query.data, "rda_")
    if not pair:
        await query.message.reply_text("❌ Invalid request.")
        return
    short_r, short_d = pair
    try:
        renewal_id = _long_uuid(short_r)
        driver_id = _long_uuid(short_d)
    except (ValueError, Exception):
        await query.message.reply_text("❌ Invalid request.")
        return

    renewal = db.get_renewal_by_id(renewal_id)
    if not renewal:
        try:
            await query.message.edit_text("❌ Renewal not found or expired.")
        except Exception:
            pass
        return

    result = db.accept_renewal_driver(renewal_id, driver_id)
    if result == "wrong_driver":
        try:
            await query.message.edit_text(
                "❌ This renewal is only for drivers on the team that accepted it."
            )
        except Exception:
            pass
        return
    if result != "ok":
        err_msg = {
            "already_accepted": "❌ This renewal delivery was already accepted by another driver.",
            "not_found": "❌ Renewal not found or expired.",
            "error": "❌ Could not accept this renewal. Please try again.",
        }.get(result, "❌ Could not accept this renewal.")
        try:
            await query.message.edit_text(err_msg)
        except Exception:
            pass
        return

    lead = renewal.get("lead") or {}
    ref = lead.get("reference_id", "N/A")
    driver = None
    all_drivers = _get_all_drivers_cached()
    driver = next((d for d in all_drivers if str(d.get("id")) == str(driver_id)), None)
    dname = driver.get("driver_name", "Driver") if driver else "Driver"

    try:
        await query.message.edit_text(
            f"✅ **Renewal accepted!**\n\nReference ID: `{ref}`",
            parse_mode="Markdown",
        )
    except Exception:
        pass

    # Full lead details for the driver — behind the location gate when configured.
    lead_full = db.get_lead_by_id(renewal.get("lead_id")) or lead
    try:
        await _start_tracking_gate_or_send_details(
            context,
            kind="renewal",
            lead=lead_full,
            driver_id=str(driver_id) if driver_id else None,
            driver_name=dname,
            chat_id=query.message.chat_id,
            renewal_id=str(renewal_id),
        )
    except Exception as e:
        logger.warning("Could not send renewal confirmation to driver: %s", e)

    # Notify the accepted group
    group_id = renewal.get("group_accepted_by_id") or renewal.get("original_group_id")
    group = db.get_group_by_id(group_id) if group_id else None
    if group:
        gcid = _parse_chat_id(group.get("group_telegram_id"))
        if gcid:
            try:
                await context.bot.send_message(
                    chat_id=gcid,
                    text=(
                        f"✅ **Renewal Delivery Accepted**\n\n"
                        f"🚗 Driver: {dname}\n"
                        f"📋 Reference ID: `{ref}`"
                    ),
                    parse_mode="Markdown",
                )
            except Exception as e:
                logger.warning("Could not notify group about renewal driver accept: %s", e)

    # Schedule the NEXT renewal cycle (28 more days from now)
    try:
        from datetime import datetime, timedelta, timezone as _tz
        next_due = datetime.now(_tz.utc) + timedelta(days=Config.RENEWAL_DAYS)
        lead_id = renewal.get("lead_id")
        accepted_group = renewal.get("group_accepted_by_id") or renewal.get("original_group_id")
        existing = db.get_active_renewal_for_lead(lead_id) if lead_id else None
        if not existing and lead_id:
            db.schedule_renewal(
                lead_id=lead_id,
                group_id=accepted_group,
                driver_id=driver_id,
                renewal_due_at=next_due.isoformat(),
            )
            logger.info("Next renewal scheduled for lead %s in %d days", ref, Config.RENEWAL_DAYS)
    except Exception as e:
        logger.warning("Could not schedule next renewal cycle: %s", e)

    logger.info("Renewal %s completed — driver %s accepted", renewal_id, dname)


async def handle_renewal_driver_reassign(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Driver taps Reassign — immediately escalate to other drivers."""
    query = update.callback_query
    await query.answer()
    pair = _parse_paired_short_uuids(query.data, "rdr_")
    if not pair:
        await query.message.reply_text("❌ Invalid request.")
        return
    short_r, short_d = pair
    try:
        renewal_id = _long_uuid(short_r)
        driver_id = _long_uuid(short_d)  # driver id in button payload
    except (ValueError, Exception):
        await query.message.reply_text("❌ Invalid request.")
        return

    renewal = db.get_renewal_by_id(renewal_id)
    if not renewal:
        return
    if renewal.get("driver_status") == "accepted":
        try:
            await query.message.edit_text("❌ Already accepted by a driver.")
        except Exception:
            pass
        return

    ref = (renewal.get("lead") or {}).get("reference_id", "N/A")
    try:
        await query.message.edit_text(
            f"🔄 **Reassigned** — this renewal delivery (`{ref}`) is now open to ALL drivers. First accept wins.",
            parse_mode="Markdown",
        )
    except Exception:
        pass
    await _escalate_renewal_driver_all(context, renewal_id, exclude_driver_id=driver_id)


# ── Client follow-ups ──────────────────────────────────────────────────────
# Tracker for prospective clients who are ready to buy but missing something
# (usually the VIN). The agent enters everything they have + the client's phone
# and email; on the chosen start/stop/frequency the BOT chases the client by
# text + email (so the client follows up with the agent), and DMs the agent +
# all supervisors a reminder with a stop button.

STATE_FU_MENU = 40

_FU_FREQ_DAYS = {"daily": 1, "weekly": 7, "biweekly": 14, "monthly": 30}
_FU_FREQ_LABEL = {"daily": "daily", "weekly": "weekly", "biweekly": "every 2 weeks", "monthly": "monthly"}
_FU_DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_FU_TIME_HOURS = {"morning": 9, "afternoon": 13, "evening": 18}
_FU_TIME_LABEL = {"morning": "🌅 Morning", "afternoon": "☀️ Afternoon", "evening": "🌙 Evening"}
# "Stop" choices: how long the bot keeps chasing before auto-stopping.
# "on_order" (default) = run until a NEW dispatch order arrives with this
# client's phone/email/name — then the follow-up auto-closes (never deleted).
_FU_END_DAYS = {"1w": 7, "2w": 14, "1m": 30, "3m": 90}
_FU_END_LABEL = {
    "1w": "1 week", "2w": "2 weeks", "1m": "1 month", "3m": "3 months",
    "forever": "until stopped", "on_order": "🧾 when they order",
}

# Text-input prompts for the tap-to-fill fields on the /followup menu.
_FU_TEXT_FIELDS = {
    "name": ("client_name", "👤 Type the client's name:"),
    "phone": ("phone_number", "📞 Type the client's phone number:"),
    "email": ("email", "📧 Type the client's email:"),
    "notes": ("notes", "📝 Type any notes (e.g. \"no VIN yet\", quote details):"),
}


def _fu_parse_iso(s: str | None):
    """Parse a stored ISO timestamp into an aware UTC datetime (best effort)."""
    if not s:
        return None
    from datetime import timezone
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _fu_compute_start(day_idx: int, hour: int) -> datetime:
    """Next occurrence of weekday ``day_idx`` (0=Mon) at ``hour`` in US/Eastern (aware)."""
    eastern = pytz.timezone("America/New_York")
    now = datetime.now(eastern)
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    days_ahead = (day_idx - now.weekday()) % 7
    if days_ahead == 0 and target <= now:
        days_ahead = 7
    return target + timedelta(days=days_ahead)


def _fu_menu_text(fu: dict) -> str:
    pending = fu.get("pending")
    if pending in _FU_TEXT_FIELDS:
        return _FU_TEXT_FIELDS[pending][1] + "\n\n(or tap a button below)"
    return (
        "📇 New client follow-up\n\n"
        "📥 Paste ALL the client info in one message and AI fills the fields — "
        "or tap a field to type it yourself.\n"
        "📧 The bot emails/texts the client on your schedule by default "
        "(reminder to send the missing info for their temporary tag) — "
        "tap the 🤖 button to turn that off.\n\n"
        "Send /cancel to stop."
    )


def _fu_parse_paste(text: str) -> dict:
    """Regex fallback when AI parsing is unavailable: email, phone, name, notes."""
    out: dict = {"name": None, "phone": None, "email": None, "notes": None}
    em = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
    if em:
        out["email"] = em.group(0)
    ph = re.search(r"\+?1?[\s\-.(]*\d{3}[)\s\-.]*\d{3}[\s\-.]*\d{4}", text)
    if ph:
        out["phone"] = ph.group(0).strip()
    for line in (l.strip() for l in text.splitlines() if l.strip()):
        if "@" in line or re.search(r"\d{3}", line):
            continue
        if 1 <= len(line.split()) <= 4 and len(line) <= 40:
            out["name"] = line
            break
    notes = " ".join(text.split())
    out["notes"] = notes[:300] if notes else None
    return out


def _fu_menu_keyboard(fu: dict) -> InlineKeyboardMarkup:
    def val(key):
        v = (fu.get(key) or "").strip() if fu.get(key) else ""
        return _truncate_btn_val(v) if v else "—"

    if fu.get("day_idx") is not None:
        day = _FU_DAY_NAMES[fu["day_idx"]]
        time_lbl = _FU_TIME_LABEL.get(fu.get("time_key"), "—")
    else:
        day = "⚡ Now"
        time_lbl = _FU_TIME_LABEL.get(fu.get("time_key"), "⚡ Now")
    freq_lbl = _FU_FREQ_LABEL.get(fu.get("freq"), "—")
    end_lbl = _FU_END_LABEL.get(fu.get("end_key"), "—")
    chase = "ON 🤖" if fu.get("chase") else "OFF"
    rows = [
        [
            InlineKeyboardButton(f"👤 Name: {val('client_name')}", callback_data="fuf_name"),
            InlineKeyboardButton(f"📞 Phone: {val('phone_number')}", callback_data="fuf_phone"),
        ],
        [
            InlineKeyboardButton(f"📧 Email: {val('email')}", callback_data="fuf_email"),
            InlineKeyboardButton(f"📝 Notes: {val('notes')}", callback_data="fuf_notes"),
        ],
        [
            InlineKeyboardButton(f"📅 Start: {day}", callback_data="fuf_day"),
            InlineKeyboardButton(f"🕘 Time: {time_lbl}", callback_data="fuf_time"),
        ],
        [
            InlineKeyboardButton(f"🔁 Every: {freq_lbl}", callback_data="fuf_freq"),
            InlineKeyboardButton(f"🛑 Stop: {end_lbl}", callback_data="fuf_end"),
        ],
        [InlineKeyboardButton(f"🤖 Bot texts/emails client: {chase}", callback_data="fuf_chase")],
        [
            InlineKeyboardButton("💾 Save", callback_data="fuf_save"),
            InlineKeyboardButton("❌ Cancel", callback_data="fuf_cancel"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


async def _fu_render_menu(fu: dict, query=None, reply=None) -> None:
    """Show/update the follow-up form (edit in place when possible)."""
    text = _fu_menu_text(fu)
    kb = _fu_menu_keyboard(fu)
    if query is not None:
        try:
            await query.message.edit_text(text, reply_markup=kb)
            return
        except Exception:
            pass
    if reply is not None:
        await reply(text, reply_markup=kb)


async def cmd_followup_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for /followup — one-message form with tap-to-fill buttons.

    Client contact defaults to ON: the bot emails/texts the client unless the
    agent taps the 🤖 button to turn it off.
    """
    context.user_data["fu"] = {"chase": True, "end_key": "on_order", "freq": "daily"}
    await _fu_render_menu(context.user_data["fu"], reply=update.message.reply_text)
    return STATE_FU_MENU


async def handle_fu_menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Free-text input: fills the tapped field, or AI-parses a full paste."""
    fu = context.user_data.setdefault("fu", {})
    pending = fu.pop("pending", None)
    text = (update.message.text or "").strip()
    if not pending or pending not in _FU_TEXT_FIELDS:
        # A LEAD is not a follow-up. This conversation stays open until it is
        # finished or cancelled, so a lead pasted while it happens to be up used
        # to be swallowed whole — two owners, two addresses, two VINs and two
        # insurers compressed into a 500-character notes field, with the lead
        # never created. A 17-character VIN is unmistakable; refuse and say why.
        vins = _all_vins_17(text)
        if vins:
            fu["pending"] = pending          # nothing was consumed
            await update.message.reply_text(
                f"🚗 That looks like a LEAD ({len(vins)} VIN"
                f"{'s' if len(vins) > 1 else ''}), not client follow-up info — "
                "so I have not touched anything here.\n\n"
                "Send /cancel to close this follow-up, then paste the lead again.",
            )
            return STATE_FU_MENU
        # No field tapped → treat as a data paste and parse it (AI first, regex fallback).
        parsed = None
        try:
            parsed = await asyncio.to_thread(ai_vision.extract_followup_fields, text)
        except Exception as e:
            logger.warning("Follow-up AI parse failed: %s", e)
        if not parsed or not any(parsed.get(k) for k in ("name", "phone", "email")):
            parsed = _fu_parse_paste(text)
        if parsed.get("name"):
            fu["client_name"] = parsed["name"]
        if parsed.get("phone"):
            fu["phone_number"] = parsed["phone"]
        if parsed.get("email"):
            fu["email"] = parsed["email"]
        # Everything NOT parsed into a field is kept as notes: strip the
        # recognized name/phone/email out of the paste and save the rest.
        leftover = text
        for v in (parsed.get("name"), parsed.get("email")):
            if v:
                leftover = re.sub(re.escape(v), " ", leftover, flags=re.IGNORECASE)
        leftover = re.sub(r"\+?1?[\s\-.(]*\d{3}[)\s\-.]*\d{3}[\s\-.]*\d{4}", " ", leftover)
        leftover = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", " ", leftover)
        leftover = " ".join(leftover.split()).strip(" -–—•,;:|")
        pieces = []
        for p in (fu.get("notes"), parsed.get("notes"), leftover):
            p = (p or "").strip()
            if p and all(p.lower() not in q.lower() for q in pieces):
                pieces.append(p)
        if pieces:
            fu["notes"] = " | ".join(pieces)[:500]
        filled = [n for n, k in (("name", "client_name"), ("phone", "phone_number"),
                                 ("email", "email"), ("notes", "notes")) if fu.get(k)]
        await update.message.reply_text(
            "🤖 Parsed your message → filled: " + (", ".join(filled) if filled else "nothing recognized")
            + ". Review below, tap any field to fix it."
        )
        await _fu_render_menu(fu, reply=update.message.reply_text)
        return STATE_FU_MENU
    key = _FU_TEXT_FIELDS[pending][0]
    if pending == "email" and "@" not in text:
        fu["pending"] = pending
        await update.message.reply_text("⚠️ That doesn't look like an email — try again (or /cancel).")
        return STATE_FU_MENU
    fu[key] = text or None
    await _fu_render_menu(fu, reply=update.message.reply_text)
    return STATE_FU_MENU


async def handle_fu_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    data = query.data
    fu = context.user_data.setdefault("fu", {})

    # Tap-to-type fields: show the prompt inside the same message.
    if data in ("fuf_name", "fuf_phone", "fuf_email", "fuf_notes"):
        await query.answer()
        fu["pending"] = data[len("fuf_"):]
        await _fu_render_menu(fu, query=query)
        return STATE_FU_MENU

    # Sub-pickers (edit the same message, Back returns to the form).
    if data == "fuf_day":
        await query.answer()
        rows = [[InlineKeyboardButton("⚡ Now (start right away)", callback_data="fud_now")]]
        rows += [
            [InlineKeyboardButton(_FU_DAY_NAMES[i], callback_data=f"fud_{i}"),
             InlineKeyboardButton(_FU_DAY_NAMES[i + 1], callback_data=f"fud_{i + 1}")]
            for i in (0, 2, 4)
        ]
        rows.append([InlineKeyboardButton(_FU_DAY_NAMES[6], callback_data="fud_6"),
                     InlineKeyboardButton("⬅️ Back", callback_data="fuf_back")])
        await query.message.edit_text("📅 When should reminders start?",
                                      reply_markup=InlineKeyboardMarkup(rows))
        return STATE_FU_MENU

    if data == "fuf_time":
        await query.answer()
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🌅 Morning", callback_data="fut_morning"),
             InlineKeyboardButton("☀️ Afternoon", callback_data="fut_afternoon")],
            [InlineKeyboardButton("🌙 Evening", callback_data="fut_evening"),
             InlineKeyboardButton("⬅️ Back", callback_data="fuf_back")],
        ])
        await query.message.edit_text("🕘 What time of day?", reply_markup=kb)
        return STATE_FU_MENU

    if data == "fuf_freq":
        await query.answer()
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Daily", callback_data="fufr_daily"),
             InlineKeyboardButton("Weekly", callback_data="fufr_weekly")],
            [InlineKeyboardButton("Every 2 weeks", callback_data="fufr_biweekly"),
             InlineKeyboardButton("Monthly", callback_data="fufr_monthly")],
            [InlineKeyboardButton("⬅️ Back", callback_data="fuf_back")],
        ])
        await query.message.edit_text("🔁 How often?", reply_markup=kb)
        return STATE_FU_MENU

    if data == "fuf_end":
        await query.answer()
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🧾 When they order (auto)", callback_data="fue_on_order")],
            [InlineKeyboardButton("1 week", callback_data="fue_1w"),
             InlineKeyboardButton("2 weeks", callback_data="fue_2w")],
            [InlineKeyboardButton("1 month", callback_data="fue_1m"),
             InlineKeyboardButton("3 months", callback_data="fue_3m")],
            [InlineKeyboardButton("♾ Until I stop it", callback_data="fue_forever"),
             InlineKeyboardButton("⬅️ Back", callback_data="fuf_back")],
        ])
        await query.message.edit_text(
            "🛑 When should follow-ups stop?\n\n"
            "🧾 When they order = auto-stops the moment a new dispatch order "
            "comes in with this client's phone, email, or name.",
            reply_markup=kb,
        )
        return STATE_FU_MENU

    # Picker selections → back to the form.
    if data.startswith("fud_"):
        await query.answer()
        sel = data[len("fud_"):]
        if sel == "now":
            fu["day_idx"] = None
            fu.pop("time_key", None)
        else:
            fu["day_idx"] = int(sel)
        await _fu_render_menu(fu, query=query)
        return STATE_FU_MENU
    if data.startswith("fut_"):
        await query.answer()
        fu["time_key"] = data[len("fut_"):]
        await _fu_render_menu(fu, query=query)
        return STATE_FU_MENU
    if data.startswith("fufr_"):
        await query.answer()
        fu["freq"] = data[len("fufr_"):]
        await _fu_render_menu(fu, query=query)
        return STATE_FU_MENU
    if data.startswith("fue_"):
        await query.answer()
        fu["end_key"] = data[len("fue_"):]
        await _fu_render_menu(fu, query=query)
        return STATE_FU_MENU

    if data == "fuf_back":
        await query.answer()
        fu.pop("pending", None)
        await _fu_render_menu(fu, query=query)
        return STATE_FU_MENU

    if data == "fuf_chase":
        if fu.get("chase"):
            # Turning OFF is always allowed.
            await query.answer()
            fu["chase"] = False
            await _fu_render_menu(fu, query=query)
            return STATE_FU_MENU
        if not (fu.get("phone_number") or fu.get("email")):
            await query.answer("Add a phone or email first 📞📧", show_alert=True)
            return STATE_FU_MENU
        has_channel = (
            (fu.get("phone_number") and Config.is_twilio_configured())
            or (fu.get("email") and Config.is_resend_configured())
        )
        if not has_channel:
            await query.answer("Text/email sending isn't configured on the server.", show_alert=True)
            return STATE_FU_MENU
        await query.answer()
        fu["chase"] = True
        await _fu_render_menu(fu, query=query)
        return STATE_FU_MENU

    if data == "fuf_cancel":
        await query.answer()
        context.user_data.pop("fu", None)
        await query.message.edit_text("❌ Cancelled.")
        return ConversationHandler.END

    if data == "fuf_save":
        if not (fu.get("client_name") or "").strip():
            await query.answer("Set the client's name first 👤", show_alert=True)
            return STATE_FU_MENU
        await query.answer()
        return await _fu_finish_save(update, context)

    await query.answer()
    return STATE_FU_MENU


async def _fu_finish_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Persist the follow-up (with schedule when set) and confirm to the agent."""
    query = update.callback_query
    fu = context.user_data.setdefault("fu", {})
    name = fu.get("client_name") or "client"
    # Chase is ON by default but only meaningful with a phone or email on file.
    chase = bool(fu.get("chase")) and bool(fu.get("phone_number") or fu.get("email"))

    # No frequency chosen → save as a plain contact, no reminders.
    if not fu.get("freq"):
        row = db.create_client_followup(
            user_id=update.effective_user.id,
            telegram_username=update.effective_user.username,
            client_name=fu.get("client_name"),
            phone_number=fu.get("phone_number"),
            notes=fu.get("notes"),
            email=fu.get("email"),
        )
        if not row:
            await query.message.edit_text(
                "❌ Could not save the follow-up to the database.\n"
                "Run database/migration_client_followups.sql (+ _v2) on Supabase, then try again."
            )
            return STATE_FU_MENU
        await query.message.edit_text(
            f"💾 Saved {name}. No reminders set (no frequency chosen)."
        )
        context.user_data.pop("fu", None)
        return ConversationHandler.END

    freq = fu["freq"]
    eastern = pytz.timezone("America/New_York")
    if fu.get("day_idx") is None:
        # Default: start NOW — first reminder fires on the next job tick.
        start_local = datetime.now(eastern)
        first_label = "now"
    else:
        day_idx = int(fu["day_idx"])
        hour = _FU_TIME_HOURS.get(fu.get("time_key", "morning"), 9)
        start_local = _fu_compute_start(day_idx, hour)
        first_label = (
            f"{_FU_DAY_NAMES[day_idx]} {fu.get('time_key', 'morning')} "
            f"({start_local.strftime('%b %d, %I:%M %p')} ET)"
        )
    start_utc = start_local.astimezone(pytz.utc)
    end_key = fu.get("end_key") or "on_order"
    end_iso = None
    if end_key in _FU_END_DAYS:
        end_iso = (start_utc + timedelta(days=_FU_END_DAYS[end_key])).isoformat()
    row = db.create_client_followup(
        user_id=update.effective_user.id,
        telegram_username=update.effective_user.username,
        client_name=fu.get("client_name"),
        phone_number=fu.get("phone_number"),
        notes=fu.get("notes"),
        email=fu.get("email"),
        frequency=freq,
        start_at=start_utc.isoformat(),
        next_reminder_at=start_utc.isoformat(),
        contact_client=chase,
        end_at=end_iso,
    )
    if not row:
        await query.message.edit_text(
            "❌ Could not save the follow-up to the database.\n"
            "Run database/migration_client_followups.sql (+ _v2) on Supabase, then try again."
        )
        return STATE_FU_MENU
    chase_line = (
        "🤖 The bot will text/email the client on this schedule — they'll chase YOU."
        if chase else "You'll get a DM until you close or stop it."
    )
    await query.message.edit_text(
        f"✅ Follow-up set for *{name}*.\n\n"
        f"First: {first_label}, {_FU_FREQ_LABEL.get(freq, freq)}, "
        f"stops: {_FU_END_LABEL.get(end_key, end_key)}.\n\n"
        f"{chase_line}",
        parse_mode="Markdown",
    )
    context.user_data.pop("fu", None)
    return ConversationHandler.END


async def _fu_auto_close_for_lead(context: ContextTypes.DEFAULT_TYPE, lead: dict | None) -> None:
    """A new dispatch order arrived → close (never delete) any open follow-up
    matching the client's phone, email, or name; DM the owning agent."""
    if not lead:
        return
    try:
        closed = await asyncio.to_thread(db.auto_close_followups_matching_lead, lead)
    except Exception as e:
        logger.warning("Follow-up auto-close on new lead failed: %s", e)
        return
    for f in closed:
        try:
            chat_id = int(str(f.get("user_id")).strip())
        except (TypeError, ValueError):
            chat_id = f.get("user_id")
        name = f.get("client_name") or "client"
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"🧾 New order came in for *{name}* — "
                    "their follow-up was auto-closed (reminders stopped)."
                ),
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.warning("Could not notify agent %s of auto-closed follow-up: %s", chat_id, e)


async def _on_lead_created(context: ContextTypes.DEFAULT_TYPE, lead: dict | None) -> None:
    """Everything that fires the moment a lead exists: auto-close matching
    follow-ups AND immediately register the lead on the dispatch ledger
    (PENDING row now; the tag send fills the remaining columns later)."""
    if not lead:
        return
    # The lead is saved (its payload already carried any attached photos) — clear the
    # per-lead attach state so a prior client's title/license photos can NEVER leak
    # onto the next lead.
    context.user_data.pop("phase1_extra_attachments", None)
    context.user_data.pop("phase1_attach_mode", None)
    await _fu_auto_close_for_lead(context, lead)
    # Deploy-safe: carry the issuer's "add insurance" choice from the review state onto
    # the lead so the tag-send step can ride an insurance card along. No-op until the
    # wants_insurance column exists (the feature stays dormant until the migration runs).
    try:
        st = db.get_user_state(lead.get("user_id"))
        if st and (st.get("data") or {}).get("wants_insurance"):
            try:
                await asyncio.to_thread(db.update_lead, lead["id"], {"wants_insurance": True})
            except Exception as e:
                logger.info("wants_insurance not persisted (column missing yet?): %s", e)
    except Exception as e:
        logger.debug("wants_insurance persist check failed: %s", e)
    try:
        from utils import ledger
        if ledger.is_configured():
            await asyncio.to_thread(ledger.preregister_lead, lead)
    except Exception as e:
        logger.warning("Ledger pre-register hook failed: %s", e)


async def cmd_followup_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("fu", None)
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END


async def cmd_my_followups(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Follow-up history (open + closed), one message each; open ones get a Stop button."""
    rows = db.get_followups_for_user(update.effective_user.id)
    if not rows:
        await update.message.reply_text("You have no client follow-ups yet. Use /followup to add one.")
        return
    open_n = sum(1 for f in rows if f.get("status") == "open")
    await update.message.reply_text(
        f"📇 *Your follow-up history* ({open_n} active / {len(rows)} total):",
        parse_mode="Markdown",
    )
    ny = pytz.timezone("America/New_York")
    for f in rows:
        name = f.get("client_name") or "client"
        is_open = f.get("status") == "open"
        kind = "🔁 renewal" if f.get("kind") == "renewal" else "📇 follow-up"
        head = f"{kind} — {name}" if is_open else f"✔️ CLOSED {kind} — {name}"
        lines = [head]
        if f.get("phone_number"):
            lines.append(f"📞 {f.get('phone_number')}")
        if f.get("email"):
            lines.append(f"📧 {f.get('email')}")
        if f.get("notes"):
            lines.append(f"📝 {f.get('notes')}")
        if is_open:
            nxt = _fu_parse_iso(f.get("next_reminder_at"))
            when = nxt.astimezone(ny).strftime("%b %d, %I:%M %p ET") if nxt else "—"
            freq = _FU_FREQ_LABEL.get(f.get("frequency"), f.get("frequency") or "no schedule")
            chase = " · 🤖 bot contacts client" if f.get("contact_client") else ""
            lines.append(f"⏰ next: {when} ({freq}){chase}")
        else:
            last = _fu_parse_iso(f.get("last_reminded_at")) or _fu_parse_iso(f.get("updated_at"))
            if last:
                lines.append(f"🕘 last activity: {last.astimezone(ny).strftime('%b %d, %I:%M %p ET')}")
        kb = None
        if is_open:
            short = _short_uuid(f.get("id"))
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔕 Stop", callback_data=f"cf_stop_{short}"),
                InlineKeyboardButton("✅ Close (sold)", callback_data=f"cf_close_{short}"),
            ]])
        await update.message.reply_text("\n".join(lines), reply_markup=kb)


# ── Where follow-ups and renewals go ────────────────────────────────────────
# All of this used to be fixed in the environment, so changing who gets a reminder
# meant a redeploy. It lives in the settings table now and every piece is editable
# from /settings → Follow-ups: the email a copy goes to, the phone shown to clients,
# the chat ids that get the reminder, and the dispatch chat the team watches.
# Unset means "use the default" — the env value, or every supervisor for the ids.
FU_EMAIL_KEY = "followup_email"
FU_PHONE_KEY = "followup_phone"
FU_CHATIDS_KEY = "followup_chat_ids"
FU_TEAM_CHAT_KEY = "followup_dispatch_chat_id"


def _fu_setting(key: str, default: str = "") -> str:
    try:
        return (db.get_setting(key) or "").strip() or default
    except Exception:
        return default


def followup_email() -> str:
    """Where the copy of every client email goes."""
    return _fu_setting(FU_EMAIL_KEY, Config.FOLLOWUP_EMAIL_COPY)


def followup_phone() -> str:
    """The number clients are told to call."""
    return _fu_setting(FU_PHONE_KEY, Config.FOLLOWUP_PHONE)


def followup_chat_ids() -> list:
    """Who gets the reminder. Defaults to every supervisor; editable to anyone."""
    raw = _fu_setting(FU_CHATIDS_KEY)
    if not raw:
        return list(_global_supervisory_chat_ids())
    out, seen = [], set()
    for tok in re.split(r"[\s,;]+", raw):
        cid = _parse_chat_id(tok)
        # _parse_chat_id passes a non-numeric string straight through (it serves
        # @username lookups elsewhere). A reminder needs a real chat id, so anything
        # that is not one is dropped rather than failing later at send time.
        if not isinstance(cid, int):
            continue
        key = _norm_chat_id(cid)
        if key in seen:
            continue
        seen.add(key)
        out.append(cid)
    return out


def followup_team_chat_id():
    """The dispatch team's own chat, where the whole team sees the reminder.
    Defaults to the first active dispatcher group so it works before anyone sets it."""
    raw = _fu_setting(FU_TEAM_CHAT_KEY)
    if raw:
        return _parse_chat_id(raw)
    try:
        for g in (db.get_all_groups() or []):
            if record_is_active(g):
                cid = _parse_chat_id(g.get("group_telegram_id") or g.get("chat_id"))
                if cid is not None:
                    return cid
    except Exception:
        pass
    return None


def _followup_team_keyboard(short: str) -> InlineKeyboardMarkup:
    """What the team can do with a reminder without leaving their chat."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Close (sold)", callback_data=f"cf_close_{short}"),
         InlineKeyboardButton("🔕 Stop", callback_data=f"cf_stop_{short}")],
        [InlineKeyboardButton("⏸ Pause 1 week", callback_data=f"cf_post_{short}"),
         InlineKeyboardButton("⏭ Snooze 1 day", callback_data=f"cf_snooze_{short}")],
        [InlineKeyboardButton("📧 Edit email", callback_data=f"cf_email_{short}"),
         InlineKeyboardButton("📞 Edit phone", callback_data=f"cf_phone_{short}")],
    ])


# How long an "Edit email/phone" prompt stays armed. Short on purpose: while it is
# armed it consumes the next thing typed, so a forgotten one must not lie in wait.
_CF_EDIT_TTL_SEC = 300


async def handle_cf_edit_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The new email/phone for a client, typed after tapping Edit on a reminder.

    Registered globally (group -45) because a reminder is answered wherever it was
    read — a team chat, a DM — not inside any conversation."""
    pending = (context.user_data or {}).get("cf_edit") or {}
    fid, field = pending.get("fid"), pending.get("field")
    if not fid or not field:
        return
    # It expires. A never-ending slot steals the NEXT thing the user types
    # anywhere — a settings value, a lead edit — and writes it onto a client record.
    if (time.time() - float(pending.get("ts") or 0)) > _CF_EDIT_TTL_SEC:
        context.user_data.pop("cf_edit", None)
        return
    msg = update.effective_message
    text = ((msg.text if msg else "") or "").strip()
    if not text:
        return
    context.user_data.pop("cf_edit", None)
    if text in ("-", "—", "none", "None"):
        value = ""
    elif field == "email":
        value = ai_vision.normalize_email(text) or ""
        if not value:
            await msg.reply_text("❌ That is not an email address. Tap Edit again to retry.")
            raise ApplicationHandlerStop
    else:
        value = _clean_inline_value("phone", text)
        if not value:
            await msg.reply_text("❌ That is not a phone number. Tap Edit again to retry.")
            raise ApplicationHandlerStop
    col = "email" if field == "email" else "phone_number"
    ok = await asyncio.to_thread(db.update_client_followup, fid, {col: value})
    await msg.reply_text(
        (f"✅ {field.title()} updated to {value}." if value and ok
         else f"✅ {field.title()} cleared — no more {field} on this client."
         if ok else "❌ Could not save that."))
    raise ApplicationHandlerStop


async def _handle_cf_action(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix: str) -> None:
    query = update.callback_query
    await query.answer()
    try:
        fid = _long_uuid(query.data[len(prefix):])
    except (ValueError, Exception):
        await query.message.reply_text("❌ Invalid request.")
        return

    if prefix == "cf_post_":
        # "Next week" — the one everybody actually wants when a client says
        # "call me later".
        from datetime import timezone
        nxt = datetime.now(timezone.utc) + timedelta(days=7)
        db.update_client_followup(fid, {"next_reminder_at": nxt.isoformat()})
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.message.reply_text("⏸ Paused — next reminder in a week.")
        return

    if prefix in ("cf_email_", "cf_phone_"):
        # Change where THIS client's outreach goes, from the reminder itself.
        what = "email" if prefix == "cf_email_" else "phone"
        context.user_data["cf_edit"] = {"fid": fid, "field": what, "ts": time.time()}
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.message.reply_text(
            f"✏️ Send the new *{what}* for this client — or *-* to stop contacting "
            f"them on it.",
            parse_mode="Markdown")
        return

    if prefix == "cf_snooze_":
        from datetime import timezone
        nxt = datetime.now(timezone.utc) + timedelta(days=1)
        db.update_client_followup(fid, {"next_reminder_at": nxt.isoformat()})
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.message.reply_text("⏭ Snoozed 1 day.")
        return

    if prefix == "cf_close_":
        # Deal closed — offer to roll straight into a monthly renewal reminder.
        short = _short_uuid(fid)
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔁 Yes — monthly renewal", callback_data=f"cf_renew_{short}"),
            InlineKeyboardButton("✔️ No — just close", callback_data=f"cf_done_{short}"),
        ]])
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.message.reply_text(
            "🎉 Deal closed! Start a *monthly renewal* reminder for this client?",
            reply_markup=kb, parse_mode="Markdown",
        )
        return

    if prefix == "cf_renew_":
        from datetime import timezone
        nxt = datetime.now(timezone.utc) + timedelta(days=30)
        db.update_client_followup(fid, {
            "status": "open",
            "kind": "renewal",
            "frequency": "monthly",
            "next_reminder_at": nxt.isoformat(),
            "end_at": None,
        })
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.message.reply_text(
            "🔁 Monthly renewal reminder started — first one in 30 days."
        )
        return

    if prefix == "cf_del_":
        if not _user_is_global_supervisor(update.effective_user.id):
            await query.message.reply_text("⛔ Only supervisors can delete follow-ups.")
            return
        db.delete_client_followup(fid)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.message.reply_text("🗑 Follow-up deleted.")
        return

    db.close_client_followup(fid)
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    if prefix == "cf_done_":
        await query.message.reply_text("✅ Marked closed — reminders stopped.")
    else:
        await query.message.reply_text("🔕 Reminders stopped.")


async def handle_cf_close(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_cf_action(update, context, "cf_close_")


async def handle_cf_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_cf_action(update, context, "cf_stop_")


async def handle_cf_postpone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_cf_action(update, context, "cf_post_")


async def handle_cf_edit_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_cf_action(update, context, "cf_email_")


async def handle_cf_edit_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_cf_action(update, context, "cf_phone_")


async def handle_cf_snooze(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_cf_action(update, context, "cf_snooze_")


async def handle_cf_renew(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_cf_action(update, context, "cf_renew_")


async def handle_cf_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_cf_action(update, context, "cf_done_")


async def handle_cf_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_cf_action(update, context, "cf_del_")


def _haversine_m(lat1, lng1, lat2, lng2) -> float:
    """Distance in meters between two WGS84 points."""
    import math
    try:
        p1, p2 = math.radians(float(lat1)), math.radians(float(lat2))
        dp = p2 - p1
        dl = math.radians(float(lng2) - float(lng1))
        a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return 6371000.0 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    except (TypeError, ValueError):
        return float("inf")


# Driver GPS tracking job: every 10s, (a) send deferred details for sessions
# whose location arrived, (b) remind drivers still pending past the window and
# alert supervisors with a manual override button (hard block — no auto-send),
# (c) arrival geofence — when a fresh ping lands within TRACKING_ARRIVAL_RADIUS_M
# of the delivery destination, DM the driver to upload the receipt.
async def process_tracking_sessions(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not Config.is_tracking_configured():
        return
    try:
        located = await asyncio.to_thread(db.get_tracking_sessions_located_unsent)
        for s in located:
            sid = s.get("id")
            if not sid:
                continue
            if not await asyncio.to_thread(db.claim_tracking_details_sent, sid):
                continue  # another tick claimed it
            if s.get("details_sent_at"):
                continue  # optional-location mode: details went out at accept
            lead = await asyncio.to_thread(db.get_lead_by_id, s.get("lead_id")) if s.get("lead_id") else None
            if lead:
                try:
                    await _send_driver_lead_details(
                        context, lead, s.get("chat_id"),
                        reassign_lead_id=s.get("lead_id") if s.get("kind") == "lead" else None,
                    )
                except Exception as e:
                    logger.error("Deferred details send failed for session %s: %s", sid, e)
            else:
                logger.error("Tracking session %s has no resolvable lead", sid)

        overdue = await asyncio.to_thread(
            db.get_tracking_sessions_pending_reminder, Config.TRACKING_TIMEOUT_MINUTES
        )
        for s in overdue:
            sid = s.get("id")
            if not sid or not await asyncio.to_thread(db.mark_tracking_reminder_sent, sid):
                continue
            if s.get("details_sent_at"):
                continue  # optional-location mode: nothing is blocked, no nag
            token = s.get("token") or ""
            link = _tracking_link(token)
            # Remind the driver (details stay blocked).
            try:
                await context.bot.send_message(
                    chat_id=s.get("chat_id"),
                    text=(
                        "⏳ Still waiting on your location!\n\n"
                        "Your delivery details are ready but can only be sent "
                        "after you share your location:\n"
                        f"{link}"
                    ),
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("📍 Share my location", url=link)
                    ]]),
                    disable_web_page_preview=True,
                )
            except Exception as e:
                logger.warning("Tracking reminder to %s failed: %s", s.get("chat_id"), e)
            # Alert supervisors with a manual override.
            short = _short_uuid(sid)
            sup_kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("📤 Send details anyway", callback_data=f"trk_force_{short}")
            ]])
            dname = s.get("driver_name") or "driver"
            ref = s.get("reference_id") or "N/A"
            for sup_id in _global_supervisory_chat_ids():
                try:
                    await context.bot.send_message(
                        chat_id=sup_id,
                        text=(
                            f"⚠️ {dname} has NOT shared their location for lead {ref} "
                            f"({Config.TRACKING_TIMEOUT_MINUTES}+ min).\n\n"
                            "Delivery details are blocked until they do. "
                            "Use the button to send the details anyway."
                        ),
                        reply_markup=sup_kb,
                    )
                except Exception as e:
                    logger.warning("Tracking supervisor alert to %s failed: %s", sup_id, e)

        # (c) Arrival geofence → receipt reminder (fires once per session).
        awaiting = await asyncio.to_thread(db.get_tracking_sessions_awaiting_arrival)
        for s in awaiting:
            sid = s.get("id")
            token = s.get("token")
            if not sid or not token:
                continue
            ping = await asyncio.to_thread(db.get_latest_ping_for_session, token)
            if not ping:
                continue
            # Only act on FRESH pings (driver actively tracking) — a stale ping
            # near the destination must not fire an arrival hours later.
            ping_dt = _fu_parse_iso(ping.get("created_at"))
            from datetime import timezone as _tz
            if not ping_dt or (datetime.now(_tz.utc) - ping_dt).total_seconds() > 15 * 60:
                continue
            dist = _haversine_m(ping.get("lat"), ping.get("lng"), s.get("dest_lat"), s.get("dest_lng"))
            if dist > Config.TRACKING_ARRIVAL_RADIUS_M:
                continue
            if not await asyncio.to_thread(db.mark_tracking_arrival_notified, sid):
                continue  # another tick claimed it
            ref = (s.get("reference_id") or "").strip()
            kb = None
            if ref:
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("🧾 Upload Receipt", callback_data=f"receipt_for_{ref}")
                ]])
            try:
                await context.bot.send_message(
                    chat_id=s.get("chat_id"),
                    text=(
                        f"📍 You've arrived at the delivery location{f' for `{ref}`' if ref else ''}!\n\n"
                        "🚨 Collect payment BEFORE handing over the tag.\n"
                        "🧾 Then upload the receipt right here."
                    ),
                    parse_mode="Markdown",
                    reply_markup=kb,
                )
                logger.info("Arrival receipt reminder sent for session %s (%.0fm)", sid, dist)
            except Exception as e:
                logger.warning("Arrival reminder to %s failed: %s", s.get("chat_id"), e)
    except Exception as e:
        logger.error("Tracking session job failed: %s", e)


async def handle_tracking_force(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Supervisor override: send delivery details without a location ping."""
    query = update.callback_query
    await query.answer()
    if not _user_is_global_supervisor(update.effective_user.id):
        await query.message.reply_text("⛔ Only supervisors can override the location gate.")
        return
    try:
        sid = _long_uuid(query.data[len("trk_force_"):])
    except (ValueError, Exception):
        await query.message.reply_text("❌ Invalid request.")
        return
    if not db.claim_tracking_override(sid):
        await query.message.reply_text(
            "ℹ️ Nothing to override — the driver already shared their location "
            "or the details were already sent."
        )
        return
    s = db.get_tracking_session_by_id(sid)
    lead = db.get_lead_by_id(s.get("lead_id")) if s and s.get("lead_id") else None
    if not (s and lead):
        await query.message.reply_text("❌ Could not load the lead for this session.")
        return
    try:
        await _send_driver_lead_details(
            context, lead, s.get("chat_id"),
            reassign_lead_id=s.get("lead_id") if s.get("kind") == "lead" else None,
        )
    except Exception as e:
        await query.message.reply_text(f"❌ Details send failed: {e}")
        return
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await query.message.reply_text(
        f"📤 Details sent to {s.get('driver_name') or 'driver'} WITHOUT a location "
        f"(lead {s.get('reference_id') or 'N/A'})."
    )


async def handle_cf_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """📝 View full info — everything on the record, incl. complete notes
    (the AI paste-parse stores all unparsed data there)."""
    query = update.callback_query
    await query.answer()
    try:
        fid = _long_uuid(query.data[len("cf_info_"):])
    except (ValueError, Exception):
        await query.message.reply_text("❌ Invalid request.")
        return
    f = db.get_client_followup_by_id(fid)
    if not f:
        await query.message.reply_text("❌ Follow-up not found.")
        return
    ny = pytz.timezone("America/New_York")

    def ts(key):
        dt = _fu_parse_iso(f.get(key))
        return dt.astimezone(ny).strftime("%b %d, %Y %I:%M %p ET") if dt else "—"

    status = "🟢 open" if f.get("status") == "open" else "✔️ closed"
    kind = "🔁 renewal" if f.get("kind") == "renewal" else "📇 follow-up"
    freq = _FU_FREQ_LABEL.get(f.get("frequency"), f.get("frequency") or "no schedule")
    lines = [
        f"📋 FULL INFO — {f.get('client_name') or 'client'}",
        "",
        f"Status: {status} ({kind})",
        f"👤 Agent: @{f.get('telegram_username')}" if f.get("telegram_username") else f"👤 Agent id: {f.get('user_id')}",
        f"📞 Phone: {f.get('phone_number') or '—'}",
        f"📧 Email: {f.get('email') or '—'}",
        f"🔁 Frequency: {freq}",
        f"🤖 Bot contacts client: {'ON' if f.get('contact_client') else 'OFF'}",
        f"▶️ Started: {ts('start_at')}",
        f"⏰ Next reminder: {ts('next_reminder_at')}",
        f"🔔 Last reminded: {ts('last_reminded_at')}",
        f"📨 Last client contact: {ts('last_client_contact_at')}",
        f"🛑 Stops: {ts('end_at') if f.get('end_at') else '🧾 when they order / manual'}",
        f"🗓 Created: {ts('created_at')}",
        "",
        "📝 NOTES:",
        (f.get("notes") or "—"),
    ]
    await query.message.reply_text("\n".join(lines))


async def _broadcast_announcement(context: ContextTypes.DEFAULT_TYPE, announcer_id, payload: str) -> tuple[int, int]:
    """Send ``payload`` to every active group chat + driver DM + lead-sender DM (deduped),
    skipping the announcer. Returns (sent, failed). Shared by /announce and the voice router."""
    msg = f"📢 ANNOUNCEMENT\n\n{payload}\n\n🏁Automated🏎Automotive"
    targets: dict = {}
    try:
        for g in db.get_all_groups() or []:
            if not record_is_active(g):
                continue
            cid = _parse_chat_id(g.get("group_telegram_id"))
            if cid is not None:
                targets[_norm_chat_id(cid)] = cid
    except Exception as e:
        logger.warning("Announce: groups lookup failed: %s", e)
    try:
        for d in _get_all_drivers_cached() or []:
            if not record_is_active(d):
                continue
            cid = _parse_chat_id(d.get("driver_telegram_id"))
            if cid is not None:
                targets.setdefault(_norm_chat_id(cid), cid)
    except Exception as e:
        logger.warning("Announce: drivers lookup failed: %s", e)
    try:
        for uid in db.get_lead_sender_telegram_ids() or []:
            cid = _parse_chat_id(uid)
            if cid is not None:
                targets.setdefault(_norm_chat_id(cid), cid)
    except Exception as e:
        logger.warning("Announce: lead senders lookup failed: %s", e)
    # Don't echo back to the announcer.
    if announcer_id is not None:
        targets.pop(_norm_chat_id(announcer_id), None)

    sent_n, failed_n = 0, 0
    for cid in targets.values():
        try:
            await context.bot.send_message(chat_id=cid, text=msg)
            sent_n += 1
        except Exception as e:
            failed_n += 1
            logger.warning("Announce to %s failed: %s", cid, e)
    return sent_n, failed_n


async def cmd_announce(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Supervisor broadcast: /announce <message> → every group chat + every
    driver DM + every lead-sender DM (deduped)."""
    if not _user_is_global_supervisor(update.effective_user.id):
        await update.message.reply_text("⛔ Supervisors only.")
        return
    raw = update.message.text or ""
    payload = raw.split(" ", 1)[1].strip() if " " in raw else ""
    if not payload:
        await update.message.reply_text(
            "Usage: /announce <message>\n\n"
            "Sends your message to every group chat, driver, and lead sender."
        )
        return
    sent_n, failed_n = await _broadcast_announcement(context, update.effective_user.id, payload)
    await update.message.reply_text(
        f"📢 Announcement delivered to {sent_n} chat(s)"
        + (f" — {failed_n} failed (blocked bot / bad id)." if failed_n else ".")
    )


async def cmd_all_followups(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Supervisor backend: view ALL open follow-ups with stop/delete controls."""
    if not _user_is_global_supervisor(update.effective_user.id):
        await update.message.reply_text("⛔ Supervisors only.")
        return
    rows = db.get_all_open_followups()
    if not rows:
        await update.message.reply_text("No open follow-ups anywhere. ✅")
        return
    await update.message.reply_text(f"🗂 *All open follow-ups ({len(rows)}):*", parse_mode="Markdown")
    for f in rows[:50]:
        name = f.get("client_name") or "client"
        agent = f.get("telegram_username")
        nxt = _fu_parse_iso(f.get("next_reminder_at"))
        when = nxt.astimezone(pytz.timezone("America/New_York")).strftime("%b %d, %I:%M %p ET") if nxt else "—"
        freq = _FU_FREQ_LABEL.get(f.get("frequency"), f.get("frequency") or "no schedule")
        kind = "🔁 renewal" if (f.get("kind") == "renewal") else "📇 follow-up"
        chase = " · 🤖 bot chases client" if f.get("contact_client") else ""
        lines = [f"{kind} *{name}*" + (f" (agent @{agent})" if agent else "")]
        if f.get("phone_number"):
            lines.append(f"📞 {f.get('phone_number')}")
        if f.get("email"):
            lines.append(f"📧 {f.get('email')}")
        lines.append(f"⏰ next: {when} ({freq}){chase}")
        short = _short_uuid(f.get("id"))
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔕 Stop", callback_data=f"cf_stop_{short}"),
             InlineKeyboardButton("🗑 Delete", callback_data=f"cf_del_{short}")],
            [InlineKeyboardButton("📝 View full info / notes", callback_data=f"cf_info_{short}")],
        ])
        await update.message.reply_text("\n".join(lines), reply_markup=kb, parse_mode="Markdown")


# ── /settings (supervisors only): plate counters + group management ─────────
SET_MENU, SET_INPUT = range(2)

_PLATE_SET_LABELS = {
    "nj_plate_next_number": "Resident plate number",
    "non_nj_plate_next_number": "Non-Resident plate number",
}


def _settings_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔢 Plate Numbers", callback_data="tset_plates")],
        # "Groups" in the schema are "Dispatchers" everywhere the user sees them.
        [InlineKeyboardButton("🏢 Dispatchers", callback_data="tset_groups")],
        [InlineKeyboardButton("🚗 Drivers", callback_data="tset_drivers")],
        [InlineKeyboardButton("🚫 Suspensions", callback_data="tset_susp")],
        [InlineKeyboardButton("📊 Client Sources", callback_data="tset_srcs")],
        [InlineKeyboardButton("👑 Supervisors", callback_data="tset_sups")],
        [InlineKeyboardButton("🔁 Follow-ups", callback_data="tset_fu")],
        [InlineKeyboardButton("✖️ Close", callback_data="tset_close")],
    ])


# Telegram rejects a message over this, so a long roster is split rather than clipped.
_TG_TEXT_LIMIT = 4000


def _driver_contact_lines(driver: dict) -> list:
    """Phone, email and chat id — ALWAYS all three, with a dash when not on file.

    Printing only the details that existed made a half-filled record look complete,
    so a missing number read as "none needed" instead of "go and add it". Wrapped in
    backticks so one tap copies the value; unrelated to the /driverblock redaction,
    which hides the CLIENT's number from drivers."""
    def _val(key: str) -> str:
        v = str(driver.get(key) or "").strip()
        return f"`{_telegram_md1_escape(v)}`" if v and v != "-" else "—"
    return [
        f"   📞 {_val('phone_number')}",
        f"   📧 {_val('email')}",
        f"   🆔 {_val('driver_telegram_id')}",
    ]


# One row per driver, at most this many on a single message's keyboard. Telegram
# accepts more, but a wall of buttons stops being a list you can read.
_DRIVER_ROWS_PER_MSG = 40


def _driver_status(driver: dict, suspended: set) -> tuple:
    """(mark, note) — suspended beats disabled beats active, the way dispatch sees it."""
    if str((driver or {}).get("id")) in (suspended or set()):
        return "🚫", "suspended"
    if record_is_active(driver):
        return "✅", ""
    return "⛔", "off"


def _driver_list_keyboard(drivers, suspended, *, back="tset_menu", add=True) -> list:
    """The roster as tappable rows, split into keyboards that stay readable.

    The label carries the name and the status because that is all one line holds;
    the phone, email and chat id live on the driver's own screen, one tap away."""
    rows_all = []
    for d in (drivers or []):
        mark, note = _driver_status(d, suspended)
        name = str(d.get("driver_name") or "(unnamed)")
        label = f"{mark} {name}" + (f" — {note}" if note else "")
        rows_all.append([InlineKeyboardButton(label[:60], callback_data=f"tset_drv:{d.get('id')}")])
    keyboards = []
    for i in range(0, len(rows_all) or 1, _DRIVER_ROWS_PER_MSG):
        chunk = rows_all[i:i + _DRIVER_ROWS_PER_MSG]
        last = i + _DRIVER_ROWS_PER_MSG >= len(rows_all)
        if last:
            if add:
                chunk = chunk + [[InlineKeyboardButton("➕ Add Driver", callback_data="tset_dadd")]]
            if back:
                chunk = chunk + [[InlineKeyboardButton("⬅️ Back", callback_data=back)]]
        keyboards.append(InlineKeyboardMarkup(chunk))
    return keyboards


def _driver_detail(driver: dict, suspended: set) -> tuple:
    """Everything on file for one driver, with every action it supports."""
    did = str((driver or {}).get("id") or "")
    mark, note = _driver_status(driver, suspended)
    name = _telegram_md1_escape(driver.get("driver_name") or "(unnamed)")
    lines = [f"{mark} *{name}*" + (f" _({note})_" if note else ""), ""]
    lines += _driver_contact_lines(driver)
    lines += ["", "_Tap a value to copy. — means nothing on file yet._"]
    is_susp = did in (suspended or set())
    rows = [
        [InlineKeyboardButton(
            "✅ Lift suspension" if is_susp else "🚫 Suspend",
            callback_data=f"tset_dlift:{did}" if is_susp else f"tset_dsusp:{did}")],
        [InlineKeyboardButton(
            "🔌 Enable" if not record_is_active(driver) else "⛔ Disable",
            callback_data=f"tset_dtog:{did}")],
        [InlineKeyboardButton("⬅️ All drivers", callback_data="tset_drivers")],
    ]
    return ("\n".join(lines).rstrip(), InlineKeyboardMarkup(rows))


async def _settings_view_drivers() -> tuple:
    """Every driver as a tappable row. Their details and actions are one tap away —
    a button label holds a name, not a name and three contact fields."""
    drivers = await asyncio.to_thread(_get_all_drivers_cached)
    suspended = await asyncio.to_thread(_get_suspended_driver_ids)
    total = len(drivers or [])
    txt = (f"🚗 *Drivers — {total} on file*\n\n"
           "Tap a driver for their phone, email and chat id — and to suspend, "
           "lift, disable or enable them.")
    if not total:
        txt += "\n\n" + "_No drivers yet._"
    keyboards = _driver_list_keyboard(drivers, suspended)
    # One editable message: show the first keyboard and say what did not fit.
    if len(keyboards) > 1:
        shown = _DRIVER_ROWS_PER_MSG
        txt += "\n\n" + f"_Showing {shown} of {total} — /drivers lists them all._"
    return (txt, keyboards[0])


async def _settings_view_suspensions() -> tuple:
    """Suspend a driver, or lift a suspension — including one earned by unpaid
    receipts, which is excused the same way an accepted appeal excuses it."""
    drivers = await asyncio.to_thread(_get_all_drivers_cached)
    suspended = await asyncio.to_thread(_get_suspended_driver_ids)
    manual = await asyncio.to_thread(db.get_manually_suspended_driver_ids)
    lines = ["🚫 *Suspensions*\n", "_Suspended drivers stay listed but get no new leads._\n"]
    rows = []
    for d in (drivers or [])[:25]:
        did = str(d.get("id"))
        name = d.get("driver_name") or "(unnamed)"
        is_susp = did in suspended
        why = ""
        if is_susp:
            why = " (by hand)" if did in manual else " (unpaid receipts)"
        lines.append(f"{'🚫' if is_susp else '✅'} {name}{why}")
        rows.append([InlineKeyboardButton(
            f"{'Lift' if is_susp else 'Suspend'} {name}"[:40],
            callback_data=f"tset_susp{'lift' if is_susp else 'on'}:{did}")])
    if not drivers:
        lines.append("_No drivers yet._")
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="tset_menu")])
    return ("\n".join(lines), InlineKeyboardMarkup(rows))


async def _settings_view_sources() -> tuple:
    """Add or remove the client sources offered after a lead is dispatched.
    Removing is a soft disable so already-sent picker buttons still resolve."""
    sources = await asyncio.to_thread(db.get_all_contact_info_sources)
    lines = ["📊 *Client Sources*\n"]
    rows = []
    for s in (sources or [])[:25]:
        active = record_is_active(s)
        lines.append(f"{'✅' if active else '⛔'} {s.get('label') or '(unnamed)'}")
        rows.append([InlineKeyboardButton(
            f"{'Remove' if active else 'Restore'} {s.get('label') or 'source'}"[:40],
            callback_data=f"tset_stog:{s.get('id')}:{0 if active else 1}")])
    if not sources:
        lines.append("_No sources yet._")
    rows.append([InlineKeyboardButton("➕ Add Source", callback_data="tset_sadd")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="tset_menu")])
    return ("\n".join(lines), InlineKeyboardMarkup(rows))


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    u = update.effective_user
    if not _user_is_global_supervisor(u.id):
        await update.message.reply_text(
            f"⛔ Settings are restricted to supervisors.\n\nYour Telegram ID: `{u.id}`\n"
            "Ask an admin to add it to SUPERVISORY_TELEGRAM_ID.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END
    context.user_data.pop("tset_await", None)
    await update.message.reply_text("⚙️ *Settings*", parse_mode="Markdown", reply_markup=_settings_main_kb())
    return SET_MENU


def _plate_with_letter(number, *, resident: bool, prefix: str = "H", suffix: str = "V") -> str:
    """"209861" -> "H209861" / "477040V" — the number as it is actually printed."""
    digits = re.sub(r"\D", "", str(number or ""))
    if not digits:
        return "—"
    digits = digits.zfill(6)
    return f"{prefix}{digits}" if resident else f"{digits}{suffix}"


async def _settings_view_plates() -> tuple:
    """Only the two plate counters, shown with the letter they are printed with.
    The control number is a per-tag serial nobody tracks, so it is minted at random
    and has nothing to set."""
    s = await asyncio.to_thread(db.get_plate_settings) or {}
    pre, suf = s.get("nj_plate_prefix", "H"), s.get("non_nj_plate_suffix", "V")
    res = _plate_with_letter(s.get("nj_plate_next_number"), resident=True, prefix=pre)
    non = _plate_with_letter(s.get("non_nj_plate_next_number"), resident=False, suffix=suf)
    txt = (
        "🔢 *Plate Numbers*\n\n"
        f"Resident (`{pre}######`) next: *{res}*\n"
        f"Non-Resident (`######{suf}`) next: *{non}*\n\n"
        "*Send Update:*\n"
        "Tap below, type a number, or send a *photo of the newest tag* — the "
        f"{pre}/{suf} on it says which counter it is, and I set that one "
        f"{PLATE_IMAGE_JUMP:,} past it.\n\n"
        "_Control numbers are random on every tag — nothing to set._"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Set Resident plate #", callback_data="tset_pf:nj_plate_next_number"),
         InlineKeyboardButton("Set Non-Res plate #", callback_data="tset_pf:non_nj_plate_next_number")],
        [InlineKeyboardButton("⬅️ Back", callback_data="tset_menu")],
    ])
    return (txt, kb)


async def _settings_view_followups() -> tuple:
    """Who follow-ups and renewals reach. Every line is editable — this all used to
    be fixed in the environment, so changing a recipient meant a redeploy."""
    email = await asyncio.to_thread(followup_email)
    phone = await asyncio.to_thread(followup_phone)
    ids = await asyncio.to_thread(followup_chat_ids)
    team = await asyncio.to_thread(followup_team_chat_id)
    custom = await asyncio.to_thread(_fu_setting, FU_CHATIDS_KEY)
    lines = [
        "🔁 *Follow-ups & Renewals*",
        "_Where every reminder goes._",
        "",
        f"📧 Email copy: `{email or '—'}`",
        f"📞 Phone shown to clients: `{phone or '—'}`",
        "",
        "🆔 Reminders go to:",
    ]
    for cid in (ids or []):
        lines.append(f"   • `{cid}`")
    if not ids:
        lines.append("   • _nobody_")
    if not custom:
        lines.append("   _(every supervisor — the default)_")
    lines.append("")
    lines.append(f"🏢 Dispatch team chat: `{team if team is not None else '—'}`")
    lines.append("")
    lines.append("_The whole team sees reminders there and can close, stop, pause a "
                 "week, or change the client's email or phone from the message._")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📧 Edit email", callback_data="tset_fuemail"),
         InlineKeyboardButton("📞 Edit phone", callback_data="tset_fuphone")],
        [InlineKeyboardButton("🆔 Edit chat ids", callback_data="tset_fuids")],
        [InlineKeyboardButton("🏢 Edit team chat", callback_data="tset_futeam")],
        [InlineKeyboardButton("⬅️ Back", callback_data="tset_menu")],
    ])
    return ("\n".join(lines), kb)


async def _settings_view_supervisors() -> tuple:
    """Who can open /settings. The env-configured ones are shown but fixed —
    removing the last way in from inside the room is not a thing to allow."""
    fixed = await asyncio.to_thread(_global_supervisory_chat_ids)
    extra = await asyncio.to_thread(_extra_supervisors, True)
    lines = ["👑 *Supervisors*", "_Anyone here can open /settings._", ""]
    rows = []
    for cid in fixed:
        lines.append(f"🔒 `{cid}` _(set on the server — cannot be removed here)_")
    if fixed and extra:
        lines.append("")
    for r in extra:
        name = _telegram_md1_escape(r.get("label") or "")
        lines.append(f"👤 `{r.get('id')}`" + (f" — {name}" if name else ""))
        rows.append([InlineKeyboardButton(
            f"Remove {r.get('label') or r.get('id')}"[:40],
            callback_data=f"tset_supdel:{r.get('id')}")])
    if not fixed and not extra:
        lines.append("_Nobody configured._")
    lines.append("")
    lines.append("_They can get their own id from /whoami._")
    rows.append([InlineKeyboardButton("➕ Add Supervisor", callback_data="tset_supadd")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="tset_menu")])
    return ("\n".join(lines).rstrip(), InlineKeyboardMarkup(rows))


async def _settings_view_groups() -> tuple:
    groups = await asyncio.to_thread(db.get_all_groups)
    lines = ["🏢 *Dispatchers*\n"]
    rows = []
    for g in (groups or [])[:25]:
        # record_is_active, not a raw get: a JSON-null is_active counts as ACTIVE on
        # the dispatch path, and reading it raw showed those rows here as disabled.
        active = record_is_active(g)
        lines.append(f"{'✅' if active else '⛔'} {g.get('group_name') or '(unnamed)'} `{g.get('group_telegram_id')}`")
        rows.append([InlineKeyboardButton(
            f"{'Disable' if active else 'Enable'} {g.get('group_name') or 'dispatcher'}"[:40],
            callback_data=f"tset_gtog:{g.get('id')}")])
    if not groups:
        lines.append("_No dispatchers yet._")
    rows.append([InlineKeyboardButton("➕ Add Dispatcher", callback_data="tset_gadd")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="tset_menu")])
    return ("\n".join(lines), InlineKeyboardMarkup(rows))


_SETTINGS_MENU_TEXT = "⚙️ *Settings*"
# Every settings screen, keyed by the callback data its button carries — so a spoken
# or typed request opens exactly the same view the button does.
_SETTINGS_VIEWS = {
    "tset_plates": _settings_view_plates,
    "tset_groups": _settings_view_groups,
    "tset_drivers": _settings_view_drivers,
    "tset_susp": _settings_view_suspensions,
    "tset_srcs": _settings_view_sources,
    "tset_sups": _settings_view_supervisors,
    "tset_fu": _settings_view_followups,
}
# Spoken/typed navigation. Ordered: "suspend driver kita" is about suspensions even
# though it also says "driver", so suspensions is tested before drivers.
_SETTINGS_NAV = [
    # "suspen" not "suspend": suspension/suspensions do not contain the word suspend.
    (re.compile(r"\b(?:suspen\w*|unsuspend\w*|lift|penalt\w*|block\w*)\b", re.I), "tset_susp"),
    (re.compile(r"\b(?:plates?|tag\s*numbers?|control\s*numbers?)\b", re.I), "tset_plates"),
    (re.compile(r"\b(?:dispatchers?|groups?|teams?|crews?)\b", re.I), "tset_groups"),
    (re.compile(r"\b(?:drivers?|drv)\b", re.I), "tset_drivers"),
    (re.compile(r"\b(?:sources?|client\s*source|lead\s*source|origin)\b", re.I), "tset_srcs"),
    (re.compile(r"\b(?:supervisors?|admins?|administrators?|owners?)\b", re.I), "tset_sups"),
    (re.compile(r"\b(?:follow[\s-]*ups?|renewals?|reminders?)\b", re.I), "tset_fu"),
]
_SETTINGS_BACK_RE = re.compile(r"^\s*(?:back|menu|main|home|up|return)\b", re.I)
_SETTINGS_CLOSE_RE = re.compile(r"^\s*(?:close|exit|quit|dismiss|finished|done)\b", re.I)
_SETTINGS_HINT = (
    "⚙️ Say or type what you want:\n"
    "• *plate numbers*\n"
    "• *dispatchers*\n"
    "• *drivers*\n"
    "• *suspensions*\n"
    "• *client sources*\n"
    "• *supervisors*\n"
    "• *follow-ups*\n"
    "…or *back* / *close*."
)


def _settings_nav_target(text: str):
    """Which settings screen a spoken/typed phrase asks for, or None."""
    t = (text or "").strip()
    if not t:
        return None
    for rx, target in _SETTINGS_NAV:
        if rx.search(t):
            return target
    return None


async def _show_settings_view(target: str, *, query=None, message=None) -> None:
    """Render one settings screen — editing the card when it came from a button,
    posting a fresh one when it was asked for by voice or typing."""
    view = _SETTINGS_VIEWS.get(target)
    if view is None:
        text, kb = _SETTINGS_MENU_TEXT, _settings_main_kb()
    else:
        text, kb = await view()
    if query is not None:
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
    elif message is not None:
        await message.reply_text(text, parse_mode="Markdown", reply_markup=kb)


async def handle_settings_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Voice/typed control of /settings, mirroring the lead card: say "plate numbers"
    and that screen opens, with all of its buttons live. Voice arrives here already
    transcribed by the global pre-processor, so speaking and typing behave the same."""
    msg = update.effective_message
    text = ((msg.text if msg else "") or "").strip()
    if not msg or not text:
        return SET_MENU
    if not _user_is_global_supervisor(update.effective_user.id):
        return ConversationHandler.END
    # A named destination wins over a leading "back" or "close". "back to
    # drivers" and "close the drivers list" both name a screen, and testing the
    # escape words first threw that away — the operator asked for the driver list
    # and landed on the main menu, or on nothing at all.
    target = _settings_nav_target(text)
    if target:
        context.user_data.pop("tset_await", None)
        await _show_settings_view(target, message=msg)
        return SET_MENU
    if _SETTINGS_CLOSE_RE.match(text):
        context.user_data.pop("tset_await", None)
        await msg.reply_text("⚙️ Settings closed.")
        return ConversationHandler.END
    if _SETTINGS_BACK_RE.match(text):
        context.user_data.pop("tset_await", None)
        await msg.reply_text(_SETTINGS_MENU_TEXT, parse_mode="Markdown",
                             reply_markup=_settings_main_kb())
        return SET_MENU
    await msg.reply_text(_SETTINGS_HINT, parse_mode="Markdown",
                         reply_markup=_settings_main_kb())
    return SET_MENU


async def handle_settings_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if not _user_is_global_supervisor(update.effective_user.id):
        return ConversationHandler.END
    data = query.data
    if data == "tset_menu":
        context.user_data.pop("tset_await", None)
        await query.edit_message_text("⚙️ *Settings*", parse_mode="Markdown", reply_markup=_settings_main_kb())
        return SET_MENU
    if data == "tset_plates":
        await _show_settings_view("tset_plates", query=query); return SET_MENU
    if data == "tset_groups":
        await _show_settings_view("tset_groups", query=query); return SET_MENU
    if data == "tset_close":
        context.user_data.pop("tset_await", None)
        await query.edit_message_text("⚙️ Settings closed."); return ConversationHandler.END
    if data.startswith("tset_pf:"):
        field = data.split(":", 1)[1]
        context.user_data["tset_await"] = {"kind": "plate", "field": field}
        await query.message.reply_text(
            f"Send the new *{_PLATE_SET_LABELS.get(field, field)}* (digits only).", parse_mode="Markdown")
        return SET_INPUT
    if data.startswith("tset_gtog:"):
        await asyncio.to_thread(db.toggle_group_status, data.split(":", 1)[1])
        await _show_settings_view("tset_groups", query=query); return SET_MENU
    if data == "tset_fu":
        await _show_settings_view("tset_fu", query=query); return SET_MENU
    if data in ("tset_fuemail", "tset_fuphone", "tset_fuids", "tset_futeam"):
        what = {"tset_fuemail": ("fu_email", "email a copy of every client message goes to"),
                "tset_fuphone": ("fu_phone", "phone number clients are told to call"),
                "tset_fuids": ("fu_ids", "chat ids that get the reminders (space or comma "
                                          "separated, or *-* for every supervisor)"),
                "tset_futeam": ("fu_team", "dispatch team chat id (*-* to use the first "
                                            "active dispatcher)")}[data]
        context.user_data["tset_await"] = {"kind": what[0]}
        await query.message.reply_text(f"Send the new {what[1]}.", parse_mode="Markdown")
        return SET_INPUT
    if data == "tset_sups":
        await _show_settings_view("tset_sups", query=query); return SET_MENU
    if data == "tset_supadd":
        context.user_data["tset_await"] = {"kind": "add_supervisor"}
        await query.message.reply_text(
            "Send the new supervisor as: *telegram_id* or *Name | telegram_id*\n"
            "They can get their id by sending /whoami to this bot.",
            parse_mode="Markdown")
        return SET_INPUT
    if data.startswith("tset_supdel:"):
        await asyncio.to_thread(_remove_extra_supervisor, data.split(":", 1)[1])
        await _show_settings_view("tset_sups", query=query); return SET_MENU
    if data == "tset_gadd":
        context.user_data["tset_await"] = {"kind": "add_group"}
        await query.message.reply_text(
            "Send the new dispatcher as: *Name | -100xxxxxxxxxx*", parse_mode="Markdown")
        return SET_INPUT
    # --- drivers -------------------------------------------------------
    if data == "tset_drivers":
        await _show_settings_view("tset_drivers", query=query); return SET_MENU
    if data.startswith("tset_drv:"):
        await _show_driver_detail(query, data.split(":", 1)[1])
        return SET_MENU
    if data.startswith("tset_dtog:"):
        did = data.split(":", 1)[1]
        await asyncio.to_thread(db.toggle_driver_status, did)
        _bust_driver_caches()
        # Back to that driver, not to the list — you are mid-decision about them.
        await _show_driver_detail(query, did)
        return SET_MENU
    if data.startswith("tset_dsusp:"):
        did = data.split(":", 1)[1]
        ok = await asyncio.to_thread(db.set_driver_suspended, did, True)
        _bust_driver_caches()
        if not ok:
            await query.message.reply_text(
                "⚠️ Manual suspension needs one database change first — run "
                "`database/migration_driver_manual_suspend.sql` in the Supabase SQL "
                "editor. Receipt-debt suspensions work without it.",
                parse_mode="Markdown")
        await _show_driver_detail(query, did)
        return SET_MENU
    if data.startswith("tset_dlift:"):
        did = data.split(":", 1)[1]
        await asyncio.to_thread(db.set_driver_suspended, did, False)
        # A receipt-debt suspension only lifts once the debt stops counting, so
        # excuse the outstanding receipts exactly as an accepted appeal does.
        waived = await asyncio.to_thread(db.waive_driver_pending_receipts, did)
        _bust_driver_caches()
        driver = await asyncio.to_thread(_driver_row_by_id, did)
        if driver:
            try:
                pending_after = await asyncio.to_thread(db.get_driver_pending_receipts, did)
                await _notify_suspension_lifted(
                    context, driver=driver, pending_after=pending_after)
            except Exception as e:
                logger.warning("suspension-lift notice failed: %s", e)
        if waived:
            logger.info("Lifted suspension for %s and waived %s receipt(s)", did, waived)
        await _show_driver_detail(query, did)
        return SET_MENU
    if data == "tset_dadd":
        context.user_data["tset_await"] = {"kind": "add_driver"}
        await query.message.reply_text(
            "Send the new driver as: *Name | telegram_id*\n"
            "Phone and email are optional and shown on this screen:\n"
            "*Name | telegram_id | 555-123-4567 | driver@example.com*",
            parse_mode="Markdown")
        return SET_INPUT
    # --- suspensions ---------------------------------------------------
    if data == "tset_susp":
        await _show_settings_view("tset_susp", query=query); return SET_MENU
    if data.startswith("tset_suspon:"):
        did = data.split(":", 1)[1]
        ok = await asyncio.to_thread(db.set_driver_suspended, did, True)
        _bust_driver_caches()
        if not ok:
            await query.message.reply_text(
                "⚠️ Manual suspension needs one database change first — run "
                "`database/migration_driver_manual_suspend.sql` in the Supabase SQL "
                "editor. Receipt-debt suspensions work without it.",
                parse_mode="Markdown")
        await _show_settings_view("tset_susp", query=query); return SET_MENU
    if data.startswith("tset_susplift:"):
        did = data.split(":", 1)[1]
        await asyncio.to_thread(db.set_driver_suspended, did, False)
        # A receipt-debt suspension only lifts once the debt stops counting, so
        # excuse the outstanding receipts exactly as an accepted appeal does.
        waived = await asyncio.to_thread(db.waive_driver_pending_receipts, did)
        _bust_driver_caches()
        driver = await asyncio.to_thread(_driver_row_by_id, did)
        if driver:
            try:
                pending_after = await asyncio.to_thread(db.get_driver_pending_receipts, did)
                await _notify_suspension_lifted(
                    context, driver=driver, pending_after=pending_after)
            except Exception as e:
                logger.warning("suspension-lift notice failed: %s", e)
        if waived:
            await query.message.reply_text(f"✅ Lifted — excused {waived} outstanding receipt(s).")
        await _show_settings_view("tset_susp", query=query); return SET_MENU
    # --- client sources ------------------------------------------------
    if data == "tset_srcs":
        await _show_settings_view("tset_srcs", query=query); return SET_MENU
    if data.startswith("tset_stog:"):
        _, sid, want = data.split(":", 2)
        await asyncio.to_thread(db.set_contact_info_source_active, sid, want == "1")
        await _show_settings_view("tset_srcs", query=query); return SET_MENU
    if data == "tset_sadd":
        context.user_data["tset_await"] = {"kind": "add_source"}
        await query.message.reply_text("Send the new client source name (e.g. *Instagram*).",
                                       parse_mode="Markdown")
        return SET_INPUT
    return SET_MENU


async def _show_driver_detail(query, driver_id: str) -> None:
    """Draw one driver's screen in place of whatever is on the message."""
    driver = await asyncio.to_thread(_driver_row_by_id, driver_id)
    if not driver:
        await _show_settings_view("tset_drivers", query=query)
        return
    suspended = await asyncio.to_thread(_get_suspended_driver_ids)
    text, kb = _driver_detail(driver, suspended)
    try:
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            await query.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)


async def apply_settings_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _user_is_global_supervisor(update.effective_user.id):
        context.user_data.pop("tset_await", None)
        return ConversationHandler.END
    st = context.user_data.pop("tset_await", None) or {}
    text = (update.message.text or "").strip()
    # "back" / "close" must escape an input prompt too, or a spoken command would be
    # swallowed as the value being asked for.
    if _SETTINGS_CLOSE_RE.match(text):
        await update.message.reply_text("⚙️ Settings closed.")
        return ConversationHandler.END
    if _SETTINGS_BACK_RE.match(text):
        await update.message.reply_text(_SETTINGS_MENU_TEXT, parse_mode="Markdown",
                                        reply_markup=_settings_main_kb())
        return SET_MENU

    async def _retry(msg: str) -> int:
        """Bad input keeps the prompt alive instead of dropping out of /settings."""
        context.user_data["tset_await"] = st
        await update.message.reply_text(msg)
        return SET_INPUT

    if st.get("kind") == "add_driver":
        parts = [p.strip() for p in text.split("|")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            return await _retry("❌ Format: Name | telegram_id")
        if not re.fullmatch(r"-?\d{5,}", parts[1]):
            return await _retry("❌ The telegram_id must be digits — the driver can get theirs from /whoami.")
        phone = parts[2] if len(parts) > 2 and parts[2] else None
        email = parts[3] if len(parts) > 3 and parts[3] else None
        ok = await asyncio.to_thread(db.create_driver, parts[0], parts[1], phone, email)
        _bust_driver_caches()
        await update.message.reply_text(
            (f"✅ Added driver “{parts[0]}”." if ok else "❌ Could not add the driver."),
            reply_markup=_settings_main_kb())
        return SET_MENU
    if st.get("kind") in ("fu_email", "fu_phone", "fu_ids", "fu_team"):
        kind = st["kind"]
        key = {"fu_email": FU_EMAIL_KEY, "fu_phone": FU_PHONE_KEY,
               "fu_ids": FU_CHATIDS_KEY, "fu_team": FU_TEAM_CHAT_KEY}[kind]
        raw = "" if text.strip() in ("-", "—") else text.strip()
        if raw and kind == "fu_email":
            raw = ai_vision.normalize_email(raw) or ""
            if not raw:
                return await _retry("❌ That is not an email address.")
        if raw and kind == "fu_phone":
            raw = _clean_inline_value("phone", raw)
            if not raw:
                return await _retry("❌ That is not a phone number.")
        if raw and kind in ("fu_ids", "fu_team"):
            found = [t for t in re.split(r"[\s,;]+", raw) if _parse_chat_id(t) is not None]
            if not found:
                return await _retry("❌ Send numeric chat ids — /whoami gives you yours.")
            raw = " ".join(found) if kind == "fu_ids" else found[0]
        ok = await asyncio.to_thread(db.set_setting, key, raw)
        await update.message.reply_text(
            ("✅ Saved." if raw else "✅ Cleared — back to the default.") if ok
            else "❌ Could not save it.",
            reply_markup=_settings_main_kb())
        return SET_MENU
    if st.get("kind") == "add_supervisor":
        parts = [p.strip() for p in text.split("|")]
        cid = parts[-1] if parts else ""
        label = parts[0] if len(parts) > 1 else ""
        if not re.fullmatch(r"-?\d{5,}", cid or ""):
            return await _retry(
                "❌ Send the numeric telegram id (or *Name | id*). "
                "They can get theirs from /whoami.")
        if _user_is_global_supervisor(cid):
            await update.message.reply_text(
                "That person is already a supervisor.", reply_markup=_settings_main_kb())
            return SET_MENU
        ok = await asyncio.to_thread(_add_extra_supervisor, cid, label)
        await update.message.reply_text(
            (f"✅ {label or cid} can now open /settings." if ok
             else "❌ Could not add the supervisor."),
            reply_markup=_settings_main_kb())
        return SET_MENU
    if st.get("kind") == "add_source":
        label = text.strip()
        if not label or len(label) > 60:
            return await _retry("❌ Send a short source name (1–60 characters).")
        ok = await asyncio.to_thread(db.create_contact_info_source, label)
        await update.message.reply_text(
            (f"✅ Added client source “{label}”." if ok else "❌ Could not add the source."),
            reply_markup=_settings_main_kb())
        return SET_MENU
    if st.get("kind") == "plate":
        digits = re.sub(r"\D", "", text)
        if not digits:
            return await _retry("❌ Digits only — send the number again.")
        ok = await asyncio.to_thread(db.update_plate_settings, {st["field"]: int(digits)})
        await update.message.reply_text(
            (f"✅ {_PLATE_SET_LABELS.get(st['field'], st['field'])} set to {int(digits)}." if ok
             else "❌ Could not update."), reply_markup=_settings_main_kb())
        return SET_MENU
    if st.get("kind") == "add_group":
        parts = [p.strip() for p in text.split("|")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            return await _retry("❌ Format: Name | -100xxxxxxxxxx")
        ok = await asyncio.to_thread(db.create_group, parts[0], parts[1], parts[1])
        await update.message.reply_text(
            (f"✅ Added dispatcher “{parts[0]}”." if ok else "❌ Could not add the dispatcher."),
            reply_markup=_settings_main_kb())
        return SET_MENU
    return ConversationHandler.END


async def settings_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("tset_await", None)
    await update.message.reply_text("Settings cancelled.")
    return ConversationHandler.END


def _card_buttons_always_live():
    """The review card never leaves the screen, so every button on it must answer
    from ANY step of the flow — open Price, pick nothing, tap Color, choose, then
    come back to Price. A state that lists none of them silently drops the tap,
    which is what made the flow feel one-field-at-a-time.

    Appended AFTER each state's own handlers, so a state that answers a callback
    its own way still wins."""
    return [
        CallbackQueryHandler(handle_phase1_ai_review_callback, pattern=PH1_REVIEW_CB_PATTERN),
        CallbackQueryHandler(handle_phase1_color_pick, pattern=f"^{PH1_COLOR_CB}"),
        CallbackQueryHandler(handle_phase1_price_pick, pattern=f"^{PH1_PRICE_CB}"),
        CallbackQueryHandler(handle_group_selection, pattern="^select_group_"),
        CallbackQueryHandler(handle_driver_selection, pattern="^(select_driver_|driver_suspended_)"),
        CallbackQueryHandler(handle_contact_source_selection, pattern="^contact_source_"),
        CallbackQueryHandler(handle_vin_choice_callback, pattern=PH1_VIN_CHOICE_CB_PATTERN),
        # "Change another field" / "Done" / "Confirm" were registered in ONE state,
        # so they died as soon as the flow moved on or the process restarted.
        CallbackQueryHandler(
            handle_phase1_edit_followup_callback,
            pattern=f"^({PH1_EDIT_MORE}|{PH1_EDIT_DONE}|{PH1_FINAL_CONFIRM})$"),
    ]


async def _close_open_field_prompt(context, chat_id) -> None:
    """Drop the prompt still on screen for the field being abandoned, so switching
    from Price to Color does not leave a live price picker behind to be tapped into
    the wrong field later. Awaited, so the old prompt is gone before the new one
    lands rather than racing it."""
    mid = context.user_data.pop("edit_prompt_msg_id", None)
    if mid and chat_id:
        await _safe_delete_chat_message(context, chat_id, mid)


def main():
    # Before anything else, so a crash during startup is still reported.
    from utils.observability import init_sentry
    init_sentry("bot")
    """Main function to start the bot."""
    logger.info("Bot starting...")
    sys.stdout.flush()
    sys.stderr.flush()

    # Validate configuration
    try:
        Config.validate()
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        logger.error("\nPlease check your .env file and ensure all required environment variables have non-empty values.")
        return

    bot_token = Config.TELEGRAM_BOT_TOKEN
    if not _wait_for_exclusive_polling(bot_token, max_wait=120):
        logger.error("Could not acquire polling slot after 120s. Exiting.")
        sys.exit(1)

    # Create application
    async def _post_init_set_commands(app: Application) -> None:
        try:
            from telegram import BotCommand
            await app.bot.set_my_commands([
                BotCommand("start", "Open the bot"),
                BotCommand("leaderboard", "Who has entered the most clients"),
                BotCommand("lead", "Add a new client/lead"),
                BotCommand("newclient", "Add a new client/lead"),
                BotCommand("newsale", "Add a new client/lead"),
                BotCommand("tag", "Add a new client/lead (temp tag)"),
                BotCommand("newtag", "Add a new client/lead (temp tag)"),
                BotCommand("enterlead", "Add a new client/lead"),
                BotCommand("followup", "Bot reminds a client by text/email (temp tag info)"),
                BotCommand("followups", "List your open follow-ups"),
                BotCommand("allfollowups", "Supervisors: view/stop/delete all follow-ups"),
                BotCommand("receipts", "Upload receipts"),
                BotCommand("appeal", "Appeal / cancel a delivery"),
                BotCommand("cancel", "Cancel and restart"),
                BotCommand("help", "Show the usage guide"),
            ])
            logger.info("Bot command menu set")
        except Exception as e:
            logger.warning("Could not set command menu: %s", e)

    application = (
        Application.builder()
        .token(Config.TELEGRAM_BOT_TOKEN)
        # Different chats in parallel, each chat still in order. See
        # PerChatUpdateProcessor for why the blanket switch would not do.
        .concurrent_updates(PerChatUpdateProcessor(max_concurrent_updates=32))
        .post_init(_post_init_set_commands)
        .build()
    )

    # Clear webhook before polling (avoids 409 when webhook was set elsewhere)
    import requests
    delete_url = f"https://api.telegram.org/bot{bot_token}/deleteWebhook"
    try:
        logger.info("Clearing webhook...")
        resp = requests.post(delete_url, json={"drop_pending_updates": True}, timeout=5)
        if resp.status_code == 200 and resp.json().get("ok"):
            logger.info("Webhook cleared — safe to poll.")
        else:
            logger.warning("deleteWebhook response: %s", resp.text)
    except Exception as e:
        logger.warning("Could not clear webhook (continuing): %s", e)
    time.sleep(0.15)

    _conflict_logged = False

    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle errors — on Conflict, hard-exit so Render restarts us."""
        nonlocal _conflict_logged
        error = context.error

        if isinstance(error, Conflict):
            if not _conflict_logged:
                _conflict_logged = True
                logger.error(
                    "TELEGRAM CONFLICT: another process is polling this token. "
                    "Hard-exiting so Render restarts a clean instance."
                )
                sys.stdout.flush()
                sys.stderr.flush()
                os._exit(1)
            return

        if isinstance(error, Exception):
            error_type = type(error).__name__
            if not hasattr(error_handler, f'_logged_{error_type}'):
                logger.error(f"Exception while handling an update: {error}", exc_info=error)
                setattr(error_handler, f'_logged_{error_type}', True)
            else:
                # Full traceback only once per type, but NEVER hide recurrences —
                # invisible repeats made prod failures look like "does nothing".
                logger.error(f"Exception while handling an update ({error_type}): {error}")
    
    # Add error handler - must be added before handlers
    application.add_error_handler(error_handler)
    
    # Create conversation handler for lead creation
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler(
                ["lead", "client", "newclient", "newsale", "enterlead", "enterclient", "newtag", "tag"],
                begin_lead_command,
            ),
            # /cancel and /restart are the same action, so both open the flow.
            CommandHandler(["restart", "cancel"], restart_command),
            CallbackQueryHandler(handle_driver_add_lead_callback, pattern="^driver_add_lead$"),
            CallbackQueryHandler(handle_another_tag_callback, pattern="^another_tag_"),
            CallbackQueryHandler(handle_driver_add_receipt_callback, pattern="^driver_add_receipt$"),
            CallbackQueryHandler(handle_resend_driver, pattern="^resend_driver_"),
            CallbackQueryHandler(handle_reassign_group_pick, pattern="^reassign_group_"),
            CallbackQueryHandler(handle_instant_pdf_request, pattern=f"^{INSTANT_PDF_CB}"),
            # Re-enter lead flow from inline buttons when CH in-memory state was lost (restart, multi-worker,
            # or rare routing gaps) but Supabase ``states`` still holds select_group / select_driver data.
            CallbackQueryHandler(handle_group_selection, pattern="^select_group_"),
            CallbackQueryHandler(handle_driver_selection, pattern="^(select_driver_|driver_suspended_)"),
            CallbackQueryHandler(handle_phase1_color_pick, pattern=f"^{PH1_COLOR_CB}"),
            CallbackQueryHandler(handle_phase1_price_pick, pattern=f"^{PH1_PRICE_CB}"),
            # A review card outlives the process that drew it. Every button on it is
            # an entry point too, so a redeploy (or a second worker) can still answer
            # the tap by reading the lead back out of Supabase.
            CallbackQueryHandler(handle_phase1_ai_review_callback,
                                 pattern=PH1_REVIEW_CB_PATTERN),
            CallbackQueryHandler(handle_vin_choice_callback,
                                 pattern=PH1_VIN_CHOICE_CB_PATTERN),
            CallbackQueryHandler(handle_contact_source_selection, pattern="^contact_source_"),
            # Idle natural-language / voice start: any substantive text starts a lead
            # and auto-fills what's given. MUST be last (only plain text reaches it).
            MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_idle_lead_start),
            # Same for a photo/PDF: a forwarded screenshot IS the lead details, and
            # with no lead running it previously reached nothing at all.
            MessageHandler(
                (filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND & filters.ChatType.PRIVATE,
                handle_idle_media_start,
            ),
        ],
        states={
            STATE_PHASE1: [
                CallbackQueryHandler(
                    handle_phase1_vision_batch_callback,
                    pattern=r"^phase1_vision_(cancel|photo|done)$",
                ),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_phase1),
                MessageHandler(filters.PHOTO, handle_phase1_photo),
                MessageHandler(filters.Document.ALL, handle_phase1_document),
            ] + _card_buttons_always_live(),
            STATE_AI_REVIEW: [
                CallbackQueryHandler(
                    handle_phase1_ai_review_callback,
                    pattern=PH1_REVIEW_CB_PATTERN,
                ),
                # Inline edits with no Edit button: type 'price $50' / 'phone 555-1234'
                # or send a photo/PDF, and it's parsed straight into the review.
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_phase1_review_message),
                MessageHandler(filters.PHOTO, handle_phase1_review_message),
                MessageHandler(filters.Document.ALL, handle_phase1_review_message),
                # A picker posted while in a SELECT state stays tappable after a typed
                # edit pulled the conversation back here (its patterns are otherwise
                # only entry points, which an ACTIVE conversation never consults).
                CallbackQueryHandler(handle_group_selection, pattern="^select_group_"),
                CallbackQueryHandler(handle_driver_selection, pattern="^select_driver_"),
                CallbackQueryHandler(handle_contact_source_selection, pattern="^contact_source_"),
                CallbackQueryHandler(handle_phase1_color_pick, pattern=f"^{PH1_COLOR_CB}"),
                CallbackQueryHandler(handle_phase1_price_pick, pattern=f"^{PH1_PRICE_CB}"),
                # The DMV Yes/No card stays on screen while other edits are made, so
                # its buttons must still resolve if the conversation drifts back here.
                CallbackQueryHandler(handle_vin_choice_callback,
                                     pattern="^(vin_use|vin_keep|vin_retype)$"),
            ] + _card_buttons_always_live(),
            STATE_ADJUST_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_phase1_adjust_input),
                MessageHandler(filters.PHOTO, handle_phase1_adjust_input),
                MessageHandler(filters.Document.ALL, handle_phase1_adjust_input),
            ] + _card_buttons_always_live(),
            STATE_AI_EDIT_MENU: [
                CallbackQueryHandler(handle_phase1_edit_menu_callback,
                                     pattern=PH1_EDIT_MENU_CB_PATTERN),
                # Single-line typed/voice edits keep working while the edit picker is
                # open ('name John Damian', 'price 150') — they apply and re-render the
                # review card instead of dying in a button-only state.
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_phase1_review_message),
                MessageHandler(filters.PHOTO, handle_media_in_any_state),
                MessageHandler(filters.Document.ALL, handle_media_in_any_state),
            ] + _card_buttons_always_live(),
            STATE_AI_EDIT_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_phase1_edit_input),
                MessageHandler(filters.PHOTO, handle_edit_field_photo),
                MessageHandler(filters.Document.ALL, handle_edit_field_photo),
                CallbackQueryHandler(handle_phase1_color_pick, pattern=f"^{PH1_COLOR_CB}"),
                CallbackQueryHandler(handle_phase1_price_pick, pattern=f"^{PH1_PRICE_CB}"),
                CallbackQueryHandler(
                    handle_phase1_edit_followup_callback,
                    pattern=f"^({PH1_EDIT_MORE}|{PH1_EDIT_DONE}|{PH1_FINAL_CONFIRM})$",
                ),
            ] + _card_buttons_always_live(),
            STATE_EDIT_FIELD_PROMPT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_edit_field_text),
                # A photo/PDF sent at a field prompt: for Color the AI reads the colour
                # off the picture, anything else goes through the review parser.
                MessageHandler(filters.PHOTO, handle_edit_field_photo),
                MessageHandler(filters.Document.ALL, handle_edit_field_photo),
                CallbackQueryHandler(handle_phase1_color_pick, pattern=f"^{PH1_COLOR_CB}"),
                CallbackQueryHandler(handle_phase1_price_pick, pattern=f"^{PH1_PRICE_CB}"),
            ] + _card_buttons_always_live(),
            STATE_MISSING_FIELD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_missing_field),
                MessageHandler(filters.PHOTO, handle_media_in_any_state),
                MessageHandler(filters.Document.ALL, handle_media_in_any_state),
            ] + _card_buttons_always_live(),
            STATE_PHASE2: [
                MessageHandler(
                    (
                        filters.TEXT
                        | filters.PHOTO
                        | filters.Document.ALL
                        | filters.VIDEO
                        | filters.VOICE
                        | filters.Sticker.ALL
                        | filters.ANIMATION
                    )
                    & ~filters.COMMAND,
                    handle_phase2,
                ),
            ] + _card_buttons_always_live(),
            STATE_SPECIAL_REQUEST_ISSUERS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_special_request_issuers),
                MessageHandler(filters.PHOTO, handle_media_in_any_state),
                MessageHandler(filters.Document.ALL, handle_media_in_any_state),
            ] + _card_buttons_always_live(),
            STATE_SPECIAL_REQUEST_DRIVERS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_special_request_drivers),
                MessageHandler(filters.PHOTO, handle_media_in_any_state),
                MessageHandler(filters.Document.ALL, handle_media_in_any_state),
            ] + _card_buttons_always_live(),
            STATE_VIN_CHOICE: [
                CallbackQueryHandler(handle_vin_choice_callback, pattern="^(vin_use|vin_keep|vin_retype)$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_vin_choice_text),
                MessageHandler(filters.PHOTO, handle_media_in_any_state),
                MessageHandler(filters.Document.ALL, handle_media_in_any_state),
            ] + _card_buttons_always_live(),
            STATE_VIN_RETYPE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_vin_retype),
                MessageHandler(filters.PHOTO, handle_media_in_any_state),
                MessageHandler(filters.Document.ALL, handle_media_in_any_state),
            ] + _card_buttons_always_live(),
            # Typed/voice text during the pickers routes through the review editor
            # (live lead) or a button nudge — never a silent drop.
            STATE_SELECT_GROUP: [
                CallbackQueryHandler(handle_group_selection, pattern="^select_group_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_select_state_text),
                MessageHandler(filters.PHOTO, handle_media_in_any_state),
                MessageHandler(filters.Document.ALL, handle_media_in_any_state),
            ] + _card_buttons_always_live(),
            STATE_SELECT_DRIVER: [
                CallbackQueryHandler(handle_driver_selection, pattern="^(select_driver_|driver_suspended_)"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_select_state_text),
                MessageHandler(filters.PHOTO, handle_media_in_any_state),
                MessageHandler(filters.Document.ALL, handle_media_in_any_state),
            ] + _card_buttons_always_live(),
            STATE_SELECT_CONTACT_SOURCE: [
                CallbackQueryHandler(handle_contact_source_selection, pattern="^contact_source_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_select_state_text),
                MessageHandler(filters.PHOTO, handle_media_in_any_state),
                MessageHandler(filters.Document.ALL, handle_media_in_any_state),
            ] + _card_buttons_always_live(),
        },
        fallbacks=[
            CommandHandler("cancel", cancel_from_lead_conversation),
            CommandHandler("restart", restart_command),
            CommandHandler("start", start),
            CommandHandler(
                ["lead", "client", "newclient", "newsale", "enterlead", "enterclient", "newtag", "tag"],
                begin_lead_command,
            ),
            CallbackQueryHandler(handle_driver_add_lead_callback, pattern="^driver_add_lead$"),
            CallbackQueryHandler(handle_another_tag_callback, pattern="^another_tag_"),
            CallbackQueryHandler(handle_driver_add_receipt_callback, pattern="^driver_add_receipt$"),
            # Reassign driver belongs here too. It was an entry point only, and
            # entry points are not consulted while a conversation is active — so
            # the moment the issuer started their next lead, the button on the
            # previous card (and the timeout job's "Pick new driver", which arrives
            # ten minutes later) reached nothing at all.
            CallbackQueryHandler(handle_resend_driver, pattern="^resend_driver_"),
            CallbackQueryHandler(handle_reassign_group_pick, pattern="^reassign_group_"),
            CallbackQueryHandler(handle_instant_pdf_request, pattern=f"^{INSTANT_PDF_CB}"),
        ],
    )

    # Receipt handler is registered before conv_handler: /receipt and /receipts are entry_points
    # (works when idle) and fallbacks (resets stuck upload flow). Issuer lead flow stays active
    # in conv_handler when a driver sends /receipt here — only drivers see the owed-receipts menu.
    _receipt_image_filter = (
        filters.PHOTO
        | filters.Document.MimeType("image/jpeg")
        | filters.Document.MimeType("image/png")
        | filters.Document.MimeType("image/webp")
    )
    receipt_handler = ConversationHandler(
        entry_points=[
            CommandHandler(["receipt", "receipts", "recipts"], handle_driver_receipts_menu_command),
            CallbackQueryHandler(handle_driver_receipt_callback, pattern="^driver_receipt$"),
            CallbackQueryHandler(handle_receipt_for_ref_callback, pattern="^receipt_for_"),
            # Confirm/Cancel were registered only inside the confirm state, and there
            # is no persistence — so a redeploy between showing the buttons and the
            # driver tapping one left them inert. The handler already rebuilds its
            # context from the DB (_merge_receipt_context_from_db).
            CallbackQueryHandler(handle_receipt_confirm_callback,
                                 pattern="^(confirm_receipt|cancel_receipt)$"),
        ],
        states={
            STATE_WAITING_REFERENCE_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_reference_id_input),
                # A photo here is not a reference id, but leaving it unhandled let it
                # fall through to the idle-lead catch-all, which started a LEAD and
                # wiped the receipt session.
                MessageHandler(filters.PHOTO | filters.Document.ALL,
                               handle_reference_id_stray),
            ],
            STATE_WAITING_RECEIPT_CONFIRM: [
                CallbackQueryHandler(handle_receipt_confirm_callback,
                                     pattern="^(confirm_receipt|cancel_receipt)$"),
                # It asks "Please confirm this is the correct lead" and used to
                # understand no word for yes — a typed or spoken answer reached
                # nothing at all.
                MessageHandler(filters.TEXT & ~filters.COMMAND,
                               handle_receipt_confirm_words),
            ],
            STATE_WAITING_RECEIPT_IMAGE: [
                MessageHandler(_receipt_image_filter, handle_receipt_image),
                MessageHandler(
                    (filters.TEXT | filters.Document.ALL) & ~filters.COMMAND,
                    handle_receipt_image_stray,
                ),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_from_receipt_conversation),
            CommandHandler("start", start),
            CommandHandler(["receipt", "receipts", "recipts"], handle_driver_receipts_menu_command),
            CallbackQueryHandler(handle_driver_add_receipt_callback, pattern="^driver_add_receipt$"),
            # Supervisor drill-down: pick a different ref while confirm/upload is active.
            CallbackQueryHandler(handle_receipt_for_ref_callback, pattern="^receipt_for_"),
        ],
    )

    application.add_handler(receipt_handler)

    from appeal_flow import register_appeal_handlers

    register_appeal_handlers(
        application,
        {
            "db": db,
            "empty_inline_kb": _EMPTY_INLINE_KB,
            "user_is_global_supervisor": _user_is_global_supervisor,
            "global_supervisory_chat_ids": _global_supervisory_chat_ids,
            "driver_row_for_telegram_user": _driver_row_for_telegram_user,
            "driver_accepted_this_lead": _driver_accepted_this_lead,
            "client_display_name_from_lead": _client_display_name_from_lead,
            "short_uuid": _short_uuid,
            "long_uuid": _long_uuid,
            "telegram_download_url_from_file_path": _telegram_download_url_from_file_path,
            "clear_lead_conversation_user_data": _clear_lead_conversation_user_data,
            "restart_bot_from_top": _restart_bot_from_top,
            "sanitize_phones_for_send": _sanitize_phones_for_send,
            "start_handler": start,
        },
    )

    # Client follow-up capture flow (/followup) — registered before conv_handler.
    # Single-message form with tap-to-fill inline buttons (same style as the lead editor).
    followup_conv = ConversationHandler(
        entry_points=[CommandHandler(["followup", "prospect"], cmd_followup_start)],
        states={
            STATE_FU_MENU: [
                CallbackQueryHandler(
                    handle_fu_menu_callback,
                    # fud_ takes WORDS, not just digits: the picker's "⚡ Now" button sends
                    # fud_now, and \d+ rejected it, so the only way to undo a
                    # chosen start day reached no handler and spun forever.
                    pattern="^(fuf_\\w+|fud_\\w+|fut_\\w+|fufr_\\w+|fue_\\w+)$",
                ),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_fu_menu_text),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_followup_cancel)],
    )
    application.add_handler(followup_conv)

    # /settings (supervisors): plate counters + group management. Registered
    # before the main conversation so its input step captures the typed value.
    settings_conv = ConversationHandler(
        entry_points=[
            CommandHandler(["settings", "setting"], cmd_settings),
            # The /drivers roster posts settings buttons OUTSIDE /settings, and a
            # restart drops the conversation anyway — so every settings button is an
            # entry point too, or the tap reaches nothing at all. handle_settings_cb
            # gates on supervisor either way.
            CallbackQueryHandler(handle_settings_cb, pattern=r"^tset_"),
        ],
        states={
            SET_MENU: [
                CallbackQueryHandler(handle_settings_cb, pattern=r"^tset_"),
                # Say or type "plate numbers" and that screen opens — the state had
                # button handlers only, so text here used to be dropped entirely.
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_settings_text),
            ],
            SET_INPUT: [
                CallbackQueryHandler(handle_settings_cb, pattern=r"^tset_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, apply_settings_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", settings_cancel)],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(settings_conv)
    # A plate photo sent inside /settings must reach the tag reader, not be deferred.
    global _SETTINGS_CONV_HANDLER
    _SETTINGS_CONV_HANDLER = settings_conv

    application.add_handler(conv_handler)

    # Let the group -2 safety net read/repair this conversation's in-memory state.
    global _MAIN_CONV_HANDLER
    _MAIN_CONV_HANDLER = conv_handler

    # Supervisor plate-from-image reader — works in ANY state (like voice). At group -1 it
    # inspects a supervisor's photo/PDF FIRST: if the AI reads a temp-tag number it stages a
    # plate-counter update and stops; if it's NOT a tag (a lead's title/license image) it
    # returns so the image flows on to the active conversation's own handler.
    _plate_img_filter = (
        filters.PHOTO
        | filters.Document.MimeType("application/pdf")
        | filters.Document.MimeType("image/jpeg")
        | filters.Document.MimeType("image/png")
        | filters.Document.MimeType("image/webp")
    )
    application.add_handler(
        MessageHandler(
            _plate_img_filter & filters.ChatType.PRIVATE & ~filters.COMMAND,
            handle_supervisor_plate_image,
        ),
        group=-1,
    )

    # Voice notes work EVERYWHERE. This MUST be the lowest group number in the
    # file: PTB runs groups lowest-first, so anything numerically below it reads
    # the update before there is any text to read. It was at -5 while
    # handle_cf_edit_reply sat at -45, which meant a SPOKEN answer to a follow-up
    # reminder was transcribed only after the handler that wanted it had already
    # been offered — and passed on — the wordless voice note.
    application.add_handler(
        MessageHandler(filters.VOICE | filters.AUDIO, _global_voice_to_text),
        group=-50,
    )

    # Commands without the slash (group -3: after transcription, before everything
    # else): "settings" runs /settings, spoken or typed. Rewrites the message into a
    # real command and lets it flow on, so PTB routes it through the same handler.
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
            _bare_command_to_slash,
        ),
        group=-3,
    )

    # The new email/phone typed after tapping Edit on a follow-up reminder. Its own
    # group: a reminder is answered wherever it was read, inside no conversation.
    # Runs after the transcriber at -50, so a SPOKEN answer arrives as text.
    #
    # No chat-type filter, deliberately: the follow-up keyboard posts its Edit
    # prompt into the dispatch GROUP, and requiring a private chat meant the
    # answer typed right underneath it was refused. Safe because the handler
    # returns immediately unless that user tapped Edit in the last five minutes.
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_cf_edit_reply),
        group=-45,
    )

    # A driver answering their offer in words (its OWN group -4, after voice
    # transcription so a SPOKEN "accept" works too — only one handler per group
    # ever runs, so sharing a group with the review-edit net would mute one of them):
    # "accept" / "yes" runs the same acceptance the Accept button does. Only fires
    # for a driver who actually has an open offer; everything else flows on.
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
            handle_driver_word_answer,
        ),
        group=-4,
    )

    # Review-edit safety net (group -2: after voice transcription, before every
    # conversation): a typed/spoken field edit for a live review card is applied
    # even when the conversation sits in a button-only state, so a single-line
    # edit ('name John Damian', 'price 150') is never silently dropped.
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
            handle_review_edit_anywhere,
        ),
        group=-2,
    )

    # /help + inline ❓ Help — outside ConversationHandler so they work during any flow.
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CallbackQueryHandler(handle_help_callback, pattern=r"^bot_help$"))

    # Secret supervisory-only commands: status (/test) + toggle (/driverblock).
    application.add_handler(CommandHandler("test", cmd_test))
    # Self-check (anyone): shows your Telegram ID + supervisor/AI status.
    application.add_handler(CommandHandler(["whoami", "id", "me"], cmd_whoami))
    # Who has entered the most clients. Open to everyone — it is a scoreboard.
    application.add_handler(
        CommandHandler(["leaderboard", "stats", "board", "ranking"], cmd_leaderboard))
    application.add_handler(CommandHandler("driverblock", cmd_driverblock))
    # Supervisory-only driver roster (phone / email / chat id), split over as many
    # messages as it takes — the /settings screen is capped at one.
    application.add_handler(CommandHandler(["drivers", "driverlist", "roster"], cmd_drivers))
    # Supervisor broadcast to all groups + drivers + lead senders.
    application.add_handler(CommandHandler("announce", cmd_announce))
    # Confirm/cancel for the supervisor voice/text router's destructive actions.
    application.add_handler(CallbackQueryHandler(handle_router_confirm_callback, pattern=r"^route_(do|no):"))

    # Add accept/decline handlers for driver assignments
    application.add_handler(CallbackQueryHandler(handle_accept_lead, pattern="^accept_lead_"))
    application.add_handler(CallbackQueryHandler(handle_decline_lead, pattern="^decline_lead_"))
    # Post-accept reassign (driver changed their mind / dispatch pulls it back)
    application.add_handler(CallbackQueryHandler(handle_reassign_lead, pattern="^reassign_lead_"))
    
    # Add accept/decline handlers for group broadcast offers
    application.add_handler(CallbackQueryHandler(handle_accept_group_offer, pattern="^ag_"))
    application.add_handler(CallbackQueryHandler(handle_different_team_offer, pattern="^dt_"))
    application.add_handler(CallbackQueryHandler(handle_decline_group_offer, pattern="^dg_"))

    # Post-dispatch insurance-card prompt (NY FS-20 PDF + Resend email).
    application.add_handler(
        CallbackQueryHandler(handle_insurance_card_decision, pattern=r"^ins_card_(yes|no)_")
    )

    # Supervisor receipts navigation (drivers list <-> driver's refs).
    # Top-level so the back/forward buttons work even while inside another
    # conversation; the actual upload flow still goes through receipt_handler.
    application.add_handler(
        CallbackQueryHandler(handle_supervisor_receipts_nav, pattern=r"^recsup_(dri_.+|back)$")
    )

    # Renewal accept / reassign handlers
    application.add_handler(CallbackQueryHandler(handle_renewal_group_accept, pattern="^rga_"))
    application.add_handler(CallbackQueryHandler(handle_renewal_group_reassign, pattern="^rgr_"))
    application.add_handler(CallbackQueryHandler(handle_renewal_driver_accept, pattern="^rda_"))
    application.add_handler(CallbackQueryHandler(handle_renewal_driver_reassign, pattern="^rdr_"))

    # Client follow-up reminder actions + list commands
    application.add_handler(CommandHandler(["followups", "myclients"], cmd_my_followups))
    application.add_handler(CommandHandler("allfollowups", cmd_all_followups))
    application.add_handler(CallbackQueryHandler(handle_cf_close, pattern="^cf_close_"))
    application.add_handler(CallbackQueryHandler(handle_cf_stop, pattern="^cf_stop_"))
    application.add_handler(CallbackQueryHandler(handle_cf_snooze, pattern="^cf_snooze_"))
    application.add_handler(CallbackQueryHandler(handle_cf_postpone, pattern="^cf_post_"))
    application.add_handler(CallbackQueryHandler(handle_cf_edit_email, pattern="^cf_email_"))
    application.add_handler(CallbackQueryHandler(handle_cf_edit_phone, pattern="^cf_phone_"))
    application.add_handler(CallbackQueryHandler(handle_cf_renew, pattern="^cf_renew_"))
    application.add_handler(CallbackQueryHandler(handle_cf_done, pattern="^cf_done_"))
    application.add_handler(CallbackQueryHandler(handle_cf_delete, pattern="^cf_del_"))
    application.add_handler(CallbackQueryHandler(handle_cf_info, pattern="^cf_info_"))
    # Driver tracking: supervisor override of the location gate
    application.add_handler(CallbackQueryHandler(handle_tracking_force, pattern="^trk_force_"))

    # Driver timeout: every minute, check for leads where no driver accepted within 10 min
    async def check_driver_timeout(context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            overdue = await asyncio.to_thread(db.get_leads_pending_driver_timeout, 10)
            for item in overdue:
                lead_id = item.get("lead_id")
                user_id = item.get("user_id")
                reference_id = item.get("reference_id", "N/A")
                drivers = item.get("drivers") or []
                driver_names = ", ".join(d.get("driver_name", "?") for d in drivers)
                # Mark FIRST to prevent spam: if mark fails (e.g. migration not run), skip sending
                if not db.mark_driver_timeout_notified(lead_id):
                    logger.error(
                        "Driver timeout: could not mark lead %s as notified (run database/migration_driver_timeout.sql). Skipping send to avoid spam.",
                        lead_id,
                    )
                    continue
                try:
                    user_chat = int(user_id) if isinstance(user_id, (int, str)) else user_id
                except (ValueError, TypeError):
                    user_chat = user_id
                for d in drivers:
                    tid = d.get("driver_telegram_id")
                    if not tid:
                        continue
                    try:
                        cid = int(str(tid).strip())
                        await context.bot.send_message(
                            chat_id=cid,
                            text=f"⏰ **Lead expired.**\n\nReference ID: `{reference_id}`\n\nNo one accepted in time.",
                            parse_mode="Markdown",
                            reply_markup=_driver_add_lead_keyboard_only(),
                        )
                    except Exception as e:
                        logger.warning("Driver timeout notify to %s: %s", tid, e)
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Pick new driver", callback_data=f"resend_driver_{lead_id}")],
                ])
                try:
                    await context.bot.send_message(
                        chat_id=user_chat,
                        text=(
                            f"⏰ **Lead not accepted**\n\n"
                            f"Driver(s) **{driver_names}** did not accept the lead.\n\n"
                            f"Reference ID: `{reference_id}`\n\n"
                            "Tap below to pick a new driver:"
                        ),
                        parse_mode="Markdown",
                        reply_markup=keyboard,
                    )
                except Exception as e:
                    logger.warning("User timeout notify: %s", e)
                logger.info("Driver timeout notified for lead %s ref %s", lead_id, reference_id)
        except Exception as e:
            logger.error("Driver timeout job failed: %s", e)
    if application.job_queue:
        application.job_queue.run_repeating(check_driver_timeout, interval=60, first=120)
        # Paid instant tags, delivered from the database rather than from the
        # webhook's own request — a payment that lands mid-restart still arrives.
        application.job_queue.run_repeating(
            deliver_paid_instant_pdfs, interval=20, first=30)
        logger.info("Driver timeout job scheduled (every 60s, first in 120s)")

    if application.job_queue:
        application.job_queue.run_repeating(
            process_tracking_sessions, interval=10, first=20, name="driver_tracking_dispatch"
        )
        logger.info("Driver tracking job scheduled (every 10s, first in 20s)")

    # Receipt permanence sweeper: any receipt still pointing at Telegram
    # (written by this bot's fallback OR a legacy deployment) is re-hosted in
    # the public storage bucket before the 1-hour Telegram link dies.
    async def rescue_telegram_receipts_job(context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            from utils import receipt_rescue
            rows = await asyncio.to_thread(db.list_recent_telegram_receipts, 3, 25)
            rescued = 0
            for lead in rows:
                try:
                    if await asyncio.to_thread(receipt_rescue.rescue_lead_receipt, db, lead):
                        rescued += 1
                except Exception as e:
                    logger.warning("Receipt rescue failed for %s: %s", lead.get("reference_id"), e)
            if rescued:
                logger.info("Receipt sweeper: re-hosted %d telegram receipt(s) to storage", rescued)
        except Exception as e:
            logger.error("Receipt sweeper job failed: %s", e)

    if application.job_queue:
        application.job_queue.run_repeating(
            rescue_telegram_receipts_job, interval=600, first=60, name="receipt_rescue"
        )
        logger.info("Receipt rescue sweeper scheduled (every 10 min)")

    if application.job_queue:
        application.job_queue.run_repeating(
            process_pending_api_lead_dispatches,
            interval=10,
            first=15,
            name="api_lead_ingest_dispatch",
        )
        logger.info("API lead ingest dispatch job scheduled (every 10s, first in 15s)")

    # Receipt reminder: every hour, send a reminder to drivers who accepted 24+ hours ago and haven't submitted receipt
    async def send_receipt_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            overdue = await asyncio.to_thread(db.get_accepted_leads_without_receipt_over_24h)
            for item in overdue:
                ref = item.get("reference_id") or "N/A"
                chat_id = item.get("driver_telegram_id")
                assignment_id = item.get("assignment_id")
                if not chat_id or not assignment_id:
                    continue
                try:
                    chat_id_int = int(str(chat_id).strip())
                except (ValueError, TypeError):
                    chat_id_int = chat_id
                try:
                    await context.bot.send_message(
                        chat_id=chat_id_int,
                        text=(
                            f"🧾 **Receipt reminder**\n\nReference ID: `{ref}`\n\n"
                            "Please submit your receipt when you can.\n\n"
                            "To view all receipts type /receipts"
                        ),
                        parse_mode="Markdown",
                        reply_markup=_driver_add_lead_keyboard_only(),
                    )
                    db.mark_receipt_reminder_sent(assignment_id)
                    logger.info("Receipt reminder sent to driver for ref %s", ref)
                except Exception as e:
                    logger.warning("Could not send receipt reminder to %s: %s", chat_id, e)
        except Exception as e:
            logger.error("Receipt reminder job failed: %s", e)
    if application.job_queue:
        application.job_queue.run_repeating(send_receipt_reminders, interval=3600, first=60)
        logger.info("Receipt reminder job scheduled (every hour, first in 60s)")

    # Renewal checker: every 5 minutes, find leads whose 28-day renewal is due
    async def check_renewals(context: ContextTypes.DEFAULT_TYPE) -> None:
        # Day-27 heads-up: one day before the renewal dispatches, 🔔 the
        # original group chat + the original driver. Atomic claim per row so
        # multi-worker deployments never double-send.
        try:
            for rn in await asyncio.to_thread(db.get_renewals_needing_day27_alert):
                rid = rn.get("id")
                if not rid:
                    continue
                if not await asyncio.to_thread(db.mark_renewal_day27_alert_sent, rid):
                    continue
                rn = db.ensure_renewal_original_group(rn)
                lead27 = rn.get("lead") or {}
                ref27 = lead27.get("reference_id", "N/A")
                drv27 = next(
                    (d for d in (_get_all_drivers_cached() or [])
                     if str(d.get("id")) == str(rn.get("original_driver_id"))),
                    None,
                )
                dname27 = _telegram_md1_escape(
                    (drv27 or {}).get("driver_name") or "the original driver"
                )
                gid27 = rn.get("original_group_id")
                if gid27:
                    try:
                        grp27 = db.get_group_by_id(gid27)
                        gchat27 = _parse_chat_id((grp27 or {}).get("group_telegram_id"))
                        if gchat27:
                            await context.bot.send_message(
                                chat_id=gchat27,
                                text=(
                                    f"🔔 Renewal reminder — `{ref27}` is due tomorrow.\n"
                                    f"The offer goes to {dname27} first, then opens to all drivers."
                                ),
                                parse_mode="Markdown",
                            )
                    except Exception as e:
                        logger.warning("Day-27 group alert failed for renewal %s: %s", rid, e)
                drv_chat27 = _parse_chat_id((drv27 or {}).get("driver_telegram_id"))
                if drv_chat27:
                    try:
                        await context.bot.send_message(
                            chat_id=drv_chat27,
                            text=(
                                f"🔔 Heads up — renewal `{ref27}` is due tomorrow.\n"
                                "You'll get the offer first when it drops."
                            ),
                            parse_mode="Markdown",
                        )
                    except Exception as e:
                        logger.warning("Day-27 driver alert failed for renewal %s: %s", rid, e)
        except Exception as e:
            logger.error("Day-27 renewal alert pass failed: %s", e)

        try:
            due = await asyncio.to_thread(db.get_due_renewals)
            for renewal in due:
                renewal_id = renewal.get("id")
                if not renewal_id:
                    continue
                renewal = db.ensure_renewal_original_group(renewal)
                original_gid = renewal.get("original_group_id")
                original_did = renewal.get("original_driver_id")

                # Driver-first routing: skip the group-accept step and send the renewal
                # straight to the driver who handled the original sale. Only that driver
                # may accept until the escalation window lapses; then it fans out to all
                # active drivers everywhere.
                if not await asyncio.to_thread(
                    db.claim_renewal_for_driver_dispatch, renewal_id, original_gid
                ):
                    continue

                original_driver = None
                if original_did:
                    original_driver = next(
                        (d for d in (_get_all_drivers_cached() or [])
                         if str(d.get("id")) == str(original_did)),
                        None,
                    )

                sent = False
                if original_driver and record_is_active(original_driver):
                    sent = await _send_renewal_to_driver(context, renewal, original_driver)

                lead = renewal.get("lead") or {}
                ref = lead.get("reference_id", "?")

                if sent:
                    esc_seconds = Config.RENEWAL_ESCALATION_MINUTES * 60
                    if context.application.job_queue:
                        async def _driver_esc_job(ctx, _rid=renewal_id, _did=original_did):
                            await _escalate_renewal_driver_all(ctx, _rid, exclude_driver_id=_did)
                        context.application.job_queue.run_once(
                            _driver_esc_job,
                            when=esc_seconds,
                            name=f"renewal_driver_esc_{renewal_id}",
                        )
                    # Same-issuer-group notice (informational — the claim already
                    # auto-accepted this group; no buttons needed).
                    if original_gid:
                        try:
                            grp = db.get_group_by_id(original_gid)
                            gchat = _parse_chat_id((grp or {}).get("group_telegram_id"))
                            if gchat:
                                dname = _telegram_md1_escape(
                                    (original_driver or {}).get("driver_name") or "the original driver"
                                )
                                await context.bot.send_message(
                                    chat_id=gchat,
                                    text=(
                                        f"🔄 Renewal `{ref}` sent to {dname}.\n"
                                        f"If they don't respond in {Config.RENEWAL_ESCALATION_MINUTES} min, "
                                        "it opens to ALL drivers — first accept wins."
                                    ),
                                    parse_mode="Markdown",
                                )
                        except Exception as e:
                            logger.warning("Renewal %s: could not notify original group: %s", renewal_id, e)
                    logger.info(
                        "Renewal %s (ref %s) sent to original driver, all-drivers escalation in %d min",
                        renewal_id, ref, Config.RENEWAL_ESCALATION_MINUTES,
                    )
                else:
                    # No original driver reachable — open to ALL drivers right away.
                    logger.info(
                        "Renewal %s: no reachable original driver — escalating to ALL drivers",
                        renewal_id,
                    )
                    await _escalate_renewal_driver_all(
                        context, renewal_id, exclude_driver_id=original_did
                    )
        except Exception as e:
            logger.error("Renewal checker job failed: %s", e)

    if application.job_queue:
        application.job_queue.run_repeating(check_renewals, interval=300, first=180)
        logger.info("Renewal checker job scheduled (every 5 min, first in 180s)")

    # Client follow-up ticks: every 5 min. For each due follow-up the bot
    # (1) texts/emails the CLIENT directly when "bot chases client" is on,
    # (2) DMs the agent AND all supervisors a reminder with a stop button,
    # (3) auto-stops when the end date (stop) has passed.
    async def check_client_followups(context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            from datetime import timezone
            from utils import client_outreach
            due = await asyncio.to_thread(db.get_due_client_followups)
            for f in due:
                fid = f.get("id")
                uid = f.get("user_id")
                if not fid or not uid:
                    continue
                try:
                    chat_id = int(str(uid).strip())
                except (TypeError, ValueError):
                    chat_id = uid
                name = f.get("client_name") or "client"
                now = datetime.now(timezone.utc)
                is_renewal = (f.get("kind") == "renewal")

                # Stop date reached → auto-close and tell the agent.
                end_at = _fu_parse_iso(f.get("end_at"))
                if end_at and now >= end_at:
                    db.close_client_followup(fid)
                    try:
                        await context.bot.send_message(
                            chat_id=chat_id,
                            text=f"🛑 Follow-up for *{name}* reached its stop date — reminders ended.",
                            parse_mode="Markdown",
                        )
                    except Exception as e:
                        logger.warning("Could not send follow-up stop notice to %s: %s", chat_id, e)
                    continue

                # 1) Bot chases the client directly (text + email).
                client_results = []
                if f.get("contact_client"):
                    agency = Config.FOLLOWUP_AGENCY_NAME
                    site = Config.FOLLOWUP_WEBSITE
                    tel = followup_phone()          # editable in /settings
                    if is_renewal:
                        sms_body = client_outreach.build_renewal_sms(name, agency, site, tel)
                        subj, mail_body = client_outreach.build_renewal_email(name, agency, site, tel)
                    else:
                        sms_body = client_outreach.build_followup_sms(name, agency, site, tel)
                        subj, mail_body = client_outreach.build_followup_email(name, agency, site, tel)
                    if f.get("phone_number"):
                        ok, err = await asyncio.to_thread(
                            client_outreach.send_client_sms, f.get("phone_number"), sms_body
                        )
                        client_results.append("📲 texted" if ok else f"📲 text failed ({err})")
                    if f.get("email"):
                        ok, err = await asyncio.to_thread(
                            client_outreach.send_client_email,
                            f.get("email"), subj, mail_body,
                            followup_email(),           # editable in /settings
                        )
                        client_results.append("📧 emailed" if ok else f"📧 email failed ({err})")
                    if client_results:
                        db.update_client_followup(fid, {"last_client_contact_at": now.isoformat()})

                # 2) Reminder DM to the agent + every supervisor, with stop button.
                head = "🔁 Renewal due" if is_renewal else "⏰ Follow up with"
                lines = [f"{head} *{name}*"]
                if f.get("phone_number"):
                    lines.append(f"📞 {f.get('phone_number')}")
                if f.get("email"):
                    lines.append(f"📧 {f.get('email')}")
                if f.get("notes"):
                    lines.append(f"📝 {f.get('notes')}")
                if client_results:
                    lines.append("🤖 Bot contacted the client: " + ", ".join(client_results))
                short = _short_uuid(fid)
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Close (sold)", callback_data=f"cf_close_{short}"),
                     InlineKeyboardButton("🔕 Stop", callback_data=f"cf_stop_{short}")],
                    [InlineKeyboardButton("⏭ Snooze 1 day", callback_data=f"cf_snooze_{short}")],
                ])
                try:
                    await context.bot.send_message(
                        chat_id=chat_id, text="\n".join(lines),
                        reply_markup=kb, parse_mode="Markdown",
                    )
                except Exception as e:
                    logger.warning("Could not send follow-up reminder to %s: %s", chat_id, e)
                # Supervisory copies (skip the agent if they're also a supervisor).
                agent_key = _norm_chat_id(chat_id)
                sup_kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔕 Stop", callback_data=f"cf_stop_{short}"),
                    InlineKeyboardButton("🗑 Delete", callback_data=f"cf_del_{short}"),
                ]])
                agent_label = f.get("telegram_username")
                sup_lines = [f"👁 Supervisor copy" + (f" (agent @{agent_label})" if agent_label else "")] + lines
                # Whoever is on the list — supervisors by default, editable to anyone.
                sent_to = {agent_key}
                for sup_id in followup_chat_ids():
                    if _norm_chat_id(sup_id) in sent_to:
                        continue
                    sent_to.add(_norm_chat_id(sup_id))
                    try:
                        await context.bot.send_message(
                            chat_id=sup_id, text="\n".join(sup_lines),
                            reply_markup=sup_kb, parse_mode="Markdown",
                        )
                    except Exception as e:
                        logger.warning("Could not send follow-up copy to %s: %s", sup_id, e)
                # And the dispatch team's own chat, so the whole team sees it and can
                # act on it there — close, stop, pause a week, or change where the
                # client is contacted.
                team_chat = followup_team_chat_id()
                if team_chat is not None and _norm_chat_id(team_chat) not in sent_to:
                    team_lines = [("🔁 *Renewal due*" if is_renewal else "⏰ *Follow-up due*")
                                  + (f" — agent @{agent_label}" if agent_label else "")] + lines[1:]
                    try:
                        await context.bot.send_message(
                            chat_id=team_chat, text="\n".join(team_lines),
                            reply_markup=_followup_team_keyboard(short),
                            parse_mode="Markdown",
                        )
                    except Exception as e:
                        logger.warning("Could not post the follow-up to the team chat %s: %s",
                                       team_chat, e)

                # 3) Schedule the next tick off the frequency (advance past now to avoid a burst).
                days = _FU_FREQ_DAYS.get(f.get("frequency"), 7)
                nxt = (_fu_parse_iso(f.get("next_reminder_at")) or now) + timedelta(days=days)
                while nxt <= now:
                    nxt += timedelta(days=days)
                db.advance_client_followup(fid, nxt.isoformat(), now.isoformat())
        except Exception as e:
            logger.error("Client follow-up job failed: %s", e)

    if application.job_queue:
        application.job_queue.run_repeating(check_client_followups, interval=300, first=150)
        logger.info("Client follow-up reminder job scheduled (every 5 min, first in 150s)")

    # Daily motivation (Pro Mode): morning PSYCHOLOGY, evening AGGRESSIVE, no-lead-24h AGGRESSIVE, top performer BONUS
    async def send_morning_motivation(context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            user_ids = db.get_lead_sender_telegram_ids()
            text = motivation.morning_psychology()
            for uid in user_ids:
                try:
                    chat_id = int(uid) if isinstance(uid, str) else uid
                    await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
                except Exception as e:
                    logger.warning("Morning motivation to %s: %s", uid, e)
        except Exception as e:
            logger.error("Morning motivation job failed: %s", e)

    async def send_evening_motivation(context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            recipients = db.get_motivation_recipients()
            top_count = max((r.get("leads_count_7d") or 0) for r in recipients) if recipients else 0
            top_performer_uid = None
            if top_count > 0:
                for r in recipients:
                    if (r.get("leads_count_7d") or 0) == top_count:
                        top_performer_uid = r.get("user_id")
                        break
            for r in recipients:
                uid = r.get("user_id")
                if not uid:
                    continue
                try:
                    chat_id = int(uid) if isinstance(uid, str) else uid
                    if r.get("no_lead_24h"):
                        text = motivation.no_clients_24h_aggressive()
                    else:
                        text = motivation.evening_aggressive()
                    await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
                    if uid == top_performer_uid and top_count > 0:
                        bonus = motivation.top_performer_bonus()
                        await context.bot.send_message(chat_id=chat_id, text=bonus, parse_mode="Markdown")
                except Exception as e:
                    logger.warning("Evening motivation to %s: %s", uid, e)
        except Exception as e:
            logger.error("Evening motivation job failed: %s", e)

    async def send_daily_receipt_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
        """Once a day: DM every driver owing receipts (accepted 24h+ ago) one
        digest with per-reference Upload buttons, and email them when the
        driver has an email on file."""
        try:
            owed = await asyncio.to_thread(db.get_drivers_owed_receipts_over_24h)
        except Exception as e:
            logger.error("Daily receipt digest: query failed: %s", e)
            return
        if not owed:
            return
        from utils import client_outreach
        # Banner truth comes from the ACTUAL suspension set (the one lead
        # dispatch enforces), not from our filtered ref count — the counter
        # includes rows the digest can't render (blank refs, waived leads).
        try:
            suspended_ids = _get_suspended_driver_ids()
        except Exception:
            suspended_ids = set()
        max_show = 90
        for drv in owed:
            refs = drv.get("refs") or []
            if not refs:
                continue
            n_total = len(refs)
            shown = refs[:max_show]
            chat_id = _parse_chat_id(drv.get("driver_telegram_id"))
            if not chat_id:
                continue
            rows = [
                [InlineKeyboardButton(f"📤 Upload {ref}", callback_data=f"receipt_for_{ref}")]
                for ref in shown
            ]
            parts = []
            if str(drv.get("driver_id")) in suspended_ids or n_total >= SUSPENSION_THRESHOLD:
                parts.append(
                    "⛔ <b>You are suspended</b>\n\n"
                    f"Reason: You owe <b>{n_total}</b> receipt(s). "
                    "You will not receive new leads until all outstanding receipts are uploaded."
                )
            else:
                parts.append(
                    f"🧾 <b>Daily receipt reminder</b>\n\n"
                    f"You owe <b>{n_total}</b> receipt(s). At <b>{SUSPENSION_THRESHOLD}</b> unpaid you will be "
                    "<b>temporarily suspended</b> from new leads."
                )
            if n_total > max_show:
                parts.append(f"Showing the first {max_show} — upload those, then send /receipts again.")
            parts.append("Tap a reference below to upload, or send /receipts.")
            body = "\n\n".join(parts)
            kb = _keyboard_receipt_plus_rows(rows)
            try:
                try:
                    await context.bot.send_message(
                        chat_id=chat_id, text=body, parse_mode="HTML", reply_markup=kb
                    )
                except BadRequest:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=body.replace("<b>", "").replace("</b>", ""),
                        reply_markup=kb,
                    )
            except Exception as e:
                logger.warning("Daily receipt digest to driver %s failed: %s", drv.get("driver_id"), e)
                continue

            email = (drv.get("email") or "").strip()
            if email and "@" in email:
                dname = drv.get("driver_name", "Driver")
                subject = f"Receipts owed — {n_total} outstanding"
                mail_lines = [
                    f"Hi {dname},",
                    "",
                    f"You currently owe {n_total} delivery receipt(s) for these references:",
                    "",
                ]
                mail_lines += [f"- {ref}" for ref in shown]
                mail_lines += [
                    "",
                    "To upload: open the Telegram bot and send /receipts, then tap the reference.",
                    "",
                    "Thank you!",
                ]
                try:
                    ok, err = await asyncio.to_thread(
                        client_outreach.send_client_email,
                        email,
                        subject,
                        "\n".join(mail_lines),
                        Config.FOLLOWUP_EMAIL_COPY,
                    )
                    if not ok:
                        logger.warning("Daily receipt email to %s failed: %s", email, err)
                except Exception as e:
                    logger.warning("Daily receipt email to %s errored: %s", email, e)

    if application.job_queue:
        eastern = pytz.timezone("America/New_York")
        application.job_queue.run_daily(send_morning_motivation, time=dt_time(hour=8, minute=0, tzinfo=eastern))
        application.job_queue.run_daily(send_evening_motivation, time=dt_time(hour=18, minute=0, tzinfo=eastern))
        application.job_queue.run_daily(send_daily_receipt_digest, time=dt_time(hour=10, minute=0, tzinfo=eastern))
        logger.info("Daily motivation jobs scheduled (8 AM ET, 6 PM ET); receipt digest at 10 AM ET")

    logger.info("Starting polling — bot is live.")

    try:
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
    except Conflict:
        logger.error("Conflict at polling startup — exiting so Render restarts.")
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
    except Exception as e:
        logger.error("Fatal error: %s", e, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
