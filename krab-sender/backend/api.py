from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pathlib import Path
from zoneinfo import ZoneInfo
import os

from pydantic import BaseModel
import httpx

from .config import ApiConfig
from .db import init_db
from .repository import (
    get_latest_transaction,
    list_transactions,
    get_rolling_summary_ny,
    list_recipients,
    get_recipient_by_id,
    create_recipient,
    delete_recipient,
)
from .issuer_supabase import (
    fetch_full_lead_context_by_references,
    fetch_lead_meta_by_references,
)
from .issuer_admin_api import router as issuer_admin_router
from .issuer_admin_db import IssuerAdminDatabase


NY_TZ = ZoneInfo("America/New_York")

_TXN_PERIOD_DAYS = {
    "1w": 7,
    "2w": 14,
    "3w": 21,
    "1m": 30,
    "3m": 90,
    "6m": 180,
    "12m": 365,
}


def _transactions_since_utc_for_period(period: Optional[str]) -> Optional[datetime]:
    """Return timezone-aware UTC lower bound for rolling windows, or None for all-time."""
    if not period:
        return None
    key = str(period).strip().lower()
    if key in ("", "all"):
        return None
    days = _TXN_PERIOD_DAYS.get(key)
    if days is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid period. Use: 1w, 2w, 3w, 1m, 3m, 6m, 12m, all",
        )
    return datetime.now(timezone.utc) - timedelta(days=days)


def _enrich_tx_rows_with_lead_meta(config: ApiConfig, rows: list[dict]) -> list[dict]:
    """Attach Krab Issuer lead client name + receipt URL when Supabase is configured."""
    if not config.supabase_configured() or not rows:
        for r in rows:
            r.setdefault("lead_client_name", None)
            r.setdefault("receipt_image_url", None)
            r.setdefault("price", None)
            r.setdefault("receipt_price", None)
            r.setdefault("lead_client_phone", None)
            r.setdefault("lead_client_email", None)
        return rows
    refs = [(r.get("reference_id") or "").strip() for r in rows if (r.get("reference_id") or "").strip()]
    meta = (
        fetch_lead_meta_by_references(config.supabase_url, config.supabase_service_role_key, refs)
        if refs
        else {}
    )
    for r in rows:
        ref = (r.get("reference_id") or "").strip()
        if not ref:
            r["lead_client_name"] = None
            r["receipt_image_url"] = None
            r["price"] = None
            r["receipt_price"] = None
            r["lead_client_phone"] = None
            r["lead_client_email"] = None
            continue
        m = meta.get(ref)
        if not m:
            r["lead_client_name"] = None
            r["receipt_image_url"] = None
            r["price"] = None
            r["receipt_price"] = None
            r["lead_client_phone"] = None
            r["lead_client_email"] = None
        else:
            r["lead_client_name"] = m.get("lead_client_name")
            r["receipt_image_url"] = m.get("receipt_image_url")
            r["price"] = m.get("price")
            r["receipt_price"] = m.get("receipt_price")
            r["lead_client_phone"] = m.get("lead_client_phone")
            r["lead_client_email"] = m.get("lead_client_email")
    return rows


def _enrich_summary_with_lead_meta(config: ApiConfig, summary: dict) -> dict:
    out = dict(summary)
    items = out.get("items")
    if isinstance(items, list):
        out["items"] = _enrich_tx_rows_with_lead_meta(config, [dict(x) for x in items])
    return out

app = FastAPI(title="Krab Dispatch Admin API")

_cfg_for_cors = ApiConfig.from_env()
# CORS: explicit origins (prod + local) + regex to cover Vercel preview deploy URLs.
# Configure via CORS_ORIGINS and CORS_ORIGIN_REGEX in backend config.
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(_cfg_for_cors.cors_origins),
    allow_origin_regex=_cfg_for_cors.cors_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_api_config() -> ApiConfig:
    # Cached via FastAPI dependency system; cheap to construct.
    return ApiConfig.from_env()


