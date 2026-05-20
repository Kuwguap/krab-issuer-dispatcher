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
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pytz
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

from config import Config
from utils import ai_vision
from utils.ai_vision import INTERVIEW_FIELD_KEYS, AIVisionQuotaError
from utils.database import Database
from utils import recipients_db
from utils.time_utils import format_dt_display, parse_user_datetime

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

db = Database()

# Conversation states
STATE_INTERVIEW_INPUT = 1
STATE_INT_EDIT_FIELD = 2
STATE_INT_SCHEDULE_APPT = 3
STATE_INT_UPLOAD_LICENSE = 4
STATE_ANNOUNCE_WAIT = 5
STATE_ANNOUNCE_SCHEDULE_TIME = 6
STATE_ANNOUNCE_SCHEDULE_CONTENT = 7

INTERVIEW_QUESTIONNAIRE_PROMPT = (
    "Enter driver interview call info below:\n"
    "Photo or text data parse\n"
    " 1. ⏳ Work Commitment\n"
    " 2. 📱 Phone Number\n"
    " 3. 📧 Email Address\n"
    " 4. 🏠 Mailing Address\n"
    " 5. 🪪 Driver's License (send to text/email)\n"
    " 6. 💬 Telegram Username (must download app)\n"
    " 7. 🚨 Emergency Contact\n"
    " 8. 👥 Referral (if any)\n"
    " 9. 💰 Payment Method\n"
    "10. ⚒️ Profession skill\n"
    "11. 💬 Telegram ID"
)

FIELD_LABELS = {
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
    fn = _display_val(interview.get("first_name"))
    if fn != "-":
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
    ref = (interview.get("first_name") or interview.get("telegram_username") or "Driver")
    when = format_dt_display(
        datetime.fromisoformat(str(appt["scheduled_at"]).replace("Z", "+00:00")),
        Config.INTERVIEWER_TIMEZONE,
    )
    msg = (
        f"📆 Interview appointment reminder\n\n"
        f"Driver: {ref}\n"
        f"Time: {when}\n"
        f"Reference: {str(interview.get('id', ''))[:8]}"
    )
    targets = set()
    creator = _parse_chat_id(appt.get("created_by_telegram_id"))
    if creator:
        targets.add(creator)
    tid = _parse_chat_id(interview.get("telegram_id"))
    if tid:
        targets.add(tid)
    for chat_id in targets:
        try:
            await context.bot.send_message(chat_id=chat_id, text=msg)
        except Exception as e:
            logger.warning("appointment reminder to %s: %s", chat_id, e)
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
            "Hi congrats on hiring a new driver !\n"
            "————————————————————\n"
            "Are you ready to hire the driver now or schedule appointment time for interview call ?"
        )
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Now", callback_data="int_now"),
                InlineKeyboardButton("Appointment", callback_data="int_appt"),
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
        context.user_data["is_supervisor_created"] = True
        if query.message:
            await query.message.reply_text(INTERVIEW_QUESTIONNAIRE_PROMPT)
        return STATE_INTERVIEW_INPUT

    if data == "int_appt":
        await query.message.reply_text(
            "📆 Send the appointment date and time for the interview call "
            f"(e.g. 2026-05-25 14:30 {Config.INTERVIEWER_TIMEZONE.split('/')[-1]}):"
        )
        context.user_data["schedule_only_before_interview"] = True
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
            sent = await query.message.reply_text(
                f"📆 Send date and time (e.g. 2026-05-25 14:30):"
            )
            _track_pending_prompt(context, sent.message_id)
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
        fn = (interview.get("first_name") or "").strip()
        if not fn:
            un = (interview.get("telegram_username") or "").lstrip("@").strip()
            fn = un.split()[0] if un else ""
            if fn:
                db.update_interview(iid, {"first_name": fn})
                interview["first_name"] = fn
        tid = (interview.get("telegram_id") or "").strip()
        em = (interview.get("email") or "").strip()
        if not fn or not tid or not em:
            await query.message.reply_text(
                "❌ Hire requires first name, Telegram ID, and email. Use ✍️ Edit to fill them."
            )
            return STATE_INTERVIEW_INPUT
        ok_d, err_d = db.create_driver(fn, tid, interview.get("phone_number"))
        ok_r, err_r = recipients_db.add_recipient(fn, em)
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
        welcome = (
            f"🎉 Welcome {fn}! You've been hired.\n"
            f"Start taking leads with @{Config.KRAB_DISPATCH_BOT_USERNAME.lstrip('@')}."
        )
        await _post_to_driver_channel(context, kind="text", body=welcome, media_file_id=None)
        try:
            await context.bot.send_message(chat_id=int(tid), text=welcome)
        except Exception:
            pass
        if errors:
            await query.message.reply_text("⚠️ Hired with warnings:\n" + "\n".join(errors))
        else:
            await query.message.reply_text("✅ Driver hired and added to Issuer + Dispatch.")
        return STATE_INTERVIEW_INPUT

    return STATE_INTERVIEW_INPUT


