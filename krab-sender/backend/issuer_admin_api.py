"""
Krab Issuer admin JSON API (merged into Krab Dispatch FastAPI).
All routes require X-Admin-Password (same as Dispatch admin).
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

try:  # Prefer requests (in requirements); httpx is the guaranteed fallback.
    import requests as _requests
except Exception:  # pragma: no cover - requests should exist, httpx covers us
    _requests = None

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
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
    # Optional: drivers.email may not exist yet in prod; the DB layer retries
    # the insert without it when Supabase rejects the column.
    email: Optional[str] = None


class DriverUpdate(BaseModel):
    driver_name: Optional[str] = None
    driver_telegram_id: Optional[str] = None
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
        if db.create_driver(
            body.driver_name,
            body.driver_telegram_id,
            body.phone_number,
            email=body.email,
        ):
            return {"success": True, "message": "Driver added"}
    except Exception as e:
        err = str(e).lower()
        logger.exception("create_driver")
        if any(x in err for x in ("unique", "duplicate", "23505", "violates")):
            raise HTTPException(status_code=409, detail="A driver with this Telegram ID already exists.") from e
        raise HTTPException(status_code=500, detail=str(e)) from e
    raise HTTPException(status_code=500, detail="Could not add driver")


@router.put("/drivers/{driver_id}")
def issuer_drivers_update(driver_id: str, body: DriverUpdate, db: IssuerAdminDatabase = Depends(get_issuer_db)):
    try:
        ok = db.update_driver(
            driver_id,
            driver_name=body.driver_name,
            driver_telegram_id=body.driver_telegram_id,
            phone_number=body.phone_number,
        )
    except Exception as e:
        err = str(e).lower()
        logger.exception("update_driver")
        if any(x in err for x in ("unique", "duplicate", "23505")):
            raise HTTPException(status_code=409, detail="A driver with this Telegram ID already exists.") from e
        raise HTTPException(status_code=500, detail=str(e)) from e
    if not ok:
        raise HTTPException(status_code=404, detail="Driver not found")
    return {"success": True, "message": "Driver updated"}


@router.delete("/drivers/{driver_id}")
def issuer_drivers_delete(driver_id: str, db: IssuerAdminDatabase = Depends(get_issuer_db)):
    try:
        ok = db.delete_driver(driver_id)
    except Exception as e:
        err = str(e).lower()
        logger.exception("delete_driver")
        if any(x in err for x in ("foreign key", "23503", "violates")):
            raise HTTPException(
                status_code=409,
                detail="Driver has history (assigned leads) — deactivate instead.",
            ) from e
        raise HTTPException(status_code=500, detail=str(e)) from e
    if not ok:
        raise HTTPException(status_code=404, detail="Driver not found")
    return {"success": True, "message": "Driver deleted"}


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


def _sniff_image_media_type(
    data: bytes,
    header_ct: Optional[str] = None,
    url_hint: str = "",
) -> str:
    """Telegram often returns application/octet-stream — detect real image type from bytes."""
    if len(data) >= 12 and data[0:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if len(data) >= 8 and data[0:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if len(data) >= 6 and data[0:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if len(data) >= 3 and data[0:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if len(data) >= 2 and data[0:2] == b"\xff\xd8":
        return "image/jpeg"
    ct = (header_ct or "").split(";")[0].strip().lower()
    if ct.startswith("image/"):
        return ct
    path = (url_hint or "").split("?")[0].lower()
    for ext, mt in (
        (".png", "image/png"),
        (".jpg", "image/jpeg"),
        (".jpeg", "image/jpeg"),
        (".gif", "image/gif"),
        (".webp", "image/webp"),
    ):
        if path.endswith(ext):
            return mt
    return "image/jpeg"


def _receipt_inline_headers(media_type: str, ref: str) -> dict[str, str]:
    ext = "jpg"
    if "png" in media_type:
        ext = "png"
    elif "gif" in media_type:
        ext = "gif"
    elif "webp" in media_type:
        ext = "webp"
    safe_ref = re.sub(r"[^\w\-]+", "_", ref.strip())[:40] or "receipt"
    return {
        "Content-Disposition": f'inline; filename="{safe_ref}.{ext}"',
        "Cache-Control": "private, max-age=3600",
        "X-Content-Type-Options": "nosniff",
    }


def _stream_receipt_image(
    content: bytes,
    header_ct: Optional[str],
    url_hint: str,
    ref: str,
) -> StreamingResponse:
    media = _sniff_image_media_type(content, header_ct, url_hint)
    return StreamingResponse(
        iter([content]),
        media_type=media,
        headers=_receipt_inline_headers(media, ref),
    )


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


def _telegram_refetch_by_file_id(client: httpx.Client, bot_token: str, file_id: str):
    """Re-sign an expired Telegram file URL: file_ids are permanent, so getFile
    returns a fresh file_path we can download even years later."""
    try:
        gf = client.get(
            f"https://api.telegram.org/bot{bot_token}/getFile",
            params={"file_id": file_id},
        )
        if gf.status_code != 200:
            return None
        path = ((gf.json() or {}).get("result") or {}).get("file_path")
        if not path:
            return None
        return client.get(f"{_TELEGRAM_FILE_PREFIX}{bot_token}/{path}")
    except Exception:
        return None


@router.get("/receipts/view")
def issuer_receipt_view(
    ref: str = Query(..., min_length=1),
    config: ApiConfig = Depends(_api_config),
):
    """Stream receipt image for a lead reference.

    Telegram-hosted receipts expire after ~1h; when the stored URL carries a
    permanent file_id (``#tgfid=`` fragment, written by the issuer bot), an
    expired fetch is transparently retried via getFile re-signing.
    """
    ref_key = ref.strip()
    if not config.supabase_configured():
        raise HTTPException(status_code=503, detail="Supabase not configured")
    meta = fetch_lead_meta_by_references(
        config.supabase_url,
        config.supabase_service_role_key,
        [ref_key],
    )
    row = meta.get(ref_key) or {}
    stored_full = (row.get("receipt_image_url") or "").strip()
    if not stored_full:
        raise HTTPException(status_code=404, detail="Receipt not found for this reference")
    stored, _, frag = stored_full.partition("#")
    file_id = frag[6:].strip() if frag.startswith("tgfid=") else None
    bot_token = _telegram_bot_token()
    fetch_url = _resolve_receipt_fetch_url(stored, bot_token)
    if not fetch_url and not (file_id and bot_token):
        raise HTTPException(status_code=502, detail="Cannot resolve receipt URL")
    try:
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            resp = client.get(fetch_url) if fetch_url else None
            if (resp is None or resp.status_code != 200) and file_id and bot_token:
                fresh = _telegram_refetch_by_file_id(client, bot_token, file_id)
                if fresh is not None:
                    resp = fresh
        if resp is None or resp.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail=f"Receipt file fetch failed ({resp.status_code if resp is not None else 'unreachable'})",
            )
        return _stream_receipt_image(
            resp.content,
            resp.headers.get("content-type"),
            fetch_url or stored,
            ref_key,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("receipt view %s: %s", ref_key, exc)
        raise HTTPException(status_code=502, detail="Failed to fetch receipt image") from exc


@router.get("/renewals/upcoming")
def issuer_renewals(db: IssuerAdminDatabase = Depends(get_issuer_db)):
    return db.get_upcoming_renewals()


# ── Driver Track (live map tab) ────────────────────────────────────────────
# Native port of krableadsV2 admin_dashboard.py tracking endpoints so the
# unified admin renders the live map itself (no iframe).
# Free services: Nominatim (geocode) + OSRM (routing).

_TRACK_GEO_CACHE: dict = {"geocode": {}, "reverse": {}, "route": {}}
_TRACK_ROUTE_TTL_S = 45
_NOMINATIM_HEADERS = {"User-Agent": "krab-dispatch-admin/1.0 (driver track tab)"}


def _track_round3(v):
    try:
        return round(float(v), 3)
    except (TypeError, ValueError):
        return None


def _track_get_json(url: str, params: dict, timeout: float = 15.0, headers: Optional[dict] = None):
    """GET a JSON payload using requests when available, httpx otherwise."""
    if _requests is not None:
        resp = _requests.get(url, params=params, headers=headers or {}, timeout=timeout)
        return resp.json()
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        resp = client.get(url, params=params, headers=headers or {})
        return resp.json()


@router.get("/tracking/overview")
def issuer_tracking_overview(
    hours: int = Query(8),
    db: IssuerAdminDatabase = Depends(get_issuer_db),
):
    """Sessions + last-known driver positions + movement trails (admin only)."""
    try:
        hours_clamped = max(1, min(48, int(hours)))
    except (TypeError, ValueError):
        hours_clamped = 8
    now = datetime.now(timezone.utc)
    s_since = (now - timedelta(hours=48)).isoformat()
    p_since = (now - timedelta(hours=hours_clamped)).isoformat()

    try:
        srows = (
            db.client.table("driver_tracking_sessions").select("*")
            .gte("created_at", s_since).order("created_at", desc=True).limit(200).execute()
        ).data or []
    except Exception as e:
        logger.warning("tracking overview sessions: %s", e)
        srows = []
    try:
        prows = (
            db.client.table("driver_location_pings")
            .select("driver_id,lat,lng,accuracy,created_at")
            .gte("created_at", p_since).order("created_at").limit(4000).execute()
        ).data or []
    except Exception as e:
        logger.warning("tracking overview pings: %s", e)
        prows = []

    # select("*") + .get(): tolerate v2 columns (delivery_address/dest_lat/…)
    # that may not exist in prod yet.
    sessions = [{
        "token": s.get("token"), "kind": s.get("kind"),
        "reference_id": s.get("reference_id"), "driver_name": s.get("driver_name"),
        "status": s.get("status"), "created_at": s.get("created_at"),
        "located_at": s.get("located_at"), "details_sent_at": s.get("details_sent_at"),
        "delivery_address": s.get("delivery_address"),
        "dest_lat": s.get("dest_lat"), "dest_lng": s.get("dest_lng"),
    } for s in srows]

    name_by: dict = {}
    sess_by: dict = {}
    for s in srows:
        did = s.get("driver_id")
        if did is not None and str(did) not in name_by:
            name_by[str(did)] = s.get("driver_name")
            sess_by[str(did)] = s.get("token")

    latest: dict = {}
    trails: dict = {}
    for p in prows:
        key = "unknown" if p.get("driver_id") is None else str(p.get("driver_id"))
        trails.setdefault(key, []).append([p.get("lat"), p.get("lng"), p.get("created_at")])
        if p.get("driver_id") is not None:
            latest[key] = p
    for k in trails:
        trails[k] = trails[k][-500:]

    drivers = []
    for did, ping in latest.items():
        try:
            age = (
                now
                - datetime.fromisoformat(str(ping.get("created_at")).replace("Z", "+00:00"))
            ).total_seconds()
        except Exception:
            age = 1e9
        drivers.append({
            "driver_id": did,
            "driver_name": name_by.get(did),
            "session_token": sess_by.get(did),
            "last_ping": {"lat": ping.get("lat"), "lng": ping.get("lng"),
                          "accuracy": ping.get("accuracy"), "created_at": ping.get("created_at")},
            "live": age < 120,
        })
    return {"sessions": sessions, "drivers": drivers, "trails": trails}


@router.get("/tracking/route")
def issuer_tracking_route(
    token: str = Query(..., min_length=1),
    db: IssuerAdminDatabase = Depends(get_issuer_db),
):
    """From/To addresses + driving route + ETA for one tracking session (admin only)."""
    token_key = token.strip()
    if not token_key:
        raise HTTPException(status_code=400, detail="token required")
    try:
        srows = (
            db.client.table("driver_tracking_sessions").select("*")
            .eq("token", token_key).limit(1).execute()
        ).data or []
    except Exception as e:
        logger.warning("tracking route session lookup: %s", e)
        srows = []
    if not srows:
        raise HTTPException(status_code=404, detail="session not found")
    s = srows[0]

    prow = []
    try:
        prow = (
            db.client.table("driver_location_pings").select("lat,lng,created_at")
            .eq("session_token", token_key).order("created_at", desc=True).limit(1).execute()
        ).data or []
    except Exception:
        prow = []
    if not prow and s.get("driver_id"):
        try:
            prow = (
                db.client.table("driver_location_pings").select("lat,lng,created_at")
                .eq("driver_id", str(s.get("driver_id"))).order("created_at", desc=True).limit(1).execute()
            ).data or []
        except Exception:
            prow = []
    frm = None
    if prow:
        frm = {"lat": prow[0].get("lat"), "lng": prow[0].get("lng"), "at": prow[0].get("created_at")}

    addr = (s.get("delivery_address") or "").strip() or None
    to = None
    if s.get("dest_lat") is not None and s.get("dest_lng") is not None:
        to = {"lat": s.get("dest_lat"), "lng": s.get("dest_lng"), "address": addr}
    elif addr:
        hit = _TRACK_GEO_CACHE["geocode"].get(addr)
        if hit is None and addr not in _TRACK_GEO_CACHE["geocode"]:
            try:
                g = _track_get_json(
                    "https://nominatim.openstreetmap.org/search",
                    params={"format": "json", "limit": 1, "countrycodes": "us", "q": addr},
                    headers=_NOMINATIM_HEADERS,
                    timeout=15,
                )
                hit = {"lat": float(g[0]["lat"]), "lng": float(g[0]["lon"])} if g else None
            except Exception:
                hit = None
            _TRACK_GEO_CACHE["geocode"][addr] = hit
        if hit:
            to = {"lat": hit["lat"], "lng": hit["lng"], "address": addr}
            try:
                # PATCH resolved coordinates back onto the session; the column
                # may not exist yet in prod, so failures are non-fatal.
                db.client.table("driver_tracking_sessions").update(
                    {"dest_lat": hit["lat"], "dest_lng": hit["lng"]}
                ).eq("token", token_key).execute()
            except Exception:
                pass

    if frm:
        rkey = (_track_round3(frm["lat"]), _track_round3(frm["lng"]))
        cached = _TRACK_GEO_CACHE["reverse"].get(rkey)
        if cached is None and rkey not in _TRACK_GEO_CACHE["reverse"]:
            try:
                rv = _track_get_json(
                    "https://nominatim.openstreetmap.org/reverse",
                    params={"format": "json", "zoom": 16, "lat": frm["lat"], "lon": frm["lng"]},
                    headers=_NOMINATIM_HEADERS,
                    timeout=15,
                )
                a = rv.get("address") or {}
                parts = [
                    " ".join(x for x in (a.get("house_number"), a.get("road")) if x),
                    a.get("city") or a.get("town") or a.get("village") or a.get("county"),
                    (f"{a.get('state')} {a.get('postcode') or ''}".strip() if a.get("state") else a.get("postcode")),
                ]
                cached = ", ".join(p for p in parts if p) or rv.get("display_name")
            except Exception:
                cached = None
            _TRACK_GEO_CACHE["reverse"][rkey] = cached
        frm["address"] = cached

    route = None
    if frm and to:
        key = (_track_round3(frm["lat"]), _track_round3(frm["lng"]),
               _track_round3(to["lat"]), _track_round3(to["lng"]))
        hit = _TRACK_GEO_CACHE["route"].get(key)
        if hit and time.time() - hit[0] < _TRACK_ROUTE_TTL_S:
            route = hit[1]
        else:
            try:
                rj = _track_get_json(
                    "https://router.project-osrm.org/route/v1/driving/"
                    f"{frm['lng']},{frm['lat']};{to['lng']},{to['lat']}",
                    params={"overview": "full", "geometries": "geojson"},
                    timeout=20,
                )
                r0 = (rj.get("routes") or [None])[0]
                if r0:
                    route = {
                        "duration": r0.get("duration") or 0,
                        "distance": r0.get("distance") or 0,
                        "coords": [[c[1], c[0]] for c in (r0.get("geometry") or {}).get("coordinates", [])],
                    }
            except Exception:
                route = None
            _TRACK_GEO_CACHE["route"][key] = (time.time(), route)

    def _eta(sec):
        m = int(round((sec or 0) / 60))
        if m < 1:
            return "under 1 min"
        if m < 60:
            return f"{m} min"
        return f"{m // 60}h {m % 60}m" if m % 60 else f"{m // 60}h"

    return {
        "ok": True, "token": token_key,
        "reference_id": s.get("reference_id"), "driver_name": s.get("driver_name"),
        "status": s.get("status"),
        "from": frm,
        "to": to or ({"address": addr} if addr else None),
        "eta_seconds": route and route["duration"],
        "eta_label": _eta(route["duration"]) if route else None,
        "distance_meters": route and route["distance"],
        "distance_label": (f"{route['distance'] / 1609.344:.1f} mi" if route else None),
        "geometry": route and route["coords"],
    }