def require_admin(
    x_admin_password: str = Header(..., alias="X-Admin-Password"),
    config: ApiConfig = Depends(get_api_config),
) -> None:
    """
    Simple header-based auth for admin endpoints.

    The frontend will send X-Admin-Password, which must match ADMIN_PASSWORD
    from the environment.
    """
    if x_admin_password != config.admin_password:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin credentials",
        )


# Public-path exceptions for the admin lock screen. The Krab Dispatch tab on
# /backend lets non-authenticated users register a new driver chatID without
# unlocking the dashboard. Only the POST is exempt - read endpoints stay locked.
_PUBLIC_ISSUER_PATHS = {
    ("POST", "/issuer-admin/drivers"),
    ("GET", "/issuer-admin/receipts/view"),
}


def require_admin_or_public(
    request: Request,
    x_admin_password: Optional[str] = Header(None, alias="X-Admin-Password"),
    config: ApiConfig = Depends(get_api_config),
) -> None:
    path = request.url.path.rstrip("/") or "/"
    if (request.method.upper(), path) in _PUBLIC_ISSUER_PATHS:
        return
    if x_admin_password is None or x_admin_password != config.admin_password:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin credentials",
        )


app.include_router(
    issuer_admin_router,
    prefix="/issuer-admin",
    tags=["issuer-admin"],
    dependencies=[Depends(require_admin_or_public)],
)


@app.on_event("startup")
def on_startup():
    # Ensure database is ready before serving any requests.
    init_db()


@app.get("/health")
def health():
    """
    Basic health-check endpoint for the Admin Dashboard.
    """
    return {"status": "ok", "service": "krab-dispatch-api"}


# Public endpoint: anyone can view recent transactions
@app.get("/transactions/public")
def transactions_public(
    limit: int = 10,
    offset: int = 0,
    config: ApiConfig = Depends(get_api_config),
):
    """
    Public endpoint to view recent transactions (no auth required).
    Used by the Telegram bot's /transactions command.

    Supports simple pagination via limit & offset.
    """
    items = list_transactions(limit=limit, offset=offset)
    result = []
    for tx in items:
        ts_ny = tx.timestamp.astimezone(NY_TZ)
        result.append(
            {
                "id": tx.id,
                "telegram_name": tx.telegram_name,
                "telegram_handle": tx.telegram_handle,
                "filename": tx.filename,
                "recipient_name": tx.recipient_name,
                "recipient_email": tx.recipient_email,
                "issuer_group": tx.issuer_group,
                "reference_id": tx.reference_id,
                "timestamp_ny": ts_ny.isoformat(),
                "delivery_status": tx.delivery_status,
            }
        )
    return _enrich_tx_rows_with_lead_meta(config, result)


# Public endpoint: bot needs to fetch recipients
@app.get("/recipients")
def recipients_public():
    """
    Public endpoint to fetch all recipients (for bot inline keyboard).
    Returns only name and id (no email exposed).
    """
    recipients = list_recipients()
    return [
        {
            "id": r["id"],
            "name": r["name"],
        }
        for r in recipients
    ]


@app.get("/recipients/{recipient_id}/email")
def recipient_email_public(recipient_id: str):
    """
    Public endpoint to get recipient email by ID (for bot to send emails).
    Returns only the email address for the given recipient ID.
    """
    recipient = get_recipient_by_id(recipient_id)
    if not recipient:
        raise HTTPException(status_code=404, detail="Recipient not found")
    return {
        "id": recipient["id"],
        "name": recipient["name"],
        "email": recipient["email"],
    }


@app.get("/transactions/latest", dependencies=[Depends(require_admin)])
def transactions_latest(config: ApiConfig = Depends(get_api_config)):
    """
    Returns the latest processed transaction, if any.
    """
    tx = get_latest_transaction()
    if not tx:
        return None

    ts_ny = tx.timestamp.astimezone(NY_TZ)
    row = {
        "id": tx.id,
        "telegram_name": tx.telegram_name,
        "telegram_handle": tx.telegram_handle,
        "filename": tx.filename,
        "client_details": tx.client_details,
        "recipient_name": tx.recipient_name,
        "recipient_email": tx.recipient_email,
        "issuer_group": tx.issuer_group,
        "reference_id": tx.reference_id,
        "timestamp_ny": ts_ny.isoformat(),
        "delivery_status": tx.delivery_status,
    }
    enriched = _enrich_tx_rows_with_lead_meta(config, [row])
    return enriched[0] if enriched else row


