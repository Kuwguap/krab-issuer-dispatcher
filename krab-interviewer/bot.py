"""
Krab Interviewer — Telegram bot for driver interviews, appointments, announcements, and hire onboarding.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re
import sys
import uuid as _uuid_mod
import calendar as _calendar_mod
from datetime import date as _date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytz
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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
    "10. 💰 Payment Method\n"
    "11. ⚒️ Profession skill\n"
    "12. 💬 Telegram ID\n\n"
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


def _user_is_paper_girl(user_id) -> bool:
    pg = _parse_chat_id(Config.PAPER_GIRL_TELEGRAM_ID)
    if pg is None:
        return False
    return _norm_chat_id(user_id) == _norm_chat_id(pg)


def _user_can_manage_shipments(user_id) -> bool:
    return _user_is_global_supervisor(user_id) or _user_is_paper_girl(user_id)


def _build_usps_tracking_url(tracking_number: str) -> str:
    tn = re.sub(r"\D", "", tracking_number or "")
    base = (Config.USPS_TRACK_URL_BASE or "").strip()
    if not base:
        base = "https://tools.usps.com/go/TrackConfirmAction?tLabels="
    return f"{base}{tn}"


def _format_paper_girl_ship_request(shipment: dict) -> str:
    qty = int(shipment.get("quantity") or Config.DEFAULT_PAPER_QTY)
    name = (shipment.get("driver_name") or "Driver").strip()
    addr = (shipment.get("driver_address") or "-").strip()
    phone = (shipment.get("driver_phone") or "-").strip()
    return (
        "📦 New Driver Shipment\n\n"
        f"Please ship {qty} papers today to:\n\n"
        f"👤 {name}\n"
        f"📍 {addr}\n"
        f"📞 {phone}\n\n"
        "🧾 Please upload receipt after shipping\n"
        "⚡ Send ASAP — First thing in the morning latest!"
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


def _driver_display_name(interview: dict) -> str:
    """Prefer full_name; fall back to first_name."""
    full = (interview.get("full_name") or "").strip()
    if full:
        return full
    fn = (interview.get("first_name") or "").strip()
    return fn or "Driver"


def _format_shipments_list(rows: List[dict]) -> str:
    if not rows:
        return "📦 No shipments yet."

    pending = [
        r for r in rows
        if (r.get("status") or "") in ("awaiting_tracking", "tracking_received")
    ]
    done = [r for r in rows if (r.get("status") or "") == "driver_notified"]
    cancelled = [r for r in rows if (r.get("status") or "") == "cancelled"]

    def fmt(r: dict) -> str:
        name = (r.get("driver_name") or "?")[:24]
        qty = r.get("quantity", "?")
        tr = (r.get("tracking_number") or "-")[:22]
        return f"• {name} | {qty} papers | {tr}"

    parts = ["📦 **Shipments**"]
    if pending:
        parts.append("\n⏳ **Pending**")
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


async def _send_training_video_to_chat(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    *,
    caption_override: Optional[str] = None,
) -> bool:
    setting = shipments_db.get_bot_setting("training_video")
    if not setting or not setting.get("media_file_id"):
        return False
    kind = (setting.get("media_kind") or "video").strip().lower()
    fid = setting["media_file_id"]
    cap = caption_override or (setting.get("caption") or "").strip() or None
    try:
        if kind == "photo":
            await context.bot.send_photo(chat_id=chat_id, photo=fid, caption=cap)
        elif kind == "document":
            await context.bot.send_document(chat_id=chat_id, document=fid, caption=cap)
        elif kind == "animation":
            await context.bot.send_animation(chat_id=chat_id, animation=fid, caption=cap)
        else:
            await context.bot.send_video(chat_id=chat_id, video=fid, caption=cap)
        return True
    except Exception as e:
        logger.warning("send training video to %s: %s", chat_id, e)
        return False


async def _create_and_notify_paper_girl(
    context: ContextTypes.DEFAULT_TYPE,
    interview: dict,
    *,
    created_by_telegram_id: str,
) -> tuple[Optional[dict], Optional[str]]:
    """Create shipment row and DM Paper Girl. Returns (shipment, warning)."""
    fn = _driver_display_name(interview)
    addr = (interview.get("mailing_address") or "").strip()
    phone = (interview.get("phone_number") or "").strip()
    em = (interview.get("email") or "").strip()
    tid = (interview.get("telegram_id") or "").strip()
    iid = interview.get("id")

    shipment = shipments_db.create_shipment(
        interview_id=iid,
        driver_name=fn or "Driver",
        driver_address=addr or None,
        driver_phone=phone or None,
        driver_email=em or None,
        driver_telegram_id=tid or None,
        quantity=Config.DEFAULT_PAPER_QTY,
        created_by_telegram_id=created_by_telegram_id,
    )
    if not shipment:
        return None, "Could not create paper_shipments row (run migration_paper_shipments.sql?)"

    pg_cid = _parse_chat_id(Config.PAPER_GIRL_TELEGRAM_ID)
    if not pg_cid:
        return shipment, "PAPER_GIRL_TELEGRAM_ID is not set — shipment saved but Paper Girl was not notified."

    try:
        await context.bot.send_message(
            chat_id=pg_cid,
            text=_format_paper_girl_ship_request(shipment),
            reply_markup=_paper_girl_ship_keyboard(shipment["id"]),
        )
    except Exception as e:
        logger.error("notify paper girl: %s", e)
        return shipment, f"Paper Girl DM failed: {e}"

    return shipment, None


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
        "• Watch the training video below 👇"
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
            if not await _send_training_video_to_chat(context, driver_tid):
                warnings.append("Training video not configured (/set_training_video)")
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
        f"Tracking: `{tracking}`\n"
        f"USPS: {usps_url}"
    )
    if warnings:
        summary += "\n\n⚠️ " + "\n".join(warnings)

    for cid in _global_supervisory_chat_ids():
        try:
            await context.bot.send_message(chat_id=cid, text=summary, parse_mode="Markdown")
        except Exception:
            pass
    pg_cid = _parse_chat_id(Config.PAPER_GIRL_TELEGRAM_ID)
    if pg_cid:
        try:
            await context.bot.send_message(chat_id=pg_cid, text=summary, parse_mode="Markdown")
        except Exception:
            pass

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


async def paper_girl_receipt_reminder_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Every 6 hours, nudge Paper Girl about shipments still awaiting tracking."""
    pg_cid = _parse_chat_id(Config.PAPER_GIRL_TELEGRAM_ID)
    if not pg_cid:
        return
    pending = [
        s for s in shipments_db.list_shipments(50)
        if (s.get("status") or "") == "awaiting_tracking"
    ]
    if not pending:
        return
    lines = ["⏰ Receipt reminder — shipments still awaiting tracking:\n"]
    for s in pending:
        name = (s.get("driver_name") or "?")[:30]
        qty = s.get("quantity", "?")
        lines.append(f"• {name} — {qty} papers")
    lines.append("\nUpload the USPS receipt photo or tracking number when ready 🧾")
    try:
        await context.bot.send_message(chat_id=pg_cid, text="\n".join(lines))
    except Exception as e:
        logger.warning("paper girl receipt reminder failed: %s", e)


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

    intro = (
        f"👋 Welcome to Krab Interviewer!\n\n"
        f"Complete your driver onboarding questionnaire below.\n"
        f"When you're hired, start taking leads with @{dispatch}.\n\n"
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
    buttons = []
    for r in rows[:20]:
        sid = _short_uuid(r["id"])
        name = _display_val(r.get("first_name") or r.get("telegram_username"))
        phone = _display_val(r.get("phone_number"))[:14]
        st = r.get("status", "?")
        buttons.append([
            InlineKeyboardButton(
                f"{name} | {phone} | {st}",
                callback_data=f"int_open_{sid}",
            ),
        ])
    await update.effective_message.reply_text(
        "📋 Interviews — tap to open:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cmd_shipments(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _user_can_manage_shipments(update.effective_user.id):
        return
    await _reply_shipments_list(update.effective_message)


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


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return

    text = (
        "🤖 <b>Krab Interviewer — Commands</b>\n\n"
        "1. ✅ /start — begin driver questionnaire (or supervisor hire menu)\n"
        "2. ❌ /cancel — cancel the current flow\n"
        "3. 🎤 /interviews — list recent interviews (tap to open)\n"
        "4. 🚗 /drivers — list Issuer drivers (tap for profile)\n"
        "5. 📂 /open &lt;id&gt; — open one interview by id\n"
        "6. 📢 /announce — post next message to drivers channel now\n"
        "7. 🗓️📢 /announce_schedule — schedule a channel post\n"
        "8. 🎥📚 /set_training_video — save the \"how to print paper\" video for new hires"
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
        await update.effective_message.reply_text("Usage: /open <interview_id>")
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
    await message.reply_text(_format_interview_understanding(interview))
    lic = (interview.get("drivers_license_file_url") or "").strip()
    if lic:
        await message.reply_text(f"🪪 License file:\n{lic}")


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
        drivers = db.get_all_drivers()
        driver = None
        for d in drivers:
            if _short_uuid(d["id"]) == short or str(d["id"]).startswith(short):
                driver = d
                break
        if not driver:
            await query.message.reply_text("Driver not found.")
            return STATE_INTERVIEW_INPUT
        lines = [
            f"🚗 {driver.get('driver_name', '?')}",
            f"📱 {driver.get('phone_number') or '-'}",
            f"💬 Telegram ID: {driver.get('driver_telegram_id') or '-'}",
        ]
        await query.message.reply_text("\n".join(lines))
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
        if not _user_is_global_supervisor(user.id):
            await query.message.reply_text("⛔ Only supervisors can hire.")
            return STATE_INTERVIEW_INPUT
        interview = db.get_interview_by_id(iid)
        if not interview:
            await query.message.reply_text("Interview not found.")
            return STATE_INTERVIEW_INPUT
        driver_name = _driver_display_name(interview)
        if not (interview.get("full_name") or "").strip() and not (interview.get("first_name") or "").strip():
            un = (interview.get("telegram_username") or "").lstrip("@").strip()
            derived = un.split()[0] if un else ""
            if derived:
                db.update_interview(iid, {"first_name": derived})
                interview["first_name"] = derived
                driver_name = derived
        tid = (interview.get("telegram_id") or "").strip()
        em = (interview.get("email") or "").strip()
        if driver_name == "Driver" or not tid or not em:
            await query.message.reply_text(
                "❌ Hire requires full name, Telegram ID, and email. Use ✍️ Edit to fill them."
            )
            return STATE_INTERVIEW_INPUT
        ok_d, err_d = db.create_driver(driver_name, tid, interview.get("phone_number"))
        ok_r, err_r = recipients_db.add_recipient(driver_name, em)
        errors = []
        if not ok_d and err_d:
            errors.append(f"Issuer drivers: {err_d}")
        if not ok_r and err_r:
            errors.append(f"Dispatch recipients: {err_r}")
        db.update_interview(iid, {"status": "hired"})
        interview = db.get_interview_by_id(iid) or interview
        interview["status"] = "hired"
        await _clear_pending_prompts(context, chat_id)
        await _refresh_understanding_card(context, interview)

        welcome_first, _ = ai_vision.split_full_name(driver_name)
        if not welcome_first:
            welcome_first = (interview.get("first_name") or "").strip() or driver_name.split()[0]

        channel_invite = await _create_driver_channel_invite(context, driver_name)
        try:
            await _add_driver_to_channel(context, tid)
        except Exception as e:
            logger.warning("Could not auto-add driver %s to channel: %s", tid, e)

        ship, ship_warn = await _create_and_notify_paper_girl(
            context,
            interview,
            created_by_telegram_id=str(user.id),
        )

        username_display = (interview.get("telegram_username") or "").strip()
        if username_display and not username_display.startswith("@"):
            username_display = "@" + username_display
        if not username_display:
            username_display = driver_name if driver_name != "Driver" else "-"

        issuer_bot = Config.KRAB_ISSUER_BOT_USERNAME.lstrip("@")
        dispatch_bot = Config.KRAB_DISPATCH_BOT_USERNAME.lstrip("@")
        channel_link = (Config.DRIVER_CHANNEL_LINK or "").strip() or "https://t.me/TriStateTags"
        pg_handle = (Config.PAPER_GIRL_USERNAME or "").strip().lstrip("@")
        pg_tag = f"@{pg_handle} " if pg_handle else ""
        qty = Config.DEFAULT_PAPER_QTY

        hire_msg = (
            "🎉 DRIVER HIRED SUCCESSFULLY ✅\n\n"
            f"👤 USERNAME: {username_display}\n"
            f"📧 EMAIL: {em}\n"
            f"ℹ️CHATID:{tid}\n\n"
            "🚀 Added to:\n"
            f"• @{issuer_bot}\n"
            f"• @{dispatch_bot}\n"
            f"• {channel_link}\n\n"
            f"📦 {qty} papers are now being prepared & shipped by papergirl "
            f"{pg_tag}please push & follow up\n\n"
            "📬 Tracking number coming shortly…\n"
            "⚡ Driver is now officially ACTIVE & ready to receive leads!"
        )

        await _post_to_driver_channel(context, kind="text", body=hire_msg, media_file_id=None)

        welcome_handle = (
            username_display
            if username_display and username_display.startswith("@")
            else f"@{welcome_first}"
        )
        driver_dm = (
            f"🎉 Welcome to the Team, {welcome_handle}! 🚗🔥\n\n"
            "You’re officially hired and now part of the family 💪\n"
            "Let’s get money, move fast, and serve clients together!\n\n"
            f"📲 Start receiving leads now at @{dispatch_bot}\n"
            "🔔 Keep notifications ON so you never miss a lead.\n\n"
            "🚀 Welcome aboard — let’s get to work!"
        )
        join_kb = None
        if channel_invite:
            channel_btn_label = "🔗 Join @TriStateTags"
            link = (Config.DRIVER_CHANNEL_LINK or "").strip()
            if link and "t.me/" in link:
                handle = link.rstrip("/").split("/")[-1]
                if handle:
                    channel_btn_label = f"🔗 Join @{handle.lstrip('@')}"
            join_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(channel_btn_label, url=channel_invite)],
            ])
        try:
            await context.bot.send_message(
                chat_id=int(tid),
                text=driver_dm,
                reply_markup=join_kb,
            )
        except Exception as e:
            logger.warning("Driver welcome DM failed: %s", e)

        warnings_lines: List[str] = []
        if errors:
            warnings_lines.extend(errors)
        if ship_warn:
            warnings_lines.append(ship_warn)
        if not channel_invite:
            warnings_lines.append("Channel invite link not created (check DRIVER_CHANNEL_ID + bot admin rights).")

        sup_lines = [hire_msg]
        if ship:
            sup_lines.append(f"\n📦 Paper shipment queued ({qty} papers).")
        if warnings_lines:
            sup_lines.append("\n⚠️ Warnings:\n" + "\n".join(f"• {w}" for w in warnings_lines))
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
        if (shipment.get("status") or "") == "driver_notified":
            if query.message:
                await query.message.reply_text(
                    f"Already notified for **{shipment.get('driver_name')}** "
                    f"(tracking: {shipment.get('tracking_number') or '-'}).",
                    parse_mode="Markdown",
                )
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


