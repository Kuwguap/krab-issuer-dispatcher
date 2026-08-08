"""Main Telegram bot application."""
import base64
import io
import logging
import os
import re
import html
import sys
import secrets
import string
import uuid as _uuid_mod
import asyncio
import time
from datetime import datetime, time as dt_time, timedelta
import pytz
from typing import Optional
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.error import BadRequest, Conflict, RetryAfter
from telegram.ext import (
    Application,
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


def _ots_enabled() -> bool:
    """OneTimeSecret is optional. When its credentials are absent we bypass the
    encrypt/redact machinery entirely and dispatch raw phone numbers — no broken
    one-time links, no finalize crash. When configured, behavior is unchanged."""
    return bool(
        (Config.ONETIMESECRET_USERNAME or "").strip()
        and (Config.ONETIMESECRET_API_KEY or "").strip()
    )


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

_VIN_CONFLICT_INTRO = (
    "Pulling up 17 Digit Vin in DMV portal🧐\n\n"
    "Success ! Your Vehicle pulls up in the Motor Vehicle system!\n"
)


def _vin_conflict_body(_stated_car: str, api_car: str) -> str:
    return (
        f"{_VIN_CONFLICT_INTRO}"
        f"• VIN result in DMV : {api_car}\n\n"
        "Choose which to use:"
    )


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


def _get_suspended_driver_ids() -> set[str]:
    """Driver IDs (as str) with 3+ pending receipts — suspended from receiving new leads."""
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
        buttons.append([InlineKeyboardButton("📢 Send to All Groups", callback_data="select_group_all")])
    return InlineKeyboardMarkup(buttons)


# Per-image cap so we don't bloat the lead row's JSONB with huge base64 blobs.
# 5 MB raw == ~6.7 MB base64; Telegram bot uploads themselves cap at 10 MB
# for photos. Anything larger gets skipped (the lead itself still dispatches).
_PHASE1_MEDIA_MAX_BYTES = 5 * 1024 * 1024


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
    cached = context.user_data.get("phase1_attached_files")
    if cached and isinstance(cached, list):
        return cached
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
        return []

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
    return attached


async def _forward_phase1_attached_files_to_targets(
    context: ContextTypes.DEFAULT_TYPE,
    attached_files: list,
    group_chat_id: int | str | None,
) -> None:
    """Forward Phase 1 files to the **accepting** group chat only.

    Invoked from ``handle_accept_group_offer`` after Accept — not during
    approval broadcast or driver pick. Two payload shapes are supported:

    - ``{"type": "photo|document", "file_id": ...}`` — legacy Telegram
      file-id reference (pre-redaction code path).
    - ``{"type": "photo|document", "data_b64": ..., "filename": ...,
      "mime": ...}`` — inline base64 of the censored bytes, produced by
      ``_finalize_phase1_media_for_dispatch``. We rebuild a fresh upload
      so the issuer never sees the censored copy.
    """
    if not attached_files:
        return
    _group_cid = _parse_chat_id(group_chat_id) if group_chat_id is not None else None
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
) -> None:
    """Website (API-ingested) lead: a team accepted — dispatch drivers now.

    There is no human issuer to hand-pick drivers, so the winning group gets
    the full tag and every eligible driver gets Accept/Decline immediately.
    """
    reference_id = lead.get("reference_id", "N/A")
    try:
        await _send_full_group_lead_to_chat(
            context,
            winner_group,
            lead,
            html_prefix="<b>✅ Your group claimed this website client</b>\n\n",
            mirror_supervisory=False,
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
            # Unified web-order flow — two messages to every active group,
            # alongside the claimable offer above: (1) the informational
            # SUPERVISORY "New Lead" copy, then (2) the generated NJ temp-tag PDF.
            group_chat_ids = [
                _parse_chat_id(g.get("group_telegram_id")) for g in active_groups
            ]
            group_chat_ids = [c for c in group_chat_ids if c]
            try:
                await _send_web_order_supervisory_notice(context, lead, active_groups)
            except Exception as e:
                logger.warning("Web-order supervisory notice failed for %s: %s", lead_id, e)
            try:
                fresh_lead = db.get_lead_by_id(lead_id) or lead
                await _build_and_send_tag_pdf(context, fresh_lead, group_chat_ids)
            except Exception as e:
                logger.warning("Web-order tag PDF failed for %s: %s", lead_id, e)
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
    """True if user_id matches any token in ``Config.SUPERVISORY_TELEGRAM_ID``."""
    target = _norm_chat_id(user_id)
    if target is None:
        return False
    for cid in _global_supervisory_chat_ids():
        if _norm_chat_id(cid) == target:
            return True
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
        f"Please have Car, Driver License, and Laser Printer Ready✅"
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
    
    name = get_line(0)
    address = get_line(1)
    city_state_zip = get_line(2)
    delivery_address = get_line(3)
    delivery_city_state_zip = get_line(4)
    vin = get_line(5)
    car = get_line(6)
    color = ai_vision.normalize_phase1_color(get_line(7))
    insurance_company = get_line(8)
    insurance_policy_number = get_line(9)
    extra_info = get_line(10)
    
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
    """Build keyboard for VIN conflict: DMV result, keep entered vehicle line, or retype VIN."""
    _ = api_car, stated_car  # context shown in message body above the buttons
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Use DMV system VIN", callback_data="vin_use")],
        [InlineKeyboardButton("Continue with Same Vin", callback_data="vin_keep")],
        [InlineKeyboardButton("Retype VIN", callback_data="vin_retype")],
    ])

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


def _set_full_name(state_data: dict, first: str, last: str) -> None:
    f, l = (first or "").strip(), (last or "").strip()
    if l in ("", "-"):
        state_data["name"] = f if f else "-"
    else:
        state_data["name"] = f"{f} {l}".strip() if f else l


def _format_phase1_field_lines(state_data: dict) -> str:
    """Plain-text list of all Phase 1 fields (same labels as the edit picker).
    Always renders Issuer note / Driver note so the summary matches the edit menu.
    """
    first, last = _name_parts_from_full(state_data.get("name"))
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
    return "\n".join(lines)


def _format_phase1_ai_review_text(state_data: dict) -> str:
    """Human-readable summary of how the bot understood Phase 1 (AI path). Plain text (safe for special chars)."""
    return (
        "📝 Review & ✍️Edit Before Dispatching ✅\n\n"
        + _format_phase1_field_lines(state_data)
        + "\n\nTap ✅Dispatch when finished"
        + "\nTap ✏️Edit to make changes"
        + "\nTap 🔍Vin to lookup car info"
    )


def _preview_value_after_phase1_edit(state_data: dict, edit_key: str) -> str:
    """Current display value for a field after an edit (for recent-changes list)."""
    if edit_key == "fn":
        first, _ = _name_parts_from_full(state_data.get("name"))
        return first
    if edit_key == "ln":
        _, last = _name_parts_from_full(state_data.get("name"))
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
        [InlineKeyboardButton("🖼 Adjust from image/text", callback_data="ph1_adjust")],
    ])

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
        return
    new_text = _format_phase1_ai_review_text(state_data)
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=mid,
            text=new_text,
            reply_markup=_build_review_keyboard_with_selections(state_data),
        )
    except Exception as e:
        logger.warning("Could not update review text: %s", e)

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


