"""Public interview draft API (web form)."""
from __future__ import annotations

import base64
import logging
from typing import Any, Dict, Optional
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Cookie, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from api.deps import DRAFT_COOKIE, _forwarded_for, current_ip_hash, get_db, get_drafts_db
from api.notify import notify_supervisors_new_web_interview, notify_supervisors_web_auto_hired
from api.bot_bridge import schedule_hire_side_effects
from config import Config
from utils.ai_vision import (
    AIVisionQuotaError,
    INTERVIEW_FIELD_KEYS,
    extract_interview_from_image,
    extract_interview_from_text,
    filled_interview_keys,
    normalize_interview_data,
)
from utils.drafts_db import DraftsDatabase, new_draft_cookie, request_ip_is_reliable, storage_ip_hash
from utils.database import Database
from utils.hire_driver import hire_driver_records, validate_hire_ready
from utils.telegram_resolve import (
    normalize_telegram_username_display,
    resolve_telegram_id_for_username,
    telegram_connect_url,
    telegram_resolve_meta,
)
from utils.telegram_login_host import (
    is_allowed_return_url,
    login_page_html,
    telegram_bot_username,
    widget_base_url,
)
from utils.telegram_widget_auth import verify_telegram_login

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/interview", tags=["interview"])

WEB_CREATED_BY = "web"
REQUIRED_SUBMIT_KEYS = [
    "full_name",
    "work_commitment",
    "phone_number",
    "email",
    "mailing_address",
    "telegram_username",
    "emergency_contact",
    "payment_method",
    "profession_skill",
]


class DraftPatchBody(BaseModel):
    payload: Dict[str, Any] = Field(default_factory=dict)


class ResolveTelegramBody(BaseModel):
    username: str = ""


class TelegramWidgetAuthBody(BaseModel):
    id: int
    auth_date: int
    hash: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    username: Optional[str] = None
    photo_url: Optional[str] = None


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


def _apply_telegram_resolution(
    merged: Dict[str, Any],
    db: Database,
    drafts: Optional[DraftsDatabase] = None,
) -> Dict[str, Any]:
    """When username is present, resolve numeric telegram_id into payload."""
    un_raw = (merged.get("telegram_username") or "").strip()
    if not un_raw:
        return merged
    display = normalize_telegram_username_display(un_raw)
    if display:
        merged["telegram_username"] = display
    tid, _source, _msg = resolve_telegram_id_for_username(db, un_raw, drafts_db=drafts)
    if tid:
        merged["telegram_id"] = str(tid)
    else:
        merged.pop("telegram_id", None)
    return merged


def _note_pending_telegram_username(
    merged: Dict[str, Any],
    draft_id: str,
    drafts: DraftsDatabase,
) -> None:
    un_raw = (merged.get("telegram_username") or "").strip()
    tid = str(merged.get("telegram_id") or "").strip()
    if un_raw and (not tid or tid == "-"):
        drafts.register_pending_telegram_username(un_raw, draft_id)


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


def _public_api_base() -> str:
    return (
        Config.KRAB_PUBLIC_BASE_URL
        or Config.TELEGRAM_LOGIN_WIDGET_BASE_URL
        or ""
    ).rstrip("/")


def _license_serve_url(kind: str, record_id: str) -> str:
    path = f"/api/interview/{kind}/{record_id}/license-file"
    base = _public_api_base()
    return f"{base}{path}" if base else path


def _embed_license_fallback(payload: Dict[str, Any], data: bytes, mime: str) -> Dict[str, Any]:
    merged = dict(payload or {})
    merged["_license_b64"] = base64.standard_b64encode(data).decode("ascii")
    merged["_license_mime"] = mime
    return merged


def _license_bytes_from_payload(payload: Dict[str, Any]) -> tuple[Optional[bytes], str]:
    b64 = (payload or {}).get("_license_b64") or ""
    if not b64:
        return None, "image/jpeg"
    mime = (payload or {}).get("_license_mime") or "image/jpeg"
    try:
        return base64.standard_b64decode(b64), mime
    except Exception:
        return None, mime


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


def _client_draft_payload(raw: Optional[dict]) -> dict:
    """Form fields only — never return internal license blobs to the browser."""
    payload = dict(raw or {})
    payload.pop("_license_b64", None)
    payload.pop("_license_mime", None)
    return payload


