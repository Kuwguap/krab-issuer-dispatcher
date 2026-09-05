"""
Krab Interviewer — Telegram bot for driver interviews, appointments, announcements, and hire onboarding.
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import re
import sys
import uuid as _uuid_mod
import calendar as _calendar_mod
from datetime import date as _date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import pytz
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from config import Config
from utils import ai_vision
from utils import ai_tracking
from utils.ai_vision import INTERVIEW_FIELD_KEYS, AIVisionQuotaError
from utils.ai_tracking import AITrackingQuotaError
from utils.database import Database
from utils import recipients_db
from utils import shipments_db
from utils import resend_client
from utils.time_utils import format_dt_display, parse_user_datetime

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

db: Optional[Database] = None

# Conversation states
STATE_INTERVIEW_INPUT = 1
STATE_INT_EDIT_FIELD = 2
STATE_INT_SCHEDULE_APPT = 3
STATE_INT_UPLOAD_LICENSE = 4
STATE_ANNOUNCE_WAIT = 5
STATE_ANNOUNCE_SCHEDULE_TIME = 6
STATE_ANNOUNCE_SCHEDULE_CONTENT = 7
STATE_AWAIT_TRACKING = 8
STATE_SET_TRAINING_VIDEO = 9
STATE_AWAIT_SUPERVISOR_EMAIL = 10
STATE_TRAINING_MENU = 11
STATE_DRV_EDIT_VALUE = 12

INTERVIEW_QUESTIONNAIRE_PROMPT = (
    "🚗 DRIVER INTERVIEW 📞CALL FORM📋\n\n"
    "⚡ AI Smart Data Parse Enabled\n"
    "📸 You may upload screenshots, photos, or documents\n\n"
    " 1. ⏳ Work Commitment\n"
    " 2. 📱 Phone Number\n"
    " 3. 📧 Email Address\n"
    " 4. 🧑 Full Name\n"
    " 5. 🏠 Mailing Address\n"
    " 6. 🪪 Driver's License (send to text/email)\n"
    " 7. 💬 Telegram Username (download settings @username)\n"
    " 8. 🚨 Emergency Contact\n"
    " 9. 👥 Referral (if any)\n"
    "10. 💰 Payment Method (Zelle, Cashapp, Venmo, PayPal)\n"
    "11. 💳 Payment ID ($Cashtag, @Venmo, Zelle phone/email)\n"
    "12. ⚒️ Profession skill\n"
    "13. 💬 Telegram ID\n\n"
    "✅ Please double-check all information before submitting"
)

FIELD_LABELS = {
    "full_name": "🧑 Full Name",
    "work_commitment": "⏳ Work Commitment",
    "phone_number": "📱 Phone Number",
    "email": "📧 Email Address",
    "mailing_address": "🏠 Mailing Address",
    "drivers_license_id": "🪪 Driver's License",
    "telegram_username": "💬 Telegram Username",
    "emergency_contact": "🚨 Emergency Contact",
    "referral": "👥 Referral",
    "payment_method": "💰 Payment Method",
    "payment_id": "💳 Payment ID",
    "profession_skill": "⚒️ Profession skill",
    "telegram_id": "💬 Telegram ID",
    "first_name": "👤 First name (hire)",
}

EDITABLE_KEYS = INTERVIEW_FIELD_KEYS + ["first_name"]


# --- UUID short helpers ---

def _short_uuid(u: str) -> str:
    return base64.urlsafe_b64encode(_uuid_mod.UUID(u).bytes).rstrip(b"=").decode()


def _long_uuid(s: str) -> str:
    padded = s + "=="
    return str(_uuid_mod.UUID(bytes=base64.urlsafe_b64decode(padded)))


def _parse_chat_id(raw) -> Optional[int]:
    if raw is None:
        return None
    if isinstance(raw, int):
        return raw
    s = str(raw).strip().lstrip("=").strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _norm_chat_id(cid) -> Optional[int]:
    if cid is None:
        return None
    if isinstance(cid, int):
        return cid
    try:
        return int(str(cid).strip().split(".", 1)[0])
    except (ValueError, TypeError):
        return None


def _raw_supervisory_tokens(*sources: object) -> List[str]:
    out: List[str] = []
    for raw in sources:
        if raw is None:
            continue
        for part in str(raw).split(","):
            t = part.strip()
            if t:
                out.append(t)
    return out


def _global_supervisory_chat_ids() -> List[int]:
    seen: set = set()
    out: List[int] = []
    for tok in _raw_supervisory_tokens(Config.SUPERVISORY_TELEGRAM_ID):
        cid = _parse_chat_id(tok)
        if cid is None:
            continue
        if cid in seen:
            continue
        seen.add(cid)
        out.append(cid)
    return out


def _user_is_global_supervisor(user_id) -> bool:
    target = _norm_chat_id(user_id)
    if target is None:
        return False
    for cid in _global_supervisory_chat_ids():
        if _norm_chat_id(cid) == target:
            return True
    return False


def _paper_girl_user_ids() -> List[int]:
    """Personal Telegram user IDs for paper girls (comma-separated PAPER_GIRL_TELEGRAM_ID)."""
    seen: set = set()
    out: List[int] = []
    for tok in _raw_supervisory_tokens(Config.PAPER_GIRL_TELEGRAM_ID):
        cid = _parse_chat_id(tok)
        if cid is None or cid in seen:
            continue
        seen.add(cid)
        out.append(cid)
    return out


def _paper_girl_notify_chat_ids() -> List[int]:
    """Paper-girl DMs plus notify groups (PAPER_GIRL_NOTIFY_CHAT_IDS)."""
    seen: set = set()
    out: List[int] = []
    for cid in _paper_girl_user_ids():
        if cid not in seen:
            seen.add(cid)
            out.append(cid)
    for tok in _raw_supervisory_tokens(Config.PAPER_GIRL_NOTIFY_CHAT_IDS):
        cid = _parse_chat_id(tok)
        if cid is None or cid in seen:
            continue
        seen.add(cid)
        out.append(cid)
    return out


def _paper_girl_usernames_tag() -> str:
    handles: List[str] = []
    for part in _raw_supervisory_tokens(Config.PAPER_GIRL_USERNAME):
        h = part.strip().lstrip("@")
        if h:
            handles.append(f"@{h}")
    if not handles:
        return ""
    return " ".join(handles) + " "


def _paper_girl_follow_up_tag() -> str:
    handles: List[str] = []
    for part in _raw_supervisory_tokens(Config.PAPER_GIRL_USERNAME):
        h = part.strip().lstrip("@")
        if h:
            handles.append(f"@{h}")
    return " & ".join(handles)


def _user_is_paper_girl(user_id) -> bool:
    target = _norm_chat_id(user_id)
    if target is None:
        return False
    for cid in _paper_girl_user_ids():
        if _norm_chat_id(cid) == target:
            return True
    return False


def _user_can_manage_shipments(user_id) -> bool:
    return _user_is_global_supervisor(user_id) or _user_is_paper_girl(user_id)


# Telegram membership statuses that mean "currently on the team". "left" and
# "kicked" deliberately absent: someone removed from the channel is removed.
_TEAM_MEMBER_STATUSES = {"creator", "owner", "administrator", "member"}


def _team_membership_chat_ids() -> List[int]:
    """Chats whose current members count as team for the purpose of hiring."""
    out: List[int] = []
    seen: set = set()
    cid = _parse_chat_id(Config.DRIVER_CHANNEL_ID)
    if cid is not None:
        seen.add(cid)
        out.append(cid)
    for cid in _paper_girl_group_notify_ids():
        if cid is not None and cid not in seen:
            seen.add(cid)
            out.append(cid)
    return out


def _user_is_hired_driver(user_id) -> bool:
    """The Issuer drivers table is this bot's roster of people it has hired."""
    if not db:
        return False
    try:
        return bool(db.get_driver_by_telegram_id(str(user_id)))
    except Exception as e:
        logger.warning("_user_is_hired_driver lookup failed: %s", e)
        return False


async def _user_can_hire(context: ContextTypes.DEFAULT_TYPE, user_id) -> bool:
    """Everyone on the team may hire: supervisors, paper girls, drivers we have
    already hired, and current members of the drivers channel or the notify
    group. Deliberately NOT everyone alive — see this module's note on what
    a hire sets in motion.

    The cheap local checks come first so the common case costs no API call.
    """
    if _user_is_global_supervisor(user_id) or _user_is_paper_girl(user_id):
        return True
    if _user_is_hired_driver(user_id):
        return True
    uid = _parse_chat_id(user_id)
    if uid is None:
        return False
    for cid in _team_membership_chat_ids():
        try:
            member = await context.bot.get_chat_member(chat_id=cid, user_id=uid)
        except Exception as e:
            # Not a member, or the bot cannot see that chat. Neither is proof of
            # membership, so both simply fail to grant it.
            logger.debug("get_chat_member(%s, %s): %s", cid, uid, e)
            continue
        if str(getattr(member, "status", "")).lower() in _TEAM_MEMBER_STATUSES:
            return True
    return False


def _is_own_application(interview: dict, user_id) -> bool:
    """True when this person is the applicant on, or the submitter of, the row."""
    target = _norm_chat_id(user_id)
    if target is None:
        return False
    for key in ("telegram_id", "created_by_telegram_id"):
        if _norm_chat_id((interview or {}).get(key)) == target:
            return True
    return False


def _paper_girl_group_notify_ids() -> List[int]:
    """Group/supergroup chats from PAPER_GIRL_NOTIFY_CHAT_IDS (not personal DMs)."""
    seen: set = set()
    out: List[int] = []
    user_ids = {_norm_chat_id(x) for x in _paper_girl_user_ids()}
    for tok in _raw_supervisory_tokens(Config.PAPER_GIRL_NOTIFY_CHAT_IDS):
        cid = _parse_chat_id(tok)
        if cid is None or cid in seen:
            continue
        if _norm_chat_id(cid) in user_ids:
            continue
        seen.add(cid)
        out.append(cid)
    return out


def _telegram_user_label(user) -> str:
    if not user:
        return "Paper Girl"
    un = (user.username or "").strip()
    if un:
        return f"@{un}"
    name = " ".join(
        p for p in [(user.first_name or "").strip(), (user.last_name or "").strip()] if p
    ).strip()
    return name or str(user.id)


def _shipment_acceptor_label(shipment: dict) -> str:
    name = (shipment.get("accepted_by_name") or "").strip()
    if name:
        return name
    tid = (shipment.get("accepted_by_telegram_id") or "").strip()
    return f"ID {tid}" if tid else "—"


def _parse_city_zip_from_address(address: str) -> tuple[str, str]:
    """Best-effort city + ZIP from a US mailing address string."""
    raw = (address or "").strip()
    if not raw:
        return "", ""
    zip_match = re.search(r"\b(\d{5})(?:-\d{4})?\b", raw)
    zip_code = zip_match.group(1) if zip_match else ""
    city = ""
    if zip_match:
        before = raw[: zip_match.start()].strip().rstrip(",")
        parts = [p.strip() for p in before.split(",") if p.strip()]
        if len(parts) >= 2:
            maybe_state = parts[-1]
            if re.fullmatch(r"[A-Za-z]{2}", maybe_state) and len(parts) >= 2:
                city = parts[-2]
            else:
                city = parts[-1]
        elif parts:
            city = parts[-1]
    return city, zip_code


_EMPTY_INLINE_KB = InlineKeyboardMarkup([])


def _shipping_address_line(name: str, address: str) -> str:
    """Drop a leading driver name duplicate from the mailing address."""
    raw = (address or "").strip()
    if not raw:
        return "—"
    nm = (name or "").strip()
    if nm and raw.lower().startswith(nm.lower()):
        rest = raw[len(nm) :].strip().lstrip(",.- ")
        if rest:
            return rest
    return raw


_PAPER_SHIP_FOOTER = (
    "⚡️🏷📬 Priority Order — Ship ASAP, preferably first thing in the morning.\n"
    "⏳ All paper orders should be shipped within 24 hours.\n\n"
    "🏁Automated🏎Automotive💨"
)


def _format_paper_ship_message(
    *,
    qty: int,
    name: str,
    address: str,
    phone: str,
    ship_intro: str,
    receipt_line: str,
    include_recipient: bool = True,
) -> str:
    # Header and quantity read as one line of fact; the person and the
    # instructions are held apart from it, so a paper girl scanning the channel
    # finds the address without reading a paragraph.
    parts = [
        "➕🚗 New Driver Hired ✅🎉",
        ship_intro,
        "",
        "",
    ]
    if include_recipient:
        addr = _shipping_address_line(name, address)
        parts.extend([
            f"👤 {name}",
            f"📍 {addr}",
            f"📞 {phone}",
            "",
            "",
        ])
    parts.extend([receipt_line, _PAPER_SHIP_FOOTER])
    return "\n".join(parts)


def _format_paper_girl_ship_request(shipment: dict) -> str:
    qty = int(shipment.get("quantity") or Config.DEFAULT_PAPER_QTY)
    name = (shipment.get("driver_name") or "Driver").strip()
    return _format_paper_ship_message(
        qty=qty,
        name=name,
        address=(shipment.get("driver_address") or "").strip(),
        phone=(shipment.get("driver_phone") or "-").strip(),
        ship_intro=f"📦 Please ship {qty} temp tag papers today to:",
        receipt_line="🧾 Please upload the tracking number shipping receipt once sent.",
    )


def _format_driver_paper_ship_notice(
    interview: dict, *, include_recipient: bool = True
) -> str:
    qty = Config.DEFAULT_PAPER_QTY
    name = _driver_display_name(interview)
    return _format_paper_ship_message(
        qty=qty,
        name=name,
        address=(interview.get("mailing_address") or "").strip(),
        phone=(interview.get("phone_number") or "-").strip(),
        ship_intro=f"📦 {qty} temp tag papers",
        receipt_line="🧾 Tracking number & shipping receipt",
        include_recipient=include_recipient,
    )


def _paper_girl_ship_keyboard(shipment_id: str) -> InlineKeyboardMarkup:
    sid = _short_uuid(shipment_id)
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "👉📥 Upload tracking number",
                callback_data=f"ship_track_{sid}",
            ),
        ],
        [
            InlineKeyboardButton(
                "👉📦 View all shipments",
                callback_data="ship_view_all",
            ),
        ],
    ])


def _build_usps_tracking_url(tracking_number: str) -> str:
    tn = re.sub(r"\D", "", tracking_number or "")
    base = (Config.USPS_TRACK_URL_BASE or "").strip()
    if not base:
        base = "https://tools.usps.com/go/TrackConfirmAction?tLabels="
    return f"{base}{tn}"


def _user_can_work_shipment(user_id, shipment: dict) -> bool:
    """Supervisors and all paper girls can upload tracking for open orders."""
    if _user_is_global_supervisor(user_id):
        return True
    if _user_is_paper_girl(user_id):
        return True
    return False


async def _broadcast_shipment_status(
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    *,
    parse_mode: Optional[str] = "Markdown",
) -> None:
    seen: set = set()
    for cid in _global_supervisory_chat_ids() + _paper_girl_notify_chat_ids():
        if cid in seen:
            continue
        seen.add(cid)
        try:
            await context.bot.send_message(chat_id=cid, text=text, parse_mode=parse_mode)
        except Exception as e:
            logger.warning("shipment broadcast to %s: %s", cid, e)