@app.get("/transactions", dependencies=[Depends(require_admin)])
def transactions(
    limit: int = 100,
    offset: int = 0,
    period: Optional[str] = Query(None, description="1w|2w|3w|1m|3m|6m|12m|all"),
    config: ApiConfig = Depends(get_api_config),
):
    """
    Paginated list of transactions for the dashboard data table.
    """
    since = _transactions_since_utc_for_period(period)
    items = list_transactions(limit=limit, offset=offset, since_utc=since)
    result = []
    for tx in items:
        ts_ny = tx.timestamp.astimezone(NY_TZ)
        result.append(
            {
                "id": tx.id,
                "telegram_name": tx.telegram_name,
                "telegram_handle": tx.telegram_handle,
                "filename": tx.filename,
                "client_details": tx.client_details,
                "recipient_name": tx.recipient_name,
                "recipient_email": tx.recipient_email,
                "issuer_group": tx.issuer_group,
                "reference_id": tx.reference_id,
                "timestamp_ny": ts_ny.isoformat(),
                "delivery_status": tx.delivery_status,
            }
        )

    return _enrich_tx_rows_with_lead_meta(config, result)


@app.get("/transactions/full", dependencies=[Depends(require_admin)])
def transactions_full(
    limit: int = 200,
    offset: int = 0,
    period: Optional[str] = Query(None, description="1w|2w|3w|1m|3m|6m|12m|all"),
    config: ApiConfig = Depends(get_api_config),
):
    """
    Cross-bot spreadsheet endpoint used by the Transactions tab.

    Joins, with forward-step validation at each step:
      Dispatcher (this DB)  →  Issuer lead (Supabase)
                            →  Issuer group (Supabase)
                            →  Driver assignments (Supabase)

    Each row combines:
      - Tag name (from Issuer lead `vehicle_details`)
      - Receipt picture URL (from Issuer lead `receipt_image_url`)
      - Price (from Issuer lead `price`)
      - Issuer group + submitting assistant (Issuer `telegram_username` / user_id)
      - Driver history (all assignments, incl. reassignments) + accepted driver
      - Dispatcher (this DB `telegram_name` / handle)
      - Timestamp (this DB `timestamp`, NY ISO)
    """
    safe_limit = max(1, min(int(limit or 200), 2000))
    safe_offset = max(0, int(offset or 0))
    since = _transactions_since_utc_for_period(period)
    items = list_transactions(limit=safe_limit, offset=safe_offset, since_utc=since)
    if not items:
        return []

    refs: list[str] = []
    for tx in items:
        r = (tx.reference_id or "").strip()
        if r and r not in refs:
            refs.append(r)

    lead_ctx: dict[str, dict] = {}
    if refs and config.supabase_configured():
        try:
            lead_ctx = fetch_full_lead_context_by_references(
                config.supabase_url,
                config.supabase_service_role_key,
                refs,
            ) or {}
        except Exception:
            lead_ctx = {}

    out: list[dict] = []
    for tx in items:
        ts_ny = tx.timestamp.astimezone(NY_TZ)
        ref = (tx.reference_id or "").strip() or None
        ctx = lead_ctx.get(ref) if ref else None

        dispatcher_name = (tx.telegram_name or "").strip() or None
        dispatcher_handle = (tx.telegram_handle or "").strip() or None

        history = (ctx or {}).get("driver_history") or []
        accepted = (ctx or {}).get("accepted_driver") or None
        selected_name = (tx.recipient_name or "").strip() or None

        out.append(
            {
                "id": tx.id,
                "reference_id": ref,
                "timestamp_ny": ts_ny.isoformat(),
                "filename": tx.filename,
                "delivery_status": tx.delivery_status,
                "tag_name": (ctx or {}).get("tag_name"),
                "price": (ctx or {}).get("price"),
                "receipt_price": (ctx or {}).get("receipt_price"),
                "receipt_image_url": (ctx or {}).get("receipt_image_url"),
                "issuer_group": (ctx or {}).get("group_name") or tx.issuer_group,
                "issuer_submitter_handle": (ctx or {}).get("submitted_by_handle"),
                "issuer_submitter_telegram_id": (ctx or {}).get("submitted_by_telegram_id"),
                "dispatcher_name": dispatcher_name,
                "dispatcher_handle": dispatcher_handle,
                "driver_selected_name": selected_name,
                "driver_recipient_email": tx.recipient_email,
                "driver_accepted": accepted,
                "driver_history": history,
                "lead_id": (ctx or {}).get("lead_id"),
            }
        )

    return out


