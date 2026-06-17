"""
Krab Issuer admin JSON API (merged into Krab Dispatch FastAPI).
All routes require X-Admin-Password (same as Dispatch admin).
"""

from __future__ import annotations

import logging
import os
import re
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import BaseModel

from .config import ApiConfig
from .issuer_admin_db import IssuerAdminDatabase
from .issuer_supabase import fetch_lead_meta_by_references

logger = logging.getLogger(__name__)

router = APIRouter()


def _api_config() -> ApiConfig:
    return ApiConfig.from_env()


def get_issuer_db(config: ApiConfig = Depends(_api_config)) -> IssuerAdminDatabase:
    if not config.supabase_configured():
        raise HTTPException(
            status_code=503,
            detail="Configure SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY for Krab Issuer admin.",
        )
    return IssuerAdminDatabase(config.supabase_url, config.supabase_service_role_key)


class GroupCreate(BaseModel):
    group_name: str
    group_telegram_id: str
    supervisory_telegram_id: str


class DriverCreate(BaseModel):
    driver_name: str
    driver_telegram_id: str
    phone_number: Optional[str] = None


class AssistantAdd(BaseModel):
    telegram_id: str


class SettingsUpdate(BaseModel):
    assistants_choose_group: Optional[bool] = None
    st_telegram_id: Optional[str] = None
    receipt_detection_mode: Optional[str] = None


class ContactSourceCreate(BaseModel):
    label: str
    sort_order: int = 0


class AssignmentCreate(BaseModel):
    group_id: str
    driver_id: str


@router.get("/groups")
def issuer_groups_list(db: IssuerAdminDatabase = Depends(get_issuer_db)):
    return db.get_all_groups()


@router.post("/groups")
def issuer_groups_create(body: GroupCreate, db: IssuerAdminDatabase = Depends(get_issuer_db)):
    if db.create_group(body.group_name, body.group_telegram_id, body.supervisory_telegram_id):
        return {"success": True, "message": "Group added"}
    raise HTTPException(status_code=500, detail="Could not add group")


@router.post("/groups/{group_id}/toggle")
def issuer_toggle_group(group_id: str, db: IssuerAdminDatabase = Depends(get_issuer_db)):
    if db.toggle_group_status(group_id):
        return {"success": True}
    raise HTTPException(status_code=400, detail="Could not toggle group")


@router.get("/drivers")
def issuer_drivers_list(db: IssuerAdminDatabase = Depends(get_issuer_db)):
    return db.get_all_drivers()


@router.post("/drivers")
def issuer_drivers_create(body: DriverCreate, db: IssuerAdminDatabase = Depends(get_issuer_db)):
    try:
        if db.create_driver(body.driver_name, body.driver_telegram_id, body.phone_number):
            return {"success": True, "message": "Driver added"}
    except Exception as e:
        err = str(e).lower()
        logger.exception("create_driver")
        if any(x in err for x in ("unique", "duplicate", "23505", "violates")):
            raise HTTPException(status_code=409, detail="A driver with this Telegram ID already exists.") from e
        raise HTTPException(status_code=500, detail=str(e)) from e
    raise HTTPException(status_code=500, detail="Could not add driver")


@router.post("/drivers/{driver_id}/toggle")
def issuer_toggle_driver(driver_id: str, db: IssuerAdminDatabase = Depends(get_issuer_db)):
    if db.toggle_driver_status(driver_id):
        return {"success": True}
    raise HTTPException(status_code=400, detail="Could not toggle driver")


@router.get("/groups/{group_id}/assistants")
def issuer_group_assistants(group_id: str, db: IssuerAdminDatabase = Depends(get_issuer_db)):
    return db.get_group_assistants(group_id)


@router.post("/groups/{group_id}/assistants")
def issuer_add_assistant(group_id: str, body: AssistantAdd, db: IssuerAdminDatabase = Depends(get_issuer_db)):
    if db.add_group_assistant(group_id, body.telegram_id):
        return {"success": True}
    raise HTTPException(status_code=400, detail="Could not add assistant")


