"""Admin dashboard API (password session)."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from api.deps import (
    ADMIN_COOKIE,
    get_drafts_db,
    require_admin,
    sign_admin_session,
    verify_admin_session,
)
from config import Config
from utils.drafts_db import DraftsDatabase

router = APIRouter(prefix="/api/admin", tags=["admin"])


class LoginBody(BaseModel):
    password: str = ""


@router.post("/login")
async def admin_login(body: LoginBody):
    expected = (Config.ADMIN_PASSWORD or "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="Admin password not configured")
    if (body.password or "").strip() != expected:
        raise HTTPException(status_code=401, detail="Invalid password")
    token = sign_admin_session()
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        key=ADMIN_COOKIE,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 7,
        secure=bool((Config.KRAB_PUBLIC_BASE_URL or "").startswith("https")),
    )
    return resp


@router.post("/logout")
async def admin_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(ADMIN_COOKIE)
    return resp


@router.get("/session")
async def admin_session(krab_admin_session: Optional[str] = Cookie(default=None)):
    return {"authenticated": verify_admin_session(krab_admin_session)}


@router.get("/interviews")
async def list_interviews(
    status: str = "",
    q: str = "",
    limit: int = 100,
    _: None = Depends(require_admin),
    drafts: DraftsDatabase = Depends(get_drafts_db),
):
    rows = drafts.list_for_admin(status_filter=status, query=q, limit=limit)
    return {"ok": True, "items": rows}


@router.get("/interviews/{draft_id}")
async def interview_detail(
    draft_id: str,
    _: None = Depends(require_admin),
    drafts: DraftsDatabase = Depends(get_drafts_db),
):
    row = drafts.get_by_id(draft_id)
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    return {
        "ok": True,
        "draft": row,
        "payload": row.get("payload") or {},
        "driversLicenseFileUrl": row.get("drivers_license_file_url"),
    }