async def handle_schedule_appt_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.effective_message
    user = update.effective_user
    text = (msg.text or "").strip()
    dt = parse_user_datetime(text, Config.INTERVIEWER_TIMEZONE)
    if not dt:
        await msg.reply_text("Could not parse date/time. Try again (e.g. 2026-05-25 14:30).")
        return STATE_INT_SCHEDULE_APPT

    await _clear_pending_prompts(context, msg.chat_id)
    try:
        await msg.delete()
    except Exception:
        pass

    if context.user_data.pop("schedule_only_before_interview", False):
        await msg.reply_text(
            f"📆 Appointment noted for {format_dt_display(dt, Config.INTERVIEWER_TIMEZONE)}.\n"
            "Tap **Now** when ready to enter driver info, or send questionnaire data."
        )
        context.user_data["pending_pre_interview_appt"] = dt.isoformat()
        return STATE_INTERVIEW_INPUT

    iid = context.user_data.get("active_interview_id")
    if not iid:
        await msg.reply_text("No active interview. Use /start")
        return ConversationHandler.END

    appt = db.create_appointment(iid, dt, str(user.id))
    if appt:
        db.update_interview(iid, {"status": "scheduled"})
        _schedule_appointment_job(context.application, appt)
    interview = db.get_interview_by_id(iid)
    if interview:
        interview["_appointment_display"] = format_dt_display(dt, Config.INTERVIEWER_TIMEZONE)
        await _refresh_understanding_card(context, interview)
    return STATE_INTERVIEW_INPUT


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


async def handle_announce_schedule_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.effective_message.text or "").strip()
    dt = parse_user_datetime(text, Config.INTERVIEWER_TIMEZONE)
    if not dt:
        await update.effective_message.reply_text("Invalid date/time. Try again.")
        return STATE_ANNOUNCE_SCHEDULE_TIME
    context.user_data["announce_schedule_at"] = dt.isoformat()
    await update.effective_message.reply_text("Now send the announcement content (text/media):")
    return STATE_ANNOUNCE_SCHEDULE_CONTENT


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
    logger.info("Krab Interviewer starting...")
    try:
        Config.validate()
    except ValueError as e:
        logger.error("Config: %s", e)
        sys.exit(1)

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
            ],
            STATE_INT_SCHEDULE_APPT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_schedule_appt_text),
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
    application.add_handler(CommandHandler("interviews", cmd_interviews))
    application.add_handler(CommandHandler("drivers", cmd_drivers))
    application.add_handler(CommandHandler("open", cmd_open))
    application.add_handler(
        CallbackQueryHandler(handle_interview_callbacks, pattern=r"^(int_|int_ef_|int_open_|int_drv_)"),
    )

    if application.job_queue:
        application.job_queue.run_repeating(check_pending_jobs, interval=30, first=15)
        _startup_reenqueue_jobs(application)

    logger.info("Polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