@router.delete("/groups/{group_id}/assistants/{telegram_id}")
def issuer_remove_assistant(group_id: str, telegram_id: str, db: IssuerAdminDatabase = Depends(get_issuer_db)):
    if db.remove_group_assistant(group_id, telegram_id):
        return {"success": True}
    raise HTTPException(status_code=400, detail="Could not remove assistant")


@router.get("/settings")
def issuer_get_settings(db: IssuerAdminDatabase = Depends(get_issuer_db)):
    val = db.get_setting("assistants_choose_group")
    st_id = (db.get_setting("st_telegram_id") or "").strip()
    rec_mode = (db.get_setting("receipt_detection_mode") or "lax").strip().lower()
    if rec_mode not in ("strict", "lax"):
        rec_mode = "lax"
    return {
        "assistants_choose_group": (val or "").lower() in ("true", "1", "yes"),
        "st_telegram_id": st_id,
        "receipt_detection_mode": rec_mode,
    }


@router.post("/settings")
def issuer_set_settings(body: SettingsUpdate, db: IssuerAdminDatabase = Depends(get_issuer_db)):
    if body.assistants_choose_group is not None:
        db.set_setting(
            "assistants_choose_group",
            "true" if body.assistants_choose_group else "false",
        )
    if body.st_telegram_id is not None:
        db.set_setting("st_telegram_id", str(body.st_telegram_id).strip())
    if body.receipt_detection_mode is not None:
        rm = str(body.receipt_detection_mode).strip().lower()
        if rm not in ("strict", "lax"):
            rm = "lax"
        db.set_setting("receipt_detection_mode", rm)
    return {"success": True}


@router.get("/contact-sources")
def issuer_contact_sources(db: IssuerAdminDatabase = Depends(get_issuer_db)):
    return db.get_contact_info_sources()


@router.post("/contact-sources")
def issuer_contact_sources_add(body: ContactSourceCreate, db: IssuerAdminDatabase = Depends(get_issuer_db)):
    if not body.label.strip():
        raise HTTPException(status_code=400, detail="Missing label")
    if db.create_contact_info_source(body.label, body.sort_order):
        return {"success": True}
    raise HTTPException(status_code=500, detail="Could not add source")


@router.post("/contact-sources/{source_id}/toggle")
def issuer_contact_sources_toggle(source_id: str, db: IssuerAdminDatabase = Depends(get_issuer_db)):
    if db.toggle_contact_source_status(source_id):
        return {"success": True}
    raise HTTPException(status_code=400, detail="Could not toggle")


@router.get("/assignments")
def issuer_assignments(db: IssuerAdminDatabase = Depends(get_issuer_db)):
    return db.get_all_assignments()


@router.post("/assignments")
def issuer_assignments_add(body: AssignmentCreate, db: IssuerAdminDatabase = Depends(get_issuer_db)):
    if db.assign_driver_to_group(body.group_id, body.driver_id):
        return {"success": True}
    raise HTTPException(status_code=400, detail="Could not assign (maybe duplicate)")


@router.delete("/assignments/{assignment_id}")
def issuer_assignments_del(assignment_id: str, db: IssuerAdminDatabase = Depends(get_issuer_db)):
    if db.remove_driver_from_group(assignment_id):
        return {"success": True}
    raise HTTPException(status_code=400, detail="Could not remove")


@router.get("/stats")
def issuer_stats(db: IssuerAdminDatabase = Depends(get_issuer_db)):
    return db.get_lead_stats()


@router.get("/bot-usage")
def issuer_bot_usage(limit: int = 100, db: IssuerAdminDatabase = Depends(get_issuer_db)):
    return db.get_bot_usage(limit=min(max(limit, 1), 500))


@router.get("/receipt-debts/summary")
def issuer_receipt_debts_summary(db: IssuerAdminDatabase = Depends(get_issuer_db)):
    return db.get_receipt_debts_summary()