def _phase1_edit_fields_keyboard(state_data: dict) -> InlineKeyboardMarkup:
    first, last = _name_parts_from_full(state_data.get("name"))
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
        [InlineKeyboardButton("⬅️ Back to review", callback_data=PH1_EDIT_BACK)],
    ]
    return InlineKeyboardMarkup(rows)


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
    payload = data or {}
    all_rows = _get_all_drivers_cached() or []
    id_set = {str(x).strip() for x in (payload.get("selected_driver_ids") or []) if str(x).strip()}
    selected = [d for d in all_rows if str(d.get("id")) in id_set]

    if not selected:
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
        if not d or not record_is_active(d):
            continue
        did = str(d.get("id") or "").strip()
        if not did or did in suspended:
            continue
        if _parse_chat_id(d.get("driver_telegram_id")) is None:
            continue
        out_ids.append(did)
    return out_ids


async def _send_phase1_ai_review(target_message, state_data: dict, context, user_id) -> None:
    # Default dispatch selections on the review row (🏢 🚗 📊): all groups, all eligible drivers, Facebook.
    groups = db.get_all_groups()
    active_groups = [g for g in groups if record_is_active(g)]
    if active_groups:
        state_data["selected_group_id"] = "all"
        state_data["selected_group_name"] = "All Groups"
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
    db.set_user_state(user_id, "phase1", state_data)
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
    missing = [f for f in missing if f not in PHASE1_OPTIONAL_FIELDS]
    if missing:
        prompts = ai_vision.MISSING_FIELD_PROMPTS
        msg = prompts.get(missing[0], (f"You missed out {missing[0]}. Can you add it?", missing[0]))[0]
        context.user_data["missing_fields"] = missing
        context.user_data["missing_field_state_data"] = state_data.copy()
        sent = await message.reply_text(msg)
        _track_missing_prompt(context, sent)
        return STATE_MISSING_FIELD
    return await _ensure_phone_price_before_files(message, context, user_id)


# Optional Phase 1 fields: never block with a question — blank shows as "-" in review.
PHASE1_OPTIONAL_FIELDS = {"insurance_company", "insurance_policy_number", "extra_info", "delivery_date"}


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


def _apply_single_phase1_edit(state_data: dict, edit_key: str, new_text: str) -> None:
    """Apply one field edit from the AI review flow."""
    new_text = (new_text or "").strip()
    if edit_key == "fn":
        first, last = _name_parts_from_full(state_data.get("name"))
        _set_full_name(state_data, new_text, last if last != "-" else "")
        return
    if edit_key == "ln":
        first, last = _name_parts_from_full(state_data.get("name"))
        _set_full_name(state_data, first if first != "-" else "", new_text)
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
    # Rebuild derived fields
    vehicle_lines = [
        state_data.get("name"),
        state_data.get("address"),
        state_data.get("city_state_zip"),
        state_data.get("delivery_address") or "-",
        state_data.get("delivery_city_state_zip") or "-",
        state_data.get("vin"),
        state_data.get("car"),
        state_data.get("color"),
        state_data.get("insurance_company"),
        state_data.get("insurance_policy_number"),
        state_data.get("extra_info"),
    ]
    state_data["vehicle_details"] = "\n".join(vehicle_lines)
    delivery_lines = [
        state_data.get("delivery_address"),
        state_data.get("delivery_city_state_zip"),
    ]
    state_data["delivery_details"] = "\n".join([l for l in delivery_lines if l])


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


def _merge_phase1_adjust(state_data: dict, structured_text: str) -> list[str]:
    """Merge an AI extraction into state_data — ONLY the fields actually found.

    Empty / "-" / placeholder values never overwrite existing data. Returns the
    human labels of the fields that changed.
    """
    normalized = _normalize_ai_phase1_text(structured_text or "")
    lines = [l.strip() for l in normalized.splitlines()]
    parsed = parse_phase1_structured("\n".join(lines[: ai_vision.PHASE1_LINE_COUNT]))
    placeholders = {"", "-", "n/a", "na", "none", "unknown", "not found"}
    updated: list[str] = []
    for key in _PHASE1_ADJUST_FIELD_ORDER:
        val = str(parsed.get(key) or "").strip()
        if val.lower() in placeholders:
            continue
        if val != str(state_data.get(key) or "").strip():
            state_data[key] = val
            updated.append(_PHASE1_ADJUST_LABELS.get(key, key))
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
            if len(re.sub(r"\D", "", v)) >= 10:
                state_data["pending_phone_number"] = v
                updated.append("phone")
        elif low.startswith("price"):
            state_data["pending_price"] = v
            updated.append("price")
        elif low.startswith("email"):
            em = ai_vision.normalize_email(v)
            if em:
                state_data["email"] = em
                updated.append("email")
        elif low.startswith("driverlicense") or low == "dl":
            dl = ai_vision.normalize_driver_license_id(v)
            if dl:
                state_data["driver_license_id"] = dl
                updated.append("driver license")
    if updated:
        for key in _PHASE1_ADJUST_FIELD_ORDER:
            if state_data.get(key) is None:
                state_data[key] = "-"
        _clean_vin_and_car(state_data)  # rebuilds vehicle/delivery details
    return updated


async def handle_phase1_adjust_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Review 'Adjust from image/text': read media/text, merge only found fields."""
    user_id = update.effective_user.id
    state = db.get_user_state(user_id)
    if not state or not state.get("data"):
        await update.message.reply_text("❌ Data lost. Please start over with /start")
        return ConversationHandler.END
    state_data = state["data"]
    message = update.message

    note = await message.reply_text("🤖 Reading…")
    structured = None
    adjust_caption = (message.caption or "").strip() or None
    try:
        if message.photo:
            f = await context.bot.get_file(message.photo[-1].file_id)
            bio = io.BytesIO()
            await f.download_to_memory(out=bio)
            _img = bio.getvalue()
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
            if "pdf" in mime or (doc.file_name or "").lower().endswith(".pdf"):
                png = await asyncio.to_thread(ai_vision.pdf_first_page_to_png_bytes, raw)
                if png:
                    structured = await asyncio.to_thread(
                        lambda: ai_vision.extract_structured_from_media_parts(
                            [(png, "image/png")], typed_text=adjust_caption
                        )
                    )
            else:
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

    if not structured:
        await message.reply_text(
            "⚠️ Couldn't read that. Send a clearer photo/PDF or paste the text — or ❌ Cancel above."
        )
        return STATE_ADJUST_INPUT

    updated = _merge_phase1_adjust(state_data, structured)
    db.set_user_state(user_id, "phase1", state_data)
    prompt_id = context.user_data.pop("adjust_prompt_msg_id", None)
    if prompt_id:
        await _safe_delete_chat_message(context, message.chat_id, prompt_id)
    if updated:
        await message.reply_text("✅ Updated: " + ", ".join(dict.fromkeys(updated)))
    else:
        await message.reply_text("ℹ️ Nothing new found — the review is unchanged.")
    await _update_review_message_text(context, state_data)
    return STATE_AI_REVIEW


async def _begin_lead_flow(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    username: str,
    reply_message,
) -> None:
    """Shared Phase 1 reset + welcome (used by /lead, /client, and Add new lead callback)."""
    db.clear_user_state(user_id)
    if context.user_data:
        context.user_data.pop("phase1_attached_files", None)
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
    await reply_message.reply_text(f"Welcome, @{username}! 👋\n\n{phase1_instruction}")


async def begin_lead_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the issuer lead flow (Phase 1). Used by /lead and /client; drivers use these because /start shows the driver menu."""
    msg = update.effective_message
    if not msg:
        return ConversationHandler.END
    user_id = update.effective_user.id
    username = update.effective_user.username or "Unknown"
    await _begin_lead_flow(context, user_id, username, msg)
    return STATE_PHASE1


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
    await _begin_lead_flow(context, user_id, username, msg)
    return STATE_PHASE1


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
        "phase1_recent_edits",
        "review_message_id",
        "review_chat_id",
        "vin_choice_api_car",
        "vin_choice_stated_car",
        "vin_conflict_msg_id",
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


