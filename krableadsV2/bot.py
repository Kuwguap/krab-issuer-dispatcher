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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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


SUSPENSION_THRESHOLD = 3  # 3+ pending receipts = suspended
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


def _driver_add_lead_keyboard_only() -> InlineKeyboardMarkup:
    """Default driver follow-up keyboard (single action — not receipt on every message)."""
    return InlineKeyboardMarkup([[_DRIVER_ADD_LEAD_BTN]])


def _driver_keyboard_lead_and_receipt() -> InlineKeyboardMarkup:
    """After receipt submitted: add another lead or open owed-receipts upload flow."""
    return InlineKeyboardMarkup([[_DRIVER_ADD_LEAD_BTN, _DRIVER_ADD_RECEIPT_BTN]])


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


async def _forward_phase1_attached_files_to_targets(
    context: ContextTypes.DEFAULT_TYPE,
    attached_files: list,
    group_chat_id: int | str | None,
) -> None:
    """Forward Phase 1 files to the **accepting** group chat only.

    Invoked from ``handle_accept_group_offer`` after Accept — not during approval broadcast or driver pick.
    """
    if not attached_files:
        return
    _group_cid = _parse_chat_id(group_chat_id) if group_chat_id is not None else None
    if not _group_cid:
        return
    for f in attached_files:
        ftype = (f.get("type") or "").lower()
        fid = f.get("file_id")
        if not fid:
            continue
        try:
            if ftype == "photo":
                await context.bot.send_photo(chat_id=_group_cid, photo=fid)
            elif ftype == "document":
                await context.bot.send_document(chat_id=_group_cid, document=fid)
            else:
                await context.bot.send_document(chat_id=_group_cid, document=fid)
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
    group_name: str,
    driver_names: str,
    username: str,
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
    gn = one_line(group_name)
    dn = one_line(driver_names)
    cn = one_line(client_name)
    if source_label and str(source_label).strip():
        src = one_line(str(source_label))
    else:
        src = "—"
    if driver_count is None:
        parts = [p.strip() for p in (driver_names or "").split(",") if p.strip()]
        n = len(parts)
    else:
        n = int(driver_count)
    if n <= 0:
        send_mode = "Send Mode: —"
    elif n == 1:
        send_mode = "Send Mode: 1 Driver"
    else:
        send_mode = "Send Mode: Multiple Drivers"
    un = (username or "").strip()
    if un and un != "Unknown":
        by_line = un if un.startswith("@") else f"@{un}"
    else:
        by_line = "Unknown"
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
    """One DM to the lead adder: Reference, Group, Driver(s) — only the driver who accepted."""
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
    text = f"New lead\n\nReference: {ref}\nGroup: {group_label}\nDriver(s): {dn}"
    try:
        await context.bot.send_message(chat_id=cid, text=text, parse_mode=None)
    except Exception as e:
        logger.warning("Could not send initiator lead summary to %s: %s", cid, e)


async def _send_driver_requests_for_group(
    context: ContextTypes.DEFAULT_TYPE,
    lead: dict,
    group: dict,
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

    reference_id = lead.get("reference_id", "N/A")
    extra_safe = _sanitize_phones_for_send(lead.get("extra_info") or "")
    spec = _lead_driver_note(lead)
    driver_request_message = (
        f"👋Hi! New client 💸 available📈❗️\n\n"
        f"📍 Delivery (City, State, Zip): {lead.get('delivery_details', '')}\n"
        f"📋 Reference ID: `{reference_id}`\n"
        f" Delivery Time 🏷️: {extra_safe}\n"
        f"Please have Car, Driver License, and Laser Printer Ready✅"
    )
    if spec:
        driver_request_message += f"\n\n📝 Special request (driver): {_sanitize_phones_for_send(spec)}"
    accept_keyboard = _keyboard_lead_accept_decline(str(lead["id"]))
    assigned_count = 0
    for driver in selected_drivers:
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
            price = m.group() if '$' in val else '$' + m.group()
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
}


def _name_parts_from_full(name: str) -> tuple:
    n = (name or "").strip()
    if not n or n == "-":
        return ("-", "-")
    parts = n.split(None, 1)
    first = parts[0]
    last = parts[1] if len(parts) > 1 else "-"
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
        f"First name: {first}",
        f"Last name: {last}",
        f"Registration address: {state_data.get('address') or '-'}",
        f"Registration city, state, ZIP: {state_data.get('city_state_zip') or '-'}",
        f"Delivery address: {state_data.get('delivery_address') or '-'}",
        f"Delivery city, state, ZIP: {state_data.get('delivery_city_state_zip') or '-'}",
        f"VIN: {state_data.get('vin') or '-'}",
        f"Car: {state_data.get('car') or '-'}",
        f"Color: {state_data.get('color') or '-'}",
        f"Insurance company: {state_data.get('insurance_company') or '-'}",
        f"Insurance policy #: {state_data.get('insurance_policy_number') or '-'}",
        f"Delivery Date/Time & Notes: {state_data.get('extra_info') or '-'}",
        f"Phone: {state_data.get('pending_phone_number') or '-'}",
        f"Price: {state_data.get('pending_price') or '-'}",
        f"Issuer note: {state_data.get('special_request_issuers') or '-'}",
        f"Driver note: {state_data.get('special_request_drivers') or '-'}",
    ]
    return "\n".join(lines)


def _format_phase1_ai_review_text(state_data: dict) -> str:
    """Human-readable summary of how the bot understood Phase 1 (AI path). Plain text (safe for special chars)."""
    return (
        "📝 Here's how I understood your lead:\n\n"
        + _format_phase1_field_lines(state_data)
        + "\n\nTap Accept to continue, or tap Edit to make changes."
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
            InlineKeyboardButton("✅ Submit", callback_data=PH1_REVIEW_ACCEPT),
        ],
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