async def _notify_paper_girl_chats(
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    *,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    parse_mode: Optional[str] = None,
) -> List[str]:
    """Send the same message to every paper-girl DM + notify group. Returns error strings."""
    errors: List[str] = []
    for cid in _paper_girl_notify_chat_ids():
        try:
            kwargs: dict = {"chat_id": cid, "text": text}
            if reply_markup is not None:
                kwargs["reply_markup"] = reply_markup
            if parse_mode:
                kwargs["parse_mode"] = parse_mode
            await context.bot.send_message(**kwargs)
        except Exception as e:
            logger.error("paper girl notify chat %s: %s", cid, e)
            errors.append(f"chat {cid}: {e}")
    return errors


from utils.hire_driver import driver_display_name as _driver_display_name
from utils.hire_driver import hire_driver_records, purge_driver_everywhere


def _format_shipments_list(rows: List[dict]) -> str:
    if not rows:
        return "📦 No shipments yet."

    pending = [
        r for r in rows
        if (r.get("status") or "") in ("awaiting_tracking", "tracking_received", "pending_accept")
    ]
    done = [r for r in rows if (r.get("status") or "") == "driver_notified"]
    cancelled = [r for r in rows if (r.get("status") or "") == "cancelled"]

    def fmt(r: dict) -> str:
        name = (r.get("driver_name") or "?")[:24]
        qty = r.get("quantity", "?")
        tr = (r.get("tracking_number") or "-")[:22]
        city = (r.get("driver_city") or "").strip()
        zip_code = (r.get("driver_zip") or "").strip()
        loc = ", ".join(p for p in [city, zip_code] if p)
        accepter = _shipment_acceptor_label(r)
        st = (r.get("status") or "")
        status_note = ""
        if st in ("pending_accept", "awaiting_tracking"):
            status_note = " | ⏳ awaiting tracking"
        elif accepter and accepter != "—":
            status_note = f" | 🙋 {accepter[:20]}"
        loc_bit = f" | {loc}" if loc else ""
        return f"• {name}{loc_bit} | {qty} papers | {tr}{status_note}"

    parts = ["📦 **Shipments**"]
    if pending:
        parts.append("\n⏳ **Pending tracking**")
        parts.extend(fmt(r) for r in pending)
    if done:
        parts.append("\n✅ **Completed**")
        parts.extend(fmt(r) for r in done)
    if cancelled:
        parts.append("\n🚫 **Cancelled**")
        parts.extend(fmt(r) for r in cancelled)
    if len(parts) == 1:
        return "📦 No shipments yet."
    return "\n".join(parts)


async def _reply_shipments_list(message) -> None:
    rows = shipments_db.list_shipments(50)
    await message.reply_text(_format_shipments_list(rows), parse_mode="Markdown")


async def _safe_answer_callback_query(query) -> None:
    try:
        await query.answer()
    except Exception:
        pass


async def _safe_delete_chat_message(context, chat_id, message_id) -> None:
    if not chat_id or not message_id:
        return
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


def _track_pending_prompt(context, message_id: int) -> None:
    ids = context.user_data.setdefault("pending_prompt_msg_ids", [])
    ids.append(message_id)


async def _clear_pending_prompts(context, chat_id) -> None:
    for mid in context.user_data.pop("pending_prompt_msg_ids", []) or []:
        await _safe_delete_chat_message(context, chat_id, mid)


def _display_val(v: Any) -> str:
    s = (str(v) if v is not None else "").strip()
    return s if s else "-"


def _interview_first_name(row: dict) -> str:
    full = (row.get("full_name") or "").strip()
    if full:
        first, _ = ai_vision.split_full_name(full)
        if first:
            return first
    fn = (row.get("first_name") or "").strip()
    if fn:
        return fn
    un = (row.get("telegram_username") or "").strip().lstrip("@")
    return un or "Driver"


def _interview_list_keyboard(rows: List[dict]) -> InlineKeyboardMarkup:
    """Two first-name buttons per row — tap to open full application."""
    buttons: List[List[InlineKeyboardButton]] = []
    row_btns: List[InlineKeyboardButton] = []
    for r in rows[:24]:
        sid = _short_uuid(r["id"])
        label = _interview_first_name(r)[:32]
        row_btns.append(InlineKeyboardButton(label, callback_data=f"int_open_{sid}"))
        if len(row_btns) == 2:
            buttons.append(row_btns)
            row_btns = []
    if row_btns:
        buttons.append(row_btns)
    return InlineKeyboardMarkup(buttons)


async def _send_license_attachment(
    message,
    context,
    license_url: str = "",
    *,
    license_bytes: Optional[bytes] = None,
    mime: str = "image/jpeg",
    caption: str = "🪪 Driver's license",
) -> None:
    if license_bytes:
        ext = "jpg"
        if "png" in mime:
            ext = "png"
        elif "webp" in mime:
            ext = "webp"
        bio = io.BytesIO(license_bytes)
        bio.name = f"license.{ext}"
        try:
            await message.reply_photo(photo=InputFile(bio, filename=bio.name), caption=caption)
            return
        except Exception as e:
            logger.warning("reply_photo license bytes failed: %s", e)
        bio.seek(0)
        try:
            await context.bot.send_document(
                chat_id=message.chat_id,
                document=InputFile(bio, filename=bio.name),
                caption=caption,
            )
            return
        except Exception as e:
            logger.warning("send_document license bytes failed: %s", e)

    lic = (license_url or "").strip()
    if not lic or not _license_url_fetchable(lic):
        return
    try:
        await message.reply_photo(photo=lic, caption=caption)
        return
    except Exception as e:
        logger.warning("reply_photo license url failed: %s", e)
    try:
        await context.bot.send_document(chat_id=message.chat_id, document=lic, caption=caption)
        return
    except Exception as e:
        logger.warning("send_document license url failed: %s", e)
    await message.reply_text(f"🪪 License file:\n{lic}")


def _license_url_fetchable(url: str) -> bool:
    """False for local/test hostnames (e.g. FastAPI TestClient ``testserver``)."""
    u = (url or "").strip()
    if not u:
        return False
    try:
        host = (urlparse(u).hostname or "").lower()
    except Exception:
        return True
    if not host:
        return u.startswith("http")
    blocked = {"testserver", "localhost", "127.0.0.1", "0.0.0.0", "::1"}
    return host not in blocked and not host.endswith(".local")


def _public_license_url(interview_id: str, fallback: str = "") -> str:
    api = _record_license_api_url(interview_id)
    if api:
        return api
    fb = (fallback or "").strip()
    return fb if _license_url_fetchable(fb) else ""


def _record_license_api_url(interview_id: str) -> str:
    base = (
        Config.KRAB_PUBLIC_BASE_URL
        or Config.TELEGRAM_LOGIN_WIDGET_BASE_URL
        or ""
    ).rstrip("/")
    path = f"/api/interview/record/{interview_id}/license-file"
    return f"{base}{path}" if base else ""


def _decode_license_payload(payload: dict) -> tuple[Optional[bytes], str]:
    b64 = (payload or {}).get("_license_b64") or ""
    if not b64:
        return None, "image/jpeg"
    mime = (payload or {}).get("_license_mime") or "image/jpeg"
    try:
        return base64.standard_b64decode(b64), mime
    except Exception as e:
        logger.warning("_decode_license_payload: %s", e)
        return None, mime


def _decode_parse_image_payload(payload: dict) -> tuple[Optional[bytes], str]:
    b64 = (payload or {}).get("_parse_image_b64") or ""
    if not b64:
        return None, "image/jpeg"
    mime = (payload or {}).get("_parse_image_mime") or "image/jpeg"
    try:
        return base64.standard_b64decode(b64), mime
    except Exception as e:
        logger.warning("_decode_parse_image_payload: %s", e)
        return None, mime


def _record_parse_image_api_url(interview_id: str) -> str:
    base = (
        Config.KRAB_PUBLIC_BASE_URL
        or Config.TELEGRAM_LOGIN_WIDGET_BASE_URL
        or ""
    ).rstrip("/")
    path = f"/api/interview/record/{interview_id}/parse-image-file"
    return f"{base}{path}" if base else ""


def _http_fetch_license(url: str) -> tuple[Optional[bytes], str]:
    import requests

    try:
        r = requests.get(url, timeout=30)
        if r.status_code == 200 and r.content:
            ct = (r.headers.get("content-type") or "image/jpeg").split(";")[0].strip()
            return r.content, ct or "image/jpeg"
        logger.warning("_http_fetch_license %s: %s %s", url[:80], r.status_code, (r.text or "")[:120])
    except Exception as e:
        logger.warning("_http_fetch_license: %s", e)
    return None, "image/jpeg"


async def _resolve_license_for_interview(interview_id: str) -> tuple[Optional[bytes], str, str]:
    """Return (bytes, mime, url) — bytes preferred for Telegram send."""
    from utils.drafts_db import DraftsDatabase

    interview = db.get_interview_by_id(interview_id)
    if not interview:
        return None, "image/jpeg", ""

    drafts = DraftsDatabase(db.client)
    linked = drafts.find_drafts_by_submitted_interview(interview_id)
    inv_url = (interview.get("drivers_license_file_url") or "").strip()

    for draft in linked:
        raw, mime = _decode_license_payload(draft.get("payload") or {})
        if raw:
            good_url = _public_license_url(
                interview_id,
                inv_url or (draft.get("drivers_license_file_url") or ""),
            )
            return raw, mime, good_url

    urls: List[str] = []
    api_url = _record_license_api_url(interview_id)
    if api_url:
        urls.append(api_url)
    for candidate in [inv_url] + [
        (d.get("drivers_license_file_url") or "").strip() for d in linked
    ]:
        if candidate and candidate not in urls and _license_url_fetchable(candidate):
            urls.append(candidate)

    loop = asyncio.get_running_loop()
    for url in urls:
        raw, mime = await loop.run_in_executor(None, _http_fetch_license, url)
        if raw:
            return raw, mime, url

    return None, "image/jpeg", _public_license_url(interview_id, inv_url)


async def _resolve_parse_image_for_interview(interview_id: str) -> tuple[Optional[bytes], str, str]:
    """Return (bytes, mime, url) for the AI auto-fill screenshot, if any."""
    from utils.drafts_db import DraftsDatabase

    interview = db.get_interview_by_id(interview_id)
    if not interview:
        return None, "image/jpeg", ""

    drafts = DraftsDatabase(db.client)
    linked = drafts.find_drafts_by_submitted_interview(interview_id)
    inv_parse = (interview.get("parse_source_image_url") or "").strip()

    for draft in linked:
        raw, mime = _decode_parse_image_payload(draft.get("payload") or {})
        if raw:
            good_url = inv_parse or _record_parse_image_api_url(interview_id)
            return raw, mime, good_url

    urls: List[str] = []
    api_url = _record_parse_image_api_url(interview_id)
    if api_url:
        urls.append(api_url)
    for candidate in [inv_parse] + [
        ((d.get("payload") or {}).get("_parse_image_url") or "").strip() for d in linked
    ]:
        if candidate and candidate not in urls and _license_url_fetchable(candidate):
            urls.append(candidate)

    loop = asyncio.get_running_loop()
    for url in urls:
        raw, mime = await loop.run_in_executor(None, _http_fetch_license, url)
        if raw:
            return raw, mime, url

    return None, "image/jpeg", inv_parse or api_url


def _images_same(a: Optional[bytes], b: Optional[bytes]) -> bool:
    if not a or not b:
        return False
    return a == b


async def _send_interview_license_bundle(
    message,
    context,
    interview_id: str,
) -> None:
    """Send driver's license photo and AI parse screenshot (when both exist)."""
    lic_bytes, lic_mime, lic_url = await _resolve_license_for_interview(interview_id)
    parse_bytes, parse_mime, parse_url = await _resolve_parse_image_for_interview(interview_id)

    if _images_same(lic_bytes, parse_bytes):
        parse_bytes = None
        parse_url = ""

    sent = False
    if lic_bytes or lic_url:
        await _send_license_attachment(
            message,
            context,
            lic_url,
            license_bytes=lic_bytes,
            mime=lic_mime,
            caption="🪪 Driver's license",
        )
        sent = True
    if parse_bytes or (parse_url and parse_url != lic_url):
        await _send_license_attachment(
            message,
            context,
            parse_url,
            license_bytes=parse_bytes,
            mime=parse_mime,
            caption="📸 AI auto-fill screenshot",
        )
        sent = True
    if not sent:
        await message.reply_text("🪪 No driver license photo on file for this application.")


def _format_interview_understanding(interview: dict) -> str:
    lines = ["📝 Here's how I understood the interview:\n"]
    for i, key in enumerate(INTERVIEW_FIELD_KEYS, 1):
        label = FIELD_LABELS.get(key, key)
        lines.append(f"{i}. {label}: {_display_val(interview.get(key))}")
    fn_full = _display_val(interview.get("full_name"))
    if fn_full != "-":
        first, last = ai_vision.split_full_name(fn_full)
        lines.append(f"\n👤 First: {first or '-'} | Last: {last or '-'}")
    fn = _display_val(interview.get("first_name"))
    if fn != "-" and fn_full == "-":
        lines.append(f"\n👤 First name (hire): {fn}")
    lic = (interview.get("drivers_license_file_url") or "").strip()
    if lic:
        lines.append("\n✅ Driver license on file")
    if (interview.get("parse_source_image_url") or "").strip():
        lines.append("✅ AI auto-fill screenshot on file")
    appt = interview.get("_appointment_display")
    if appt:
        lines.append(f"\n📆 Appointment: {appt}")
    st = (interview.get("status") or "").strip()
    if st == "hired":
        lines.append("\n✅ Hired — added to dispatch & issuer")
    return "\n".join(lines)


def _review_keyboard(interview_id: str) -> InlineKeyboardMarkup:
    sid = _short_uuid(interview_id)
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🪪 Upload license", callback_data=f"int_lic_{sid}"),
            InlineKeyboardButton("📆 Schedule appointment", callback_data=f"int_sched_{sid}"),
        ],
        [
            InlineKeyboardButton("✍️ Edit", callback_data=f"int_edit_{sid}"),
            InlineKeyboardButton("✅ Hire", callback_data=f"int_hire_{sid}"),
        ],
    ])


def _edit_fields_keyboard(interview_id: str) -> InlineKeyboardMarkup:
    sid = _short_uuid(interview_id)
    rows = []
    for key in EDITABLE_KEYS:
        label = FIELD_LABELS.get(key, key)
        short = label.split(" ", 1)[-1][:18]
        rows.append([
            InlineKeyboardButton(short, callback_data=f"int_ef_{key}_{sid}"),
        ])
    rows.append([InlineKeyboardButton("⬅️ Back to review", callback_data=f"int_eback_{sid}")])
    return InlineKeyboardMarkup(rows)


async def _refresh_understanding_card(
    context: ContextTypes.DEFAULT_TYPE, interview: dict,
) -> None:
    chat_id = context.user_data.get("understanding_chat_id")
    mid = context.user_data.get("understanding_message_id")
    if not chat_id or not mid:
        return
    iid = interview.get("id")
    text = _format_interview_understanding(interview)
    kb = _review_keyboard(iid) if (interview.get("status") or "") != "hired" else None
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=mid,
            text=text,
            reply_markup=kb,
        )
    except Exception as e:
        logger.warning("refresh understanding card: %s", e)


def _resolve_interview_id_from_callback(data: str, prefix: str) -> Optional[str]:
    if not data.startswith(prefix):
        return None
    short = data[len(prefix):]
    try:
        return _long_uuid(short)
    except Exception:
        return None