def _normalize_ai_phase1_text(text: str) -> str:
    """Strip optional leading 'N) ' from each line so parse_phase1_structured gets clean lines."""
    lines = []
    for line in text.splitlines():
        line = line.strip()
        # Remove leading "1) ", "2) ", ... "11) "
        line = re.sub(r"^\d{1,2}\)\s*", "", line).strip()
        lines.append(line)
    return "\n".join(lines)


def _sanitize_phones_for_send(text: str) -> str:
    """Replace any phone numbers in user content with OneTimeSecret links (no raw numbers).

    When OneTimeSecret isn't configured, leave the text untouched — otherwise every
    phone would be swapped for a dead ``[phone – use link above]`` placeholder with
    no link above it (the raw number would be lost)."""
    if not text or not str(text).strip():
        return text or ""
    if not _ots_enabled():
        return str(text).strip()
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


def _group_lead_copy_pre_html(phase1_data: dict, encrypted_link: str, raw_phone: str = "") -> str:
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
    # With OneTimeSecret retired (no link), show the raw phone directly so the
    # group still gets the client's number instead of an empty link line.
    if link:
        phone_line = f"📞 Phone 🔗 Encrypted Link: {link}"
    else:
        phone_line = f"📞 Phone: {(raw_phone or '').strip() or '—'}"
    copy_plain = "\n".join([
        "- - - - - - copy & paste - - - - - -",
        f"Client Name: {client_name}",
        f"⏰ {extra_time}",
        f"📍Delivery address: {delivery_combined}",
        phone_line,
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
    raw_phone: str = "",
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

    issue_s = issue_dt.strftime("%Y-%m-%d %H:%M:%S %Z") if issue_dt else "N/A"
    expiry_s = expiry_dt.strftime("%Y-%m-%d %H:%M:%S %Z") if expiry_dt else "N/A"

    pre_wrapped = _group_lead_copy_pre_html(phase1_data, encrypted_link, raw_phone)
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
    if _ots_enabled() and not enc.get("link"):
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
    if _ots_enabled() and not (lead.get("encrypted_link") or "").strip():
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
) -> None:
    """Post the same detailed HTML lead as the issuer flow; optionally mirror to supervisory chat(s)."""
    reference_id = (lead.get("reference_id") or "N/A").strip()
    phase1 = _phase1_from_stored_lead(lead)
    link = (lead.get("encrypted_link") or "").strip()
    issuer_note = _lead_issuer_note(lead)
    issue_dt, exp_dt = _issue_and_expiration_for_group_display(lead)
    body = _format_group_lead_message_html(
        reference_id, phase1, link, issue_dt, exp_dt, issuer_note, header_text=header_text,
        raw_phone=(lead.get("phone_number") or ""),
    )
    full_html = f"{html_prefix}{body}" if html_prefix else body
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

    # Second message: the generated NJ temp-tag PDF to the same targets — sent
    # at the LATER part of the flow, when a group ACCEPTS the lead. Website leads
    # already receive the tag at dispatch time (they carry external_order_id), so
    # they're skipped here; renewals always mint a brand-new tag.
    if renewal or not (lead.get("external_order_id") or "").strip():
        try:
            await _build_and_send_tag_pdf(
                context, lead, [tid for tid, _ in targets], renewal=renewal
            )
        except Exception as e:
            logger.warning("Tag PDF send failed for ref %s: %s", reference_id, e)


async def _tag_fields_from_lead(lead: dict, *, renewal: bool = False) -> dict:
    """Resolve a stored lead into the field dict tag_pdf.build_tag_pdf expects.

    Allocates (and persists) the plate + control number once per lead, decodes
    the VIN for year/make/model/body (falling back to the typed vehicle line),
    and sets the registration state that picks the NJ vs non-NJ template. On a
    ``renewal`` the tag gets a FRESH plate + control and a new 30-day window
    (issued today) instead of the original, now-expired values.
    """
    from utils import tag_pdf

    phase1 = _phase1_from_stored_lead(lead)
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
    plate = "" if renewal else (lead.get("plate") or "").strip()
    control = "" if renewal else (lead.get("tag_control_number") or "").strip()
    if not plate or not control:
        alloc = await asyncio.to_thread(db.allocate_temp_plate, state == "NJ")
        plate = plate or alloc["plate"]
        control = control or alloc["control_number"]
        try:
            db.update_lead(str(lead.get("id")), {"plate": plate, "tag_control_number": control})
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
    *, renewal: bool = False,
) -> int:
    """Generate the NJ temp-tag PDF for a lead and send it to each chat.

    Returns the number of chats that actually received the PDF (0 if every
    send failed) so callers can avoid marking the tag delivered when it wasn't.
    """
    from utils import tag_pdf

    if not target_chat_ids:
        return 0
    fields = await _tag_fields_from_lead(lead, renewal=renewal)
    pdf = await asyncio.to_thread(tag_pdf.build_tag_pdf, fields)
    reference_id = (lead.get("reference_id") or "N/A").strip()
    plate = fields.get("plate") or "tag"
    caption = f"🧾 NJ 30-Day Temp Tag — {plate}\nReference: {reference_id}"
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
    return sent


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
    is_link = link_raw.startswith("http://") or link_raw.startswith("https://")
    if is_link:
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
        "📍 Delivery Address",
        delivery,
        "",
        f"📝Extra info: {extra}",
        "📞 Call Client Now Confirm: 💰 Price • ⏱️ Time • 📍 Location • 🏷 Tag",
        link_line,
        *(["📞 Click link 🔗 enter password “ callclient “ to view"] if is_link else []),
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
) -> int:
    """Normalize AI vision output, validate, then AI review — shared by photo and PDF.

    ``typed_text`` is the text the user typed alongside the files (photo
    captions) — it gets the same fill-missing regex fallbacks the pure-text
    path applies to the raw message.
    """
    if not msg:
        return STATE_PHASE1
    if not raw_text or not raw_text.strip():
        await msg.reply_text(
            f"❌ Could not extract details from the {source_label}. "
            "Please send the details as text in the required structure."
        )
        return STATE_PHASE1
    normalized = _normalize_ai_phase1_text(raw_text)
    lines = [ln.strip() for ln in normalized.splitlines() if ln.strip()]
    normalized_11 = "\n".join(lines[: ai_vision.PHASE1_LINE_COUNT]) if len(lines) >= ai_vision.PHASE1_LINE_COUNT else normalized
    state_data = parse_phase1_structured(normalized_11)
    _apply_single_address_as_both(state_data)
    valid, validation_errors = ai_vision.validate_phase1_extraction(normalized_11, state_data)
    if not valid:
        err_blurb = "\n• ".join(validation_errors)
        preview = (
            f"Name: {state_data.get('name') or '-'}\n"
            f"VIN: {state_data.get('vin') or '-'}\n"
            f"Delivery: {state_data.get('delivery_address') or '-'} / {state_data.get('delivery_city_state_zip') or '-'}"
        )
        await msg.reply_text(
            "⚠️ Extraction didn’t pass validation:\n\n• " + err_blurb + "\n\n"
            "Extracted preview:\n" + preview + "\n\n"
            "Please send the details as text in the required 11-line structure, or try another photo or PDF."
        )
        return STATE_PHASE1
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

    # Robust VIN extraction from whole raw output
    vin_from_raw = _extract_vin_17(raw_text)
    if vin_from_raw:
        state_data["vin"] = vin_from_raw

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
        typed_text = "\n".join(
            (it.get("caption") or "").strip() for it in batch if (it.get("caption") or "").strip()
        ) or None
        parts: list[tuple[bytes, str]] = []
        pending_media: list[dict] = []
        for item in batch:
            if item.get("kind") == "image":
                img_bytes = item["bytes"]
                img_mime = item.get("mime") or "image/jpeg"
                parts.append((img_bytes, img_mime))
                pending_media.append({"kind": "image", "bytes": img_bytes, "mime": img_mime})
            elif item.get("kind") == "pdf":
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
            context, user_id, raw_text, status, source_label=label, typed_text=typed_text
        )
    finally:
        if context.user_data:
            context.user_data.pop("phase1_vision_extracting", None)
            context.user_data.pop("phase1_batch_status_msg_id", None)


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
        valid, validation_errors = ai_vision.validate_phase1_extraction(normalized_11, state_data)
        if not valid:
            err_blurb = "\n• ".join(validation_errors)
            await update.message.reply_text(
                "⚠️ I couldn't find enough info:\n\n• " + err_blurb + "\n\n"
                "Please include at least name and delivery address/city."
            )
            return STATE_PHASE1
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
        await _send_phase1_ai_review(update.message, state_data, context, user_id)
        # VIN decode is opt-in via the review screen's "🔍 VIN" button.
        missing = ai_vision.detect_missing_fields(state_data, message_text)
        missing = [f for f in missing if f not in PHASE1_OPTIONAL_FIELDS]
        if missing:
            prompts = ai_vision.MISSING_FIELD_PROMPTS
            msg = prompts.get(missing[0], (f"You missed out {missing[0]}. Can you add it?", missing[0]))[0]
            context.user_data["missing_fields"] = missing
            context.user_data["missing_field_state_data"] = state_data.copy()
            await update.message.reply_text(msg)
            return STATE_MISSING_FIELD
        return await _ensure_phone_price_before_files(update.message, context, update.effective_user.id)