async def _send_phase1_ai_review(target_message, state_data: dict, context, user_id) -> None:
    # Set default selections
    groups = db.get_all_groups()
    active_groups = [g for g in groups if record_is_active(g)]
    if active_groups:
        user_tg = str(user_id)
        group = db.get_group_by_assistant_telegram_id(user_tg)
        if not group or not record_is_active(group):
            group = active_groups[0]
        state_data["selected_group_id"] = group["id"]
        state_data["selected_group_name"] = group.get("group_name", "?")
    else:
        state_data["selected_group_id"] = None
        state_data["selected_group_name"] = "None"

    drivers = _get_all_drivers_cached()
    active_drivers = [d for d in drivers if record_is_active(d)]
    suspended = _get_suspended_driver_ids()
    eligible = [d for d in active_drivers if str(d.get("id")) not in suspended]
    if eligible:
        d0 = eligible[0]
        state_data["selected_driver_ids"] = [d0["id"]]
        state_data["selected_driver_names"] = d0.get("driver_name", "?")
    else:
        state_data["selected_driver_ids"] = []
        state_data["selected_driver_names"] = "None"

    state_data["selected_source_label"] = ""

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
    alert_msg, conflict = _vin_check_after_phase1(state_data)
    if conflict:
        api_car, stated_car = conflict
        context.user_data["vin_choice_api_car"] = api_car
        context.user_data["vin_choice_stated_car"] = stated_car
        keyboard = _vin_choice_keyboard(api_car, stated_car)
        vin_msg = await message.reply_text(
            _vin_conflict_body(stated_car, api_car),
            reply_markup=keyboard,
        )
        context.user_data["vin_conflict_msg_id"] = vin_msg.message_id
        return STATE_VIN_CHOICE
    if alert_msg:
        await message.reply_text(alert_msg)
    # Re-check missing fields against a synthetic blob so detector still works
    blob = "\n".join(
        str(state_data.get(k) or "")
        for k in ("name", "address", "city_state_zip", "delivery_address", "delivery_city_state_zip", "vin", "car", "color", "insurance_company", "insurance_policy_number", "extra_info")
    )
    missing = ai_vision.detect_missing_fields(state_data, blob)
    if missing:
        prompts = ai_vision.MISSING_FIELD_PROMPTS
        msg = prompts.get(missing[0], (f"You missed out {missing[0]}. Can you add it?", missing[0]))[0]
        context.user_data["missing_fields"] = missing
        context.user_data["missing_field_state_data"] = state_data.copy()
        await message.reply_text(msg)
        return STATE_MISSING_FIELD
    return await _ensure_phone_price_before_files(message, context, user_id)


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
        context.user_data.pop("phase1_pending_edit_key", None)
        context.user_data.pop("phase1_recent_edits", None)
        for _k in ("receipt_lead_id", "receipt_reference_id", "receipt_monday_item_id"):
            context.user_data.pop(_k, None)

    db.set_user_state(user_id, "phase1", {})

    phase1_instruction = (
    "📤 **Send all information at once**\n\n"
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
    "📝 Special request for drivers (optional)\n\n"
    "You can attach files after this step.\n\n"
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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the conversation and initialize state."""
    if not update.message:
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
        lines.append(f"\n{motivation.get_random_quote()}")
        lines.append("\n🏁Automated🏎Automotive")
        await update.message.reply_text(
            "\n".join(lines),
            reply_markup=_driver_add_lead_keyboard_only(),
        )
        return ConversationHandler.END

    return await begin_lead_command(update, context)


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
) -> str:
    """Telegram HTML for the detailed group lead: copy section in <pre> for tap-to-copy."""
    def _safe_raw(s: str) -> str:
        return (_sanitize_phones_for_send(s or "") or "").strip() or "-"

    def _h(s: str) -> str:
        return html.escape(s or "", quote=False)

    vin_raw = (phase1_data.get("vin") or "").strip() or "-"
    car_raw = (phase1_data.get("car") or "").strip() or "-"
    name_line = _h(_safe_raw(phase1_data.get("name")))
    tail_lines = [
        _h(_safe_raw(phase1_data.get("address"))),
        _h(_safe_raw(phase1_data.get("city_state_zip"))),
        _h(vin_raw),
        _h(car_raw),
        _h(_safe_raw(phase1_data.get("color"))),
        _h(_safe_raw(phase1_data.get("insurance_company"))),
        _h(_safe_raw(phase1_data.get("insurance_policy_number"))),
        _h(_safe_raw(phase1_data.get("extra_info"))),
    ]
    note_i = (special_request_issuers or "").strip()
    if note_i:
        tail_lines.append(_h("📝 " + _safe_raw(note_i)))
    else:
        tail_lines.append(_h("📝 No"))
    vehicle_block = f"🚗 Vehicle: {name_line}\n" + "\n".join(tail_lines)

    issue_s = issue_dt.strftime("%Y-%m-%d %H:%M:%S %Z") if issue_dt else "N/A"
    expiry_s = expiry_dt.strftime("%Y-%m-%d %H:%M:%S %Z") if expiry_dt else "N/A"

    pre_wrapped = _group_lead_copy_pre_html(phase1_data, encrypted_link)
    return (
        "🏷NEW CLIENT❗️\n\n"
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
    if un and un.lower() != "unknown":
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
    mirror_supervisory: bool = False,
) -> None:
    """Post the same detailed HTML lead as the issuer flow; optionally mirror to supervisory chat(s)."""
    reference_id = (lead.get("reference_id") or "N/A").strip()
    phase1 = _phase1_from_stored_lead(lead)
    link = (lead.get("encrypted_link") or "").strip()
    issuer_note = _lead_issuer_note(lead)
    issue_dt, exp_dt = _issue_and_expiration_for_group_display(lead)
    body = _format_group_lead_message_html(
        reference_id, phase1, link, issue_dt, exp_dt, issuer_note,
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

    link_raw = (lead.get("encrypted_link") or "").strip() or "N/A"
    if link_raw.startswith("http://") or link_raw.startswith("https://"):
        link_line = f'📞Phone <a href="{html.escape(link_raw, quote=True)}">open link</a>'
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
        "• Be fast, polite, professional🤵",
        "• Double-check all info ℹ️",
        "• Drive safely 🚘",
        "• Upload receipt 🧾 within 1 hour ⚡️",
        "",
        "👇 Upload Payment Receipt Below 📸",
    ])
    return "\n".join(lines)


async def _phase1_finish_vision_extraction(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    raw_text: Optional[str],
    *,
    source_label: str = "image",
) -> int:
    """Normalize AI vision output, validate, then AI review — shared by photo and PDF."""
    if not raw_text or not raw_text.strip():
        await update.message.reply_text(
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
        await update.message.reply_text(
            "⚠️ Extraction didn’t pass validation:\n\n• " + err_blurb + "\n\n"
            "Extracted preview:\n" + preview + "\n\n"
            "Please send the details as text in the required 11-line structure, or try another photo or PDF."
        )
        return STATE_PHASE1
    # Parse extra fields (phone, price, notes) from lines 12-15
    phone = price = issuer_note = driver_note = None
    extra_lines = lines[ai_vision.PHASE1_LINE_COUNT:]
    for line in extra_lines:
        l = line.strip()
        if not l or l == "-":
            continue
        if l.lower().startswith("phone:"):
            phone = l.split(":", 1)[1].strip()
        elif l.lower().startswith("price:"):
            price = l.split(":", 1)[1].strip()
        elif l.lower().startswith("issuer note:"):
            issuer_note = l.split(":", 1)[1].strip()
            if issuer_note.lower() in ("-", "none", "n/a", "na"):
                issuer_note = None
        elif l.lower().startswith("driver note:"):
            driver_note = l.split(":", 1)[1].strip()
            if driver_note.lower() in ("-", "none", "n/a", "na"):
                driver_note = None

    if phone and price:
        state_data["pending_phone_number"] = phone
        state_data["pending_price"] = price
        if issuer_note:
            state_data["special_request_issuers"] = issuer_note
        if driver_note:
            state_data["special_request_drivers"] = driver_note

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

    db.set_user_state(user_id, "phase1", state_data)
    await _send_phase1_ai_review(update.message, state_data, context, user_id)
    return STATE_AI_REVIEW


async def handle_phase1_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle Phase 1 image upload: OCR + AI extract structured fields, then same flow as text."""
    user_id = update.effective_user.id
    if not Config.is_ai_vision_configured():
        await update.message.reply_text(
            "❌ Image extraction is not configured. Please send the details as text in the required structure."
        )
        return STATE_PHASE1
    await update.message.reply_text("⏳ Processing image…")
    if not update.message.photo:
        await update.message.reply_text("❌ No image received. Please send a screenshot or try sending as text.")
        return STATE_PHASE1
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    bio = io.BytesIO()
    await file.download_to_memory(out=bio)
    image_bytes = bio.getvalue()
    mime = "image/jpeg"
    if file.file_path and file.file_path.lower().endswith(".png"):
        mime = "image/png"
    try:
        raw_text = await asyncio.to_thread(
            lambda: ai_vision.extract_structured_from_image(image_bytes, mime_type=mime)
        )
    except ai_vision.AIVisionQuotaError:
        await update.message.reply_text(
            "❌ Image extraction is temporarily unavailable (API quota exceeded). "
            "Please send the details as text in the required structure."
        )
        return STATE_PHASE1
    return await _phase1_finish_vision_extraction(update, context, user_id, raw_text, source_label="photo")


async def handle_phase1_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Phase 1 PDF: render first page and run the same AI extraction + review as photos."""
    user_id = update.effective_user.id
    if not Config.is_ai_vision_configured():
        await update.message.reply_text(
            "❌ Document extraction is not configured. Please send the details as text in the required structure."
        )
        return STATE_PHASE1
    msg = update.message
    doc = msg.document if msg else None
    if not doc:
        await update.message.reply_text("❌ No document received.")
        return STATE_PHASE1
    mime = (doc.mime_type or "").lower()
    fname = (doc.file_name or "").lower()
    pdf_mimes = ("application/pdf", "application/x-pdf")
    if mime not in pdf_mimes and not fname.endswith(".pdf"):
        await update.message.reply_text(
            "📄 In Phase 1, send **text**, a **photo/screenshot**, or a **PDF** with vehicle and delivery details.\n\n"
            "Other file types are not supported for auto-extraction — use a PDF or type the details.",
            parse_mode="Markdown",
        )
        return STATE_PHASE1
    sz = doc.file_size
    if sz is not None and sz > 20 * 1024 * 1024:
        await update.message.reply_text(
            "❌ This PDF is too large (max ~20 MB). Please send a smaller file or a screenshot."
        )
        return STATE_PHASE1
    await update.message.reply_text("⏳ Processing PDF…")
    file = await context.bot.get_file(doc.file_id)
    bio = io.BytesIO()
    await file.download_to_memory(out=bio)
    pdf_bytes = bio.getvalue()
    try:
        raw_text = await asyncio.to_thread(
            lambda: ai_vision.extract_structured_from_pdf(pdf_bytes)
        )
    except ai_vision.AIVisionQuotaError:
        await update.message.reply_text(
            "❌ Extraction is temporarily unavailable (API quota exceeded). "
            "Please send the details as text in the required structure."
        )
        return STATE_PHASE1
    if not raw_text:
        await update.message.reply_text(
            "❌ Could not read this PDF (empty, invalid, or install failed). "
            "Try another PDF, send a photo/screenshot, or type the details."
        )
        return STATE_PHASE1
    return await _phase1_finish_vision_extraction(update, context, user_id, raw_text, source_label="photo")


async def handle_phase1(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle Phase 1: Vehicle and delivery details. If AI is configured, accept any format and let model rearrange."""
    user_id = update.effective_user.id
    message_text = (update.message.text or "").strip()
    if not message_text:
        await update.message.reply_text(
            "Please send the client/vehicle and delivery details (text, screenshot, or PDF)."
        )
        return STATE_PHASE1

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
            if l.lower().startswith("phone:"):
                state_data["pending_phone_number"] = l.split(":", 1)[1].strip()
            elif l.lower().startswith("price:"):
                state_data["pending_price"] = l.split(":", 1)[1].strip()
            elif l.lower().startswith("issuer note:"):
                note = l.split(":", 1)[1].strip()
                if note.lower() not in ("-", "none", "n/a", "na"):
                    state_data["special_request_issuers"] = note
                else:
                    state_data["special_request_issuers"] = ""   # explicitly clear
            elif l.lower().startswith("driver note:"):
                note = l.split(":", 1)[1].strip()
                if note.lower() not in ("-", "none", "n/a", "na"):
                    state_data["special_request_drivers"] = note
                else:
                    state_data["special_request_drivers"] = ""

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
        _sanitize_phase1_pending_phone_price(state_data)
        db.set_user_state(user_id, "phase1", state_data)
        await _send_phase1_ai_review(update.message, state_data, context, user_id)
        alert_msg, conflict = _vin_check_after_phase1(state_data)
        if conflict:
            api_car, stated_car = conflict
            context.user_data["vin_choice_api_car"] = api_car
            context.user_data["vin_choice_stated_car"] = stated_car
            keyboard = _vin_choice_keyboard(api_car, stated_car)
            await update.message.reply_text(
                _vin_conflict_body(stated_car, api_car),
                reply_markup=keyboard,
            )
            return STATE_VIN_CHOICE
        if alert_msg:
            await update.message.reply_text(alert_msg)
        missing = ai_vision.detect_missing_fields(state_data, message_text)
        OPTIONAL_FIELDS = {"insurance_company", "insurance_policy_number", "extra_info", "delivery_date"}
        missing = [f for f in missing if f not in OPTIONAL_FIELDS]
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


async def _ask_add_files(message, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ask user if they want to add files; returns STATE_ADD_FILES."""
    context.user_data["phase1_attached_files"] = []
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Yes", callback_data="add_files_yes")],
        [InlineKeyboardButton("No", callback_data="add_files_no")],
    ])
    sent = await message.reply_text("Do you want to add files?", reply_markup=keyboard)
    if sent is not None:
        context.user_data["add_files_prompt_msg_id"] = sent.message_id
    return STATE_ADD_FILES


async def _ensure_phone_price_before_files(message, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> int:
    """Require client phone + price before the add-files step (same gate as other mandatory fields)."""
    state = db.get_user_state(user_id)
    if not state or not state.get("data"):
        await message.reply_text("❌ Phase 1 data not found. Please start over with /start")
        return ConversationHandler.END
    state_data = state["data"].copy()
    if _phase1_has_phone_and_price(state_data):
        return await _ask_add_files(message, context)
    context.user_data["phase2_before_files"] = True
    db.set_user_state(user_id, "phase1", state_data)
    await message.reply_text(
        "📞💲 **Phone number and price are required before you can attach files.**\n\n" + PHASE2_INTRO_MESSAGE,
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
        await msg.reply_text("File saved. Tap Yes/No on the previous keyboard if you still see it, or send /cancel and /start.")
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
            names = ", ".join(d.get("driver_name", "?") for d in selected)
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
        await update.message.reply_text("Please send the missing value.")
        return STATE_MISSING_FIELD
    missing_fields = context.user_data.get("missing_fields") or []
    state_data = context.user_data.get("missing_field_state_data") or {}
    field = missing_fields[0] if missing_fields else "color"
    state_key = MISSING_FIELD_TO_STATE_KEY.get(field, field)
    state_data[state_key] = text
    missing_fields = missing_fields[1:]
    context.user_data["missing_fields"] = missing_fields
    context.user_data["missing_field_state_data"] = state_data
    if missing_fields:
        next_field = missing_fields[0]
        prompts = ai_vision.MISSING_FIELD_PROMPTS
        msg = prompts.get(next_field, (f"You missed out {next_field}. Can you add it?", next_field))[0]
        await update.message.reply_text(msg)
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
    if still_missing:
        context.user_data["missing_fields"] = still_missing
        context.user_data["missing_field_state_data"] = state_data.copy()
        prompts = ai_vision.MISSING_FIELD_PROMPTS
        first = still_missing[0]
        msg = prompts.get(first, (f"You missed out {first}. Can you add it?", first))[0]
        await update.message.reply_text(msg)
        return STATE_MISSING_FIELD

    _sanitize_phase1_pending_phone_price(state_data)
    db.set_user_state(user_id, "phase1", state_data)
    context.user_data.pop("missing_fields", None)
    context.user_data.pop("missing_field_state_data", None)
    alert_msg, conflict = _vin_check_after_phase1(state_data)
    if conflict:
        api_car, stated_car = conflict
        context.user_data["vin_choice_api_car"] = api_car
        context.user_data["vin_choice_stated_car"] = stated_car
        keyboard = _vin_choice_keyboard(api_car, stated_car)
        await update.message.reply_text(
            _vin_conflict_body(stated_car, api_car),
            reply_markup=keyboard,
        )
        return STATE_VIN_CHOICE
    if alert_msg:
        await update.message.reply_text(alert_msg)
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
    # re-fetch state and update review
    state = db.get_user_state(user_id)
    if state and state.get("data"):
        await _update_review_message_text(context, state["data"])
    return await _ensure_phone_price_before_files(query.message, context, user_id)


async def handle_vin_retype(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle user's new VIN input; re-run lookup and either proceed or show choice again."""
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
        await update.message.reply_text(
            _vin_conflict_body(stated_car, api_car),
            reply_markup=keyboard,
        )
        return STATE_VIN_CHOICE
    if alert_msg:
        await update.message.reply_text(alert_msg)
    return await _ensure_phone_price_before_files(update.message, context, user_id)


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
    if context.user_data.pop("phase2_before_files", None):
        db.set_user_state(user_id, "phase1", state_data)
        await msg.reply_text(PHASE2_SUCCESS_BEFORE_FILES_MESSAGE)
        return await _ask_add_files(msg, context)
    db.set_user_state(user_id, "special_request_issuers", state_data)
    await msg.reply_text(PHASE2_ISSUERS_PROMPT)
    return STATE_SPECIAL_REQUEST_ISSUERS


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
    db.set_user_state(user_id, "special_request_drivers", state_data)
    await update.message.reply_text(
        "📝 Would you like to say any Special Requests to the delivery drivers? (optional)"
    )
    return STATE_SPECIAL_REQUEST_DRIVERS


async def handle_special_request_drivers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """After issuer + driver notes: encrypt phone and continue to group/driver selection."""
    user_id = update.effective_user.id
    username = update.effective_user.username or "Unknown"
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
    phone_number = state_data.pop("pending_phone_number", None)
    price = state_data.pop("pending_price", None)

    raw_d = (update.message.text or "").strip()
    skip_tokens = frozenset(("-", "—", "–", "none", "n/a", "na"))
    drivers_note = "" if not raw_d or raw_d.lower() in skip_tokens else raw_d
    state_data["special_request_drivers"] = drivers_note
    issuers_note = (state_data.get("special_request_issuers") or "").strip()

    encrypted_data = await asyncio.to_thread(ots.encrypt_phone, phone_number)
    if not encrypted_data:
        state_data["pending_phone_number"] = phone_number
        state_data["pending_price"] = price
        db.set_user_state(user_id, "special_request_drivers", state_data)
        reason = (getattr(ots, "last_error", "") or "").strip()
        if reason:
            await update.message.reply_text(
                "❌ Error encrypting phone number.\n\n"
                f"Reason: {reason}\n\n"
                "If this keeps happening, the `clientsphonenumber` service is usually missing env vars "
                "(SUPABASE_URL/SUPABASE_KEY and ONETIMESECRET_USERNAME/ONETIMESECRET_API_KEY) on Vercel "
                "or the bot has wrong credentials.\n\n"
                "Send your reply again when ready (or **-** for none).",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text(
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
        await update.message.reply_text("❌ Error: No active groups configured. Please contact admin.")
        return ConversationHandler.END

    assistants_choose_group = (db.get_setting("assistants_choose_group") or "").lower() in ("true", "1", "yes")

    if assistants_choose_group:
        db.set_user_state(user_id, "select_group", state_data)
        group_keyboard = _build_group_keyboard(active_groups, include_all=True)
        await update.message.reply_text(
            "✅ Ready.\n\n**Select which group to send this lead to:**",
            parse_mode="Markdown",
            reply_markup=group_keyboard,
        )
        return STATE_SELECT_GROUP

    user_telegram_id = str(update.effective_user.id)
    assistant_group = db.get_group_by_assistant_telegram_id(user_telegram_id)
    if assistant_group and record_is_active(assistant_group):
        selected_group = assistant_group
        group_id = selected_group["id"]
        logger.info(f"User is assistant for group '{selected_group.get('group_name')}'; using that group for lead")
    else:
        selected_group = active_groups[0]
        group_id = selected_group["id"]
    logger.info(
        f"Using group '{selected_group.get('group_name')}' (id={group_id}, "
        f"group_telegram_id={selected_group.get('group_telegram_id')}) for lead"
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
        "phase1_attached_files": state_data.get("attached_files") or [],
    }
    lead = db.create_lead(final_lead_data)
    if not lead:
        await update.message.reply_text("❌ Error saving lead to database.")
        return ConversationHandler.END

    reference_id = lead.get("reference_id", "N/A")

    await _post_single_group_approval(context, lead, selected_group)

    continue_data = state_data.copy()
    continue_data["lead_id"] = lead["id"]
    continue_data["group_id"] = group_id
    continue_data["selected_group"] = selected_group
    continue_data["follow_after_broadcast"] = True
    _store_issuer_await_group_accept(
        user_id,
        lead_id=str(lead["id"]),
        await_mode="pick_drivers",
        pick_payload=continue_data,
    )
    ref_h = html.escape(str(reference_id), quote=False)
    await update.message.reply_text(
        "✅ Lead saved.\n\n"
        f"📋 Reference ID: <code>{ref_h}</code>\n"
        f"Approval sent to <b>{html.escape(selected_group.get('group_name') or 'group', quote=False)}</b>.\n\n"
        "**Wait for the team to tap Accept** in their group chat. You will get a prompt here to choose drivers "
        "**right after** they accept.",
        parse_mode="HTML",
    )
    return ConversationHandler.END

async def _submit_lead_from_review(message, context, user_id, data):
    if not _is_valid_pending_phone(data.get("pending_phone_number")) or not _is_valid_pending_price(
        data.get("pending_price")
    ):
        _sanitize_phase1_pending_phone_price(data)
        db.set_user_state(user_id, "phase1", data)
        await message.reply_text(
            "📞💲 **Phone number and price are required** before sending the lead.\n\n" + PHASE2_INTRO_MESSAGE,
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

    lead = db.create_lead({
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
        "contact_info_source": data.get("selected_source_label", ""),
        "phase1_attached_files": data.get("attached_files") or [],
    })
    if not lead:
        await message.reply_text("❌ Could not save lead.")
        return ConversationHandler.END

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
    context.user_data.pop("review_message_id", None)
    success_label = "broadcast sent" if is_all_groups else "sent"
    await message.reply_text(
        f"✅ **Lead created & {success_label}!**\n📋 Reference: `{ref_id}`\n\n"
        "**Teams must tap Accept** in their group chat before drivers are notified.\n\n"
        f"In **Krab Dispatch**, name the tag PDF **similar to this client’s name** (first line of details) so it auto‑links to reference `{ref_id}`.\n\n"
        f"Use /lead to add another.",
        parse_mode="Markdown",
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
            "phase1_attached_files": lead_data.get("attached_files") or [],
        }
        lead = db.create_lead(final_lead_data)
        if not lead:
            await query.message.reply_text("❌ Error saving lead to database.")
            return ConversationHandler.END

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
            await_mode="pick_drivers",
            pick_payload=continue_data,
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
                f"Sent to {sent_count} group(s).\n\n"
                "**Wait for a team to tap Accept** in their chat. You will get a prompt here to choose drivers "
                "right after a team accepts."
                f"{html.escape(summary, quote=False)}"
            )
            await query.message.reply_text(body, parse_mode="HTML")
        except Exception as e:
            logger.error("Broadcast: could not reply to issuer: %s", e)
            await query.message.reply_text(
                "📣 Approval requests sent. Wait for a team to **Accept**, then choose drivers here.",
                parse_mode="Markdown",
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
            await_mode="pick_drivers",
            pick_payload=continue_data,
        )
        reference_id = lead.get("reference_id", "N/A")
        ref_h = html.escape(str(reference_id), quote=False)
        await query.message.reply_text(
            "✅ Group updated.\n\n"
            f"📋 Reference ID: <code>{ref_h}</code>\n"
            f"Approval sent to <b>{html.escape(selected_group.get('group_name') or 'group', quote=False)}</b>.\n\n"
            "**Wait for the team to tap Accept** — you will then be prompted here to choose drivers.",
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
        "phase1_attached_files": lead_data.get("attached_files") or [],
    }
    lead = db.create_lead(final_lead_data)
    if not lead:
        await query.message.reply_text("❌ Error saving lead to database.")
        return ConversationHandler.END

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
        await_mode="pick_drivers",
        pick_payload=continue_data,
    )
    ref_h = html.escape(str(reference_id), quote=False)
    await query.message.reply_text(
        f"✅ Group selected: <b>{html.escape(selected_group.get('group_name', 'N/A'), quote=False)}</b>\n\n"
        f"📋 Reference ID: <code>{ref_h}</code>\n"
        "**Wait for the team to tap Accept** in their chat. You will get a prompt here to choose drivers "
        "**right after** they accept.",
        parse_mode="HTML",
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
        _safe(phase1_data.get("insurance_policy_number")),
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

    contact_sources = db.get_contact_info_sources()

    if contact_sources:
        db.set_user_state(
            user_id,
            "select_contact_source",
            {
                "lead_id": lead["id"],
                "reference_id": reference_id,
                "driver_names": driver_names,
                "group_name": selected_group.get("group_name", "N/A"),
                "username": username,
            },
        )
        buttons = [
            [InlineKeyboardButton(s.get("label", str(s["id"])), callback_data=f"contact_source_{s['id']}")]
            for s in contact_sources
        ]
        await query.message.reply_text(
            "📊 **Where did this lead come from?**\n\n"
            "Tap a **lead source** below.\n"
            f"⏱️ You have **{CONTACT_SOURCE_TIMEOUT_SEC // 60} minutes** — if you don't, we save the lead without a source.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        if context.application.job_queue:
            context.application.job_queue.run_once(
                _contact_source_timeout_job,
                when=CONTACT_SOURCE_TIMEOUT_SEC,
                data={
                    "user_id": user_id,
                    "lead_id": lead["id"],
                    "reference_id": reference_id,
                },
                name=f"contact_source_timeout_{user_id}_{lead['id']}",
            )
        return STATE_SELECT_CONTACT_SOURCE

    await _issuer_lead_success_and_motivation(
        query.message, user_id, username, reference_id, driver_names, selected_group.get("group_name", "N/A"),
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
    """3 minutes without tapping a lead source: clear state; lead stays without source."""
    job = context.job
    if not job or not job.data:
        return
    user_id = job.data.get("user_id")
    expected_lead_id = job.data.get("lead_id")
    reference_id = job.data.get("reference_id", "N/A")
    if user_id is None or expected_lead_id is None:
        return
    st = db.get_user_state(user_id)
    if not st or st.get("state") != "select_contact_source":
        return
    data = st.get("data") or {}
    if str(data.get("lead_id")) != str(expected_lead_id):
        return
    db.clear_user_state(user_id)
    ref_esc = _telegram_md1_escape(str(reference_id))
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                "⏱️ **Lead source not selected in time**\n\n"
                "Your lead was already sent to drivers **without** a lead source.\n\n"
                f"📋 Reference: `{ref_esc}`\n\n"
                "Start a new lead anytime with /lead or /client."
            ),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.warning("Could not send contact source timeout message to %s: %s", user_id, e)

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
    body_supervisory = _new_lead_supervisory_notice_text(
        reference_id,
        group_name,
        driver_names,
        uname,
        client_name=client_nm,
        source_label=src_raw or None,
        include_lead_issuer=True,
        driver_count=driver_count,
    )
    body_st_only = _new_lead_supervisory_notice_text(
        reference_id,
        group_name,
        driver_names,
        uname,
        client_name=client_nm,
        source_label=src_raw or None,
        include_lead_issuer=False,
        driver_count=driver_count,
    )
    sup_text_supervisory = _prefix_supervisory_message(body_supervisory)
    sup_text_st = _prefix_supervisory_message(body_st_only)
    lead = lead_row
    group_row = None
    if lead and lead.get("group_id"):
        group_row = db.get_group_by_id(lead["group_id"])
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
                parse_mode=None,
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
                    parse_mode=None,
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
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the conversation."""
    user_id = update.effective_user.id
    if context.user_data:
        context.user_data.pop("phase1_pending_edit_key", None)
    db.clear_user_state(user_id)
    
    await update.message.reply_text("❌ Operation cancelled. Use /start to begin again.")
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
            _safe(phase1_data.get("insurance_policy_number")),
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

        contact_sources = db.get_contact_info_sources()
        if contact_sources:
            db.set_user_state(
                issuer_uid,
                "select_contact_source",
                {
                    "lead_id": lead_ref["id"],
                    "reference_id": reference_id,
                    "driver_names": driver_names,
                    "group_name": winner_group.get("group_name", "N/A"),
                    "username": username,
                },
            )
            buttons = [
                [InlineKeyboardButton(s.get("label", str(s["id"])), callback_data=f"contact_source_{s['id']}")]
                for s in contact_sources
            ]
            try:
                await context.bot.send_message(
                    chat_id=issuer_uid,
                    text=(
                        "📊 **Where did this lead come from?**\n\n"
                        "Tap a **lead source** below.\n"
                        f"⏱️ You have **{CONTACT_SOURCE_TIMEOUT_SEC // 60} minutes** — if you don't, we save the lead without a source."
                    ),
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(buttons),
                )
            except Exception as e:
                logger.warning("contact source prompt after delayed dispatch: %s", e)
            if context.application.job_queue:
                context.application.job_queue.run_once(
                    _contact_source_timeout_job,
                    when=CONTACT_SOURCE_TIMEOUT_SEC,
                    data={
                        "user_id": issuer_uid,
                        "lead_id": lead_ref["id"],
                        "reference_id": reference_id,
                    },
                    name=f"contact_source_timeout_{issuer_uid}_{lead_ref['id']}",
                )
        else:
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

    # Send confirmation to driver (plain text — long template with payment lines from Config)
    confirmation_message = _build_driver_lead_accepted_message_html(lead)

    add_lead_kb = _driver_add_lead_keyboard_only()

    await query.message.edit_text(
        "✅ **You accepted this lead!**",
        parse_mode="Markdown",
        reply_markup=_EMPTY_INLINE_KB,
    )
    try:
        await query.message.reply_text(
            confirmation_message,
            reply_markup=add_lead_kb,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except BadRequest:
        plain = re.sub(r"<[^>]+>", "", confirmation_message)
        await query.message.reply_text(plain, reply_markup=add_lead_kb)

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
            db.schedule_renewal(
                lead_id=lead_id,
                group_id=group_id if group_id else None,
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
                        text=f"✅ **Accepted by {gname}**\n\nReference ID: `{ref_show}`",
                        parse_mode="Markdown",
                        reply_markup=_EMPTY_INLINE_KB,
                    )
                else:
                    await context.bot.edit_message_text(
                        chat_id=ocid,
                        message_id=int(mid),
                        text=(
                            f"❌ **Taken by another group**\n\n"
                            f"Accepted by: **{gname}**\nReference ID: `{ref_show}`"
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
                    text=f"✅ **Accepted by {winner_name}**\n\nReference ID: `{reference_id}`",
                    parse_mode="Markdown",
                    reply_markup=_EMPTY_INLINE_KB,
                )
            else:
                await context.bot.edit_message_text(
                    chat_id=ocid,
                    message_id=int(mid),
                    text=f"❌ **Taken by another group**\n\nAccepted by: **{winner_name}**\nReference ID: `{reference_id}`",
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
            try:
                await _issuer_open_driver_selection_after_group_accept(
                    context, str(lead_id), lead_for_files
                )
            except Exception as e:
                logger.warning("Could not notify issuer after group accept: %s", e)
            try:
                lead_for_group = db.get_lead_by_id(lead_id) or lead
                await _send_full_group_lead_to_chat(
                    context,
                    winner_group,
                    lead_for_group,
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


def _merge_receipt_context_from_db(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    row = db.get_user_state(user_id)
    if not row or not row.get("data"):
        return
    st_name = (row.get("state") or "").strip()
    if st_name not in ("waiting_receipt_image", "waiting_receipt_confirm", "waiting_reference_id"):
        return
    data = row["data"]
    for key in ("receipt_lead_id", "receipt_reference_id", "receipt_monday_item_id"):
        if data.get(key) is not None and context.user_data.get(key) is None:
            context.user_data[key] = data[key]


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
    driver = _driver_row_for_telegram_user(query.from_user.id)
    if not driver:
        await msg.reply_text(
            "❌ This Telegram account is not registered as a driver.\n"
            "Ask an admin to add your Telegram user ID in the dashboard."
        )
        return ConversationHandler.END
    await _send_driver_pending_receipts_menu(context, msg, driver)
    return ConversationHandler.END


async def handle_driver_receipts_menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Drivers: show every owed receipt with inline upload buttons. Commands: /receipts, /receipt, /recipts."""
    user = update.effective_user
    if not user or not update.message:
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
    ref = query.data.partition("receipt_for_")[2].strip()
    lead = db.get_lead_by_reference_id(ref)
    if not lead:
        await query.message.reply_text(f"❌ Reference ID `{ref}` not found.")
        return ConversationHandler.END
    driver = _driver_row_for_telegram_user(query.from_user.id)
    if not driver or not _driver_accepted_this_lead(driver["id"], lead["id"]):
        await query.message.reply_text(
            "❌ You can only upload receipts for leads you accepted."
        )
        return ConversationHandler.END
    context.user_data["receipt_lead_id"] = lead["id"]
    context.user_data["receipt_reference_id"] = ref
    context.user_data["receipt_monday_item_id"] = lead.get("monday_item_id")
    delivery_safe = _sanitize_phones_for_send(lead.get("delivery_details") or "")
    msg = (
        f"📋 **Reference ID:** `{ref}`\n\n"
        f"📍 Delivery: {delivery_safe or 'N/A'}\n"
        f"🚗 Vehicle: {lead.get('vehicle_details', 'N/A')[:300]}\n\n"
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

    await query.message.reply_text(
        "📋 **Driver Receipt Submission**\n\n"
        "Please enter the Reference ID for the lead you want to submit a receipt for.",
        parse_mode="Markdown",
    )

    return STATE_WAITING_REFERENCE_ID


async def handle_reference_id_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle reference ID input."""
    user_id = update.effective_user.id
    msg = update.effective_message
    if not msg or not (getattr(msg, "text", None) or "").strip():
        if msg:
            await msg.reply_text("Please send the reference ID as text, or type /cancel.")
        return STATE_WAITING_REFERENCE_ID
    reference_id = msg.text.strip().upper()
    
    # Get lead by reference ID
    lead = db.get_lead_by_reference_id(reference_id)
    
    if not lead:
        await msg.reply_text(
            "❌ Reference ID not found. Please check and try again.\n"
            "Or type /cancel to cancel."
        )
        return STATE_WAITING_REFERENCE_ID

    drv = _driver_row_for_telegram_user(user_id)
    if not drv or not _driver_accepted_this_lead(drv["id"], lead["id"]):
        await msg.reply_text(
            "❌ You can only upload receipts for leads you accepted."
        )
        db.clear_user_state(user_id)
        return ConversationHandler.END

    context.user_data['receipt_lead_id'] = lead['id']
    context.user_data['receipt_reference_id'] = reference_id
    context.user_data['receipt_monday_item_id'] = lead.get('monday_item_id')
    db.set_user_state(user_id, "waiting_receipt_confirm", context.user_data)

    delivery_safe = _sanitize_phones_for_send(lead.get('delivery_details') or '')
    confirmation_message = (
        f"✅ **Lead Found**\n\n"
        f"📍 Delivery Address: {delivery_safe or 'N/A'}\n"
        f"📞 Phone (one-time link): {lead.get('encrypted_link', 'N/A')}\n"
        f"📋 Reference ID: `{reference_id}`\n\n"
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
    
    # Set state to waiting for image
    user_id = query.from_user.id
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
) -> None:
    """Plain receipt summary + same image as caption (no SUPERVISORY prefix); fallback text + forward."""
    gn = (group.get("group_name") or "—") if group else "—"
    client_name = _client_display_name_from_lead(lead)
    ref_plain = str(reference_id or "N/A")
    issuer_line = _lead_issuer_display_from_lead(lead)
    caption = (
        "🧾 New receipt sent\n"
        f"Reference: {ref_plain}\n"
        f"Group: {gn}\n"
        f"Driver(s): {driver_display_name}\n"
        f"Client name: {client_name}\n"
        f"Lead issued by: {issuer_line}"
    )

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

    dr_check = _driver_row_for_telegram_user(user_id)
    if not dr_check or not _driver_accepted_this_lead(dr_check["id"], lead_id):
        await update.message.reply_text("❌ You can only upload receipts for leads you accepted.")
        db.clear_user_state(user_id)
        return ConversationHandler.END

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
    if not (stored_url or "").strip():
        logger.error("Receipt upload: no durable URL (lead_id=%s)", lead_id)
        await update.message.reply_text(
            "❌ Could not save the receipt file URL. Please try again or contact support."
        )
        return STATE_WAITING_RECEIPT_IMAGE

    # Update lead with receipt URL (prefer durable Supabase Storage public URL)
    success = db.update_lead_receipt(lead_id, stored_url)
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
        dq = html.escape(driver_motivation.get_random_driver_quote(), quote=False)
        driver_confirm_html = (
            "✅ <b>Receipt submitted successfully</b>\n\n"
            f"Reference ID: <code>{ref_show}</code>\n"
            "Your receipt is on file.\n\n"
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
        try:
            await _notify_supervisory_receipt_submission(
                context,
                update,
                lead,
                str(reference_id or ""),
                receipt_file_id,
                group,
                driver_name,
            )
        except Exception as e:
            logger.error("Supervisory receipt notification failed: %s", e, exc_info=True)

        if was_suspended and dr_check:
            pending_after = db.get_driver_pending_receipts(dr_check["id"])
            if len(pending_after) < SUSPENSION_THRESHOLD:
                await update.message.reply_text(
                    "✅ **Suspension lifted!**\n\n"
                    "You have cleared enough receipts. You can now receive new leads again.",
                    parse_mode="Markdown",
                    reply_markup=_driver_keyboard_lead_and_receipt(),
                )
                dn_esc = _telegram_md1_escape(driver_name)
                try:
                    _lift = _prefix_supervisory_message(
                        f"✅ **Suspension removed**\n\n"
                        f"Driver: **{dn_esc}**\n"
                        f"Remaining receipts: {len(pending_after)}"
                    )
                    for sup_id in _global_supervisory_chat_ids():
                        try:
                            await context.bot.send_message(
                                chat_id=sup_id,
                                text=_lift,
                                parse_mode="Markdown",
                            )
                        except BadRequest:
                            await context.bot.send_message(
                                chat_id=sup_id,
                                text=f"✅ Suspension removed\n\nDriver: {driver_name}\nRemaining receipts: {len(pending_after)}",
                            )
                except Exception as e:
                    logger.warning("Could not send suspension-lifted alert to supervisory: %s", e)
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
    ref = lead.get("reference_id") or "N/A"
    vehicle = (lead.get("vehicle_details") or "")[:200]
    delivery = _sanitize_phones_for_send(lead.get("delivery_details") or "") or "N/A"
    extra = _sanitize_phones_for_send(lead.get("extra_info") or "") or "—"
    note = _lead_issuer_note(lead)
    lines = [
        "🔄 RENEWAL DUE",
        f"📋 Ref ID: {ref}",
        "",
        f"🚗 Vehicle: {vehicle}",
        f"📍 Delivery: {delivery}",
        f"📝 Extra info: {extra}",
    ]
    if note:
        lines.append(f"📝 Issuers note: {_sanitize_phones_for_send(note)}")
    lines.extend([
        "",
        "Tap Accept to keep this renewal.",
        "Tap Reassign to pass it to another team.",
    ])
    return "\n".join(lines)


def _build_renewal_driver_message(renewal: dict) -> str:
    """Build the driver-facing renewal notice (plain text)."""
    lead = renewal.get("lead") or {}
    ref = lead.get("reference_id") or "N/A"
    delivery = _delivery_block_plain(lead)
    extra = _sanitize_phones_for_send(lead.get("extra_info") or "") or "—"
    link = (lead.get("encrypted_link") or "").strip() or "N/A"
    price = (lead.get("price") or "").strip() or "N/A"
    spec_d = _lead_driver_note(lead)
    lines = [
        "🔄 RENEWAL DELIVERY AVAILABLE",
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


async def _escalate_renewal_group(context: ContextTypes.DEFAULT_TYPE, renewal_id: str) -> None:
    """Timer callback: original group didn't accept within the escalation window — broadcast to all."""
    renewal = db.get_renewal_by_id(renewal_id)
    if not renewal:
        return
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


async def _escalate_renewal_driver(context: ContextTypes.DEFAULT_TYPE, renewal_id: str) -> None:
    """Timer callback: original driver didn't accept — send to all drivers in the accepted group."""
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
    drivers = db.get_active_drivers_for_group(group_id) if group_id else []
    suspended = _get_suspended_driver_ids()
    original_did = renewal.get("original_driver_id")
    refreshed = db.get_renewal_by_id(renewal_id) or renewal
    for d in (drivers or []):
        if str(d.get("id")) == str(original_did):
            continue
        if str(d.get("id")) in suspended:
            continue
        await _send_renewal_to_driver(context, refreshed, d)


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
    group = db.get_group_by_id(group_id)
    if not renewal or not group:
        try:
            await query.message.edit_text("❌ Renewal not found or expired.")
        except Exception:
            pass
        return

    accepted = db.accept_renewal_group(renewal_id, group_id)
    if not accepted:
        try:
            await query.message.edit_text("❌ This renewal was already accepted by another team.")
        except Exception:
            pass
        return

    ref = (renewal.get("lead") or {}).get("reference_id", "N/A")
    gname = group.get("group_name", "Group")
    try:
        await query.message.edit_text(
            f"✅ **Renewal accepted by {gname}**\n\n"
            f"Reference ID: `{ref}`\n\n"
            "Now sending to driver…",
            parse_mode="Markdown",
        )
    except Exception:
        pass

    refreshed = db.get_renewal_by_id(renewal_id) or renewal
    original_did = renewal.get("original_driver_id")
    original_driver = None
    if original_did:
        all_drivers = _get_all_drivers_cached()
        original_driver = next((d for d in all_drivers if str(d.get("id")) == str(original_did)), None)

    # Phase 2: send to original driver first
    sent_to_driver = False
    if original_driver and record_is_active(original_driver):
        db.update_renewal(renewal_id, {
            "driver_status": "sent",
            "driver_sent_at": datetime.utcnow().isoformat(),
        })
        sent_to_driver = await _send_renewal_to_driver(context, refreshed, original_driver)

    if sent_to_driver:
        esc_seconds = Config.RENEWAL_ESCALATION_MINUTES * 60
        if context.application.job_queue:
            async def _driver_esc_job(ctx, _rid=renewal_id):
                await _escalate_renewal_driver(ctx, _rid)
            context.application.job_queue.run_once(
                _driver_esc_job,
                when=esc_seconds,
                name=f"renewal_driver_esc_{renewal_id}",
            )
    else:
        # No original driver available — immediately escalate to all drivers in group
        await _escalate_renewal_driver(context, renewal_id)


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

    accepted = db.accept_renewal_driver(renewal_id, driver_id)
    if not accepted:
        try:
            await query.message.edit_text("❌ This renewal delivery was already accepted by another driver.")
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

    # Send the full accepted lead details to the driver
    lead_full = db.get_lead_by_id(renewal.get("lead_id")) or lead
    confirmation = _build_driver_lead_accepted_message_html(lead_full)
    receipt_kb = _driver_add_lead_keyboard_only()
    try:
        await query.message.reply_text(
            confirmation,
            reply_markup=receipt_kb,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except BadRequest:
        plain = re.sub(r"<[^>]+>", "", confirmation)
        try:
            await query.message.reply_text(plain, reply_markup=receipt_kb)
        except Exception as e:
            logger.warning("Could not send renewal confirmation to driver: %s", e)
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
        _long_uuid(short_d)  # validate second token (driver id in button payload)
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
            f"🔄 **Reassigned** — this renewal delivery (`{ref}`) has been sent to other drivers.",
            parse_mode="Markdown",
        )
    except Exception:
        pass
    await _escalate_renewal_driver(context, renewal_id)


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
    application = Application.builder().token(Config.TELEGRAM_BOT_TOKEN).build()

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
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_phase1),
                MessageHandler(filters.PHOTO, handle_phase1_photo),
                MessageHandler(filters.Document.ALL, handle_phase1_document),
            ],
            STATE_AI_REVIEW: [
                CallbackQueryHandler(
                    handle_phase1_ai_review_callback,
                    pattern=r"^(ph1_accept|ph1_edit|ph1_back|ph1_pick_group|ph1_pick_driver|ph1_pick_source|selgrp_|seldrv_|selsrc_|ph1_sel_back|driver_suspended_|edit_cancel|ph1edit_)"
                ),
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
            STATE_ADD_FILES: [
                CallbackQueryHandler(handle_add_files_callback, pattern="^(add_files_yes|add_files_no)$"),
                MessageHandler(
                    (filters.TEXT & ~filters.COMMAND) | filters.PHOTO | filters.Document.ALL,
                    handle_add_files_stray_message,
                ),
            ],
            STATE_WAITING_FILE: [
                MessageHandler(filters.PHOTO | filters.Document.ALL, handle_file_upload),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_waiting_file_text),
                CallbackQueryHandler(handle_another_file_callback, pattern="^(another_file_yes|another_file_no)$"),
            ],
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
            CommandHandler("cancel", cancel),
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
            CommandHandler("cancel", cancel),
            CommandHandler("start", start),
            CommandHandler(["receipt", "receipts", "recipts"], handle_driver_receipts_menu_command),
            CallbackQueryHandler(handle_driver_add_receipt_callback, pattern="^driver_add_receipt$"),
        ],
    )

    application.add_handler(receipt_handler)
    application.add_handler(conv_handler)
    
    # Add accept/decline handlers for driver assignments
    application.add_handler(CallbackQueryHandler(handle_accept_lead, pattern="^accept_lead_"))
    application.add_handler(CallbackQueryHandler(handle_decline_lead, pattern="^decline_lead_"))
    
    # Add accept/decline handlers for group broadcast offers
    application.add_handler(CallbackQueryHandler(handle_accept_group_offer, pattern="^ag_"))
    application.add_handler(CallbackQueryHandler(handle_different_team_offer, pattern="^dt_"))
    application.add_handler(CallbackQueryHandler(handle_decline_group_offer, pattern="^dg_"))

    # Renewal accept / reassign handlers
    application.add_handler(CallbackQueryHandler(handle_renewal_group_accept, pattern="^rga_"))
    application.add_handler(CallbackQueryHandler(handle_renewal_group_reassign, pattern="^rgr_"))
    application.add_handler(CallbackQueryHandler(handle_renewal_driver_accept, pattern="^rda_"))
    application.add_handler(CallbackQueryHandler(handle_renewal_driver_reassign, pattern="^rdr_"))
    
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
        try:
            due = await asyncio.to_thread(db.get_due_renewals)
            for renewal in due:
                renewal_id = renewal.get("id")
                if not renewal_id:
                    continue
                original_gid = renewal.get("original_group_id")
                original_group = db.get_group_by_id(original_gid) if original_gid else None
                db.update_renewal(renewal_id, {
                    "status": "group_phase",
                    "group_status": "sent",
                    "group_sent_at": datetime.utcnow().isoformat(),
                })
                sent = False
                if original_group and record_is_active(original_group):
                    sent = await _send_renewal_to_group(context, renewal, original_group)

                if sent:
                    esc_seconds = Config.RENEWAL_ESCALATION_MINUTES * 60
                    if context.application.job_queue:
                        async def _group_esc_job(ctx, _rid=renewal_id):
                            await _escalate_renewal_group(ctx, _rid)
                        context.application.job_queue.run_once(
                            _group_esc_job,
                            when=esc_seconds,
                            name=f"renewal_group_esc_{renewal_id}",
                        )
                    lead = renewal.get("lead") or {}
                    ref = lead.get("reference_id", "?")
                    logger.info(
                        "Renewal %s (ref %s) sent to original group, escalation in %d min",
                        renewal_id, ref, Config.RENEWAL_ESCALATION_MINUTES,
                    )
                else:
                    await _escalate_renewal_group(context, renewal_id)
        except Exception as e:
            logger.error("Renewal checker job failed: %s", e)

    if application.job_queue:
        application.job_queue.run_repeating(check_renewals, interval=300, first=180)
        logger.info("Renewal checker job scheduled (every 5 min, first in 180s)")

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

    if application.job_queue:
        eastern = pytz.timezone("America/New_York")
        application.job_queue.run_daily(send_morning_motivation, time=dt_time(hour=8, minute=0, tzinfo=eastern))
        application.job_queue.run_daily(send_evening_motivation, time=dt_time(hour=18, minute=0, tzinfo=eastern))
        logger.info("Daily motivation jobs scheduled (8 AM ET, 6 PM ET)")

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