def _interview_id_from_callback(data: str) -> Optional[str]:
    for prefix in (
        "int_lic_", "int_sched_", "int_edit_", "int_hire_", "int_eback_", "int_open_",
    ):
        if data.startswith(prefix):
            return _resolve_interview_id_from_callback(data, prefix)
    m = re.match(r"^int_ef_([a-z_]+)_([A-Za-z0-9_-]+)$", data)
    if m:
        try:
            return _long_uuid(m.group(2))
        except Exception:
            pass
    return None


# --- Channel announce ---

async def _post_to_driver_channel(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    kind: str,
    body: str,
    media_file_id: Optional[str],
) -> bool:
    cid = _parse_chat_id(Config.DRIVER_CHANNEL_ID)
    if not cid:
        logger.warning("DRIVER_CHANNEL_ID not set")
        return False
    try:
        if kind == "photo" and media_file_id:
            await context.bot.send_photo(chat_id=cid, photo=media_file_id, caption=body or None)
        elif kind == "video" and media_file_id:
            await context.bot.send_video(chat_id=cid, video=media_file_id, caption=body or None)
        elif kind == "document" and media_file_id:
            await context.bot.send_document(chat_id=cid, document=media_file_id, caption=body or None)
        else:
            await context.bot.send_message(chat_id=cid, text=body or "(empty)")
        return True
    except Exception as e:
        logger.error("channel post failed: %s", e)
        return False


# --- Driver channel onboarding ---

async def _create_driver_channel_invite(
    context: ContextTypes.DEFAULT_TYPE,
    driver_name: str,
) -> Optional[str]:
    """Create a one-time invite link to the drivers channel. Returns URL or None."""
    cid = _parse_chat_id(Config.DRIVER_CHANNEL_ID)
    if not cid:
        return None
    try:
        link = await context.bot.create_chat_invite_link(
            chat_id=cid,
            name=f"Hire: {driver_name}"[:32] if driver_name else "Hire",
            member_limit=1,
            creates_join_request=False,
        )
        return getattr(link, "invite_link", None)
    except Exception as e:
        logger.warning("create_chat_invite_link failed: %s", e)
        return None


async def _add_driver_to_channel(
    context: ContextTypes.DEFAULT_TYPE,
    driver_telegram_id: str,
) -> bool:
    """Best-effort add to channel. Bots can't add users to channels directly;
    we approve a join request if one is pending. Returns True on success.
    """
    cid = _parse_chat_id(Config.DRIVER_CHANNEL_ID)
    tid = _parse_chat_id(driver_telegram_id)
    if not cid or not tid:
        return False
    try:
        await context.bot.approve_chat_join_request(chat_id=cid, user_id=tid)
        return True
    except Exception as e:
        logger.debug("approve_chat_join_request (probably no pending request): %s", e)
        return False


# --- Paper shipments (hire -> paper girl -> tracking -> driver) ---

_PAPER_GIRL_REMINDER_INTERVAL_SEC = 8 * 60 * 60  # 3 reminders per day


async def _send_one_media(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    *,
    kind: str,
    file_id: str,
    caption: Optional[str] = None,
) -> bool:
    try:
        if kind == "photo":
            await context.bot.send_photo(chat_id=chat_id, photo=file_id, caption=caption)
        elif kind == "document":
            await context.bot.send_document(chat_id=chat_id, document=file_id, caption=caption)
        elif kind == "animation":
            await context.bot.send_animation(chat_id=chat_id, animation=file_id, caption=caption)
        else:
            await context.bot.send_video(chat_id=chat_id, video=file_id, caption=caption)
        return True
    except Exception as e:
        logger.warning("send training media (%s) to %s: %s", kind, chat_id, e)
        return False


async def _send_training_video_to_chat(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    *,
    caption_override: Optional[str] = None,
) -> bool:
    videos = shipments_db.list_training_videos()
    if not videos:
        legacy = shipments_db.get_bot_setting("training_video")
        if legacy and legacy.get("media_file_id"):
            videos = [{
                "media_kind": legacy.get("media_kind") or "video",
                "media_file_id": legacy["media_file_id"],
                "caption": legacy.get("caption"),
            }]
    if not videos:
        return False

    sent_any = False
    for idx, v in enumerate(videos):
        fid = v.get("media_file_id")
        if not fid:
            continue
        kind = (v.get("media_kind") or "video").strip().lower()
        if idx == 0 and caption_override:
            cap: Optional[str] = caption_override
        else:
            cap = (v.get("caption") or "").strip() or None
        if await _send_one_media(context, chat_id, kind=kind, file_id=fid, caption=cap):
            sent_any = True
    return sent_any


async def _create_and_notify_paper_girl(
    context: ContextTypes.DEFAULT_TYPE,
    interview: dict,
    *,
    created_by_telegram_id: str,
) -> tuple[Optional[dict], Optional[str]]:
    """Create shipment and broadcast the full order to all paper girls."""
    fn = _driver_display_name(interview)
    addr = (interview.get("mailing_address") or "").strip()
    phone = (interview.get("phone_number") or "").strip()
    em = (interview.get("email") or "").strip()
    tid = (interview.get("telegram_id") or "").strip()
    iid = interview.get("id")
    city, zip_code = _parse_city_zip_from_address(addr)
    qty = Config.DEFAULT_PAPER_QTY

    shipment = shipments_db.create_shipment(
        interview_id=iid,
        driver_name=fn or "Driver",
        driver_address=addr or None,
        driver_phone=phone or None,
        driver_email=em or None,
        driver_telegram_id=tid or None,
        quantity=qty,
        created_by_telegram_id=created_by_telegram_id,
        status="awaiting_tracking",
        driver_city=city or None,
        driver_zip=zip_code or None,
    )
    if not shipment:
        return None, "Could not create paper_shipments row (run migration_paper_shipments.sql?)"

    if not _paper_girl_notify_chat_ids():
        return shipment, (
            "PAPER_GIRL_TELEGRAM_ID / PAPER_GIRL_NOTIFY_CHAT_IDS not set — "
            "shipment saved but nobody was notified."
        )

    ship_text = _format_paper_girl_ship_request(shipment)
    ship_kb = _paper_girl_ship_keyboard(shipment["id"])
    errs = await _notify_paper_girl_chats(
        context,
        ship_text,
        reply_markup=ship_kb,
        parse_mode="Markdown",
    )

    supervisor_text = (
        "📦 **New paper delivery order**\n\n"
        f"👤 Driver: **{fn or 'Driver'}**\n"
        f"🏙 City: **{city or '—'}**\n"
        f"📮 ZIP: **{zip_code or '—'}**\n"
        f"📄 Quantity: **{qty}** papers\n\n"
        "Sent to all paper girls — awaiting tracking."
    )
    for cid in _global_supervisory_chat_ids():
        try:
            await context.bot.send_message(
                chat_id=cid,
                text=supervisor_text,
                parse_mode="Markdown",
            )
        except Exception:
            pass

    if errs:
        return shipment, "Some paper girl notifications failed: " + "; ".join(errs)
    return shipment, None


def _driver_channel_join_keyboard(
    channel_invite: Optional[str],
) -> Optional[InlineKeyboardMarkup]:
    if not channel_invite:
        return None
    channel_btn_label = "🔗 Join @TriStateTags"
    link = (Config.DRIVER_CHANNEL_LINK or "").strip()
    if link and "t.me/" in link:
        handle = link.rstrip("/").split("/")[-1]
        if handle:
            channel_btn_label = f"🔗 Join @{handle.lstrip('@')}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(channel_btn_label, url=channel_invite)],
    ])


def _build_driver_welcome_dm(interview: dict, driver_name: str) -> str:
    welcome_first, _ = ai_vision.split_full_name(driver_name)
    if not welcome_first:
        welcome_first = (interview.get("first_name") or "").strip() or driver_name.split()[0]

    username_display = (interview.get("telegram_username") or "").strip()
    if username_display and not username_display.startswith("@"):
        username_display = "@" + username_display
    welcome_handle = (
        username_display
        if username_display and username_display.startswith("@")
        else f"@{welcome_first}"
    )
    dispatch_bot = Config.KRAB_DISPATCH_BOT_USERNAME.lstrip("@")
    interviewer_bot = Config.KRAB_INTERVIEWER_BOT_USERNAME.lstrip("@")
    pg_follow = _paper_girl_follow_up_tag()
    qty = Config.DEFAULT_PAPER_QTY
    pg_line = (
        f"📦 {qty} papers are now being prepared & shipped by papergirl "
        f"please push & follow up {pg_follow}\n\n"
        if pg_follow
        else f"📦 {qty} papers are now being prepared & shipped by papergirl please push & follow up\n\n"
    )
    return (
        f"🎉 Welcome to the Team, {welcome_handle}! 🚗🔥\n\n"
        "🎉 DRIVER HIRED SUCCESSFULLY ✅\n\n"
        f"Welcome to the Family {welcome_first} !\n\n"
        f"Please start this 2 bots —-> @{interviewer_bot} & @{dispatch_bot}\n"
        f"{pg_line}"
        "Get laserjet printer ready to print & drop off temp tags !\n\n"
        "📬 Tracking number coming shortly !\n"
        "⚡️ Driver is now officially ACTIVE & ready to receive leads!\n"
        "🖨️ Laserjet Printer purchase anywhere or Amazon https://shorturl.at/gvOrb\n\n"
        "You're officially hired and now part of the family 💪\n"
        "Let's get money, move fast, and serve clients together!\n\n"
        "1.\n"
        "1st thing first, all clients belong to our car dealership; clients that we train you to retain all belong to our car dealership. "
        "We are in the business of building clientele, hence every single client cellphone number must be accounted for and recorded. "
        "Therefore we strictly DO NOT ALLOW ❌ any partners to serve clients behind our backs nor withhold any information from our clients.\n\n"
        "2.\n"
        "Here comes the 😊 Good news 🗞️ ! All clients are monthly renewals ! So when you make 1 delivery, expect to be receiving $50/month every month !\n\n"
        "3. More Good News !📰\n"
        "We have a government product, license titles, insurance and registration, it is necessary for everyone's daily life "
        "and sums up to hundreds of millions of dollars every year ! You are now in on the action!\n\n"
        "4.\n"
        "Remember the location of dealership is for cars only. DMV Services department is remote and online work. "
        "Therefore no trips to the dealership are necessary everything is remote, welcome to your future ✨.\n\n"
        "5. Push dispatchers @uDominica\n"
        "@highkage0_0\n"
        "@sensei_vi daily for new deliveries\n\n"
        "6. Sitting duck. 🦆 instead of sitting waiting for deliveries:\n"
        "Go the extra mile\n"
        "Post ads find clients close some deals, everyone needs temp tags 🏷️ go out there and bring some clients !\n\n"
        f"📲 Start receiving leads now at @{dispatch_bot}\n\n"
        "🔔 Keep notifications ON so you never miss a lead.\n\n"
        "🚀 Welcome aboard — let's get to work!"
    )


def _build_driver_onboarding_steps_message() -> str:
    dispatch_bot = Config.KRAB_DISPATCH_BOT_USERNAME.lstrip("@")
    return (
        "🚗 Welcome to the Team – Driver Onboarding Steps 🚗\n\n"
        "👋 Step 1: Introduce Yourself\n"
        "Please introduce yourself to the entire team, including dispatchers, supervisors, and fellow drivers. "
        "Take a few minutes to get acquainted with everyone and build good communication from day one.\n\n"
        "📲 Step 2: Turn On Notifications\n"
        f"Enable notifications for @{dispatch_bot} so you receive delivery assignments immediately.\n\n"
        "✅ Step 3: Accept Deliveries\n"
        "When a new delivery appears, click Accept and wait for the delivery email to arrive.\n\n"
        "🖨️ Step 4: Print the Temporary Plate\n"
        "A temporary plate will be emailed to you. Print it using your LaserJet printer before contacting the client.\n\n"
        "📞 Step 5: Call the Client\n"
        "Contact the client and confirm:\n"
        "• Delivery date and time\n"
        "• Pickup/drop-off location\n"
        "• Client's full name\n"
        "• Address\n"
        "• Phone number\n"
        "• Delivery price\n\n"
        "🔍 Step 6: Verify All Information\n"
        "Carefully review the temporary tag and ensure all details are correct:\n"
        "• Client name\n"
        "• Address\n"
        "• Vehicle color\n"
        "• VIN number\n"
        "• Any other vehicle information\n\n"
        "⚠️ Double-check everything to prevent mistakes, delays, and unnecessary return trips.\n\n"
        "💳 Step 7: Payment Instructions\n"
        "Clients should pay the dealership directly through electronic payment whenever possible.\n\n"
        "💵 If the Client Pays Cash\n"
        "If the client insists on paying cash, you may accept it. Once received, forward the payment to us "
        "electronically using your approved payment method.\n\n"
        "💰 Driver Compensation\n"
        "We will send you $50+ per delivery plus any applicable toll reimbursements.\n\n"
        "📱 If you have any questions or encounter any issues during a delivery, contact dispatch immediately.\n\n"
        "Thank you and drive safely! 🚘"
    )


_DRIVER_TRAINING_INTRO = (
    "🎬 Training time! 🫪Watch these quick training videos to learn how temp tag deliveries work from start to finish.\n"
    "Replay & Access them anytime by typing /training."
)


async def _send_driver_onboarding_messages(
    context: ContextTypes.DEFAULT_TYPE,
    interview: dict,
    *,
    channel_invite: Optional[str] = None,
) -> List[str]:
    """All Telegram messages a newly hired driver receives."""
    warnings: List[str] = []
    tid = (interview.get("telegram_id") or "").strip()
    try:
        driver_chat_id = int(tid)
    except Exception:
        return ["Driver telegram_id missing or invalid"]

    driver_name = _driver_display_name(interview)
    join_kb = _driver_channel_join_keyboard(channel_invite)

    try:
        await context.bot.send_message(
            chat_id=driver_chat_id,
            text=_build_driver_welcome_dm(interview, driver_name),
            reply_markup=join_kb,
        )
    except Exception as e:
        warnings.append(f"Welcome DM: {e}")

    try:
        await context.bot.send_message(
            chat_id=driver_chat_id,
            text=_build_driver_onboarding_steps_message(),
        )
    except Exception as e:
        warnings.append(f"Onboarding steps: {e}")

    try:
        await context.bot.send_message(
            chat_id=driver_chat_id,
            text=_format_driver_paper_ship_notice(interview),
        )
    except Exception as e:
        warnings.append(f"Paper ship notice: {e}")

    try:
        await context.bot.send_message(
            chat_id=driver_chat_id,
            text=_DRIVER_TRAINING_INTRO,
        )
    except Exception as e:
        warnings.append(f"Training intro: {e}")

    if not await _send_training_video_to_chat(context, driver_chat_id):
        warnings.append("No training videos configured (supervisor: run /training).")

    return warnings


def _resolve_hired_driver_interview(telegram_id: str) -> Optional[dict]:
    """Latest hired interview for this Telegram user, if any."""
    if not db:
        return None
    tid = str(telegram_id or "").strip()
    if not tid:
        return None
    interview = db.get_hired_interview_for_telegram_id(tid)
    if interview:
        return interview
    driver = db.get_driver_by_telegram_id(tid)
    if not driver:
        return None
    latest = db.get_latest_interview_for_telegram_id(tid)
    if latest and (latest.get("status") or "") == "hired":
        return latest
    return {
        "telegram_id": tid,
        "full_name": driver.get("driver_name"),
        "first_name": driver.get("driver_name"),
        "phone_number": driver.get("phone_number"),
        "mailing_address": "",
        "status": "hired",
    }