async def handle_edit_field_text(update, context):
    user_id = update.effective_user.id
    ek = context.user_data.get("phase1_pending_edit_key")
    if not ek:
        return STATE_AI_REVIEW

    state = db.get_user_state(user_id)
    if not state or not state.get("data"):
        return ConversationHandler.END
    state_data = state["data"]

    text = (update.message.text or "").strip()
    _apply_single_phase1_edit(state_data, ek, text)
    _apply_single_address_as_both(state_data)
    _clean_vin_and_car(state_data)
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

    # Update the review message
    await _update_review_message_text(context, state_data)
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
    return cleaned


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
    await message.reply_text(
        "📞 **Phone number is required to continue.**\n\n" + PHASE2_INTRO_MESSAGE,
        parse_mode="Markdown",
    )
    return STATE_PHASE2


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
        if chat_id:
            await context.bot.send_message(chat_id=chat_id, text=PHASE2_INTRO_MESSAGE)
        else:
            await query.message.reply_text(PHASE2_INTRO_MESSAGE)
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
            if chat_id:
                await context.bot.send_message(chat_id=chat_id, text=PHASE2_INTRO_MESSAGE)
            else:
                await query.message.reply_text(PHASE2_INTRO_MESSAGE)
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
        await _edit_message_keyboard(
            context,
            context.user_data["review_chat_id"],
            context.user_data["review_message_id"],
            _phase1_edit_fields_keyboard(state_data)
        )
        return STATE_AI_REVIEW

    elif data == "ph1_adjust":
        prompt = await query.message.reply_text(
            "🖼 Send a photo, PDF, or paste text with the corrected info.\n\n"
            "Only the fields found in it will be updated — everything else "
            "on the review stays as-is.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="adjust_cancel")
            ]]),
        )
        context.user_data["adjust_prompt_msg_id"] = prompt.message_id
        return STATE_ADJUST_INPUT

    elif data == "adjust_cancel":
        try:
            await query.message.delete()
        except Exception:
            pass
        context.user_data.pop("adjust_prompt_msg_id", None)
        return STATE_AI_REVIEW

    elif data.startswith("ph1edit_"):
        edit_key = data.replace("ph1edit_", "", 1)
        context.user_data["phase1_pending_edit_key"] = edit_key
        label = PH1_EDIT_PROMPT_LABEL.get(edit_key, edit_key)
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
                await query.message.reply_text("⚠️ No active groups configured.")
            return STATE_AI_REVIEW
        buttons = [[InlineKeyboardButton(g.get("group_name", str(g["id"])), callback_data=f"selgrp_{g['id']}")] for g in active]
        buttons.append([InlineKeyboardButton("📢 Send to All Groups", callback_data="selgrp_all")])
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
            state_data["selected_group_name"] = "All Groups"
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
    user_id = query.from_user.id
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
    if not query.data.startswith("ph1edit_"):
        return STATE_AI_EDIT_MENU
    edit_key = query.data.replace("ph1edit_", "", 1)
    if edit_key not in PH1_EDIT_PROMPT_LABEL:
        return STATE_AI_EDIT_MENU
    context.user_data["phase1_pending_edit_key"] = edit_key
    label = PH1_EDIT_PROMPT_LABEL[edit_key]
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
        await update.message.reply_text(
            "Use the buttons above (**Change another field** or **Done — continue lead**), or /start to begin again.",
            parse_mode="Markdown",
        )
        return STATE_AI_EDIT_INPUT
    text = (update.message.text or "").strip()
    state = db.get_user_state(user_id)
    if not state or not state.get("data"):
        context.user_data.pop("phase1_pending_edit_key", None)
        await update.message.reply_text("❌ Lead data not found. Please start over with /start")
        return ConversationHandler.END
    state_data = state["data"].copy()
    if text == "-":
        _apply_single_phase1_edit(state_data, ek, "")
    else:
        _apply_single_phase1_edit(state_data, ek, text)
    _apply_single_address_as_both(state_data)
    _clean_vin_and_car(state_data)
    db.set_user_state(user_id, "phase1", state_data)
    context.user_data.pop("phase1_pending_edit_key", None)
    label = PH1_EDIT_PROMPT_LABEL.get(ek, ek)
    preview = _preview_value_after_phase1_edit(state_data, ek)
    context.user_data.setdefault("phase1_recent_edits", [])
    re_list = context.user_data["phase1_recent_edits"]
    re_list.append({"label": label, "value": preview})
    if len(re_list) > 15:
        context.user_data["phase1_recent_edits"] = re_list[-15:]
    await update.message.reply_text(
        "✅ Updated.\n\n"
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


async def handle_missing_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle user reply when we asked for a missing field (e.g. color)."""
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()
    if not text:
        sent = await update.message.reply_text("Please send the missing value.")
        _track_missing_prompt(context, sent)
        return STATE_MISSING_FIELD
    missing_fields = context.user_data.get("missing_fields") or []
    state_data = context.user_data.get("missing_field_state_data") or {}
    field = missing_fields[0] if missing_fields else "color"
    state_key = MISSING_FIELD_TO_STATE_KEY.get(field, field)
    state_data[state_key] = text
    missing_fields = missing_fields[1:]
    context.user_data["missing_fields"] = missing_fields
    context.user_data["missing_field_state_data"] = state_data
    # The question (and the answer) disappear — the value lands on the review.
    await _clear_missing_prompts(context)
    try:
        await update.message.delete()
    except Exception:
        pass
    if missing_fields:
        next_field = missing_fields[0]
        prompts = ai_vision.MISSING_FIELD_PROMPTS
        msg = prompts.get(next_field, (f"You missed out {next_field}. Can you add it?", next_field))[0]
        sent = await update.message.reply_text(msg)
        _track_missing_prompt(context, sent)
        return STATE_MISSING_FIELD

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
    still_missing = [f for f in still_missing if f not in PHASE1_OPTIONAL_FIELDS]
    if still_missing:
        context.user_data["missing_fields"] = still_missing
        context.user_data["missing_field_state_data"] = state_data.copy()
        prompts = ai_vision.MISSING_FIELD_PROMPTS
        first = still_missing[0]
        msg = prompts.get(first, (f"You missed out {first}. Can you add it?", first))[0]
        sent = await update.message.reply_text(msg)
        _track_missing_prompt(context, sent)
        return STATE_MISSING_FIELD

    _sanitize_phase1_pending_phone_price(state_data)
    db.set_user_state(user_id, "phase1", state_data)
    context.user_data.pop("missing_fields", None)
    context.user_data.pop("missing_field_state_data", None)
    # VIN decode is opt-in via the review screen's "🔍 Check VIN" button.
    return await _ensure_phone_price_before_files(update.message, context, user_id)


async def handle_vin_choice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle VIN conflict choice: use API result, keep stated car, or retype VIN."""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    if query.data == "vin_retype":
        await query.message.reply_text("Please type the correct VIN (17 characters):")
        return STATE_VIN_RETYPE
    if query.data == "vin_use":
        api_car = context.user_data.get("vin_choice_api_car")
        if api_car:
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
    else:
        context.user_data.pop("vin_choice_api_car", None)
        context.user_data.pop("vin_choice_stated_car", None)
        
    vin_msg_id = context.user_data.pop("vin_conflict_msg_id", None)
    if vin_msg_id:
        try:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=vin_msg_id)
        except:
            pass
    # Return to the AI review screen — Submit is the only path forward.
    state = db.get_user_state(user_id)
    if state and state.get("data"):
        await _update_review_message_text(context, state["data"])
    return STATE_AI_REVIEW