@router.post("/draft")
async def create_or_resume_draft(
    request: Request,
    fresh: bool = False,
    ip_hash: str = Depends(current_ip_hash),
    krab_draft_id: Optional[str] = Cookie(default=None),
    drafts: DraftsDatabase = Depends(get_drafts_db),
):
    ua = (request.headers.get("user-agent") or "")[:500]
    forwarded = _forwarded_for(request)
    remote = request.client.host if request.client else None
    if fresh:
        drafts.archive_visitor_drafts(ip_hash, krab_draft_id)

    existing = None
    if krab_draft_id and not fresh:
        by_cookie = drafts.get_by_id(krab_draft_id)
        if by_cookie and by_cookie.get("status") != "submitted":
            existing = by_cookie

    if existing is None and not fresh and request_ip_is_reliable(forwarded, remote):
        by_ip = drafts.get_by_ip_hash(ip_hash)
        if by_ip:
            if by_ip.get("status") == "submitted":
                drafts.release_ip_hash(str(by_ip["id"]))
            else:
                existing = by_ip

    if existing:
        drafts.touch(str(existing["id"]))
        return _cookie_response(
            {
                "draftId": str(existing["id"]),
                "payload": _client_draft_payload(existing.get("payload")),
                "alreadySubmitted": False,
                "driversLicenseFileUrl": existing.get("drivers_license_file_url"),
            },
            str(existing["id"]),
        )

    cookie_token = new_draft_cookie()
    storage_ip = storage_ip_hash(forwarded, remote, cookie_token)
    row = drafts.create(ip_hash=storage_ip, draft_cookie=cookie_token, user_agent=ua)
    if not row:
        fallback = drafts.get_by_ip_hash(storage_ip) or drafts.get_by_ip_hash(ip_hash)
        if fallback and fallback.get("status") == "submitted":
            drafts.release_ip_hash(str(fallback["id"]))
            row = drafts.create(ip_hash=storage_ip, draft_cookie=cookie_token, user_agent=ua)
        elif fallback and fallback.get("status") != "submitted":
            drafts.touch(str(fallback["id"]))
            return _cookie_response(
                {
                    "draftId": str(fallback["id"]),
                    "payload": _client_draft_payload(fallback.get("payload")),
                    "alreadySubmitted": False,
                    "driversLicenseFileUrl": fallback.get("drivers_license_file_url"),
                },
                str(fallback["id"]),
            )
    if not row:
        raise HTTPException(
            status_code=500,
            detail=(
                "Could not create draft. Run migration_interview_drafts.sql in Supabase, "
                "then redeploy krab-interviewer-bot on Render."
            ),
        )
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
    merged = _apply_telegram_resolution(merged, db, drafts)
    if not drafts.update(draft_id, payload=merged):
        raise HTTPException(status_code=500, detail="Could not save draft")
    _note_pending_telegram_username(merged, draft_id, drafts)
    un = (merged.get("telegram_username") or "").strip()
    resolve_info = telegram_resolve_meta(db, un, drafts_db=drafts) if un else None
    return {"ok": True, "payload": merged, "telegramResolve": resolve_info}


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

    mime = _mime_from_upload(file)
    merged = dict(draft.get("payload") or {})
    url = db.upload_driver_license_to_storage(draft_id, data, file.filename or "license.jpg")
    if url:
        merged.pop("_license_b64", None)
        merged.pop("_license_mime", None)
    else:
        merged = _embed_license_fallback(merged, data, mime)
        url = _license_serve_url("draft", draft_id)
        logger.warning("Supabase storage upload failed; using API-hosted license for draft %s", draft_id[:8])

    parsed = False
    if Config.is_ai_configured():
        try:
            extracted = extract_interview_from_image(data, _mime_from_upload(file))
            merged = _merge_extracted_payload(merged, extracted)
            merged = _apply_telegram_resolution(merged, db, drafts)
            parsed = any((merged.get(k) or "").strip() for k in INTERVIEW_FIELD_KEYS)
        except AIVisionQuotaError:
            raise HTTPException(status_code=429, detail="OpenAI quota exceeded. Try again later.")
        except Exception as e:
            logger.warning("license auto-parse failed: %s", e)

    if not drafts.update(draft_id, drivers_license_file_url=url, payload=merged):
        raise HTTPException(status_code=500, detail="Could not save license URL")
    return {"ok": True, "driversLicenseFileUrl": url, "payload": merged, "parsed": parsed}


