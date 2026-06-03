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
from utils.ai_vision import INTERVIEW_FIELD_KEYS, normalize_interview_data
from utils.drafts_db import DraftsDatabase, new_draft_cookie
from utils.database import Database

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
):
    draft = drafts.get_by_id(draft_id)
    draft = _verify_draft_access(draft, draft_id, ip_hash, krab_draft_id)
    if draft.get("status") == "submitted":
        raise HTTPException(status_code=409, detail="Application already submitted")
    merged = dict(draft.get("payload") or {})
    for k, v in (body.payload or {}).items():
        if k in INTERVIEW_FIELD_KEYS or k == "first_name":
            merged[k] = v
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
    if not drafts.update(draft_id, drivers_license_file_url=url):
        raise HTTPException(status_code=500, detail="Could not save license URL")
    return {"ok": True, "driversLicenseFileUrl": url}


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