async def handle_vin_retype(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle user's new VIN input; re-run lookup and either show choice again or return to review."""
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()
    vin_new = _extract_vin_17(text)
    if not vin_new or len(vin_new) != 17:
        await update.message.reply_text(
            "Please send a valid 17-character VIN (letters and numbers only)."
        )
        return STATE_VIN_RETYPE
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
        keyboard = _vin_choice_keyboard(api_car, stated_car)
        vin_msg = await update.message.reply_text(
            _vin_conflict_body(stated_car, api_car),
            reply_markup=keyboard,
        )
        context.user_data["vin_conflict_msg_id"] = vin_msg.message_id
        return STATE_VIN_CHOICE
    if alert_msg:
        await update.message.reply_text(alert_msg)
    await _update_review_message_text(context, state_data)
    return STATE_AI_REVIEW


async def _handle_phase1_vin_check_button(
    context: ContextTypes.DEFAULT_TYPE, query, user_id: int, state_data: dict,
) -> int:
    """Run the DMV VIN decode on demand from the review screen.

    Shows the "Pulling up 17 Digit Vin in DMV portal..." message and the same
    three inline buttons (Use DMV / Continue / Retype) as the legacy
    auto-decode flow; after the user picks one, we return to the AI review
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

    # Get phase 1 data
    state = db.get_user_state(user_id)
    if not state or not state.get("data"):
        await msg.reply_text("❌ Error: Phase 1 data not found. Please start over with /start")
        return ConversationHandler.END

    phase1_data = state.get("data", {})

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
    if len(digits_only) not in (9, 10) or not price:
        await msg.reply_text(
            "❌ Please provide both phone number and price.\n"
            "Phone in any format (e.g. +1 (732) 534-2659, 732-534-2659, 732 534 2659) and price with $ (e.g. $500)."
        )
        return STATE_PHASE2
    # Normalize to +1XXXXXXXXXX for storage (no double 1)
    phone_number = "+1" + digits_only

    state_data = phase1_data.copy()
    state_data["pending_phone_number"] = phone_number
    state_data["pending_price"] = price
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
        await update.message.reply_text(
            "📞💲 **Phone number and price are required.**\n\n" + PHASE2_INTRO_MESSAGE,
            parse_mode="Markdown",
        )
        return STATE_PHASE2

    raw = (update.message.text or "").strip()
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
        await update.message.reply_text(
            "📞💲 **Phone number and price are required.**\n\n" + PHASE2_INTRO_MESSAGE,
            parse_mode="Markdown",
        )
        return STATE_PHASE2

    raw_d = (update.message.text or "").strip()
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
        await message.reply_text(
            "📞💲 **Phone number and price are required.**\n\n" + PHASE2_INTRO_MESSAGE,
            parse_mode="Markdown",
        )
        return STATE_PHASE2

    phone_number = state_data.pop("pending_phone_number", None)
    price = state_data.pop("pending_price", None)
    issuers_note = (state_data.get("special_request_issuers") or "").strip()
    drivers_note = (state_data.get("special_request_drivers") or "").strip()

    # OneTimeSecret is optional. When it's configured we still one-time-link the
    # phone; when it isn't (retired), skip it and dispatch the raw number.
    encrypted_data = None
    if _ots_enabled():
        encrypted_data = await asyncio.to_thread(ots.encrypt_phone, phone_number)
        if not encrypted_data:
            state_data["pending_phone_number"] = phone_number
            state_data["pending_price"] = price
            db.set_user_state(user_id, "special_request_drivers", state_data)
            reason = (getattr(ots, "last_error", "") or "").strip()
            # Plain text (no parse_mode): the reason/env-var names contain '_' and
            # '*' that break Telegram's Markdown entity parser.
            if reason:
                await message.reply_text(
                    "❌ Error encrypting phone number.\n\n"
                    f"Reason: {reason}\n\n"
                    "If this keeps happening, the clientsphonenumber service is usually "
                    "missing env vars on Vercel or the bot has wrong credentials.\n\n"
                    "Send your reply again when ready (or - for none)."
                )
            else:
                await message.reply_text(
                    "❌ Error encrypting phone number. Please try again.\n\n"
                    "Send your reply again (or - for none)."
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
    lead = db.create_lead(final_lead_data)
    if not lead:
        await message.reply_text("❌ Error saving lead to database.")
        return ConversationHandler.END
    await _on_lead_created(context, lead)

    reference_id = lead.get("reference_id", "N/A")

    # Honor selected drivers from review defaults/edits (all active by default).
    drivers_list: list = []
    try:
        raw_driver_ids = state_data.get("selected_driver_ids") or []
        driver_id_set = {str(x).strip() for x in raw_driver_ids if str(x).strip()}
        all_drivers_now = _get_all_drivers_cached() or []
        drivers_list = [d for d in all_drivers_now if str(d.get("id")) in driver_id_set]
    except Exception as e:
        logger.warning("finalize_lead_after_notes: resolving selected drivers failed: %s", e)
        drivers_list = []
    if not drivers_list:
        try:
            suspended = _get_suspended_driver_ids()
        except Exception:
            suspended = set()
        if is_all_groups:
            fallback_pool = _get_all_drivers_cached() or []
        else:
            try:
                linked = db.get_group_driver_rows_for_group(group_id)
            except Exception:
                linked = []
            fallback_pool = linked or _get_all_drivers_cached() or []
        drivers_list = [
            d for d in fallback_pool
            if d and record_is_active(d) and str(d.get("id")) not in suspended
        ]

    if is_all_groups:
        await _post_lead_to_all_groups_for_approval(context, lead, active_groups)
    else:
        await _post_single_group_approval(context, lead, selected_group)

    # The temp-tag PDF is sent later, when a group ACCEPTS the lead
    # (see _send_full_group_lead_to_chat), not at creation.

    _store_issuer_await_group_accept(
        user_id,
        lead_id=str(lead["id"]),
        await_mode="dispatch_pending",
        selected_driver_ids=[str(d.get("id")) for d in drivers_list if d.get("id")],
    )
    context.user_data.pop("phase1_pending_media", None)
    context.user_data.pop("phase1_attached_files", None)
    ref_h = html.escape(str(reference_id), quote=False)
    drivers_count = len([d for d in drivers_list if d and d.get("id")])
    source_label = html.escape((state_data.get("selected_source_label") or "—"), quote=False)
    if is_all_groups:
        await message.reply_text(
            "✅ Lead saved.\n\n"
            f"📋 Reference ID: <code>{ref_h}</code>\n"
            f"📣 Approval sent to <b>{len(active_groups)}</b> group(s)\n"
            f"🚗 Approval sent to <b>{drivers_count}</b> driver(s)\n"
            f"📊 Lead source: <b>{source_label}</b>",
            parse_mode="HTML",
        )
    else:
        await message.reply_text(
            "✅ Lead saved.\n\n"
            f"📋 Reference ID: <code>{ref_h}</code>\n"
            f"Approval sent to <b>{html.escape(selected_group.get('group_name') or 'group', quote=False)}</b>\n"
            f"🚗 Approval sent to <b>{drivers_count}</b> driver(s)\n"
            f"📊 Lead source: <b>{source_label}</b>",
            parse_mode="HTML",
        )
    await _maybe_offer_insurance_card(
        context, message, lead_id=str(lead["id"]), reference_id=str(reference_id),
    )
    return ConversationHandler.END

async def _submit_lead_from_review(message, context, user_id, data):
    # Phone is the only hard requirement at submit; price may be "-".
    if not _is_valid_pending_phone(data.get("pending_phone_number")):
        _sanitize_phase1_pending_phone_price(data)
        db.set_user_state(user_id, "phase1", data)
        await message.reply_text(
            "📞 **Phone number is required** before sending the lead.\n\n" + PHASE2_INTRO_MESSAGE,
            parse_mode="Markdown",
        )
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

    # Encrypt (only when OneTimeSecret is configured; otherwise dispatch raw phone)
    enc = None
    if _ots_enabled():
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
    lead = db.create_lead({
        "user_id": user_id, "telegram_username": username,
        "vehicle_details": vd,
        "delivery_details": data.get("delivery_details", ""),
        "phone_number": phone, "price": price,
        "encrypted_link": (enc or {}).get("link"),
        "onetimesecret_token": (enc or {}).get("secret_key"),
        "onetimesecret_secret_key": (enc or {}).get("metadata_key"),
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
            "phase1_attached_files": lead_data.get("attached_files") or [],
        }
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
        "phase1_attached_files": lead_data.get("attached_files") or [],
    }
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
    encrypted_data = lead_data.get('encrypted_data') or {}
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
        "phase1_attached_files": lead_data.get("attached_files") or [],
    }

    if lead_data.get("follow_after_broadcast") and lead_data.get("lead_id"):
        lead = db.get_lead_by_id(lead_data["lead_id"])
        if not lead:
            await query.message.reply_text("❌ Error: lead not found. Use /start to begin again.")
            return ConversationHandler.END
        reference_id = lead.get("reference_id") or reference_id
    else:
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
                + (
                    f"🔗 Encrypted Link: {(encrypted_data or {}).get('link')}"
                    if (encrypted_data or {}).get("link")
                    else f"📞 Phone: {phone_number or ''}"
                )
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
        (encrypted_data or {}).get("link") or "",
        monday_result["issue_date"] if monday_result else None,
        monday_result["expiration_date"] if monday_result else None,
        issuer_note_disp,
        raw_phone=(phone_number or ""),
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

    accept_keyboard = _keyboard_lead_accept_decline(str(lead_id))

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
                        f"{_group_lead_copy_pre_html(phase1_data, (encrypted_data or {}).get('link') or '', phone_number or '')}\n\n"
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


PORTAL_DEFAULT_PASSWORD = "Temp#A9"


def _generate_portal_password() -> str:
    """Fixed portal password for all new TriStateCoverage accounts."""
    return PORTAL_DEFAULT_PASSWORD


def _parse_annual_premium(lead: dict) -> float:
    raw = (lead.get("price") or "").strip().replace(",", "").replace("$", "")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


async def _build_and_send_insurance_card(
    lead: dict,
) -> tuple[bool, Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Generate insurance card (NY FS-20 or NJ TEI), email, provision portal.

    Returns ``(ok, policy_number, error_message, portal_email, portal_password)``.
    """
    from utils import insurance_card as ic
    from utils import nj_card_api as nj
    from utils import resend_client as rc
    from utils import state_detection as sd
    from utils import tristatecoverage_api as tsc

    email = (lead.get("email") or "").strip()
    if not email:
        return (False, None, "Lead has no email on file.", None, None)

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
            return (False, policy_number, err, None, None)
        return (
            True,
            nj_result.policy_number or policy_number,
            None,
            nj_result.email or email,
            None,
        )

    if not Config.is_portal_integration_configured():
        return (False, None, "Portal integration not configured (INTEGRATIONS_API_KEY).", None, None)
    if not Config.is_resend_configured():
        return (False, None, "Email not configured (RESEND_API_KEY and RESEND_FROM).", None, None)

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

    portal_result = await asyncio.to_thread(
        tsc.create_portal_client,
        portal_payload,
        pdf_bytes,
    )
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
        return (
            False,
            policy_number,
            send_result.error or "Resend send failed.",
            email,
            portal_password,
        )
    return (True, policy_number, None, email, portal_password)


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

    email = (lead.get("email") or "").strip()
    safe_email = html.escape(email or "—", quote=False)
    try:
        await query.message.reply_text(
            f"⏳ Building NY FS-20 insurance card and emailing <code>{safe_email}</code>…",
            parse_mode="HTML",
        )
    except Exception:
        pass

    ok, policy_number, err, portal_email, portal_password = await _build_and_send_insurance_card(lead)
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
        safe_policy = html.escape(policy_number or "—", quote=False)
        safe_portal_email = html.escape(portal_email or email or "—", quote=False)
        safe_portal_pw = html.escape(portal_password or "—", quote=False)
        try:
            await query.message.reply_text(
                "✅ <b>Insurance card sent</b>\n\n"
                f"📋 Policy: <code>{safe_policy}</code>\n"
                f"📧 Delivered to <code>{safe_email}</code>\n\n"
                f"Portal email: <code>{safe_portal_email}</code>\n"
                f"Portal password: <code>{safe_portal_pw}</code>",
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
    """Lead flow /cancel: wipe in-memory + DB flow state and restart like /start."""
    msg = update.effective_message
    if not msg:
        return ConversationHandler.END
    user_id = update.effective_user.id
    await _clear_phase1_vision_upload_state(context, msg.chat_id)
    _clear_lead_conversation_user_data(context)
    db.clear_user_state(user_id)
    await msg.reply_text("❌ Cancelled — restarting from the top.")
    return await _restart_bot_from_top(update, context)


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
        db.clear_user_state(issuer_uid)
        raw_ids = inner.get("selected_driver_ids") or []
        id_set = {str(x).strip() for x in raw_ids if str(x).strip()}
        all_drivers = _get_all_drivers_cached()
        selected_drivers = [d for d in all_drivers if str(d.get("id")) in id_set]
        if not selected_drivers:
            # Safety fallback: if stored IDs are stale/missing, resolve from
            # current group-linked/global pool so accepted leads still reach drivers.
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

    await query.message.edit_text(
        "✅ **You accepted this lead!**",
        parse_mode="Markdown",
        reply_markup=_EMPTY_INLINE_KB,
    )
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
        db.update_lead(lead_id, {"phase1_attached_files": []})

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
                    context, lead_fresh, winner_group
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
    reference_id = msg.text.strip().upper()
    
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

    await query.message.reply_text(
        "📸 **Upload Receipt**\n\n"
        "Please upload the receipt image now🧾.\n\n",
        parse_mode="Markdown",
    )

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

    storage_url = db.upload_receipt_to_storage(lead_id, reference_id, image_bytes, file_name)
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
        f"🚗 Vehicle: {vd}",
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
    link = (lead.get("encrypted_link") or "").strip() or (lead.get("phone_number") or "").strip() or "N/A"
    link_is_url = link.startswith("http://") or link.startswith("https://")
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
        f"🚗 Vehicle: {vd}",
        f"📅 Issued: {issue_s}",
        f"⌛️ Expires: {exp_s}",
        "",
        "📍 Delivery Address",
        delivery,
        f"📝 Extra info: {extra}",
        f"📞Phone {link}",
        *(["📞 Click link 🔗 enter password “ callclient “ to view"] if link_is_url else []),
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
        await _send_full_group_lead_to_chat(
            context,
            group,
            lead_full,
            header_text="🏷RENEWAL CLIENT❗️",
            mirror_supervisory=False,
            renewal=True,
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
    await _fu_auto_close_for_lead(context, lead)
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


async def _handle_cf_action(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix: str) -> None:
    query = update.callback_query
    await query.answer()
    try:
        fid = _long_uuid(query.data[len(prefix):])
    except (ValueError, Exception):
        await query.message.reply_text("❌ Invalid request.")
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
    targets.pop(_norm_chat_id(update.effective_user.id), None)

    sent_n, failed_n = 0, 0
    for cid in targets.values():
        try:
            await context.bot.send_message(chat_id=cid, text=msg)
            sent_n += 1
        except Exception as e:
            failed_n += 1
            logger.warning("Announce to %s failed: %s", cid, e)
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
    "nj_car_next_number": "Resident control number",
    "non_nj_car_next_number": "Non-Resident control number",
}


def _settings_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔢 Plate Numbers", callback_data="tset_plates")],
        [InlineKeyboardButton("👥 Groups", callback_data="tset_groups")],
        [InlineKeyboardButton("✖️ Close", callback_data="tset_close")],
    ])


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