@router.get("/draft/{draft_id}/license-file")
async def serve_draft_license_file(
    draft_id: str,
    drafts: DraftsDatabase = Depends(get_drafts_db),
):
    draft = drafts.get_by_id(draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Not found")
    payload = draft.get("payload") or {}
    raw, mime = _license_bytes_from_payload(payload)
    if raw:
        return Response(content=raw, media_type=mime)
    ext_url = (draft.get("drivers_license_file_url") or "").strip()
    if ext_url and "/license-file" not in ext_url:
        return RedirectResponse(ext_url, status_code=302)
    raise HTTPException(status_code=404, detail="License file not found")


@router.get("/record/{interview_id}/license-file")
async def serve_interview_license_file(
    interview_id: str,
    db: Database = Depends(get_db),
    drafts: DraftsDatabase = Depends(get_drafts_db),
):
    interview = db.get_interview_by_id(interview_id)
    if not interview:
        raise HTTPException(status_code=404, detail="Not found")
    ext_url = (interview.get("drivers_license_file_url") or "").strip()
    if ext_url and "/license-file" not in ext_url:
        return RedirectResponse(ext_url, status_code=302)
    for draft in drafts.find_drafts_by_submitted_interview(interview_id):
        raw, mime = _license_bytes_from_payload(draft.get("payload") or {})
        if raw:
            return Response(content=raw, media_type=mime)
    raise HTTPException(status_code=404, detail="License file not found")


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
        raise HTTPException(status_code=422, detail="Could not read image. Try a clearer JPG or PNG photo.")

    filled = filled_interview_keys(extracted)
    if not filled:
        raise HTTPException(
            status_code=422,
            detail="Could not find interview fields in that image. Try a clearer screenshot or paste text instead.",
        )

    merged = _merge_extracted_payload(dict(draft.get("payload") or {}), extracted)
    merged = _apply_telegram_resolution(merged, db, drafts)
    if not drafts.update(draft_id, payload=merged):
        raise HTTPException(status_code=500, detail="Could not save parsed fields")
    return {"ok": True, "payload": merged, "filledKeys": filled}


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

    filled = filled_interview_keys(extracted)
    if not filled:
        raise HTTPException(
            status_code=422,
            detail="Could not find interview fields in that text. Add labels like Name, Phone, Email, Telegram.",
        )

    merged = _merge_extracted_payload(dict(draft.get("payload") or {}), extracted)
    merged = _apply_telegram_resolution(merged, db, drafts)
    if not drafts.update(draft_id, payload=merged):
        raise HTTPException(status_code=500, detail="Could not save parsed fields")
    return {"ok": True, "payload": merged, "filledKeys": filled}


@router.post("/resolve-telegram")
async def resolve_telegram(
    body: ResolveTelegramBody,
    db: Database = Depends(get_db),
    drafts: DraftsDatabase = Depends(get_drafts_db),
):
    meta = telegram_resolve_meta(db, body.username, drafts_db=drafts)
    display = normalize_telegram_username_display(body.username)
    return {
        **meta,
        "telegramUsername": display or None,
    }


@router.get("/telegram-login-config")
async def telegram_login_config():
    """Where to host Telegram Login Widget (API domain, not Vercel)."""
    base = widget_base_url()
    return {
        "botUsername": telegram_bot_username(),
        "widgetBaseUrl": base or None,
        "hostedLoginPath": "/api/interview/telegram-login-page",
        "inlineOnFrontend": bool(base and False),
    }


@router.get("/telegram-login-page", response_class=HTMLResponse)
async def telegram_login_page(return_url: str = ""):
    """
    Hosted Login Widget page. BotFather /setdomain must include this host
    (e.g. krab-interviewer-bot-j5dv.onrender.com).
    """
    base = widget_base_url()
    if not base:
        raise HTTPException(
            status_code=503,
            detail="TELEGRAM_LOGIN_WIDGET_BASE_URL is not configured on the API server.",
        )
    target = (return_url or "").strip()
    if not target or not is_allowed_return_url(target):
        raise HTTPException(status_code=400, detail="Invalid or missing return_url")
    return HTMLResponse(login_page_html(target))


@router.get("/telegram-widget-callback")
async def telegram_widget_callback(
    request: Request,
    return_url: str = "",
    db: Database = Depends(get_db),
):
    """Telegram redirects here after Login Widget auth (data-auth-url flow)."""
    target = (return_url or "").strip()
    if not target or not is_allowed_return_url(target):
        raise HTTPException(status_code=400, detail="Invalid return_url")

    params = dict(request.query_params)
    verified = verify_telegram_login(params, Config.TELEGRAM_BOT_TOKEN or "")
    if not verified:
        raise HTTPException(status_code=403, detail="Telegram login verification failed")

    tid = verified["telegram_id"]
    un_display = normalize_telegram_username_display(params.get("username") or "")
    if not un_display:
        un_display = verified.get("telegram_username") or ""
    if un_display and db:
        db.upsert_telegram_user_directory(tid, un_display.lstrip("@"))

    sep = "&" if "?" in target else "?"
    redirect = (
        f"{target}{sep}telegram_auth=1"
        f"&id={quote(str(tid))}"
        f"&auth_date={quote(str(params.get('auth_date') or ''))}"
        f"&hash={quote(str(params.get('hash') or ''))}"
    )
    if params.get("username"):
        redirect += f"&username={quote(str(params.get('username')))}"
    if params.get("first_name"):
        redirect += f"&first_name={quote(str(params.get('first_name')))}"
    return RedirectResponse(url=redirect, status_code=302)


@router.post("/verify-telegram-auth")
async def verify_telegram_auth(
    body: TelegramWidgetAuthBody,
    db: Database = Depends(get_db),
):
    """Verify Telegram Login Widget callback and upsert username → id."""
    verified = verify_telegram_login(body.model_dump(), Config.TELEGRAM_BOT_TOKEN or "")
    if not verified:
        raise HTTPException(status_code=403, detail="Telegram login verification failed")

    tid = verified["telegram_id"]
    un_raw = (body.username or "").strip()
    display = normalize_telegram_username_display(un_raw) if un_raw else verified.get("telegram_username") or ""

    if display and db:
        db.upsert_telegram_user_directory(tid, display.lstrip("@"))

    return {
        "ok": True,
        "telegramId": tid,
        "telegramUsername": display or None,
        "firstName": verified.get("first_name") or None,
        "message": f"Telegram ID {tid} linked.",
    }


@router.post("/submit/{draft_id}")
async def submit_draft(
    draft_id: str,
    background_tasks: BackgroundTasks,
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
    payload = _apply_telegram_resolution(payload, db, drafts)
    if _empty_val(payload.get("telegram_id")):
        un = normalize_telegram_username_display(payload.get("telegram_username") or "")
        connect = telegram_connect_url("web")
        raise HTTPException(
            status_code=400,
            detail=(
                f"Could not find a Telegram ID for {un or 'that username'}. "
                f'Use "Log in with Telegram" on the form, or open {connect} and tap Start once, then try again.'
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

    hire_check = {**fields, "telegram_id": payload.get("telegram_id")}
    if Config.WEB_AUTO_HIRE_ON_SUBMIT:
        ok_hire, hire_msg = validate_hire_ready(hire_check)
        if not ok_hire:
            raise HTTPException(status_code=400, detail=hire_msg)

    interview = db.create_interview(
        fields,
        created_by_telegram_id=WEB_CREATED_BY,
        is_supervisor_created=False,
        status="pending",
    )
    if not interview:
        raise HTTPException(status_code=500, detail="Could not create interview")

    iid = str(interview["id"])
    lic_url = (draft.get("drivers_license_file_url") or "").strip()
    payload_b64, _mime = _license_bytes_from_payload(payload)
    if payload_b64 and (not lic_url or "/license-file" in lic_url):
        lic_url = _license_serve_url("record", iid)
    if lic_url:
        db.update_interview(iid, {"drivers_license_file_url": lic_url})
        interview["drivers_license_file_url"] = lic_url

    hired = False
    hire_errors: list[str] = []
    if Config.WEB_AUTO_HIRE_ON_SUBMIT:
        interview, hire_errors = hire_driver_records(db, iid)
        if not interview:
            db.update_interview(iid, {"status": "cancelled"})
            raise HTTPException(
                status_code=500,
                detail=hire_errors[0] if hire_errors else "Auto-hire failed",
            )
        hired = True
        schedule_hire_side_effects(iid, created_by=WEB_CREATED_BY)

    drafts.mark_submitted(draft_id, iid)
    interview_snapshot = dict(interview)
    errors_snapshot = list(hire_errors)
    if hired:
        background_tasks.add_task(
            notify_supervisors_web_auto_hired, interview_snapshot, errors_snapshot
        )
    else:
        background_tasks.add_task(notify_supervisors_new_web_interview, interview_snapshot)

    return {
        "ok": True,
        "alreadySubmitted": False,
        "interviewId": iid,
        "hired": hired,
    }