@router.get("/receipt-debts/drivers/{driver_id}")
def issuer_receipt_debts_driver(driver_id: str, db: IssuerAdminDatabase = Depends(get_issuer_db)):
    return db.get_driver_pending_receipts(driver_id)


@router.delete("/receipt-debts/drivers/{driver_id}/pending")
def issuer_receipt_debts_clear_driver(driver_id: str, db: IssuerAdminDatabase = Depends(get_issuer_db)):
    deleted = db.delete_pending_receipts_for_driver(driver_id)
    return {"success": True, "deleted": deleted}


@router.delete("/receipt-debts/assignments/{assignment_id}")
def issuer_receipt_debts_del_assignment(assignment_id: str, db: IssuerAdminDatabase = Depends(get_issuer_db)):
    if db.delete_pending_receipt_assignment(assignment_id):
        return {"success": True}
    raise HTTPException(status_code=400, detail="Not found or receipt already submitted")


@router.get("/receipts/submitted")
def issuer_receipts_submitted(limit: int = 100, db: IssuerAdminDatabase = Depends(get_issuer_db)):
    return db.get_submitted_receipts_recent(limit=limit)


_TELEGRAM_FILE_PREFIX = "https://api.telegram.org/file/bot"


def _telegram_bot_token() -> str:
    return (
        os.getenv("TELEGRAM_BOT_TOKEN")
        or os.getenv("ISSUER_TELEGRAM_BOT_TOKEN")
        or os.getenv("KRAB_ISSUER_BOT_TOKEN")
        or ""
    ).strip()


def _resolve_receipt_fetch_url(stored: str, bot_token: str) -> Optional[str]:
    u = (stored or "").strip()
    if not u:
        return None
    if u.startswith("http://") or u.startswith("https://"):
        if _TELEGRAM_FILE_PREFIX in u and bot_token:
            m = re.search(r"/file/bot[^/]+/(.+)$", u)
            if m:
                return f"{_TELEGRAM_FILE_PREFIX}{bot_token}/{m.group(1)}"
        return u
    if bot_token:
        return f"{_TELEGRAM_FILE_PREFIX}{bot_token}/{u.lstrip('/')}"
    return None


@router.get("/receipts/view")
def issuer_receipt_view(
    ref: str = Query(..., min_length=1),
    config: ApiConfig = Depends(_api_config),
):
    """Stream receipt image for a lead reference (re-signs Telegram file URLs server-side)."""
    ref_key = ref.strip()
    if not config.supabase_configured():
        raise HTTPException(status_code=503, detail="Supabase not configured")
    meta = fetch_lead_meta_by_references(
        config.supabase_url,
        config.supabase_service_role_key,
        [ref_key],
    )
    row = meta.get(ref_key) or {}
    stored = (row.get("receipt_image_url") or "").strip()
    if not stored:
        raise HTTPException(status_code=404, detail="Receipt not found for this reference")
    bot_token = _telegram_bot_token()
    fetch_url = _resolve_receipt_fetch_url(stored, bot_token)
    if not fetch_url:
        raise HTTPException(status_code=502, detail="Cannot resolve receipt URL")
    if _TELEGRAM_FILE_PREFIX not in fetch_url:
        return RedirectResponse(url=fetch_url, status_code=302)
    try:
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            resp = client.get(fetch_url)
        if resp.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail=f"Telegram file fetch failed ({resp.status_code})",
            )
        media = resp.headers.get("content-type") or "image/jpeg"
        return StreamingResponse(iter([resp.content]), media_type=media)
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("receipt view %s: %s", ref_key, exc)
        raise HTTPException(status_code=502, detail="Failed to fetch receipt image") from exc


@router.get("/renewals/upcoming")
def issuer_renewals(db: IssuerAdminDatabase = Depends(get_issuer_db)):
    return db.get_upcoming_renewals()