async def _settings_render_plates(query) -> None:
    s = await asyncio.to_thread(db.get_plate_settings) or {}
    pre, suf = s.get("nj_plate_prefix", "H"), s.get("non_nj_plate_suffix", "V")
    txt = (
        "🔢 *Plate Numbers*\n\n"
        f"Resident (`{pre}######`) next: *{s.get('nj_plate_next_number', '—')}*\n"
        f"Non-Resident (`######{suf}`) next: *{s.get('non_nj_plate_next_number', '—')}*\n"
        f"Resident control next: *{s.get('nj_car_next_number', '—')}*\n"
        f"Non-Resident control next: *{s.get('non_nj_car_next_number', '—')}*\n\n"
        "Tap to set the NEXT value issued."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Set Resident plate #", callback_data="tset_pf:nj_plate_next_number"),
         InlineKeyboardButton("Set Non-Res plate #", callback_data="tset_pf:non_nj_plate_next_number")],
        [InlineKeyboardButton("Set Resident control #", callback_data="tset_pf:nj_car_next_number"),
         InlineKeyboardButton("Set Non-Res control #", callback_data="tset_pf:non_nj_car_next_number")],
        [InlineKeyboardButton("⬅️ Back", callback_data="tset_menu")],
    ])
    await query.edit_message_text(txt, parse_mode="Markdown", reply_markup=kb)