@app.options("/transactions/full")
def options_transactions_full():
    return {}


@app.get("/summaries/weekly/previous", dependencies=[Depends(require_admin)])
def weekly_previous_summary(config: ApiConfig = Depends(get_api_config)):
    """
    Returns a rolling 7‑day summary in America/New_York time.
    This is what the Saturday 12 AM NJ-time cron job will generate,
    and can also be triggered manually at any time.
    """
    return _enrich_summary_with_lead_meta(config, get_rolling_summary_ny(days=7))


@app.get("/summaries/rolling", dependencies=[Depends(require_admin)])
def rolling_summary(window: str = "1m", config: ApiConfig = Depends(get_api_config)):
    """
    Returns rolling summaries for multiple windows:
    1w, 1m, 3m, 6m, 1y, all
    """
    mapping = {
        "1w": 7,
        "1m": 30,
        "3m": 90,
        "6m": 180,
        "1y": 365,
        "all": None,
    }
    key = (window or "1m").lower()
    if key not in mapping:
        raise HTTPException(
            status_code=400,
            detail="Invalid window. Use: 1w, 1m, 3m, 6m, 1y, all",
        )
    return _enrich_summary_with_lead_meta(config, get_rolling_summary_ny(days=mapping[key]))


# Explicit OPTIONS handlers so browser CORS preflight succeeds from Vercel
@app.options("/transactions")
def options_transactions():
    # CORSMiddleware will add the appropriate CORS headers.
    return {}


@app.options("/transactions/latest")
def options_transactions_latest():
    return {}

@app.options("/summaries/weekly/previous")
def options_weekly_previous_summary():
    return {}


@app.options("/summaries/rolling")
def options_rolling_summary():
    return {}


# Recipient management endpoints (admin only)
class RecipientCreate(BaseModel):
    name: str
    email: str


class DispatchDriverCreate(BaseModel):
    driver_name: str
    driver_telegram_id: str
    phone_number: Optional[str] = None


class SummaryAiAskRequest(BaseModel):
    question: str
    summary: dict
    window: str | None = None
    history: list[dict] | None = None
    # Optional Issuer-admin snapshot from the client (Krab Issuer tab) for chat context.
    issuer_admin_snapshot: dict | None = None


def _extract_openai_answer(data: dict) -> str:
    if not isinstance(data, dict):
        return ""
    direct = data.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()

    output = data.get("output") or []
    if not isinstance(output, list):
        return ""

    parts: list[str] = []
    for block in output:
        if not isinstance(block, dict):
            continue
        content = block.get("content") or []
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict):
                continue
            txt = item.get("text")
            if isinstance(txt, str) and txt.strip():
                parts.append(txt.strip())
                continue
            if isinstance(txt, dict):
                maybe = txt.get("value") or txt.get("text")
                if isinstance(maybe, str) and maybe.strip():
                    parts.append(maybe.strip())
                    continue
            maybe = item.get("value") or item.get("output_text")
            if isinstance(maybe, str) and maybe.strip():
                parts.append(maybe.strip())

    return "\n".join(parts).strip()