async def _deliver_hired_driver_onboarding(
    context: ContextTypes.DEFAULT_TYPE,
    interview: dict,
    *,
    source: str = "start",
) -> List[str]:
    """Send full hire onboarding DMs (welcome, steps, paper notice, training)."""
    driver_name = _driver_display_name(interview)
    channel_invite = await _create_driver_channel_invite(context, driver_name)
    warnings = await _send_driver_onboarding_messages(
        context,
        interview,
        channel_invite=channel_invite,
    )
    if warnings:
        logger.warning(
            "Hired driver onboarding (%s, %s): %s",
            source,
            interview.get("telegram_id"),
            "; ".join(warnings),
        )
    return warnings


def _build_hire_announcement_message(interview: dict, driver_name: str) -> str:
    """What the drivers channel is told when somebody is hired.

    Just the shipment. The welcome, the onboarding steps, the printer link, the
    house rules and the training intro all reach the new driver as their own
    DMs (see _send_driver_onboarding_messages) -- posting a second copy to the
    channel addressed it to nobody and buried the one line the channel exists to
    act on: a driver was hired, their papers need to go out.

    The recipient block is deliberately left off here. The paper girls get the
    name, address and phone in their own request; the channel does not need a
    new colleague's home address.
    """
    return _format_driver_paper_ship_notice(interview, include_recipient=False)


async def _run_hire_side_effects(
    context: ContextTypes.DEFAULT_TYPE,
    interview: dict,
    *,
    created_by_telegram_id: str,
    prior_errors: Optional[List[str]] = None,
) -> tuple[str, List[str]]:
    """Channel post, driver DM, paper shipment, training. Returns (hire_msg, warnings)."""
    driver_name = _driver_display_name(interview)

    channel_invite = await _create_driver_channel_invite(context, driver_name)
    tid = (interview.get("telegram_id") or "").strip()
    try:
        await _add_driver_to_channel(context, tid)
    except Exception as e:
        logger.warning("Could not auto-add driver %s to channel: %s", tid, e)

    ship, ship_warn = await _create_and_notify_paper_girl(
        context,
        interview,
        created_by_telegram_id=created_by_telegram_id,
    )

    hire_msg = _build_hire_announcement_message(interview, driver_name)
    await _post_to_driver_channel(context, kind="text", body=hire_msg, media_file_id=None)

    dm_warnings = await _send_driver_onboarding_messages(
        context,
        interview,
        channel_invite=channel_invite,
    )

    warnings: List[str] = list(prior_errors or [])
    if ship_warn:
        warnings.append(ship_warn)
    warnings.extend(dm_warnings)
    if not channel_invite:
        warnings.append("Channel invite link not created (check DRIVER_CHANNEL_ID + bot admin rights).")

    qty = Config.DEFAULT_PAPER_QTY
    if ship:
        hire_msg = hire_msg + f"\n\n📦 Paper shipment queued ({qty} papers)."
    return hire_msg, warnings


async def job_hire_side_effects(context: ContextTypes.DEFAULT_TYPE) -> None:
    """JobQueue callback: Telegram onboarding after web auto-hire."""
    data = context.job.data if context.job else {}
    iid = (data or {}).get("interview_id")
    created_by = (data or {}).get("created_by") or "web"
    if not iid:
        return
    interview = db.get_interview_by_id(str(iid))
    if not interview or (interview.get("status") or "") != "hired":
        logger.warning("job_hire_side_effects: interview %s not hired", iid)
        return
    hire_msg, warnings = await _run_hire_side_effects(
        context,
        interview,
        created_by_telegram_id=str(created_by),
        prior_errors=[],
    )
    try:
        from api.notify import notify_supervisors_hire_complete

        notify_supervisors_hire_complete(interview, hire_msg, warnings, source="web")
    except Exception as e:
        logger.error("notify_supervisors_hire_complete failed: %s", e)


async def _notify_driver_of_shipment(
    context: ContextTypes.DEFAULT_TYPE,
    shipment: dict,
) -> tuple[bool, List[str]]:
    """Forward tracking to driver via Telegram + email. Returns (ok, warnings)."""
    warnings: List[str] = []
    tracking = (shipment.get("tracking_number") or "").strip()
    if not tracking:
        return False, ["No tracking number on shipment"]

    usps_url = _build_usps_tracking_url(tracking)
    zip_code = (shipment.get("tracking_zip") or "").strip()
    name = (shipment.get("driver_name") or "Driver").strip()
    qty = int(shipment.get("quantity") or Config.DEFAULT_PAPER_QTY)
    zip_line = zip_code if zip_code else "—"

    tg_msg = (
        "📦 Your training papers are on the way!\n\n"
        f"USPS Tracking: {tracking}\n"
        f"Track here: {usps_url}\n"
        f"Destination ZIP: {zip_line}\n\n"
        "Tips:\n"
        "• Don't open until day of\n"
        "• Print on letter-size paper\n"
        "• Re-watch your training videos anytime with /training"
    )

    driver_tid = _parse_chat_id(shipment.get("driver_telegram_id"))
    if driver_tid:
        try:
            await context.bot.send_message(chat_id=driver_tid, text=tg_msg)
            receipt_fid = (shipment.get("receipt_file_id") or "").strip()
            if receipt_fid:
                try:
                    await context.bot.send_photo(
                        chat_id=driver_tid,
                        photo=receipt_fid,
                        caption="🧾 USPS receipt",
                    )
                except Exception as e:
                    warnings.append(f"Receipt photo to driver: {e}")
        except Exception as e:
            warnings.append(f"Telegram DM to driver: {e}")
    else:
        warnings.append("Driver telegram_id missing")

    driver_email = (shipment.get("driver_email") or "").strip()
    if driver_email:
        ok_em, err_em = resend_client.send_driver_tracking_email(
            to_address=driver_email,
            driver_name=name,
            tracking_number=tracking,
            tracking_url=usps_url,
            zip_code=zip_code or None,
            qty=qty,
        )
        if not ok_em:
            warnings.append(f"Email: {err_em or 'send failed'}")
    else:
        warnings.append("Driver email missing — skipped Resend")

    shipments_db.update_shipment(shipment["id"], {"status": "driver_notified"})
    shipment["status"] = "driver_notified"

    summary = (
        f"✅ Tracking sent for **{name}**\n"
        f"🙋 Shipped by: **{_shipment_acceptor_label(shipment)}**\n"
        f"Tracking: `{tracking}`\n"
        f"USPS: {usps_url}"
    )
    if warnings:
        summary += "\n\n⚠️ " + "\n".join(warnings)

    await _broadcast_shipment_status(context, summary)

    return driver_tid is not None or bool(driver_email), warnings


# --- JobQueue ---

async def _fire_appointment_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    if not job or not job.data:
        return
    appt_id = job.data.get("appointment_id")
    if not appt_id:
        return
    appt = db.get_appointment_by_id(appt_id)
    if not appt or appt.get("status") != "pending":
        return
    interview = db.get_interview_by_id(appt.get("interview_id", ""))
    if not interview:
        return
    full = (interview.get("full_name") or "").strip()
    ref = full or (interview.get("first_name") or interview.get("telegram_username") or "Driver")
    when = format_dt_display(
        datetime.fromisoformat(str(appt["scheduled_at"]).replace("Z", "+00:00")),
        Config.INTERVIEWER_TIMEZONE,
    )
    reference = str(interview.get("id", ""))[:8]
    msg = (
        f"📆 Interview appointment reminder\n\n"
        f"Driver: {ref}\n"
        f"Time: {when}\n"
        f"Reference: {reference}"
    )
    targets = set()
    creator = _parse_chat_id(appt.get("created_by_telegram_id"))
    if creator:
        targets.add(creator)
    tid = _parse_chat_id(interview.get("telegram_id"))
    if tid:
        targets.add(tid)
    for cid in _global_supervisory_chat_ids():
        targets.add(cid)
    for chat_id in targets:
        try:
            await context.bot.send_message(chat_id=chat_id, text=msg)
        except Exception as e:
            logger.warning("appointment reminder to %s: %s", chat_id, e)

    for row in shipments_db.list_supervisor_emails():
        email_addr = (row.get("email") or "").strip()
        if not email_addr:
            continue
        ok_em, err_em = resend_client.send_appointment_reminder_email(
            to_address=email_addr,
            driver_name=ref,
            appointment_display=when,
            reference=reference,
        )
        if not ok_em:
            logger.warning("appointment email to %s failed: %s", email_addr, err_em)

    db.mark_appointment_reminded(appt_id)