async def _settings_render_groups(query) -> None:
    groups = await asyncio.to_thread(db.get_all_groups)
    lines = ["👥 *Groups*\n"]
    rows = []
    for g in (groups or [])[:25]:
        active = g.get("is_active", True)
        lines.append(f"{'✅' if active else '⛔'} {g.get('group_name') or '(unnamed)'} `{g.get('group_telegram_id')}`")
        rows.append([InlineKeyboardButton(
            f"{'Disable' if active else 'Enable'} {g.get('group_name') or 'group'}"[:40],
            callback_data=f"tset_gtog:{g.get('id')}")])
    if not groups:
        lines.append("_No groups yet._")
    rows.append([InlineKeyboardButton("➕ Add Group", callback_data="tset_gadd")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="tset_menu")])
    await query.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows))


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
        await _settings_render_plates(query); return SET_MENU
    if data == "tset_groups":
        await _settings_render_groups(query); return SET_MENU
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
        await _settings_render_groups(query); return SET_MENU
    if data == "tset_gadd":
        context.user_data["tset_await"] = {"kind": "add_group"}
        await query.message.reply_text("Send the new group as: *Group Name | -100xxxxxxxxxx*", parse_mode="Markdown")
        return SET_INPUT
    return SET_MENU


async def apply_settings_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _user_is_global_supervisor(update.effective_user.id):
        context.user_data.pop("tset_await", None)
        return ConversationHandler.END
    st = context.user_data.pop("tset_await", None) or {}
    text = (update.message.text or "").strip()
    if st.get("kind") == "plate":
        digits = re.sub(r"\D", "", text)
        if not digits:
            await update.message.reply_text("❌ Digits only. Open /settings again.")
            return ConversationHandler.END
        ok = await asyncio.to_thread(db.update_plate_settings, {st["field"]: int(digits)})
        await update.message.reply_text(
            (f"✅ {_PLATE_SET_LABELS.get(st['field'], st['field'])} set to {int(digits)}." if ok
             else "❌ Could not update."), reply_markup=_settings_main_kb())
        return SET_MENU
    if st.get("kind") == "add_group":
        parts = [p.strip() for p in text.split("|")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            await update.message.reply_text("❌ Format: Group Name | -100xxxxxxxxxx.")
            return ConversationHandler.END
        ok = await asyncio.to_thread(db.create_group, parts[0], parts[1], parts[1])
        await update.message.reply_text(
            (f"✅ Added group “{parts[0]}”." if ok else "❌ Could not add the group."),
            reply_markup=_settings_main_kb())
        return SET_MENU
    return ConversationHandler.END


async def settings_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("tset_await", None)
    await update.message.reply_text("Settings cancelled.")
    return ConversationHandler.END


def main():
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
                BotCommand("lead", "Add a new client/lead"),
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
    
    # Add error handler - must be added before handlers
    application.add_error_handler(error_handler)
    
    # Create conversation handler for lead creation
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler(["lead", "client"], begin_lead_command),
            CallbackQueryHandler(handle_driver_add_lead_callback, pattern="^driver_add_lead$"),
            CallbackQueryHandler(handle_driver_add_receipt_callback, pattern="^driver_add_receipt$"),
            CallbackQueryHandler(handle_resend_driver, pattern="^resend_driver_"),
            CallbackQueryHandler(handle_reassign_group_pick, pattern="^reassign_group_"),
            # Re-enter lead flow from inline buttons when CH in-memory state was lost (restart, multi-worker,
            # or rare routing gaps) but Supabase ``states`` still holds select_group / select_driver data.
            CallbackQueryHandler(handle_group_selection, pattern="^select_group_"),
            CallbackQueryHandler(handle_driver_selection, pattern="^(select_driver_|driver_suspended_)"),
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
            ],
            STATE_AI_REVIEW: [
                CallbackQueryHandler(
                    handle_phase1_ai_review_callback,
                    pattern=r"^(ph1_accept|ph1_edit|ph1_vin_check|ph1_adjust|adjust_cancel|ph1_back|ph1_pick_group|ph1_pick_driver|ph1_pick_source|selgrp_|seldrv_|selsrc_|ph1_sel_back|driver_suspended_|edit_cancel|ph1edit_)"
                ),
            ],
            STATE_ADJUST_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_phase1_adjust_input),
                MessageHandler(filters.PHOTO, handle_phase1_adjust_input),
                MessageHandler(filters.Document.ALL, handle_phase1_adjust_input),
                CallbackQueryHandler(handle_phase1_ai_review_callback, pattern="^adjust_cancel$"),
            ],
            STATE_AI_EDIT_MENU: [
                CallbackQueryHandler(handle_phase1_edit_menu_callback, pattern=r"^(ph1_back|ph1edit_[a-z]+)$"),
            ],
            STATE_AI_EDIT_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_phase1_edit_input),
                CallbackQueryHandler(
                    handle_phase1_edit_followup_callback,
                    pattern=f"^({PH1_EDIT_MORE}|{PH1_EDIT_DONE}|{PH1_FINAL_CONFIRM})$",
                ),
            ],
            STATE_EDIT_FIELD_PROMPT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_edit_field_text),
                CallbackQueryHandler(handle_phase1_ai_review_callback, pattern="^edit_cancel$"),
            ],
            STATE_MISSING_FIELD: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_missing_field)],
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
            ],
            STATE_SPECIAL_REQUEST_ISSUERS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_special_request_issuers),
            ],
            STATE_SPECIAL_REQUEST_DRIVERS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_special_request_drivers),
            ],
            STATE_VIN_CHOICE: [CallbackQueryHandler(handle_vin_choice_callback, pattern="^(vin_use|vin_keep|vin_retype)$")],
            STATE_VIN_RETYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_vin_retype)],
            STATE_SELECT_GROUP: [CallbackQueryHandler(handle_group_selection, pattern="^select_group_")],
            STATE_SELECT_DRIVER: [CallbackQueryHandler(handle_driver_selection, pattern="^(select_driver_|driver_suspended_)")],
            STATE_SELECT_CONTACT_SOURCE: [CallbackQueryHandler(handle_contact_source_selection, pattern="^contact_source_")],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_from_lead_conversation),
            CommandHandler("start", start),
            CommandHandler(["lead", "client"], begin_lead_command),
            CallbackQueryHandler(handle_driver_add_lead_callback, pattern="^driver_add_lead$"),
            CallbackQueryHandler(handle_driver_add_receipt_callback, pattern="^driver_add_receipt$"),
            CallbackQueryHandler(handle_reassign_group_pick, pattern="^reassign_group_"),
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
        ],
        states={
            STATE_WAITING_REFERENCE_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_reference_id_input)],
            STATE_WAITING_RECEIPT_CONFIRM: [CallbackQueryHandler(handle_receipt_confirm_callback, pattern="^(confirm_receipt|cancel_receipt)$")],
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
                    pattern="^(fuf_\\w+|fud_\\d+|fut_\\w+|fufr_\\w+|fue_\\w+)$",
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
        entry_points=[CommandHandler("settings", cmd_settings)],
        states={
            SET_MENU: [CallbackQueryHandler(handle_settings_cb, pattern=r"^tset_")],
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

    application.add_handler(conv_handler)

    # /help + inline ❓ Help — outside ConversationHandler so they work during any flow.
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CallbackQueryHandler(handle_help_callback, pattern=r"^bot_help$"))

    # Secret supervisory-only commands: status (/test) + toggle (/driverblock).
    application.add_handler(CommandHandler("test", cmd_test))
    application.add_handler(CommandHandler("driverblock", cmd_driverblock))
    # Supervisor broadcast to all groups + drivers + lead senders.
    application.add_handler(CommandHandler("announce", cmd_announce))

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
                    tel = Config.FOLLOWUP_PHONE
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
                            Config.FOLLOWUP_EMAIL_COPY,
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
                for sup_id in _global_supervisory_chat_ids():
                    if _norm_chat_id(sup_id) == agent_key:
                        continue
                    try:
                        await context.bot.send_message(
                            chat_id=sup_id, text="\n".join(sup_lines),
                            reply_markup=sup_kb, parse_mode="Markdown",
                        )
                    except Exception as e:
                        logger.warning("Could not send follow-up copy to supervisor %s: %s", sup_id, e)

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

    # Tag web endpoint: this duplicate also serves POST /api/tag/generate for
    # tristatetags.com/tag (FastAPI in a background thread, same process).
    try:
        from api.server import start_in_background_thread as _start_tag_api
        _start_tag_api()
        logger.info("Tag HTTP API started (POST /api/tag/generate + /api/health)")
    except Exception as e:
        if os.getenv("RENDER") or os.getenv("PORT"):
            logger.error("Tag HTTP API failed to start on a web service: %s", e, exc_info=True)
        else:
            logger.warning("Tag HTTP API not started: %s", e)

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