def _openai_model_candidates() -> list[str]:
    """
    Ordered model fallback list.
    Set OPENAI_MODELS as comma-separated list to override.
    """
    raw = (os.getenv("OPENAI_MODELS") or "").strip()
    if raw:
        models = [m.strip() for m in raw.split(",") if m.strip()]
        if models:
            return models
    return ["gpt-5", "gpt-4.1-mini", "gpt-4o-mini"]


@app.get("/recipients/all", dependencies=[Depends(require_admin)])
def recipients_all():
    """
    Admin endpoint: list all recipients with full details.
    """
    return list_recipients()


@app.post("/recipients", dependencies=[Depends(require_admin)])
def recipients_create(recipient: RecipientCreate):
    """
    Admin endpoint: create a new recipient.
    """
    return create_recipient(name=recipient.name, email=recipient.email)


@app.delete("/recipients/{recipient_id}", dependencies=[Depends(require_admin)])
def recipients_delete(recipient_id: str):
    """
    Admin endpoint: delete a recipient by ID.
    """
    deleted = delete_recipient(recipient_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Recipient not found")
    return {"success": True}


@app.get("/recipients/ui")
def recipients_ui_list():
    """
    Same payload as /recipients/all for the static dashboard Add driver card.

    Intentionally does not require admin auth so operators can manage dispatch
    email targets from the Krab Issuer tab without unlocking with a password.
    """
    return list_recipients()


@app.post("/recipients/ui")
def recipients_ui_create(recipient: RecipientCreate):
    """Create recipient — open auth path paired with GET /recipients/ui."""
    return create_recipient(name=recipient.name, email=recipient.email)


@app.delete("/recipients/ui/{recipient_id}")
def recipients_ui_delete(recipient_id: str):
    deleted = delete_recipient(recipient_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Recipient not found")
    return {"success": True}


def _issuer_db_open(config: ApiConfig) -> IssuerAdminDatabase:
    if not config.supabase_configured():
        raise HTTPException(
            status_code=503,
            detail="Configure SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY for Krab Issuer admin.",
        )
    return IssuerAdminDatabase(config.supabase_url, config.supabase_service_role_key)


@app.get("/dispatch-drivers/ui")
def dispatch_drivers_ui_list(config: ApiConfig = Depends(get_api_config)):
    """
    Open dashboard list — same Supabase drivers table used by Issuer admin.
    """
    db = _issuer_db_open(config)
    return db.get_all_drivers()


@app.post("/dispatch-drivers/ui/{driver_id}/toggle")
def dispatch_drivers_ui_toggle(driver_id: str, config: ApiConfig = Depends(get_api_config)):
    db = _issuer_db_open(config)
    if db.toggle_driver_status(driver_id):
        return {"success": True}
    raise HTTPException(status_code=400, detail="Could not toggle driver")


@app.post("/dispatch-drivers/ui")
def dispatch_drivers_ui_create(body: DispatchDriverCreate, config: ApiConfig = Depends(get_api_config)):
    """
    Open (no-admin-password) endpoint used by the dashboard to add a Dispatch driver
    by Telegram chat/user ID. Stored in Issuer Supabase `drivers` table.
    """
    db = _issuer_db_open(config)
    try:
        ok = db.create_driver(
            body.driver_name,
            body.driver_telegram_id,
            (body.phone_number or "").strip() or None,
        )
    except Exception as e:
        err = str(e).lower()
        if any(x in err for x in ("unique", "duplicate", "23505", "violates")):
            raise HTTPException(status_code=409, detail="A driver with this Telegram ID already exists.") from e
        raise HTTPException(status_code=500, detail=str(e)) from e
    if not ok:
        raise HTTPException(status_code=500, detail="Could not add driver")
    return {"success": True, "message": "Driver added"}


@app.options("/recipients")
def options_recipients():
    return {}


@app.options("/recipients/all")
def options_recipients_all():
    return {}


@app.options("/recipients/ui")
def options_recipients_ui():
    return {}


@app.options("/recipients/ui/{recipient_id}")
def options_recipients_ui_id(recipient_id: str):
    return {}


@app.options("/dispatch-drivers/ui")
def options_dispatch_drivers_ui():
    return {}


@app.options("/dispatch-drivers/ui/{driver_id}/toggle")
def options_dispatch_drivers_ui_toggle(driver_id: str):
    return {}


@app.options("/transactions/public")
def options_transactions_public():
    return {}


def _compact_issuer_lead_ctx_for_ai(ctx: dict) -> dict:
    """Shrink Supabase lead context for the AI prompt (token-safe)."""
    if not isinstance(ctx, dict):
        return {}
    hist = ctx.get("driver_history") or []
    if isinstance(hist, list):
        hist = hist[:8]
    ad = ctx.get("accepted_driver")
    if isinstance(ad, dict):
        ad = {k: ad.get(k) for k in ("driver_name", "driver_telegram_id", "accepted_at")}
    return {
        "lead_id": ctx.get("lead_id"),
        "tag_name": ctx.get("tag_name"),
        "price": ctx.get("price"),
        "receipt_price": ctx.get("receipt_price"),
        "receipt_image_url": bool(ctx.get("receipt_image_url")),
        "submitted_by_handle": ctx.get("submitted_by_handle"),
        "group_name": ctx.get("group_name"),
        "driver_history": hist,
        "accepted_driver": ad,
    }


@app.post("/ai/summary-ask", dependencies=[Depends(require_admin)])
async def ai_summary_ask(
    payload: SummaryAiAskRequest,
    config: ApiConfig = Depends(get_api_config),
):
    """
    Ask GPT-5 questions about rolling summary data, with optional Issuer (Supabase)
    lead context joined by reference_id (same linkage as the unified Transactions tab).
    """
    question = (payload.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question is required")

    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise HTTPException(
            status_code=503, detail="OPENAI_API_KEY is not configured on server"
        )

    # Forward-step validation + data access: fetch server-side for selected window.
    mapping = {"1w": 7, "1m": 30, "3m": 90, "6m": 180, "1y": 365, "all": None}
    requested_window = (payload.window or "1w").lower()
    days = mapping.get(requested_window, 7)
    try:
        full_summary = get_rolling_summary_ny(days=days, max_items=None)
    except TypeError:
        # Backward compatibility if repository signature differs on some deploys.
        full_summary = get_rolling_summary_ny(days=days)
    except Exception:
        # Never hard-fail AI chat due to summary load issues; use client-provided data.
        full_summary = payload.summary or {}
    items = full_summary.get("items") or []
    if not isinstance(items, list):
        items = []

    # Bound prompt size while still using server-fetched data.
    compact_items = items[:1500]
    compact_summary = {
        "window": requested_window,
        "period_start_ny": full_summary.get("period_start_ny"),
        "period_end_ny": full_summary.get("period_end_ny"),
        "total_transactions": full_summary.get("total_transactions"),
        "delivered": full_summary.get("delivered"),
        "pending": full_summary.get("pending"),
        "failed": full_summary.get("failed"),
        "items": compact_items,
    }

    cross_bot: dict = {
        "note": (
            "Krab Dispatch rows (summary items) tie to Krab Issuer Supabase leads via "
            "`reference_id` when present."
        ),
        "issuer_leads_by_reference_id": {},
    }
    refs: list[str] = []
    seen_r: set[str] = set()
    for it in compact_items:
        if not isinstance(it, dict):
            continue
        r = str(it.get("reference_id") or "").strip()
        if r and r not in seen_r:
            seen_r.add(r)
            refs.append(r)
        if len(refs) >= 100:
            break
    if refs and config.supabase_configured():
        try:
            full_ctx = fetch_full_lead_context_by_references(
                config.supabase_url,
                config.supabase_service_role_key,
                refs,
            )
            for ref in refs:
                raw = full_ctx.get(ref) if isinstance(full_ctx, dict) else None
                if isinstance(raw, dict):
                    cross_bot["issuer_leads_by_reference_id"][ref] = (
                        _compact_issuer_lead_ctx_for_ai(raw)
                    )
        except Exception:
            pass

    issuer_snap = payload.issuer_admin_snapshot
    if isinstance(issuer_snap, dict) and issuer_snap:
        # Bound size (client may send stats / driver lists).
        cross_bot["issuer_admin_snapshot"] = (
            issuer_snap if len(str(issuer_snap)) < 12000 else {"_truncated": True}
        )

    history = payload.history or []
    compact_history = []
    for h in history[-12:]:
        role = str(h.get("role") or "").strip().lower()
        content = str(h.get("content") or "").strip()
        if role in {"user", "assistant"} and content:
            compact_history.append({"role": role, "content": content[:1000]})

    system_prompt = (
        "You are a friendly analytics copilot for Krab Dispatch + Krab Issuer. "
        "Krab Dispatch stores transmission rows (Telegram sends, drivers, delivery status). "
        "Krab Issuer stores leads in Supabase (tag name, price, receipts, assignments). "
        "When a row has reference_id, the two systems describe the same real-world lead—use "
        "cross_bot.issuer_leads_by_reference_id together with summary items to reason across both. "
        "For factual claims use ONLY the provided JSON (summary, cross_bot, issuer_admin_snapshot). "
        "For greetings or small talk, respond naturally. If data is missing, say so briefly. "
        "Keep answers concise."
    )
    user_prompt = (
        f"Conversation history: {compact_history}\n\n"
        f"Question: {question}\n\n"
        f"Summary JSON (Dispatch rolling window): {compact_summary}\n\n"
        f"Cross-bot context (Issuer joined by reference_id): {cross_bot}"
    )

    errors: list[str] = []
    models = _openai_model_candidates()

    async def _ask_once(model: str, user_content: str) -> tuple[str, str]:
        try:
            async with httpx.AsyncClient(timeout=25.0) as client:
                res = await client.post(
                    "https://api.openai.com/v1/responses",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "input": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_content},
                        ],
                        "max_output_tokens": 320,
                    },
                )
        except Exception as e:
            return "", f"{model}: request failed ({e})"
        if res.status_code >= 400:
            return "", f"{model}: HTTP {res.status_code}"
        try:
            data = res.json()
        except Exception as e:
            return "", f"{model}: invalid JSON ({e})"
        answer_text = _extract_openai_answer(data)
        if not answer_text:
            return "", f"{model}: empty answer"
        return answer_text, ""

    answer = ""
    for model in models:
        answer, err = await _ask_once(model, user_prompt)
        if answer:
            break
        if err:
            errors.append(err)

    if not answer:
        retry_user_prompt = (
            "Answer the question directly. "
            "If it is general conversation, respond conversationally. "
            "If it is data-related, rely on summary JSON and cross_bot context.\n\n"
            f"Question: {question}\n\n"
            f"Summary JSON: {compact_summary}\n\n"
            f"Cross-bot: {cross_bot}"
        )
        for model in models:
            answer, err = await _ask_once(model, retry_user_prompt)
            if answer:
                break
            if err:
                errors.append(err)

    if not answer:
        # Keep response shape stable for frontend chat UI while reporting backend issue.
        return {
            "answer": "AI is temporarily unavailable. Please try again in a moment.",
            "error": " ; ".join(errors[:6]),
        }
    return {"answer": answer}


@app.options("/ai/summary-ask")
def options_ai_summary_ask():
    return {}


# Serve the admin dashboard at /admin (works regardless of current working directory).
_ADMIN_DIR = Path(__file__).resolve().parent.parent / "admin"
if _ADMIN_DIR.is_dir():
    app.mount(
        "/admin",
        StaticFiles(directory=str(_ADMIN_DIR), html=True),
        name="admin",
    )


@app.get("/", include_in_schema=False)
def root_redirect():
    """Open the admin dashboard by default when hitting the API base."""
    return RedirectResponse(url="/admin/")

