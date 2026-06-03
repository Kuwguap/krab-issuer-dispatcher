"""Public interview draft API (web form)."""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Cookie, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from api.deps import DRAFT_COOKIE, current_ip_hash, get_db, get_drafts_db
from api.notify import notify_supervisors_new_web_interview
from config import Config
from utils.ai_vision import (
    AIVisionQuotaError,
    INTERVIEW_FIELD_KEYS,
    extract_interview_from_image,
    extract_interview_from_text,
    normalize_interview_data,
)
from utils.drafts_db import DraftsDatabase, new_draft_cookie
from utils.database import Database
from utils.telegram_resolve import (
    normalize_telegram_username_display,
    resolve_telegram_id_for_username,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/interview", tags=["interview"])

WEB_CREATED_BY = "web"
REQUIRED_SUBMIT_KEYS = [
    "full_name",
    "work_commitment",
    "phone_number",
    "email",
    "mailing_address",
    "drivers_license_id",
    "telegram_username",
    "emergency_contact",
    "payment_method",
    "profession_skill",
]


class DraftPatchBody(BaseModel):
    payload: Dict[str, Any] = Field(default_factory=dict)


class ResolveTelegramBody(BaseModel):
    username: str = ""


class ParseTextBody(BaseModel):
    text: str = ""


def _mime_from_upload(file: UploadFile) -> str:
    ct = (file.content_type or "").lower()
    if ct.startswith("image/"):
        return ct
    name = (file.filename or "").lower()
    if name.endswith(".png"):
        return "image/png"
    if name.endswith(".webp"):
        return "image/webp"
    if name.endswith(".gif"):
        return "image/gif"
    if name.endswith(".heic") or name.endswith(".heif"):
        return "image/heic"
    return "image/jpeg"


def _merge_extracted_payload(existing: Dict[str, Any], extracted: Dict[str, str]) -> Dict[str, Any]:
    merged = dict(existing or {})
    for key in INTERVIEW_FIELD_KEYS:
        val = (extracted.get(key) or "").strip()
        if val:
            merged[key] = val
    return merged


def _apply_telegram_resolution(merged: Dict[str, Any], db: Database) -> Dict[str, Any]:
    """When username is present, resolve numeric telegram_id into payload."""
    un_raw = (merged.get("telegram_username") or "").strip()
    if not un_raw:
        return merged
    display = normalize_telegram_username_display(un_raw)
    if display:
        merged["telegram_username"] = display
    tid, _source, _msg = resolve_telegram_id_for_username(db, un_raw)
    if tid:
        merged["telegram_id"] = tid
    return merged


def _cookie_response(data: dict, draft_id: str, status_code: int = 200) -> JSONResponse:
    resp = JSONResponse(content=data, status_code=status_code)
    resp.set_cookie(
        key=DRAFT_COOKIE,
        value=draft_id,
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 365,
        secure=bool((Config.KRAB_PUBLIC_BASE_URL or "").startswith("https")),
    )
    return resp


def _empty_val(v: Any) -> bool:
    s = (str(v) if v is not None else "").strip()
    return not s or s == "-"


def _verify_draft_access(
    draft: Optional[dict],
    draft_id: str,
    ip_hash: str,
    cookie_id: Optional[str],
) -> dict:
    if not draft or str(draft.get("id")) != str(draft_id):
        raise HTTPException(status_code=404, detail="Draft not found")
    if draft.get("ip_hash") != ip_hash:
        if not cookie_id or str(cookie_id) != str(draft_id):
            raise HTTPException(status_code=403, detail="Draft access denied")
    return draft


@router.post("/draft")
async def create_or_resume_draft(
    request: Request,
    ip_hash: str = Depends(current_ip_hash),
    krab_draft_id: Optional[str] = Cookie(default=None),
    drafts: DraftsDatabase = Depends(get_drafts_db),
):
    ua = (request.headers.get("user-agent") or "")[:500]
    existing = drafts.get_by_ip_hash(ip_hash)

    if not existing and krab_draft_id:
        by_cookie = drafts.get_by_id(krab_draft_id)
        if by_cookie and by_cookie.get("ip_hash") == ip_hash:
            existing = by_cookie

    if existing:
        drafts.touch(str(existing["id"]))
        if existing.get("status") == "submitted":
            payload = existing.get("payload") or {}
            inv_id = existing.get("submitted_interview_id")
            return _cookie_response(
                {
                    "draftId": str(existing["id"]),
                    "payload": payload,
                    "alreadySubmitted": True,
                    "submittedInterviewId": str(inv_id) if inv_id else None,
                    "driversLicenseFileUrl": existing.get("drivers_license_file_url"),
                },
                str(existing["id"]),
            )
        return _cookie_response(
            {
                "draftId": str(existing["id"]),
                "payload": existing.get("payload") or {},
                "alreadySubmitted": False,
                "driversLicenseFileUrl": existing.get("drivers_license_file_url"),
            },
            str(existing["id"]),
        )

    cookie_token = new_draft_cookie()
    row = drafts.create(ip_hash=ip_hash, draft_cookie=cookie_token, user_agent=ua)
    if not row:
        raise HTTPException(status_code=500, detail="Could not create draft")
    return _cookie_response(
        {
            "draftId": str(row["id"]),
            "payload": {},
            "alreadySubmitted": False,
            "driversLicenseFileUrl": None,
        },
        str(row["id"]),
        status_code=201,
    )


@router.patch("/draft/{draft_id}")
async def patch_draft(
    draft_id: str,
    body: DraftPatchBody,
    ip_hash: str = Depends(current_ip_hash),
    krab_draft_id: Optional[str] = Cookie(default=None),
    drafts: DraftsDatabase = Depends(get_drafts_db),
    db: Database = Depends(get_db),
):
    draft = drafts.get_by_id(draft_id)
    draft = _verify_draft_access(draft, draft_id, ip_hash, krab_draft_id)
    if draft.get("status") == "submitted":
        raise HTTPException(status_code=409, detail="Application already submitted")
    merged = dict(draft.get("payload") or {})
    for k, v in (body.payload or {}).items():
        if k in INTERVIEW_FIELD_KEYS or k == "first_name":
            merged[k] = v
    merged = _apply_telegram_resolution(merged, db)
    if not drafts.update(draft_id, payload=merged):
        raise HTTPException(status_code=500, detail="Could not save draft")
    return {"ok": True, "payload": merged}


@router.post("/draft/{draft_id}/license")
async def upload_license(
    draft_id: str,
    file: UploadFile = File(...),
    ip_hash: str = Depends(current_ip_hash),
    krab_draft_id: Optional[str] = Cookie(default=None),
    drafts: DraftsDatabase = Depends(get_drafts_db),
    db: Database = Depends(get_db),
):
    draft = drafts.get_by_id(draft_id)
    draft = _verify_draft_access(draft, draft_id, ip_hash, krab_draft_id)
    if draft.get("status") == "submitted":
        raise HTTPException(status_code=409, detail="Application already submitted")

    data = await file.read()
    if not data or len(data) > 12 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Invalid or too large file")

    url = db.upload_driver_license_to_storage(draft_id, data, file.filename or "license.jpg")
    if not url:
        raise HTTPException(status_code=500, detail="License upload failed")

    merged = dict(draft.get("payload") or {})
    parsed = False
    if Config.is_ai_configured():
        try:
            extracted = extract_interview_from_image(data, _mime_from_upload(file))
            merged = _merge_extracted_payload(merged, extracted)
            merged = _apply_telegram_resolution(merged, db)
            parsed = any((merged.get(k) or "").strip() for k in INTERVIEW_FIELD_KEYS)
        except AIVisionQuotaError:
            raise HTTPException(status_code=429, detail="OpenAI quota exceeded. Try again later.")
        except Exception as e:
            logger.warning("license auto-parse failed: %s", e)

    if not drafts.update(draft_id, drivers_license_file_url=url, payload=merged):
        raise HTTPException(status_code=500, detail="Could not save license URL")
    return {"ok": True, "driversLicenseFileUrl": url, "payload": merged, "parsed": parsed}


@router.post("/draft/{draft_id}/parse-image")
async def parse_draft_image(
    draft_id: str,
    file: UploadFile = File(...),
    ip_hash: str = Depends(current_ip_hash),
    krab_draft_id: Optional[str] = Cookie(default=None),
    drafts: DraftsDatabase = Depends(get_drafts_db),
    db: Database = Depends(get_db),
):
    if not Config.is_ai_configured():
        raise HTTPException(status_code=503, detail="Image parsing is not configured")

    draft = drafts.get_by_id(draft_id)
    draft = _verify_draft_access(draft, draft_id, ip_hash, krab_draft_id)
    if draft.get("status") == "submitted":
        raise HTTPException(status_code=409, detail="Application already submitted")

    data = await file.read()
    if not data or len(data) > 12 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Invalid or too large file")

    try:
        extracted = extract_interview_from_image(data, _mime_from_upload(file))
    except AIVisionQuotaError:
        raise HTTPException(status_code=429, detail="OpenAI quota exceeded. Try again later.")
    except Exception as e:
        logger.error("parse_draft_image: %s", e, exc_info=True)
        raise HTTPException(status_code=422, detail="Could not read image. Try a clearer photo.")

    merged = _merge_extracted_payload(dict(draft.get("payload") or {}), extracted)
    merged = _apply_telegram_resolution(merged, db)
    if not drafts.update(draft_id, payload=merged):
        raise HTTPException(status_code=500, detail="Could not save parsed fields")
    return {"ok": True, "payload": merged}


@router.post("/draft/{draft_id}/parse-text")
async def parse_draft_text(
    draft_id: str,
    body: ParseTextBody,
    ip_hash: str = Depends(current_ip_hash),
    krab_draft_id: Optional[str] = Cookie(default=None),
    drafts: DraftsDatabase = Depends(get_drafts_db),
    db: Database = Depends(get_db),
):
    if not Config.is_ai_configured():
        raise HTTPException(status_code=503, detail="Text parsing is not configured")

    text = (body.text or "").strip()
    if len(text) < 10:
        raise HTTPException(status_code=400, detail="Paste more application text to parse")

    draft = drafts.get_by_id(draft_id)
    draft = _verify_draft_access(draft, draft_id, ip_hash, krab_draft_id)
    if draft.get("status") == "submitted":
        raise HTTPException(status_code=409, detail="Application already submitted")

    try:
        extracted = extract_interview_from_text(text)
    except AIVisionQuotaError:
        raise HTTPException(status_code=429, detail="OpenAI quota exceeded. Try again later.")
    except Exception as e:
        logger.error("parse_draft_text: %s", e, exc_info=True)
        raise HTTPException(status_code=422, detail="Could not parse text. Try clearer formatting.")

    merged = _merge_extracted_payload(dict(draft.get("payload") or {}), extracted)
    merged = _apply_telegram_resolution(merged, db)
    if not drafts.update(draft_id, payload=merged):
        raise HTTPException(status_code=500, detail="Could not save parsed fields")
    return {"ok": True, "payload": merged}


@router.post("/resolve-telegram")
async def resolve_telegram(
    body: ResolveTelegramBody,
    db: Database = Depends(get_db),
):
    tid, source, message = resolve_telegram_id_for_username(db, body.username)
    display = normalize_telegram_username_display(body.username)
    return {
        "ok": bool(tid),
        "telegramId": tid,
        "telegramUsername": display or None,
        "source": source,
        "message": message,
        "botUsername": (getattr(Config, "KRAB_INTERVIEWER_BOT_USERNAME", None) or "krabinterviewerbot").lstrip("@"),
    }


@router.post("/submit/{draft_id}")
async def submit_draft(
    draft_id: str,
    ip_hash: str = Depends(current_ip_hash),
    krab_draft_id: Optional[str] = Cookie(default=None),
    drafts: DraftsDatabase = Depends(get_drafts_db),
    db: Database = Depends(get_db),
):
    draft = drafts.get_by_id(draft_id)
    draft = _verify_draft_access(draft, draft_id, ip_hash, krab_draft_id)

    if draft.get("status") == "submitted":
        return {
            "ok": True,
            "alreadySubmitted": True,
            "interviewId": draft.get("submitted_interview_id"),
        }

    payload = dict(draft.get("payload") or {})
    payload = _apply_telegram_resolution(payload, db)
    if _empty_val(payload.get("telegram_id")):
        bot = (getattr(Config, "KRAB_INTERVIEWER_BOT_USERNAME", None) or "krabinterviewerbot").lstrip("@")
        raise HTTPException(
            status_code=400,
            detail=(
                f"Could not verify your Telegram account. Open @{bot} in Telegram, tap Start, "
                "then return to the form and enter your @username again."
            ),
        )
    if not drafts.update(draft_id, payload=payload):
        raise HTTPException(status_code=500, detail="Could not save resolved Telegram ID")
    missing = [k for k in REQUIRED_SUBMIT_KEYS if _empty_val(payload.get(k))]
    if missing:
        raise HTTPException(
            status_code=400,
            detail={"message": "Missing required fields", "fields": missing},
        )

    tg = (payload.get("telegram_username") or "").strip()
    dup = db.find_pending_interview_by_telegram_username(tg)
    if dup:
        raise HTTPException(
            status_code=409,
            detail=(
                "An application with this Telegram username is already on file. "
                "Ask your supervisor to /cancel the previous interview, or use a different username."
            ),
        )

    fields = normalize_interview_data(payload)
    lic_url = (draft.get("drivers_license_file_url") or "").strip()
    if lic_url:
        fields["drivers_license_file_url"] = lic_url

    interview = db.create_interview(
        fields,
        created_by_telegram_id=WEB_CREATED_BY,
        is_supervisor_created=False,
        status="pending",
    )
    if not interview:
        raise HTTPException(status_code=500, detail="Could not create interview")

    iid = str(interview["id"])
    if lic_url:
        db.update_interview(iid, {"drivers_license_file_url": lic_url})

    drafts.mark_submitted(draft_id, iid)
    notify_supervisors_new_web_interview(interview)

    return {
        "ok": True,
        "alreadySubmitted": False,
        "interviewId": iid,
    }