async def _fire_scheduled_announcement(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    if not job or not job.data:
        return
    job_id = job.data.get("announcement_job_id")
    if not job_id:
        return
    row = db.get_announcement_job(job_id)
    if not row or row.get("status") != "pending":
        return
    ok = await _post_to_driver_channel(
        context,
        kind=row.get("kind") or "text",
        body=row.get("body") or "",
        media_file_id=row.get("media_file_id"),
    )
    if ok:
        db.mark_announcement_sent(job_id)


def _schedule_appointment_job(application: Application, appt: dict) -> None:
    if not application.job_queue:
        return
    appt_id = appt.get("id")
    scheduled = appt.get("scheduled_at")
    if not appt_id or not scheduled:
        return
    try:
        dt = datetime.fromisoformat(str(scheduled).replace("Z", "+00:00"))
    except ValueError:
        return
    now = datetime.now(timezone.utc)
    delay = (dt - now).total_seconds()
    if delay <= 0:
        delay = 1
    application.job_queue.run_once(
        _fire_appointment_reminder,
        when=delay,
        data={"appointment_id": appt_id},
        name=f"appt_{appt_id}",
    )


def _schedule_announcement_job(application: Application, row: dict) -> None:
    if not application.job_queue:
        return
    job_id = row.get("id")
    scheduled = row.get("scheduled_at")
    if not job_id or not scheduled:
        return
    try:
        dt = datetime.fromisoformat(str(scheduled).replace("Z", "+00:00"))
    except ValueError:
        return
    now = datetime.now(timezone.utc)
    delay = (dt - now).total_seconds()
    if delay <= 0:
        delay = 1
    application.job_queue.run_once(
        _fire_scheduled_announcement,
        when=delay,
        data={"announcement_job_id": job_id},
        name=f"ann_{job_id}",
    )


async def _run_appointment_reminder_by_id(
    context: ContextTypes.DEFAULT_TYPE, appointment_id: str,
) -> None:
    class _Job:
        data = {"appointment_id": appointment_id}

    context.job = _Job()
    await _fire_appointment_reminder(context)


async def _run_announcement_by_id(
    context: ContextTypes.DEFAULT_TYPE, announcement_job_id: str,
) -> None:
    class _Job:
        data = {"announcement_job_id": announcement_job_id}

    context.job = _Job()
    await _fire_scheduled_announcement(context)


async def check_pending_jobs(context: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.now(timezone.utc).isoformat()
    for appt in db.list_pending_appointments_before(now):
        aid = appt.get("id")
        if aid:
            await _run_appointment_reminder_by_id(context, aid)
    for row in db.list_pending_announcements_before(now):
        jid = row.get("id")
        if jid:
            await _run_announcement_by_id(context, jid)


async def abandoned_drafts_sweep_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mark web form drafts inactive for 7+ days as abandoned."""
    try:
        from utils.drafts_db import DraftsDatabase

        drafts = DraftsDatabase(db.client)
        n = drafts.mark_abandoned_older_than_days(7)
        if n:
            logger.info("Marked %s interview draft(s) as abandoned", n)
    except Exception as e:
        logger.warning("abandoned_drafts_sweep_job: %s", e)


async def paper_girl_receipt_reminder_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """3x daily: re-broadcast pending paper orders to all paper girls."""
    if not _paper_girl_notify_chat_ids():
        return
    pending = [
        s for s in shipments_db.list_shipments(50)
        if (s.get("status") or "") in ("awaiting_tracking", "pending_accept")
    ]
    if not pending:
        return

    all_errs: List[str] = []
    for shipment in pending:
        sid = shipment.get("id")
        if not sid:
            continue
        if (shipment.get("status") or "") == "pending_accept":
            shipments_db.update_shipment(sid, {"status": "awaiting_tracking"})
            shipment = {**shipment, "status": "awaiting_tracking"}

        reminder_text = (
            "⏰ **Reminder** — ship ASAP and upload tracking:\n\n"
            + _format_paper_girl_ship_request(shipment)
        )
        errs = await _notify_paper_girl_chats(
            context,
            reminder_text,
            reply_markup=_paper_girl_ship_keyboard(sid),
            parse_mode="Markdown",
        )
        all_errs.extend(errs)

    if all_errs:
        logger.warning("paper girl receipt reminder failed: %s", "; ".join(all_errs))


def _startup_reenqueue_jobs(application: Application) -> None:
    if not application.job_queue:
        logger.warning("Job queue not available")
        return
    for appt in db.list_future_pending_appointments():
        _schedule_appointment_job(application, appt)
    for row in db.list_future_pending_announcements():
        _schedule_announcement_job(application, row)
    logger.info("Re-enqueued pending appointment/announcement jobs")


# --- Interview flow helpers ---

async def _begin_questionnaire(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, supervisor_created: bool,
) -> int:
    user = update.effective_user
    if not user:
        return ConversationHandler.END
    context.user_data["is_supervisor_created"] = supervisor_created
    context.user_data.pop("active_interview_id", None)
    # The card ids belong to the previous applicant's message. Left behind, the
    # first update for the NEW driver would edit the OLD driver's card.
    context.user_data.pop("understanding_chat_id", None)
    context.user_data.pop("understanding_message_id", None)
    msg = update.effective_message
    if msg:
        sent = await msg.reply_text(INTERVIEW_QUESTIONNAIRE_PROMPT)
        if sent:
            _track_pending_prompt(context, sent.message_id)
    return STATE_INTERVIEW_INPUT


async def _process_interview_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> int:
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return STATE_INTERVIEW_INPUT

    await _clear_pending_prompts(context, msg.chat_id)

    fields: Dict[str, str] = {}
    try:
        if msg.photo:
            photo = msg.photo[-1]
            tg_file = await context.bot.get_file(photo.file_id)
            data = await tg_file.download_as_bytearray()
            fields = ai_vision.extract_interview_from_image(bytes(data), "image/jpeg")
        elif msg.document:
            doc = msg.document
            mime = (doc.mime_type or "").lower()
            if not mime.startswith("image/"):
                await msg.reply_text("Please send an image (photo or image document).")
                return STATE_INTERVIEW_INPUT
            tg_file = await context.bot.get_file(doc.file_id)
            data = await tg_file.download_as_bytearray()
            fields = ai_vision.extract_interview_from_image(bytes(data), mime)
        else:
            text = (msg.text or msg.caption or "").strip()
            if not text:
                await msg.reply_text("Send a photo or paste the interview information as text.")
                return STATE_INTERVIEW_INPUT
            fields = ai_vision.extract_interview_from_text(text)
    except AIVisionQuotaError:
        await msg.reply_text("❌ OpenAI quota exceeded. Try again later or paste structured text.")
        return STATE_INTERVIEW_INPUT
    except Exception as e:
        logger.error("interview extract: %s", e, exc_info=True)
        await msg.reply_text("❌ Could not parse interview data. Try again with clearer text or image.")
        return STATE_INTERVIEW_INPUT

    # What this message actually told us. Only non-blank values count: a photo
    # the AI could not read must never wipe details an earlier message got right.
    normalized = ai_vision.normalize_interview_data(fields)
    new_values = {
        k: normalized[k]
        for k in INTERVIEW_FIELD_KEYS
        if str(normalized.get(k) or "").strip()
    }
    if not new_values:
        # Previously this inserted a completely blank interview row.
        await msg.reply_text(
            "\U0001f914 I couldn't read any driver details in that message. "
            "Send it again as clearer text or a sharper photo."
        )
        return STATE_INTERVIEW_INPUT

    # A continuation, not a new person. This is the whole fix: the handler wrote
    # active_interview_id and then never read it, so every extra message about
    # the SAME driver started another entry. Finished records are excluded --
    # a stray message must not reopen somebody already hired or cancelled.
    active_id = context.user_data.get("active_interview_id")
    active = db.get_interview_by_id(active_id) if active_id else None
    if active and str(active.get("status") or "pending") in ("pending", "scheduled"):
        db.update_interview(active["id"], new_values)
        active = db.get_interview_by_id(active["id"]) or active
        if not context.user_data.get("understanding_message_id"):
            context.user_data["understanding_chat_id"] = (
                active.get("understanding_chat_id") or msg.chat_id
            )
            context.user_data["understanding_message_id"] = active.get(
                "understanding_message_id"
            )
        if context.user_data.get("understanding_message_id"):
            await _refresh_understanding_card(context, active)
        else:
            # The card was lost (a restart, a deleted message). Post a fresh one
            # rather than leaving the operator with no way to act on the entry.
            card = await msg.reply_text(
                _format_interview_understanding(active),
                reply_markup=_review_keyboard(active["id"]),
            )
            context.user_data["understanding_chat_id"] = card.chat_id
            context.user_data["understanding_message_id"] = card.message_id
            db.update_interview(
                active["id"],
                {
                    "understanding_chat_id": card.chat_id,
                    "understanding_message_id": card.message_id,
                },
            )
        return STATE_INTERVIEW_INPUT

    context.user_data.pop("active_interview_id", None)
    interview = db.create_interview(
        fields,
        created_by_telegram_id=str(user.id),
        is_supervisor_created=bool(context.user_data.get("is_supervisor_created")),
    )
    if not interview:
        await msg.reply_text("❌ Could not save interview to database. Run migration_krab_interviewer.sql?")
        return ConversationHandler.END

    context.user_data["active_interview_id"] = interview["id"]
    context.user_data["understanding_chat_id"] = msg.chat_id
    card = await msg.reply_text(
        _format_interview_understanding(interview),
        reply_markup=_review_keyboard(interview["id"]),
    )
    context.user_data["understanding_message_id"] = card.message_id
    db.update_interview(
        interview["id"],
        {
            "understanding_chat_id": card.chat_id,
            "understanding_message_id": card.message_id,
        },
    )
    return STATE_INTERVIEW_INPUT


# --- Commands ---

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return ConversationHandler.END

    dispatch = Config.KRAB_DISPATCH_BOT_USERNAME.lstrip("@")

    if _user_is_global_supervisor(user.id):
        text = (
            "🎊 Great news! You hired a new driver to join the Team ! 🚗\n"
            "Let’s get them started quickly with @krabinterviewerbot 🤖⚡\n\n"
            "━━━━━━━━━━━━━━━\n"
            "What would you like to do?\n\n"
            "✅Hire Driver Now\n"
            "📅 Book Interview Appointment\n\n"
            "👇Tap Below\n"
            "━━━━━━━━━━━━━━━"
        )
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🙋‍♂️Now", callback_data="int_now"),
                InlineKeyboardButton("📆 Appointment", callback_data="int_appt"),
            ],
        ])
        await msg.reply_text(text, reply_markup=kb)
        return STATE_INTERVIEW_INPUT

    if user.username:
        try:
            db.upsert_telegram_user_directory(str(user.id), user.username)
        except Exception as e:
            logger.warning("upsert telegram_user_directory: %s", e)
        try:
            from utils.drafts_db import DraftsDatabase
            from utils.telegram_link import on_bot_user_started

            drafts = DraftsDatabase(db.client)
            linked = on_bot_user_started(drafts, str(user.id), user.username)
            if linked:
                logger.info(
                    "Linked Telegram ID to %s web draft(s) for @%s",
                    linked,
                    user.username,
                )
        except Exception as e:
            logger.warning("link web drafts on start: %s", e)

    interviewer_bot = (getattr(Config, "KRAB_INTERVIEWER_BOT_USERNAME", None) or "krabinterviewerbot").lstrip("@")
    args = context.args or []
    hired_interview = _resolve_hired_driver_interview(str(user.id))

    if args and args[0].startswith("web"):
        if hired_interview:
            await _deliver_hired_driver_onboarding(
                context, hired_interview, source="start_web"
            )
        await msg.reply_text(
            "✅ Your Telegram is linked for the driver application.\n\n"
            "Return to the application form — your Telegram ID will appear automatically.\n\n"
            f"When you're hired, start taking leads with @{dispatch}."
        )
        return ConversationHandler.END

    if hired_interview:
        await _deliver_hired_driver_onboarding(
            context, hired_interview, source="start"
        )
        return ConversationHandler.END

    intro = (
        f"👋 Welcome to Krab Interviewer!\n\n"
        f"Applying on the website? After you tap Start here, go back to the form — "
        f"we'll fill in your Telegram ID automatically.\n\n"
        f"Or complete your driver onboarding questionnaire below.\n"
        f"When you're hired, start taking leads with @{dispatch}.\n\n"
        f"🌐 Web apply: use @{interviewer_bot} with /start first, then finish at tristatetags.com/interview/apply"
    )
    await msg.reply_text(intro)
    return await _begin_questionnaire(update, context, supervisor_created=False)


async def cmd_interviews(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _user_is_global_supervisor(update.effective_user.id):
        return
    rows = db.list_interviews(40)
    if not rows:
        await update.effective_message.reply_text("No interviews yet.")
        return
    await update.effective_message.reply_text(
        "📋 Driver applications — tap a first name:",
        reply_markup=_interview_list_keyboard(rows),
    )


async def cmd_shipments(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _user_can_manage_shipments(update.effective_user.id):
        return
    await _reply_shipments_list(update.effective_message)


def _driver_profile_lines(driver: dict) -> List[str]:
    tid = str(driver.get("driver_telegram_id") or "").strip()
    username = "-"
    email = "-"
    if db and tid:
        interview = db.get_latest_interview_for_telegram_id(tid)
        if interview:
            un_raw = (interview.get("telegram_username") or "").strip().lstrip("@")
            if un_raw:
                username = f"@{un_raw}"
            em = (interview.get("email") or "").strip()
            if em:
                email = em
    return [
        f"🚗 {driver.get('driver_name', '?')}",
        f"📱 {driver.get('phone_number') or '-'}",
        f"💬 {username}",
        f"💬 Telegram ID: {tid or '-'}",
        f"📧 {email}",
    ]


def _driver_profile_keyboard(driver_short: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✏️ Edit", callback_data=f"drv_edit_{driver_short}"),
            InlineKeyboardButton("🗑 Delete", callback_data=f"drv_del_{driver_short}"),
        ],
    ])


def _resolve_driver_by_short(short: str) -> Optional[dict]:
    for d in db.get_all_drivers():
        did = str(d.get("id") or "")
        if not did:
            continue
        try:
            if _short_uuid(did) == short:
                return d
        except Exception:
            pass
        if did.startswith(short):
            return d
    return None


async def _send_driver_profile_card(
    msg, context: ContextTypes.DEFAULT_TYPE, driver: dict
) -> None:
    short = _short_uuid(str(driver["id"]))
    await msg.reply_text(
        "\n".join(_driver_profile_lines(driver)),
        reply_markup=_driver_profile_keyboard(short),
    )


async def cmd_drivers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _user_is_global_supervisor(update.effective_user.id):
        return
    drivers = db.get_all_drivers()
    if not drivers:
        await update.effective_message.reply_text("No drivers in Issuer database.")
        return
    buttons = []
    for d in drivers[:25]:
        did = str(d.get("id", ""))
        sid = _short_uuid(did) if len(did) == 36 else did[:22]
        name = (d.get("driver_name") or "?")[:20]
        phone = (d.get("phone_number") or "-")[:12]
        buttons.append([
            InlineKeyboardButton(f"{name} | {phone}", callback_data=f"int_drv_{sid}"),
        ])
    await update.effective_message.reply_text(
        "🚗 Drivers — tap for profile:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _user_is_global_supervisor(update.effective_user.id):
        return
    drivers = db.get_all_drivers()
    if not drivers:
        await update.effective_message.reply_text("No drivers to delete.")
        return
    buttons = []
    for d in drivers[:25]:
        did = str(d.get("id", ""))
        sid = _short_uuid(did) if len(did) == 36 else did[:22]
        name = (d.get("driver_name") or "?")[:20]
        phone = (d.get("phone_number") or "-")[:12]
        buttons.append([
            InlineKeyboardButton(
                f"🗑 {name} | {phone}", callback_data=f"drv_dell_{sid}"
            ),
        ])
    await update.effective_message.reply_text(
        "🗑 <b>Delete driver</b> — tap a name, then confirm.\n\n"
        "Removes Issuer driver, Dispatch recipient, and interview records.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return

    text = (
        "🤖 <b>Krab Interviewer — Commands</b>\n\n"
        "1. ✅ /start — begin driver questionnaire (or supervisor hire menu)\n"
        "2. ❌ /cancel — cancel the current flow\n"
        "3. 🎤 /interviews — driver applications (tap a first name to open)\n"
        "4. 🚗 /drivers — list Issuer drivers (tap for profile)\n"
        "5. 🗑 /delete — remove a driver (Issuer + Dispatch + interviews)\n"
        "6. 📂 /open — hired drivers (tap a name) or /open &lt;id&gt; — open by id\n"
        "7. 📢 /announce — post next message to drivers channel now\n"
        "8. 🗓️📢 /announce_schedule — schedule a channel post\n"
        "9. 🎥📚 /training — view training videos (supervisors: add or remove)\n"
        "10. ❓ /help — show this command list"
    )

    try:
        await msg.reply_text(text, parse_mode="HTML")
    except Exception as e:
        logger.warning("cmd_help HTML send failed: %s; retrying plain", e)
        plain = text.replace("<b>", "").replace("</b>", "").replace("&lt;", "<").replace("&gt;", ">")
        await msg.reply_text(plain)


async def cmd_open(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _user_is_global_supervisor(update.effective_user.id):
        return
    args = context.args or []
    if not args:
        rows = db.list_interviews_by_status("hired", limit=40)
        if not rows:
            await update.effective_message.reply_text("No hired drivers yet.")
            return
        await update.effective_message.reply_text(
            "✅ Hired drivers — tap a first name:",
            reply_markup=_interview_list_keyboard(rows),
        )
        return
    iid = args[0].strip()
    if len(iid) < 36:
        for r in db.list_interviews(200):
            if str(r["id"]).startswith(iid):
                iid = r["id"]
                break
    await _send_interview_detail(update.effective_message, context, iid)


async def _send_interview_detail(message, context, interview_id: str) -> None:
    interview = db.get_interview_by_id(interview_id)
    if not interview:
        await message.reply_text("Interview not found.")
        return
    appt = db.get_appointment_for_interview(interview_id)
    if appt and appt.get("scheduled_at"):
        try:
            dt = datetime.fromisoformat(str(appt["scheduled_at"]).replace("Z", "+00:00"))
            interview["_appointment_display"] = format_dt_display(dt, Config.INTERVIEWER_TIMEZONE)
        except ValueError:
            pass
    st = (interview.get("status") or "").strip()
    kb = _review_keyboard(interview_id) if st not in ("hired", "cancelled") else None
    await message.reply_text(_format_interview_understanding(interview), reply_markup=kb)
    await _send_interview_license_bundle(message, context, interview_id)


async def cmd_setemail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return ConversationHandler.END
    if not _user_is_global_supervisor(user.id):
        return ConversationHandler.END
    existing = shipments_db.get_supervisor_email(str(user.id))
    note = f"\n\nCurrent: `{existing}`" if existing else ""
    await msg.reply_text(
        "📧 Send your email address to receive appointment reminders." + note,
        parse_mode="Markdown",
    )
    return STATE_AWAIT_SUPERVISOR_EMAIL


async def handle_supervisor_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return ConversationHandler.END
    if not _user_is_global_supervisor(user.id):
        return ConversationHandler.END
    text = (msg.text or "").strip()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", text):
        await msg.reply_text("That doesn't look like a valid email. Try again or /cancel.")
        return STATE_AWAIT_SUPERVISOR_EMAIL
    ok = shipments_db.set_supervisor_email(str(user.id), text)
    if ok:
        await msg.reply_text(f"✅ Saved `{text}`. You'll receive appointment reminders here.", parse_mode="Markdown")
    else:
        await msg.reply_text("❌ Could not save email. Run migration_supervisor_emails.sql?")
    return ConversationHandler.END


async def cmd_announce(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _user_is_global_supervisor(update.effective_user.id):
        return ConversationHandler.END
    await update.effective_message.reply_text(
        "📣 Announce mode: send your next message (text, photo, video, or document). "
        "It will be posted to the drivers channel immediately."
    )
    context.user_data["announce_immediate"] = True
    return STATE_ANNOUNCE_WAIT


async def cmd_announce_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _user_is_global_supervisor(update.effective_user.id):
        return ConversationHandler.END
    await update.effective_message.reply_text(
        "📅 Send the date and time to post (e.g. 2026-05-25 14:30 ET):"
    )
    context.user_data.pop("announce_immediate", None)
    return STATE_ANNOUNCE_SCHEDULE_TIME


# --- Callbacks ---

async def handle_interview_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _safe_answer_callback_query(query)
    data = query.data or ""
    user = query.from_user
    chat_id = query.message.chat_id if query.message else None

    if data == "int_now":
        await _clear_pending_prompts(context, chat_id)
        if query.message:
            await _safe_delete_chat_message(context, chat_id, query.message.message_id)
        context.user_data["is_supervisor_created"] = True
        # A NEW driver. Without this the first message about them would be folded
        # into the previous applicant's entry -- the exact bug just fixed, only
        # now in the other direction.
        context.user_data.pop("active_interview_id", None)
        context.user_data.pop("understanding_chat_id", None)
        context.user_data.pop("understanding_message_id", None)
        if query.message:
            await query.message.reply_text(INTERVIEW_QUESTIONNAIRE_PROMPT)
        return STATE_INTERVIEW_INPUT

    if data == "int_appt":
        if query.message:
            await _safe_delete_chat_message(context, chat_id, query.message.message_id)
        context.user_data["schedule_only_before_interview"] = True
        if query.message:
            await _send_appointment_prompt(query.message, context)
        return STATE_INT_SCHEDULE_APPT

    iid = context.user_data.get("active_interview_id") or _interview_id_from_callback(data)

    if data.startswith("int_open_"):
        oid = _resolve_interview_id_from_callback(data, "int_open_")
        if oid and query.message:
            await _send_interview_detail(query.message, context, oid)
        return STATE_INTERVIEW_INPUT

    if data.startswith("int_drv_"):
        short = data.replace("int_drv_", "")
        driver = _resolve_driver_by_short(short)
        if not driver:
            await query.message.reply_text("Driver not found.")
            return STATE_INTERVIEW_INPUT
        await _send_driver_profile_card(query.message, context, driver)
        tid = str(driver.get("driver_telegram_id") or "").strip()
        if tid:
            li = db.get_latest_interview_for_telegram_id(tid)
            if li:
                await _send_interview_detail(query.message, context, li["id"])
        return STATE_INTERVIEW_INPUT

    if data.startswith("int_ef_"):
        m = re.match(r"^int_ef_([a-z_]+)_([A-Za-z0-9_-]+)$", data)
        if m:
            field_key = m.group(1)
            try:
                eid = _long_uuid(m.group(2))
            except Exception:
                eid = None
            if eid and field_key in EDITABLE_KEYS:
                context.user_data["active_interview_id"] = eid
                context.user_data["edit_field_key"] = field_key
                label = FIELD_LABELS.get(field_key, field_key)
                sent = await query.message.reply_text(
                    f"✍️ Send new value for: {label}\n(Type - to clear)"
                )
                _track_pending_prompt(context, sent.message_id)
                return STATE_INT_EDIT_FIELD

    if data.startswith("int_lic_"):
        iid = iid or _resolve_interview_id_from_callback(data, "int_lic_")
        if iid:
            context.user_data["active_interview_id"] = iid
            sent = await query.message.reply_text("🪪 Send the driver's license image:")
            _track_pending_prompt(context, sent.message_id)
            return STATE_INT_UPLOAD_LICENSE

    if data.startswith("int_sched_"):
        iid = iid or _resolve_interview_id_from_callback(data, "int_sched_")
        if iid:
            context.user_data["active_interview_id"] = iid
            if query.message:
                await _send_appointment_prompt(query.message, context)
            return STATE_INT_SCHEDULE_APPT

    if data.startswith("int_edit_"):
        iid = iid or _resolve_interview_id_from_callback(data, "int_edit_")
        if iid and query.message:
            context.user_data["active_interview_id"] = iid
            await context.bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=context.user_data.get("understanding_message_id") or query.message.message_id,
                reply_markup=_edit_fields_keyboard(iid),
            )
        return STATE_INTERVIEW_INPUT

    if data.startswith("int_eback_"):
        iid = iid or _resolve_interview_id_from_callback(data, "int_eback_")
        interview = db.get_interview_by_id(iid) if iid else None
        if interview:
            await _refresh_understanding_card(context, interview)
        return STATE_INTERVIEW_INPUT

    if data.startswith("int_hire_"):
        iid = iid or _resolve_interview_id_from_callback(data, "int_hire_")
        if not iid:
            return STATE_INTERVIEW_INPUT
        if not await _user_can_hire(context, user.id):
            await query.message.reply_text(
                "⛔ Only people on the team can hire. Ask a supervisor to add "
                "you to the drivers channel first."
            )
            return STATE_INTERVIEW_INPUT
        # Nobody approves themselves. Supervisors are exempt so the existing
        # supervisor-created flow, where they enter the driver themselves, keeps
        # working exactly as it did.
        _pending = db.get_interview_by_id(iid)
        if (_pending and not _user_is_global_supervisor(user.id)
                and _is_own_application(_pending, user.id)):
            await query.message.reply_text(
                "⛔ You can't hire your own application — a teammate has to "
                "approve it."
            )
            return STATE_INTERVIEW_INPUT
        interview, errors = hire_driver_records(db, iid)
        if not interview:
            await query.message.reply_text(
                f"❌ {errors[0] if errors else 'Hire failed.'}"
            )
            return STATE_INTERVIEW_INPUT
        # Who approved this. Nothing recorded it before, which mattered less
        # when only supervisors could; with the team able to hire it is the
        # difference between an accountable action and an anonymous one.
        # Fail-soft: the column may not exist yet, and a hire must not be undone
        # by an audit write.
        try:
            db.update_interview(iid, {"hired_by_telegram_id": str(user.id)})
        except Exception as e:
            logger.info("hired_by not recorded for %s (column missing?): %s", iid, e)
        await _clear_pending_prompts(context, chat_id)
        await _refresh_understanding_card(context, interview)
        hire_msg, warnings = await _run_hire_side_effects(
            context,
            interview,
            created_by_telegram_id=str(user.id),
            prior_errors=errors,
        )
        sup_lines = [hire_msg]
        if warnings:
            sup_lines.append("\n⚠️ Warnings:\n" + "\n".join(f"• {w}" for w in warnings))
        await query.message.reply_text("\n".join(sup_lines))
        return STATE_INTERVIEW_INPUT

    return STATE_INTERVIEW_INPUT




# --- Calendar picker for appointments ---

_DAY_HEADERS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
_HOUR_CHOICES = [8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21]
_MINUTE_CHOICES = [0, 15, 30, 45]


def _appt_tz():
    try:
        return pytz.timezone(Config.INTERVIEWER_TIMEZONE)
    except Exception:
        return pytz.timezone("America/New_York")


def _build_calendar_keyboard(year: int, month: int) -> InlineKeyboardMarkup:
    cal = _calendar_mod.Calendar(firstweekday=0)
    weeks = cal.monthdatescalendar(year, month)
    today = datetime.now(_appt_tz()).date()
    header = [InlineKeyboardButton(_calendar_mod.month_name[month] + f" {year}", callback_data="cal_noop")]
    rows: List[List[InlineKeyboardButton]] = [header]
    rows.append([InlineKeyboardButton(d, callback_data="cal_noop") for d in _DAY_HEADERS])
    for week in weeks:
        row = []
        for d in week:
            if d.month != month:
                row.append(InlineKeyboardButton(" ", callback_data="cal_noop"))
            elif d < today:
                row.append(InlineKeyboardButton("·", callback_data="cal_noop"))
            else:
                label = f"·{d.day}·" if d == today else str(d.day)
                row.append(
                    InlineKeyboardButton(label, callback_data=f"cal_day_{d.isoformat()}")
                )
        rows.append(row)

    prev_month = month - 1 or 12
    prev_year = year - 1 if month == 1 else year
    next_month = 1 if month == 12 else month + 1
    next_year = year + 1 if month == 12 else year
    rows.append([
        InlineKeyboardButton("◀️", callback_data=f"cal_nav_{prev_year}-{prev_month:02d}"),
        InlineKeyboardButton("Cancel", callback_data="cal_cancel"),
        InlineKeyboardButton("▶️", callback_data=f"cal_nav_{next_year}-{next_month:02d}"),
    ])
    return InlineKeyboardMarkup(rows)


def _build_hour_keyboard(day_iso: str) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    row: List[InlineKeyboardButton] = []
    for h in _HOUR_CHOICES:
        label = datetime.strptime(f"{h:02d}:00", "%H:%M").strftime("%-I%p") if False else (
            datetime(2000, 1, 1, h, 0).strftime("%I%p").lstrip("0")
        )
        row.append(InlineKeyboardButton(label, callback_data=f"cal_hr_{day_iso}_{h:02d}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    today_iso = datetime.now(_appt_tz()).strftime("%Y-%m")
    target_month = day_iso[:7]
    rows.append([
        InlineKeyboardButton("⬅️ Day", callback_data=f"cal_nav_{target_month}"),
        InlineKeyboardButton("Cancel", callback_data="cal_cancel"),
    ])
    return InlineKeyboardMarkup(rows)


def _build_minute_keyboard(day_iso: str, hour: int) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = [[]]
    for m in _MINUTE_CHOICES:
        label = f"{hour:02d}:{m:02d}"
        rows[0].append(
            InlineKeyboardButton(label, callback_data=f"cal_set_{day_iso}_{hour:02d}-{m:02d}")
        )
    rows.append([
        InlineKeyboardButton("⬅️ Hour", callback_data=f"cal_day_{day_iso}"),
        InlineKeyboardButton("Cancel", callback_data="cal_cancel"),
    ])
    return InlineKeyboardMarkup(rows)


def _appt_text_prompt() -> str:
    return (
        "📆 Pick a date and time below, or type the appointment time.\n\n"
        "AI understands: `May 26 7pm`, `Sunday 12pm`, `Next Tuesday 8pm`, "
        "`2026-05-25 14:30`, `tomorrow 3pm`."
    )


async def _send_appointment_prompt(message, context: ContextTypes.DEFAULT_TYPE) -> None:
    today = datetime.now(_appt_tz()).date()
    sent = await message.reply_text(
        _appt_text_prompt(),
        reply_markup=_build_calendar_keyboard(today.year, today.month),
        parse_mode="Markdown",
    )
    if sent:
        _track_pending_prompt(context, sent.message_id)


async def _finalize_appointment(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    dt: datetime,
    via_message=None,
) -> int:
    msg = via_message or update.effective_message
    user = update.effective_user
    await _clear_pending_prompts(context, msg.chat_id if msg else None)

    when_label = format_dt_display(dt, Config.INTERVIEWER_TIMEZONE)

    if context.user_data.pop("schedule_only_before_interview", False):
        if msg:
            await msg.reply_text(
                f"📆 Appointment noted for {when_label}.\n"
                "Tap **🙋‍♂️Now** when ready to enter driver info."
            )
        context.user_data["pending_pre_interview_appt"] = dt.isoformat()
        return STATE_INTERVIEW_INPUT

    iid = context.user_data.get("active_interview_id")
    if not iid:
        if msg:
            await msg.reply_text("No active interview. Use /start")
        return ConversationHandler.END

    appt = db.create_appointment(iid, dt, str(user.id))
    if appt:
        db.update_interview(iid, {"status": "scheduled"})
        _schedule_appointment_job(context.application, appt)
    interview = db.get_interview_by_id(iid)
    if interview:
        interview["_appointment_display"] = when_label
        await _refresh_understanding_card(context, interview)
    if msg:
        await msg.reply_text(f"✅ Appointment set for {when_label}.")
    return STATE_INTERVIEW_INPUT


async def handle_calendar_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _safe_answer_callback_query(query)
    data = query.data or ""
    message = query.message

    if data == "cal_noop":
        return STATE_INT_SCHEDULE_APPT

    if data == "cal_cancel":
        if message:
            await _safe_delete_chat_message(context, message.chat_id, message.message_id)
        context.user_data.pop("schedule_only_before_interview", None)
        return STATE_INTERVIEW_INPUT

    if data.startswith("cal_nav_"):
        ym = data.replace("cal_nav_", "")
        try:
            year, month = [int(x) for x in ym.split("-")]
        except (ValueError, AttributeError):
            return STATE_INT_SCHEDULE_APPT
        if message:
            try:
                await context.bot.edit_message_reply_markup(
                    chat_id=message.chat_id,
                    message_id=message.message_id,
                    reply_markup=_build_calendar_keyboard(year, month),
                )
            except Exception as e:
                logger.debug("calendar nav edit: %s", e)
        return STATE_INT_SCHEDULE_APPT

    if data.startswith("cal_day_"):
        day_iso = data.replace("cal_day_", "")
        if message:
            try:
                await context.bot.edit_message_text(
                    chat_id=message.chat_id,
                    message_id=message.message_id,
                    text=f"⏰ Pick an hour for {day_iso}:",
                    reply_markup=_build_hour_keyboard(day_iso),
                )
            except Exception as e:
                logger.debug("calendar day edit: %s", e)
        return STATE_INT_SCHEDULE_APPT

    if data.startswith("cal_hr_"):
        rest = data.replace("cal_hr_", "")
        try:
            day_iso, hr_str = rest.rsplit("_", 1)
            hour = int(hr_str)
        except (ValueError, AttributeError):
            return STATE_INT_SCHEDULE_APPT
        if message:
            try:
                await context.bot.edit_message_text(
                    chat_id=message.chat_id,
                    message_id=message.message_id,
                    text=f"🕐 Pick minute for {day_iso} {hour:02d}:xx",
                    reply_markup=_build_minute_keyboard(day_iso, hour),
                )
            except Exception as e:
                logger.debug("calendar hour edit: %s", e)
        return STATE_INT_SCHEDULE_APPT

    if data.startswith("cal_set_"):
        rest = data.replace("cal_set_", "")
        try:
            day_iso, hm = rest.rsplit("_", 1)
            hour, minute = [int(x) for x in hm.split("-")]
            y, m, d = [int(x) for x in day_iso.split("-")]
        except (ValueError, AttributeError):
            return STATE_INT_SCHEDULE_APPT
        tz = _appt_tz()
        try:
            dt = tz.localize(datetime(y, m, d, hour, minute))
        except Exception:
            return STATE_INT_SCHEDULE_APPT
        if message:
            await _safe_delete_chat_message(context, message.chat_id, message.message_id)
        return await _finalize_appointment(update, context, dt=dt, via_message=message)

    return STATE_INT_SCHEDULE_APPT


async def handle_schedule_appt_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.effective_message
    text = (msg.text or "").strip()
    dt = parse_user_datetime(text, Config.INTERVIEWER_TIMEZONE)
    if not dt:
        await msg.reply_text(
            "Could not parse date/time. Try `May 26 7pm`, `Sunday 12pm`, "
            "`Next Tuesday 8pm`, or use the calendar above.",
            parse_mode="Markdown",
        )
        return STATE_INT_SCHEDULE_APPT

    try:
        await msg.delete()
    except Exception:
        pass
    return await _finalize_appointment(update, context, dt=dt)


async def handle_upload_license(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.effective_message
    iid = context.user_data.get("active_interview_id")
    if not iid:
        return STATE_INTERVIEW_INPUT

    file_id = None
    name = "license.jpg"
    if msg.photo:
        file_id = msg.photo[-1].file_id
        tg = await context.bot.get_file(file_id)
        data = await tg.download_as_bytearray()
        name = "license.jpg"
    elif msg.document and (msg.document.mime_type or "").startswith("image/"):
        file_id = msg.document.file_id
        tg = await context.bot.get_file(file_id)
        data = await tg.download_as_bytearray()
        name = msg.document.file_name or "license.jpg"
    else:
        await msg.reply_text("Please send a license image.")
        return STATE_INT_UPLOAD_LICENSE

    url = db.upload_driver_license_to_storage(iid, bytes(data), name)
    await _clear_pending_prompts(context, msg.chat_id)
    try:
        await msg.delete()
    except Exception:
        pass

    if url:
        db.update_interview(iid, {"drivers_license_file_url": url})
    interview = db.get_interview_by_id(iid)
    if interview:
        await _refresh_understanding_card(context, interview)
    return STATE_INTERVIEW_INPUT


async def handle_edit_field_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.effective_message
    iid = context.user_data.get("active_interview_id")
    key = context.user_data.pop("edit_field_key", None)
    if not iid or not key:
        return STATE_INTERVIEW_INPUT

    val = (msg.text or "").strip()
    if val.lower() in ("-", "none", "n/a"):
        val = ""
    updates = {key: val}
    if key == "telegram_username" and val and not val.startswith("@"):
        val = "@" + val
        updates[key] = val
    if key == "full_name" and val:
        first, _ = ai_vision.split_full_name(val)
        if first:
            updates["first_name"] = first
    db.update_interview(iid, updates)
    interview = db.get_interview_by_id(iid)
    if interview and key == "telegram_username" and val:
        fn = val.lstrip("@").split()[0]
        db.update_interview(iid, {"first_name": fn})
        interview = db.get_interview_by_id(iid)

    await _clear_pending_prompts(context, msg.chat_id)
    try:
        await msg.delete()
    except Exception:
        pass

    if interview:
        await _refresh_understanding_card(context, interview)
    return STATE_INTERVIEW_INPUT


async def handle_announce_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.effective_message
    user = update.effective_user
    if not _user_is_global_supervisor(user.id):
        return ConversationHandler.END

    kind = "text"
    body = (msg.text or msg.caption or "").strip()
    media_id = None
    if msg.photo:
        kind, media_id = "photo", msg.photo[-1].file_id
    elif msg.video:
        kind, media_id = "video", msg.video.file_id
    elif msg.document:
        kind, media_id = "document", msg.document.file_id

    if context.user_data.get("announce_schedule_at"):
        sched = context.user_data.pop("announce_schedule_at")
        try:
            dt = datetime.fromisoformat(sched)
        except ValueError:
            dt = None
        row = db.create_announcement_job(
            kind=kind,
            body=body,
            media_file_id=media_id,
            created_by_telegram_id=str(user.id),
            scheduled_at=dt,
            status="pending",
        )
        if row:
            _schedule_announcement_job(context.application, row)
            await msg.reply_text(
                f"📅 Scheduled for {format_dt_display(dt, Config.INTERVIEWER_TIMEZONE)}"
            )
        return ConversationHandler.END

    ok = await _post_to_driver_channel(context, kind=kind, body=body, media_file_id=media_id)
    db.create_announcement_job(
        kind=kind,
        body=body,
        media_file_id=media_id,
        created_by_telegram_id=str(user.id),
        status="sent" if ok else "pending",
    )
    await msg.reply_text("✅ Posted to drivers channel." if ok else "❌ Channel post failed.")
    return ConversationHandler.END


async def handle_shipment_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _safe_answer_callback_query(query)
    data = query.data or ""
    user = query.from_user
    if not _user_can_manage_shipments(user.id):
        if query.message:
            await query.message.reply_text("⛔ Not authorized for shipments.")
        return STATE_INTERVIEW_INPUT

    if data == "ship_view_all":
        if query.message:
            await _reply_shipments_list(query.message)
        return STATE_INTERVIEW_INPUT

    if data.startswith("ship_track_"):
        try:
            sid = _long_uuid(data.replace("ship_track_", ""))
        except Exception:
            if query.message:
                await query.message.reply_text("Invalid shipment reference.")
            return STATE_INTERVIEW_INPUT
        shipment = shipments_db.get_shipment(sid)
        if not shipment:
            if query.message:
                await query.message.reply_text("Shipment not found.")
            return STATE_INTERVIEW_INPUT
        if not _user_can_work_shipment(user.id, shipment):
            if query.message:
                await query.message.reply_text("⛔ Not authorized for this shipment.")
            return STATE_INTERVIEW_INPUT
        st = (shipment.get("status") or "")
        if st == "driver_notified":
            if query.message:
                await query.message.reply_text(
                    f"Already notified for **{shipment.get('driver_name')}** "
                    f"(tracking: {shipment.get('tracking_number') or '-'}).",
                    parse_mode="Markdown",
                )
            return STATE_INTERVIEW_INPUT
        if st == "pending_accept":
            shipments_db.update_shipment(sid, {"status": "awaiting_tracking"})
        elif st not in ("awaiting_tracking", "tracking_received"):
            if query.message:
                await query.message.reply_text("This shipment is no longer awaiting tracking.")
            return STATE_INTERVIEW_INPUT
        context.user_data["active_shipment_id"] = sid
        dname = (shipment.get("driver_name") or "Driver").strip()
        sent = await query.message.reply_text(
            f"📥 Please upload the tracking number photo/text of the USPS receipt 🧾 for **{dname}**!",
            parse_mode="Markdown",
        )
        if sent:
            _track_pending_prompt(context, sent.message_id)
        return STATE_AWAIT_TRACKING

    return STATE_INTERVIEW_INPUT


async def handle_tracking_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return STATE_AWAIT_TRACKING
    if not _user_can_manage_shipments(user.id):
        await msg.reply_text("⛔ Not authorized.")
        return ConversationHandler.END

    sid = context.user_data.get("active_shipment_id")
    if not sid:
        await msg.reply_text("No active shipment. Tap Upload tracking on a shipment message.")
        return STATE_INTERVIEW_INPUT

    shipment = shipments_db.get_shipment(sid)
    if not shipment:
        context.user_data.pop("active_shipment_id", None)
        await msg.reply_text("Shipment not found.")
        return STATE_INTERVIEW_INPUT

    if not _user_can_work_shipment(user.id, shipment):
        await msg.reply_text("⛔ Not authorized for this shipment.")
        return STATE_INTERVIEW_INPUT

    tracking = ""
    zip_code = ""
    receipt_file_id = None
    receipt_chat_id = msg.chat_id
    receipt_message_id = msg.message_id

    if msg.photo:
        photo = msg.photo[-1]
        receipt_file_id = photo.file_id
        try:
            tg_file = await context.bot.get_file(photo.file_id)
            data = await tg_file.download_as_bytearray()
            extracted = ai_tracking.extract_tracking_zip(bytes(data), "image/jpeg")
            tracking = (extracted.get("tracking_number") or "").strip()
            zip_code = (extracted.get("zip") or "").strip()
        except AITrackingQuotaError:
            await msg.reply_text("❌ OpenAI quota exceeded. Please type the tracking number.")
            return STATE_AWAIT_TRACKING
        except Exception as e:
            logger.error("tracking extract: %s", e)
            await msg.reply_text("❌ Could not read receipt. Please type the tracking number.")
            return STATE_AWAIT_TRACKING
        if not tracking:
            await msg.reply_text(
                "Could not find a tracking number in that photo.\n"
                "Please send the tracking number as text."
            )
            return STATE_AWAIT_TRACKING
    elif msg.document and (msg.document.mime_type or "").lower().startswith("image/"):
        receipt_file_id = msg.document.file_id
        mime = msg.document.mime_type or "image/jpeg"
        try:
            tg_file = await context.bot.get_file(msg.document.file_id)
            data = await tg_file.download_as_bytearray()
            extracted = ai_tracking.extract_tracking_zip(bytes(data), mime)
            tracking = (extracted.get("tracking_number") or "").strip()
            zip_code = (extracted.get("zip") or "").strip()
        except AITrackingQuotaError:
            await msg.reply_text("❌ OpenAI quota exceeded. Please type the tracking number.")
            return STATE_AWAIT_TRACKING
        except Exception as e:
            logger.error("tracking extract doc: %s", e)
            await msg.reply_text("❌ Could not read receipt. Please type the tracking number.")
            return STATE_AWAIT_TRACKING
        if not tracking:
            await msg.reply_text("Could not find tracking in image. Send tracking number as text.")
            return STATE_AWAIT_TRACKING
    else:
        text = (msg.text or msg.caption or "").strip()
        if not text:
            await msg.reply_text("Send a tracking number (text) or receipt photo.")
            return STATE_AWAIT_TRACKING
        extracted = ai_tracking.extract_tracking_from_text(text)
        tracking = (extracted.get("tracking_number") or "").strip()
        zip_code = (extracted.get("zip") or "").strip()
        if not tracking:
            await msg.reply_text("Could not parse tracking number. Send 15+ digit USPS tracking.")
            return STATE_AWAIT_TRACKING

    await _clear_pending_prompts(context, msg.chat_id)

    updates: Dict[str, Any] = {
        "tracking_number": tracking,
        "tracking_zip": zip_code or None,
        "status": "tracking_received",
    }
    if not (shipment.get("accepted_by_telegram_id") or "").strip():
        updates["accepted_by_telegram_id"] = str(user.id)
        updates["accepted_by_name"] = _telegram_user_label(user)
    if receipt_file_id:
        updates["receipt_file_id"] = receipt_file_id
        updates["receipt_chat_id"] = receipt_chat_id
        updates["receipt_message_id"] = receipt_message_id

    shipments_db.update_shipment(sid, updates)
    shipment = shipments_db.get_shipment(sid) or {**shipment, **updates}
    context.user_data.pop("active_shipment_id", None)

    await msg.reply_text("⏳ Notifying driver…")
    ok, warns = await _notify_driver_of_shipment(context, shipment)
    if ok and not warns:
        await msg.reply_text(f"✅ Driver notified for {shipment.get('driver_name', 'driver')}.")
    elif ok:
        await msg.reply_text(
            f"✅ Driver notified with warnings:\n" + "\n".join(f"• {w}" for w in warns)
        )
    else:
        await msg.reply_text(
            "⚠️ Tracking saved but driver notify had issues:\n"
            + "\n".join(f"• {w}" for w in warns)
        )
    return STATE_INTERVIEW_INPUT


def _training_menu_keyboard(count: int) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("➕ Add video", callback_data="train_add"),
            InlineKeyboardButton(f"📋 View all ({count})", callback_data="train_list"),
        ],
        [InlineKeyboardButton("❌ Close", callback_data="train_close")],
    ]
    return InlineKeyboardMarkup(rows)


def _training_kind_icon(kind: str) -> str:
    return {
        "video": "🎬",
        "animation": "🎞️",
        "document": "📄",
        "photo": "🖼️",
    }.get((kind or "").lower(), "🎬")


def _training_short_caption(cap: Optional[str], limit: int = 40) -> str:
    if not cap:
        return ""
    cap = cap.strip().splitlines()[0]
    if len(cap) > limit:
        cap = cap[: limit - 1] + "…"
    return cap


async def _show_training_menu(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    *,
    edit_message_id: Optional[int] = None,
) -> None:
    videos = shipments_db.list_training_videos()
    count = len(videos)
    if count == 0:
        text = (
            "🎬 Training videos\n\n"
            "No videos saved yet.\n"
            "Tap ➕ Add video to upload your first one.\n\n"
            "All saved videos are auto-sent to each new hire with their tracking info."
        )
    else:
        text = (
            f"🎬 Training videos\n\n"
            f"You have {count} video(s) saved.\n"
            "They are auto-sent to each new hire with tracking info."
        )
    kb = _training_menu_keyboard(count)
    if edit_message_id is not None:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=edit_message_id, text=text, reply_markup=kb
            )
            return
        except Exception:
            pass
    await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)


async def _show_training_list(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    *,
    edit_message_id: Optional[int] = None,
) -> None:
    videos = shipments_db.list_training_videos()
    if not videos:
        await _show_training_menu(context, chat_id, edit_message_id=edit_message_id)
        return

    lines = [f"🎬 Saved training videos ({len(videos)})\n"]
    rows: List[List[InlineKeyboardButton]] = []
    for idx, v in enumerate(videos, start=1):
        kind = (v.get("media_kind") or "video").lower()
        icon = _training_kind_icon(kind)
        cap = _training_short_caption(v.get("caption"))
        desc = f"{idx}. {icon} {kind}"
        if cap:
            desc += f" — {cap}"
        lines.append(desc)
        vid = v.get("id")
        rows.append([
            InlineKeyboardButton(f"▶️ #{idx}", callback_data=f"train_preview_{vid}"),
            InlineKeyboardButton(f"🗑 Remove #{idx}", callback_data=f"train_remove_{vid}"),
        ])
    rows.append([
        InlineKeyboardButton("➕ Add another", callback_data="train_add"),
        InlineKeyboardButton("⬅️ Back", callback_data="train_back"),
    ])
    text = "\n".join(lines)
    kb = InlineKeyboardMarkup(rows)
    if edit_message_id is not None:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=edit_message_id, text=text, reply_markup=kb
            )
            return
        except Exception:
            pass
    await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)


_DRV_EDIT_FIELDS = {
    "name": ("driver_name", "Driver name"),
    "phone": ("phone_number", "Phone number"),
}


def _driver_edit_menu_keyboard(driver_short: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Name", callback_data=f"drv_ef_name_{driver_short}")],
        [InlineKeyboardButton("Phone", callback_data=f"drv_ef_phone_{driver_short}")],
        [InlineKeyboardButton("⬅️ Back", callback_data=f"drv_back_{driver_short}")],
    ])


def _driver_delete_confirm_keyboard(
    driver_short: str, *, back_to_profile: bool = True
) -> InlineKeyboardMarkup:
    cancel_cb = (
        f"drv_back_{driver_short}"
        if back_to_profile
        else f"drv_delx_{driver_short}"
    )
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ Yes, delete", callback_data=f"drv_delc_{driver_short}"
            ),
            InlineKeyboardButton("❌ No, cancel", callback_data=cancel_cb),
        ],
    ])


def _driver_delete_confirm_text(driver: dict) -> str:
    nm = driver.get("driver_name") or "this driver"
    return (
        f"🗑 Delete <b>{nm}</b>?\n\n"
        "This removes:\n"
        "• Issuer driver record\n"
        "• Dispatch email recipient\n"
        "• Interview records in this bot\n\n"
        + "\n".join(_driver_profile_lines(driver))
    )


async def handle_driver_callbacks(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    q = update.callback_query
    if not q:
        return STATE_INTERVIEW_INPUT
    await q.answer()
    user = update.effective_user
    if not user or not _user_is_global_supervisor(user.id):
        return ConversationHandler.END

    data = (q.data or "").strip()
    msg = q.message

    if data.startswith("drv_back_"):
        short = data[len("drv_back_"):]
        driver = _resolve_driver_by_short(short)
        if not driver:
            try:
                await q.edit_message_text("Driver not found.")
            except Exception:
                pass
            return STATE_INTERVIEW_INPUT
        try:
            await q.edit_message_text(
                "\n".join(_driver_profile_lines(driver)),
                reply_markup=_driver_profile_keyboard(short),
            )
        except Exception:
            await _send_driver_profile_card(msg, context, driver)
        return STATE_INTERVIEW_INPUT

    if data.startswith("drv_edit_"):
        short = data[len("drv_edit_"):]
        driver = _resolve_driver_by_short(short)
        if not driver:
            try:
                await q.edit_message_text("Driver not found.")
            except Exception:
                pass
            return STATE_INTERVIEW_INPUT
        try:
            await q.edit_message_text(
                "✏️ Edit driver — pick a field:\n\n"
                + "\n".join(_driver_profile_lines(driver)),
                reply_markup=_driver_edit_menu_keyboard(short),
            )
        except Exception:
            pass
        return STATE_INTERVIEW_INPUT

    if data.startswith("drv_ef_"):
        m = re.match(r"^drv_ef_([a-z_]+)_([A-Za-z0-9_-]+)$", data)
        if not m:
            return STATE_INTERVIEW_INPUT
        field_key = m.group(1)
        short = m.group(2)
        if field_key not in _DRV_EDIT_FIELDS:
            return STATE_INTERVIEW_INPUT
        driver = _resolve_driver_by_short(short)
        if not driver:
            try:
                await q.edit_message_text("Driver not found.")
            except Exception:
                pass
            return STATE_INTERVIEW_INPUT
        context.user_data["drv_edit_id"] = str(driver["id"])
        context.user_data["drv_edit_short"] = short
        context.user_data["drv_edit_field"] = field_key
        label = _DRV_EDIT_FIELDS[field_key][1]
        sent = await msg.reply_text(
            f"✍️ Send new value for: {label}\n(Type - to clear, /cancel to abort)"
        )
        _track_pending_prompt(context, sent.message_id)
        return STATE_DRV_EDIT_VALUE

    if data.startswith("drv_delc_"):
        short = data[len("drv_delc_"):]
        driver = _resolve_driver_by_short(short)
        if not driver:
            try:
                await q.edit_message_text("Driver not found.")
            except Exception:
                pass
            return STATE_INTERVIEW_INPUT
        from utils.drafts_db import DraftsDatabase

        drafts = DraftsDatabase(db.client)
        ok, purge_errors = purge_driver_everywhere(db, driver, drafts_db=drafts)
        nm = driver.get("driver_name") or "(unnamed)"
        if ok:
            lines = [
                f"🗑 Deleted: <b>{nm}</b>",
                "",
                "Removed from Issuer, Dispatch, and interview records.",
            ]
            if purge_errors:
                lines.append("")
                lines.append("Notes:")
                lines.extend(f"• {e}" for e in purge_errors[:5])
            try:
                await q.edit_message_text(
                    "\n".join(lines), parse_mode="HTML", reply_markup=None
                )
            except Exception:
                await msg.reply_text(f"🗑 Deleted: {nm}")
        else:
            err = purge_errors[0] if purge_errors else "Could not delete driver."
            await msg.reply_text(f"❌ {err}")
        return STATE_INTERVIEW_INPUT

    if data.startswith("drv_delx_"):
        short = data[len("drv_delx_"):]
        try:
            await q.edit_message_text("Delete cancelled.", reply_markup=None)
        except Exception:
            pass
        return STATE_INTERVIEW_INPUT

    if data.startswith("drv_dell_"):
        short = data[len("drv_dell_"):]
        driver = _resolve_driver_by_short(short)
        if not driver:
            try:
                await q.edit_message_text("Driver not found.")
            except Exception:
                pass
            return STATE_INTERVIEW_INPUT
        try:
            await q.edit_message_text(
                _driver_delete_confirm_text(driver),
                parse_mode="HTML",
                reply_markup=_driver_delete_confirm_keyboard(
                    short, back_to_profile=False
                ),
            )
        except Exception:
            pass
        return STATE_INTERVIEW_INPUT

    if data.startswith("drv_del_"):
        short = data[len("drv_del_"):]
        driver = _resolve_driver_by_short(short)
        if not driver:
            try:
                await q.edit_message_text("Driver not found.")
            except Exception:
                pass
            return STATE_INTERVIEW_INPUT
        try:
            await q.edit_message_text(
                _driver_delete_confirm_text(driver),
                parse_mode="HTML",
                reply_markup=_driver_delete_confirm_keyboard(short),
            )
        except Exception:
            pass
        return STATE_INTERVIEW_INPUT

    return STATE_INTERVIEW_INPUT


async def handle_driver_edit_value(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg or not _user_is_global_supervisor(user.id):
        return ConversationHandler.END

    did = context.user_data.get("drv_edit_id")
    field_key = context.user_data.get("drv_edit_field")
    short = context.user_data.get("drv_edit_short")
    if not did or field_key not in _DRV_EDIT_FIELDS:
        await msg.reply_text("No driver field selected.")
        return STATE_INTERVIEW_INPUT

    text = (msg.text or "").strip()
    column = _DRV_EDIT_FIELDS[field_key][0]
    label = _DRV_EDIT_FIELDS[field_key][1]
    new_value: Optional[str]
    if text == "-":
        new_value = None
    elif not text:
        await msg.reply_text("Empty value. Try again or /cancel.")
        return STATE_DRV_EDIT_VALUE
    else:
        new_value = text

    ok = db.update_driver(did, {column: new_value})
    if not ok:
        await msg.reply_text("❌ Update failed.")
        return STATE_INTERVIEW_INPUT

    for key in ("drv_edit_id", "drv_edit_field", "drv_edit_short"):
        context.user_data.pop(key, None)

    driver = db.get_driver_by_id(did) or {"id": did}
    await msg.reply_text(f"✅ {label} updated.")
    if short:
        await msg.reply_text(
            "\n".join(_driver_profile_lines(driver)),
            reply_markup=_driver_profile_keyboard(short),
        )
    return STATE_INTERVIEW_INPUT


async def cmd_set_training_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return ConversationHandler.END

    if _user_is_global_supervisor(user.id):
        await _show_training_menu(context, msg.chat_id)
        return STATE_TRAINING_MENU

    videos = shipments_db.list_training_videos()
    if not videos:
        legacy = shipments_db.get_bot_setting("training_video")
        if legacy and legacy.get("media_file_id"):
            videos = [{
                "media_kind": legacy.get("media_kind") or "video",
                "media_file_id": legacy["media_file_id"],
                "caption": legacy.get("caption"),
            }]

    if not videos:
        await msg.reply_text(
            "🎬 No training videos available yet.\n"
            "Check back soon — your supervisor will upload them."
        )
        return ConversationHandler.END

    await msg.reply_text(f"🎬 Training videos ({len(videos)}) — here you go 👇")
    await _send_training_video_to_chat(context, msg.chat_id)
    return ConversationHandler.END


async def handle_training_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if not q:
        return STATE_TRAINING_MENU
    await q.answer()
    user = update.effective_user
    if not user or not _user_is_global_supervisor(user.id):
        return ConversationHandler.END

    data = (q.data or "").strip()
    chat_id = q.message.chat_id if q.message else update.effective_chat.id
    msg_id = q.message.message_id if q.message else None

    if data == "train_close":
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await context.bot.send_message(chat_id=chat_id, text="Closed.")
        return ConversationHandler.END

    if data == "train_back":
        await _show_training_menu(context, chat_id, edit_message_id=msg_id)
        return STATE_TRAINING_MENU

    if data == "train_list":
        await _show_training_list(context, chat_id, edit_message_id=msg_id)
        return STATE_TRAINING_MENU

    if data == "train_add":
        try:
            await q.edit_message_text(
                "📤 Send the video now (video, animation, document, or photo).\n"
                "An optional caption in the same message will be saved with it.\n\n"
                "Send /cancel to abort."
            )
        except Exception:
            await context.bot.send_message(
                chat_id=chat_id,
                text="📤 Send the video now (video, animation, document, or photo).",
            )
        return STATE_SET_TRAINING_VIDEO

    if data.startswith("train_preview_"):
        vid = data[len("train_preview_"):]
        v = shipments_db.get_training_video(vid)
        if not v:
            await context.bot.send_message(chat_id=chat_id, text="Video not found.")
            return STATE_TRAINING_MENU
        kind = (v.get("media_kind") or "video").strip().lower()
        fid = v.get("media_file_id")
        cap = (v.get("caption") or "").strip() or None
        if fid:
            await _send_one_media(context, chat_id, kind=kind, file_id=fid, caption=cap)
        return STATE_TRAINING_MENU

    if data.startswith("train_remove_"):
        vid = data[len("train_remove_"):]
        ok = shipments_db.delete_training_video(vid)
        if not ok:
            await context.bot.send_message(chat_id=chat_id, text="❌ Could not remove.")
        await _show_training_list(context, chat_id, edit_message_id=msg_id)
        return STATE_TRAINING_MENU

    return STATE_TRAINING_MENU


async def handle_set_training_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.effective_message
    user = update.effective_user
    if not msg or not _user_is_global_supervisor(user.id):
        return ConversationHandler.END

    kind = None
    media_id = None
    cap = (msg.caption or "").strip() or None
    if msg.video:
        kind, media_id = "video", msg.video.file_id
    elif msg.animation:
        kind, media_id = "animation", msg.animation.file_id
    elif msg.document:
        kind, media_id = "document", msg.document.file_id
    elif msg.photo:
        kind, media_id = "photo", msg.photo[-1].file_id
    else:
        await msg.reply_text("Please send a video, animation, document, or photo.")
        return STATE_SET_TRAINING_VIDEO

    row = shipments_db.add_training_video(
        media_kind=kind,
        media_file_id=media_id,
        caption=cap,
        added_by_telegram_id=str(user.id),
    )
    if row:
        await msg.reply_text(
            f"✅ {_training_kind_icon(kind)} {kind.capitalize()} added. "
            "New hires will receive every saved video with their tracking info."
        )
    else:
        await msg.reply_text(
            "❌ Could not save (run database/migration_training_videos.sql in Supabase)."
        )
    await _show_training_menu(context, msg.chat_id)
    return STATE_TRAINING_MENU


async def handle_announce_schedule_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.effective_message.text or "").strip()
    dt = parse_user_datetime(text, Config.INTERVIEWER_TIMEZONE)
    if not dt:
        await update.effective_message.reply_text("Invalid date/time. Try again.")
        return STATE_ANNOUNCE_SCHEDULE_TIME
    context.user_data["announce_schedule_at"] = dt.isoformat()
    await update.effective_message.reply_text("Now send the announcement content (text/media):")
    return STATE_ANNOUNCE_SCHEDULE_CONTENT


async def handle_chat_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    req = update.chat_join_request
    if not req:
        return
    target_cid = _parse_chat_id(Config.DRIVER_CHANNEL_ID)
    if target_cid and _norm_chat_id(req.chat.id) != _norm_chat_id(target_cid):
        return
    try:
        await context.bot.approve_chat_join_request(
            chat_id=req.chat.id,
            user_id=req.from_user.id,
        )
        logger.info("Auto-approved join request from %s in %s", req.from_user.id, req.chat.id)
    except Exception as e:
        logger.warning("approve_chat_join_request failed: %s", e)
        return

    tid = str(req.from_user.id)
    interview = _resolve_hired_driver_interview(tid)
    if not interview:
        return
    await _deliver_hired_driver_onboarding(context, interview, source="channel_join")


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.effective_message.reply_text("Cancelled.")
    return ConversationHandler.END


def _wait_for_exclusive_polling(bot_token: str, max_wait: int = 120) -> bool:
    import time

    import requests as req

    api = f"https://api.telegram.org/bot{bot_token}"
    try:
        req.post(f"{api}/deleteWebhook", json={"drop_pending_updates": True}, timeout=5)
    except Exception:
        pass
    waited = 0
    backoff = 3
    while waited < max_wait:
        try:
            r = req.post(f"{api}/getUpdates", json={"timeout": 1, "limit": 1}, timeout=10)
            if r.status_code == 200:
                return True
            if r.status_code == 409:
                time.sleep(backoff)
                waited += backoff
                backoff = min(backoff + 2, 10)
                continue
            return True
        except Exception:
            time.sleep(backoff)
            waited += backoff
    return False


def main() -> None:
    global db
    logger.info("Krab Interviewer starting...")
    try:
        Config.validate()
    except ValueError as e:
        logger.error("Config: %s", e)
        sys.exit(1)

    db = Database()

    try:
        from api.server import start_in_background_thread

        start_in_background_thread(db)
        logger.info("FastAPI web API started (draft form + /api/health)")
    except Exception as e:
        on_render = bool(os.getenv("RENDER") or os.getenv("PORT"))
        if on_render:
            logger.error(
                "FastAPI is required on Render (healthCheckPath /api/health). Fix: %s",
                e,
                exc_info=True,
            )
            sys.exit(1)
        logger.warning("FastAPI web server not started: %s", e)

    token = Config.TELEGRAM_BOT_TOKEN
    if not _wait_for_exclusive_polling(token):
        logger.error("Could not acquire polling slot")
        sys.exit(1)

    application = Application.builder().token(token).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", cmd_start),
            CommandHandler("announce", cmd_announce),
            CommandHandler("announce_schedule", cmd_announce_schedule),
            CommandHandler("training", cmd_set_training_video),
            CommandHandler("setemail", cmd_setemail),
            CallbackQueryHandler(handle_driver_callbacks, pattern=r"^drv_ef_"),
        ],
        states={
            STATE_INTERVIEW_INPUT: [
                MessageHandler(
                    filters.PHOTO | filters.Document.ALL | (filters.TEXT & ~filters.COMMAND),
                    _process_interview_input,
                ),
                CallbackQueryHandler(
                    handle_interview_callbacks,
                    pattern=r"^(int_|int_ef_)",
                ),
                CallbackQueryHandler(
                    handle_shipment_callbacks,
                    pattern=r"^ship_",
                ),
            ],
            STATE_AWAIT_TRACKING: [
                MessageHandler(
                    filters.PHOTO | filters.Document.ALL | (filters.TEXT & ~filters.COMMAND),
                    handle_tracking_input,
                ),
                CallbackQueryHandler(
                    handle_shipment_callbacks,
                    pattern=r"^ship_",
                ),
            ],
            STATE_TRAINING_MENU: [
                CallbackQueryHandler(handle_training_callbacks, pattern=r"^train_"),
            ],
            STATE_DRV_EDIT_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_driver_edit_value),
                CallbackQueryHandler(handle_driver_callbacks, pattern=r"^drv_"),
            ],
            STATE_SET_TRAINING_VIDEO: [
                MessageHandler(
                    filters.PHOTO | filters.VIDEO | filters.ANIMATION | filters.Document.ALL,
                    handle_set_training_video,
                ),
                CallbackQueryHandler(handle_training_callbacks, pattern=r"^train_"),
            ],
            STATE_INT_SCHEDULE_APPT: [
                CallbackQueryHandler(handle_calendar_callbacks, pattern=r"^cal_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_schedule_appt_text),
            ],
            STATE_AWAIT_SUPERVISOR_EMAIL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_supervisor_email),
            ],
            STATE_INT_UPLOAD_LICENSE: [
                MessageHandler(filters.PHOTO | filters.Document.ALL, handle_upload_license),
            ],
            STATE_INT_EDIT_FIELD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_edit_field_text),
            ],
            STATE_ANNOUNCE_WAIT: [
                MessageHandler(
                    (filters.TEXT | filters.PHOTO | filters.VIDEO | filters.Document.ALL) & ~filters.COMMAND,
                    handle_announce_message,
                ),
            ],
            STATE_ANNOUNCE_SCHEDULE_TIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_announce_schedule_time),
            ],
            STATE_ANNOUNCE_SCHEDULE_CONTENT: [
                MessageHandler(
                    (filters.TEXT | filters.PHOTO | filters.VIDEO | filters.Document.ALL) & ~filters.COMMAND,
                    handle_announce_message,
                ),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_cmd)],
        allow_reentry=True,
    )

    application.add_handler(conv)
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("interviews", cmd_interviews))
    application.add_handler(CommandHandler("applications", cmd_interviews))
    application.add_handler(CommandHandler("shipments", cmd_shipments))
    application.add_handler(CommandHandler("drivers", cmd_drivers))
    application.add_handler(CommandHandler("delete", cmd_delete))
    application.add_handler(CommandHandler("open", cmd_open))
    application.add_handler(
        CallbackQueryHandler(handle_interview_callbacks, pattern=r"^(int_|int_ef_|int_open_|int_drv_)"),
    )
    application.add_handler(
        CallbackQueryHandler(handle_shipment_callbacks, pattern=r"^ship_"),
    )
    application.add_handler(
        CallbackQueryHandler(handle_driver_callbacks, pattern=r"^drv_"),
    )
    application.add_handler(ChatJoinRequestHandler(handle_chat_join_request))

    if application.job_queue:
        application.job_queue.run_repeating(check_pending_jobs, interval=30, first=15)
        application.job_queue.run_repeating(
            paper_girl_receipt_reminder_job,
            interval=_PAPER_GIRL_REMINDER_INTERVAL_SEC,
            first=_PAPER_GIRL_REMINDER_INTERVAL_SEC,
            name="paper_girl_receipt_reminder",
        )
        application.job_queue.run_repeating(
            abandoned_drafts_sweep_job,
            interval=10 * 60,
            first=60,
            name="abandoned_drafts_sweep",
        )
        _startup_reenqueue_jobs(application)

    from api.bot_bridge import register_application, set_hire_job_callback

    set_hire_job_callback(job_hire_side_effects)
    register_application(application)

    logger.info("Polling...")
    # Render native Python 3.12+ / 3.14: no default asyncio loop on MainThread; PTB needs one.
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