async def cmd_set_training_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _user_is_global_supervisor(update.effective_user.id):
        return ConversationHandler.END
    await update.effective_message.reply_text(
        "🎬 Training video setup\n\n"
        "Send the video now (video, animation, or document).\n"
        "Optional caption in the same message is saved for new hires.\n\n"
        "This video is auto-sent to each driver when they receive tracking info.\n"
        "Use /announce to broadcast to the drivers channel anytime."
    )
    return STATE_SET_TRAINING_VIDEO


async def handle_set_training_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.effective_message
    user = update.effective_user
    if not msg or not _user_is_global_supervisor(user.id):
        return ConversationHandler.END

    kind = "text"
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

    ok = shipments_db.set_bot_setting(
        "training_video",
        value="stored",
        media_kind=kind,
        media_file_id=media_id,
        caption=cap,
    )
    if ok:
        await msg.reply_text("✅ Training video saved. New hires will receive it with tracking info.")
    else:
        await msg.reply_text("❌ Could not save (run migration_paper_shipments.sql?).")
    return ConversationHandler.END


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
            CommandHandler("set_training_video", cmd_set_training_video),
            CommandHandler("setemail", cmd_setemail),
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
            STATE_SET_TRAINING_VIDEO: [
                MessageHandler(
                    filters.PHOTO | filters.VIDEO | filters.ANIMATION | filters.Document.ALL,
                    handle_set_training_video,
                ),
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
    application.add_handler(CommandHandler("shipments", cmd_shipments))
    application.add_handler(CommandHandler("drivers", cmd_drivers))
    application.add_handler(CommandHandler("open", cmd_open))
    application.add_handler(
        CallbackQueryHandler(handle_interview_callbacks, pattern=r"^(int_|int_ef_|int_open_|int_drv_)"),
    )
    application.add_handler(
        CallbackQueryHandler(handle_shipment_callbacks, pattern=r"^ship_"),
    )
    application.add_handler(ChatJoinRequestHandler(handle_chat_join_request))

    if application.job_queue:
        application.job_queue.run_repeating(check_pending_jobs, interval=30, first=15)
        application.job_queue.run_repeating(
            paper_girl_receipt_reminder_job,
            interval=6 * 60 * 60,
            first=6 * 60 * 60,
            name="paper_girl_receipt_reminder",
        )
        _startup_reenqueue_jobs(application)

    logger.info("Polling...")
    # Render native Python 3.12+ / 3.14: no default asyncio loop on MainThread; PTB needs one.
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
