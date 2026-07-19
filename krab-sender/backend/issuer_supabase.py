"""
Optional Supabase (Krab Issuer) integration: match tag PDF filenames to open leads,
mark tag as sent, and read receipt URLs for the admin dashboard.
Requires SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY and migration_krab_dispatch_tag_tracking.sql on leads.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


def _headers(service_key: str) -> dict[str, str]:
    return {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }


def _first_nonblank_line(text: str) -> str:
    for line in (text or "").splitlines():
        s = line.strip()
        if s:
            return s
    return ""


def _normalize_alnum(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _tokenize(s: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", s.lower()) if len(t) >= 2]


def _lead_name_from_vehicle_details(vehicle_details: str) -> str:
    return _first_nonblank_line(vehicle_details or "")


def score_filename_against_lead(file_stem: str, vehicle_details: str) -> float:
    """How well the PDF stem matches the lead client name (first line of vehicle_details)."""
    name_line = _first_nonblank_line(vehicle_details)
    if len(name_line) < 2:
        return 0.0
    stem = (file_stem or "").strip()
    if not stem:
        return 0.0

    ft = _tokenize(stem)
    nt = _tokenize(name_line)
    token_score = 0.0
    if ft and nt:
        fset, nset = set(ft), set(nt)
        inter = fset & nset
        if inter:
            recall = len(inter) / max(len(nset), 1)
            prec = len(inter) / max(len(fset), 1)
            token_score = min(1.0, 0.55 * recall + 0.45 * prec)

    a = _normalize_alnum(stem)
    b = _normalize_alnum(name_line)
    seq_score = 0.0
    if len(a) >= 3 and len(b) >= 3:
        seq_score = float(SequenceMatcher(None, a, b).ratio())

    # Bonus when the filename alnum is a substring of the name alnum (or vice versa).
    # This covers cases like `kylesmithreceipt.pdf` against client "Kyle Smith".
    substr_score = 0.0
    if len(a) >= 3 and len(b) >= 3:
        if a in b:
            substr_score = 0.7 + 0.3 * (len(a) / max(len(b), 1))
        elif b in a:
            substr_score = 0.7 + 0.3 * (len(b) / max(len(a), 1))

    return max(token_score, seq_score, substr_score)


def _fetch_reference_leads(
    base_url: str,
    service_key: str,
    *,
    limit: int = 300,
    pending_only: bool = False,
) -> list[dict[str, Any]]:
    """
    Fetch leads that have a reference_id.
    pending_only=True limits to records where krab_tag_pdf_sent_at is null.
    """
    base = (base_url or "").strip().rstrip("/")
    key = (service_key or "").strip()
    if not base or not key:
        return []
    params = {
        "select": "id,reference_id,vehicle_details,receipt_image_url,krab_tag_pdf_sent_at,created_at",
        "reference_id": "not.is.null",
        "order": "created_at.desc",
        "limit": str(max(1, min(int(limit or 300), 1000))),
    }
    if pending_only:
        params["krab_tag_pdf_sent_at"] = "is.null"
    try:
        with httpx.Client(timeout=30.0) as client:
            r = client.get(f"{base}/rest/v1/leads", params=params, headers=_headers(key))
        if r.status_code >= 400:
            logger.warning("_fetch_reference_leads HTTP %s: %s", r.status_code, (r.text or "")[:400])
            return []
        data = r.json()
        if not isinstance(data, list):
            return []
        out: list[dict[str, Any]] = []
        for row in data:
            rid = (row.get("reference_id") or "").strip()
            if rid:
                out.append(row)
        return out
    except Exception as e:
        logger.warning("_fetch_reference_leads: %s", e)
        return []


def fetch_pending_tag_leads(base_url: str, service_key: str, limit: int = 200) -> list[dict[str, Any]]:
    return _fetch_reference_leads(
        base_url,
        service_key,
        limit=limit,
        pending_only=True,
    )


def search_leads_by_name_tokens(
    base_url: str,
    service_key: str,
    tokens: list[str],
    *,
    limit: int = 80,
) -> list[dict[str, Any]]:
    """
    Directly search Issuer leads where vehicle_details contains any given name token.
    Uses PostgREST `ilike.any` to avoid downloading the full table.
    """
    base = (base_url or "").strip().rstrip("/")
    key = (service_key or "").strip()
    clean = [t for t in (tokens or []) if isinstance(t, str) and len(t.strip()) >= 2]
    if not base or not key or not clean:
        return []
    # PostgREST: OR over multiple `ilike` filters on the same column.
    clauses = ",".join(f"vehicle_details.ilike.*{t}*" for t in clean[:6])
    params = {
        "select": "id,reference_id,vehicle_details,receipt_image_url,krab_tag_pdf_sent_at,created_at",
        "or": f"({clauses})",
        "reference_id": "not.is.null",
        "order": "created_at.desc",
        "limit": str(max(1, min(int(limit or 80), 200))),
    }
    try:
        with httpx.Client(timeout=25.0) as client:
            r = client.get(f"{base}/rest/v1/leads", params=params, headers=_headers(key))
        if r.status_code >= 400:
            logger.warning(
                "search_leads_by_name_tokens HTTP %s: %s",
                r.status_code,
                (r.text or "")[:400],
            )
            return []
        data = r.json()
        if not isinstance(data, list):
            return []
        out: list[dict[str, Any]] = []
        for row in data:
            rid = (row.get("reference_id") or "").strip()
            if rid:
                out.append(row)
        logger.info(
            "search_leads_by_name_tokens: tokens=%s hits=%d",
            clean[:6],
            len(out),
        )
        return out
    except Exception as e:
        logger.warning("search_leads_by_name_tokens: %s", e)
        return []


def _ai_pick_reference(
    file_stem: str,
    context_text: str,
    rows: list[dict[str, Any]],
) -> Optional[str]:
    """
    Optional OpenAI tie-breaker for ambiguous name matches.
    Returns a reference_id string or None.
    """
    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key or not rows:
        return None
    models = [m.strip() for m in (os.getenv("OPENAI_MODELS") or "").split(",") if m.strip()]
    if not models:
        models = ["gpt-5", "gpt-4.1-mini", "gpt-4o-mini"]

    compact = []
    for i, row in enumerate(rows[:40], start=1):
        compact.append(
            {
                "rank": i,
                "reference_id": (row.get("reference_id") or "").strip(),
                "name": _lead_name_from_vehicle_details(row.get("vehicle_details") or ""),
                "created_at": row.get("created_at"),
                "score": round(float(row.get("_score") or 0.0), 4),
            }
        )
    prompt = {
        "task": "Pick the best matching reference_id for this PDF filename/client text. If unsure, return NONE.",
        "filename_stem": file_stem,
        "context_text": (context_text or "")[:500],
        "candidates": compact,
        "output_format": {"reference_id": "AB12CD34 or NONE"},
    }
    payload = json.dumps(prompt, ensure_ascii=True)

    for model in models:
        try:
            with httpx.Client(timeout=20.0) as client:
                res = client.post(
                    "https://api.openai.com/v1/responses",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "input": [
                            {
                                "role": "system",
                                "content": "Reply with only a JSON object: {\"reference_id\":\"...\"}.",
                            },
                            {"role": "user", "content": payload},
                        ],
                        "max_output_tokens": 80,
                    },
                )
            if res.status_code >= 400:
                continue
            data = res.json()
            txt = (data.get("output_text") or "").strip()
            if not txt and isinstance(data.get("output"), list):
                for block in data.get("output") or []:
                    for item in block.get("content") or []:
                        t = item.get("text")
                        if isinstance(t, str) and t.strip():
                            txt = t.strip()
                            break
                    if txt:
                        break
            if not txt:
                continue
            m = re.search(r"([A-Z0-9]{8}|NONE)", txt.upper())
            if not m:
                continue
            picked = m.group(1)
            if picked == "NONE":
                return None
            for row in compact:
                if row["reference_id"] == picked:
                    return picked
        except Exception as e:
            logger.warning("_ai_pick_reference (%s): %s", model, e)
            continue
    return None


def get_pending_tag_lead_id_by_reference(
    base_url: str,
    service_key: str,
    reference_id: str,
) -> Optional[str]:
    """
    Return lead UUID if reference_id matches a lead whose Krab tag PDF has not been marked sent yet.
    Used so Dispatch can attach the correct ref when it appears anywhere in filename or client notes.
    """
    ref = (reference_id or "").strip().upper()
    base = (base_url or "").strip().rstrip("/")
    key = (service_key or "").strip()
    if not ref or not base or not key:
        return None
    params = {
        "select": "id",
        "reference_id": f"eq.{ref}",
        "krab_tag_pdf_sent_at": "is.null",
        "limit": "1",
    }
    try:
        with httpx.Client(timeout=20.0) as client:
            r = client.get(f"{base}/rest/v1/leads", params=params, headers=_headers(key))
        if r.status_code >= 400:
            return None
        data = r.json()
        if isinstance(data, list) and data:
            return str(data[0].get("id") or "").strip() or None
    except Exception as e:
        logger.warning("get_pending_tag_lead_id_by_reference: %s", e)
    return None


def resolve_reference_from_filename(
    base_url: str,
    service_key: str,
    file_name: str,
    *,
    context_text: str = "",
    min_score: float = 0.48,
) -> tuple[Optional[str], Optional[str]]:
    """
    Match PDF basename to a Krab Issuer lead by client name.
    Strategy:
      1. Build tokens from filename stem (and any context text).
      2. Query Issuer Supabase via `vehicle_details ilike.any(*token*)`.
      3. Also fetch recent leads as a fallback pool.
      4. Score union, return best above `min_score`, using AI tie-break when ambiguous.
    Returns (reference_id, lead_uuid) or (None, None).
    """
    stem = Path(file_name or "").stem.strip()
    if not stem:
        return None, None

    file_tokens = _tokenize(stem)
    context_tokens = _tokenize(context_text or "")
    all_tokens = list(dict.fromkeys(file_tokens + context_tokens))

    targeted = search_leads_by_name_tokens(base_url, service_key, all_tokens, limit=80)
    fallback = _fetch_reference_leads(base_url, service_key, limit=350, pending_only=False)

    seen: dict[str, dict[str, Any]] = {}
    for row in list(targeted) + list(fallback):
        rid = (row.get("reference_id") or "").strip()
        if rid and rid not in seen:
            seen[rid] = row
    leads = list(seen.values())

    logger.info(
        "resolve_reference_from_filename: stem=%r tokens=%s targeted=%d fallback=%d merged=%d",
        stem,
        all_tokens[:6],
        len(targeted),
        len(fallback),
        len(leads),
    )

    if not leads:
        return None, None

    scored: list[tuple[float, dict[str, Any]]] = []
    for row in leads:
        vd = row.get("vehicle_details") or ""
        sc = score_filename_against_lead(stem, vd)
        row["_score"] = sc
        scored.append((sc, row))
    scored.sort(key=lambda x: x[0], reverse=True)

    best_sc, best_row = scored[0]
    second_sc = scored[1][0] if len(scored) > 1 else 0.0
    top3 = [
        (_first_nonblank_line(r.get("vehicle_details") or ""), round(s, 3))
        for s, r in scored[:3]
    ]
    logger.info(
        "resolve_reference_from_filename: best=%.3f second=%.3f top3=%s",
        best_sc,
        second_sc,
        top3,
    )

    if best_sc >= min_score:
        ref = (best_row.get("reference_id") or "").strip()
        lid = str(best_row.get("id") or "").strip()
        if ref and lid:
            return ref, lid

    # Ambiguous / below threshold — try AI tie-break on top 40.
    picked_ref = _ai_pick_reference(stem, context_text, [r for _, r in scored[:40]])
    if picked_ref:
        for _, row in scored:
            ref = (row.get("reference_id") or "").strip()
            if ref == picked_ref:
                lid = str(row.get("id") or "").strip()
                if ref and lid:
                    return ref, lid
    return None, None


def fetch_lead_row_by_reference(
    base_url: str,
    service_key: str,
    reference_id: str,
) -> Optional[dict[str, Any]]:
    """
    Return the full Krab Issuer lead row matching ``reference_id``.

    Used by the Krab Dispatch bot's insurance flow to pull the same data the
    Issuer bot uses (vehicle_details, email, driver_license_id, ...) so the
    generated NY FS-20 card matches across bots.
    """
    ref = (reference_id or "").strip()
    base = (base_url or "").strip().rstrip("/")
    key = (service_key or "").strip()
    if not ref or not base or not key:
        return None
    params = {
        "select": "*",
        "reference_id": f"eq.{ref}",
        "limit": "1",
    }
    try:
        with httpx.Client(timeout=20.0) as client:
            r = client.get(f"{base}/rest/v1/leads", params=params, headers=_headers(key))
        if r.status_code >= 400:
            logger.warning(
                "fetch_lead_row_by_reference HTTP %s: %s",
                r.status_code,
                (r.text or "")[:300],
            )
            return None
        data = r.json()
        if isinstance(data, list) and data:
            row = data[0]
            if isinstance(row, dict):
                return row
    except Exception as e:
        logger.warning("fetch_lead_row_by_reference: %s", e)
    return None


def get_lead_id_by_reference(base_url: str, service_key: str, reference_id: str) -> Optional[str]:
    ref = (reference_id or "").strip()
    base = (base_url or "").strip().rstrip("/")
    if not ref or not base or not (service_key or "").strip():
        return None
    params = {"select": "id", "reference_id": f"eq.{ref}", "limit": "1"}
    try:
        with httpx.Client(timeout=20.0) as client:
            r = client.get(f"{base}/rest/v1/leads", params=params, headers=_headers(service_key.strip()))
        if r.status_code >= 400:
            return None
        data = r.json()
        if isinstance(data, list) and data:
            return str(data[0].get("id") or "").strip() or None
    except Exception as e:
        logger.warning("get_lead_id_by_reference: %s", e)
    return None


def mark_krab_tag_pdf_sent(base_url: str, service_key: str, lead_id: str) -> bool:
    lid = (lead_id or "").strip()
    base = (base_url or "").strip().rstrip("/")
    if not lid or not base or not (service_key or "").strip():
        return False
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    try:
        with httpx.Client(timeout=20.0) as client:
            r = client.patch(
                f"{base}/rest/v1/leads",
                params={"id": f"eq.{lid}"},
                headers=_headers(service_key.strip()),
                json={"krab_tag_pdf_sent_at": now},
            )
        if r.status_code >= 400:
            logger.warning("mark_krab_tag_pdf_sent HTTP %s: %s", r.status_code, (r.text or "")[:300])
            return False
        return True
    except Exception as e:
        logger.warning("mark_krab_tag_pdf_sent: %s", e)
        return False


def fetch_lead_meta_by_references(
    base_url: str,
    service_key: str,
    reference_ids: list[str],
) -> dict[str, dict[str, Any]]:
    """
    For each reference_id, return { lead_client_name, receipt_image_url, price, receipt_price } (best effort).
    """
    base = (base_url or "").strip().rstrip("/")
    key = (service_key or "").strip()
    uniq = []
    seen: set[str] = set()
    for r in reference_ids:
        s = (r or "").strip()
        if s and s not in seen:
            seen.add(s)
            uniq.append(s)
    if not base or not key or not uniq:
        return {}
    out: dict[str, dict[str, Any]] = {}
    chunk_size = 40
    for i in range(0, len(uniq), chunk_size):
        chunk = uniq[i : i + chunk_size]
        in_list = ",".join(chunk)
        params = {
            "select": "reference_id,vehicle_details,receipt_image_url,price,receipt_price,phone_number",
            "reference_id": f"in.({in_list})",
        }
        try:
            with httpx.Client(timeout=25.0) as client:
                r = client.get(f"{base}/rest/v1/leads", params=params, headers=_headers(key))
            if r.status_code >= 400:
                continue
            rows = r.json()
            if not isinstance(rows, list):
                continue
            for row in rows:
                ref = (row.get("reference_id") or "").strip()
                if not ref:
                    continue
                vd = row.get("vehicle_details") or ""
                raw_price = row.get("price")
                price_str = (
                    str(raw_price).strip()
                    if raw_price is not None and str(raw_price).strip()
                    else None
                )
                raw_receipt_price = row.get("receipt_price")
                receipt_price_str = (
                    str(raw_receipt_price).strip()
                    if raw_receipt_price is not None and str(raw_receipt_price).strip()
                    else None
                )
                out[ref] = {
                    "lead_client_name": _first_nonblank_line(vd) or "—",
                    "receipt_image_url": (row.get("receipt_image_url") or "").strip() or None,
                    "price": price_str,
                    "receipt_price": receipt_price_str,
                    "lead_client_phone": (str(row.get("phone_number") or "").strip() or None),
                }
        except Exception as e:
            logger.warning("fetch_lead_meta_by_references: %s", e)
    return out


def fetch_full_lead_context_by_references(
    base_url: str,
    service_key: str,
    reference_ids: list[str],
) -> dict[str, dict[str, Any]]:
    """
    Forward-step join used by the cross-bot Transactions spreadsheet.

    For each reference_id return a dict:
      {
        "lead_id": str,
        "tag_name": str,                 # first nonblank line of vehicle_details
        "price": str,                    # raw price string from lead
        "receipt_price": str | None,     # parsed / stored receipt total when driver uploads
        "receipt_image_url": str | None,
        "submitted_by_handle": str,      # telegram_username from lead
        "submitted_by_telegram_id": str, # user_id (Issuer bot submitter)
        "group_id": str | None,
        "group_name": str | None,
        "supervisory_telegram_id": str | None,
        "group_telegram_id": str | None,
        "created_at": str | None,
        "driver_history": list[ { driver_name, driver_telegram_id, status, accepted_at, created_at } ],
        "accepted_driver": { driver_name, driver_telegram_id, accepted_at } | None,
      }

    Each step is wrapped in its own try/except so a single failed join never
    destroys the whole response (forward-step validation).
    """
    base = (base_url or "").strip().rstrip("/")
    key = (service_key or "").strip()
    uniq: list[str] = []
    seen: set[str] = set()
    for r in reference_ids or []:
        s = (r or "").strip()
        if s and s not in seen:
            seen.add(s)
            uniq.append(s)
    if not base or not key or not uniq:
        return {}

    headers = _headers(key)
    out: dict[str, dict[str, Any]] = {}

    # Step 1: leads by reference_id (batch via `in`).
    leads_by_ref: dict[str, dict[str, Any]] = {}
    lead_id_to_ref: dict[str, str] = {}
    lead_id_to_group_id: dict[str, str] = {}
    chunk = 40
    for i in range(0, len(uniq), chunk):
        batch = uniq[i : i + chunk]
        in_list = ",".join(batch)
        params = {
            "select": (
                "id,reference_id,vehicle_details,price,receipt_price,receipt_image_url,"
                "user_id,telegram_username,group_id,created_at"
            ),
            "reference_id": f"in.({in_list})",
        }
        try:
            with httpx.Client(timeout=25.0) as client:
                r = client.get(f"{base}/rest/v1/leads", params=params, headers=headers)
            if r.status_code >= 400:
                logger.warning(
                    "fetch_full_lead_context leads HTTP %s: %s",
                    r.status_code,
                    (r.text or "")[:300],
                )
                continue
            rows = r.json()
            if not isinstance(rows, list):
                continue
            for row in rows:
                ref = (row.get("reference_id") or "").strip()
                if not ref:
                    continue
                leads_by_ref[ref] = row
                lid = str(row.get("id") or "").strip()
                if lid:
                    lead_id_to_ref[lid] = ref
                    gid = str(row.get("group_id") or "").strip()
                    if gid:
                        lead_id_to_group_id[lid] = gid
        except Exception as e:
            logger.warning("fetch_full_lead_context leads: %s", e)

    if not leads_by_ref:
        return {}

    # Step 2: groups by id.
    group_ids = sorted({gid for gid in lead_id_to_group_id.values()})
    groups_by_id: dict[str, dict[str, Any]] = {}
    for i in range(0, len(group_ids), chunk):
        batch = group_ids[i : i + chunk]
        in_list = ",".join(batch)
        params = {
            "select": "id,group_name,group_telegram_id,supervisory_telegram_id",
            "id": f"in.({in_list})",
        }
        try:
            with httpx.Client(timeout=20.0) as client:
                r = client.get(f"{base}/rest/v1/groups", params=params, headers=headers)
            if r.status_code >= 400:
                continue
            for row in r.json() or []:
                gid = str(row.get("id") or "").strip()
                if gid:
                    groups_by_id[gid] = row
        except Exception as e:
            logger.warning("fetch_full_lead_context groups: %s", e)

    # Step 3: lead_assignments for all lead ids (join driver inline).
    lead_ids = sorted(lead_id_to_ref.keys())
    assignments_by_lead: dict[str, list[dict[str, Any]]] = {lid: [] for lid in lead_ids}
    for i in range(0, len(lead_ids), chunk):
        batch = lead_ids[i : i + chunk]
        in_list = ",".join(batch)
        params = {
            "select": (
                "id,lead_id,driver_id,status,accepted_at,created_at,"
                "driver:drivers(driver_name,driver_telegram_id)"
            ),
            "lead_id": f"in.({in_list})",
            "order": "created_at",
        }
        try:
            with httpx.Client(timeout=25.0) as client:
                r = client.get(
                    f"{base}/rest/v1/lead_assignments",
                    params=params,
                    headers=headers,
                )
            if r.status_code >= 400:
                continue
            for row in r.json() or []:
                lid = str(row.get("lead_id") or "").strip()
                if lid and lid in assignments_by_lead:
                    assignments_by_lead[lid].append(row)
        except Exception as e:
            logger.warning("fetch_full_lead_context assignments: %s", e)

    # Step 4: assemble per-reference payload.
    for ref, lead in leads_by_ref.items():
        vd = lead.get("vehicle_details") or ""
        lid = str(lead.get("id") or "").strip()
        gid = str(lead.get("group_id") or "").strip()
        grp = groups_by_id.get(gid) if gid else None
        price_raw = lead.get("price")
        price_str = "" if price_raw is None else str(price_raw).strip()
        rcp_raw = lead.get("receipt_price")
        receipt_price_str = "" if rcp_raw is None else str(rcp_raw).strip()

        history: list[dict[str, Any]] = []
        accepted: dict[str, Any] | None = None
        for a in assignments_by_lead.get(lid, []):
            dr = a.get("driver") or {}
            item = {
                "driver_name": (dr.get("driver_name") or "").strip() or None,
                "driver_telegram_id": (dr.get("driver_telegram_id") or "").strip() or None,
                "status": (a.get("status") or "").strip() or None,
                "accepted_at": a.get("accepted_at"),
                "created_at": a.get("created_at"),
            }
            history.append(item)
            if not accepted and item["status"] == "accepted":
                accepted = {
                    "driver_name": item["driver_name"],
                    "driver_telegram_id": item["driver_telegram_id"],
                    "accepted_at": item["accepted_at"],
                }

        out[ref] = {
            "lead_id": lid or None,
            "tag_name": _first_nonblank_line(vd) or None,
            "price": price_str or None,
            "receipt_price": receipt_price_str or None,
            "receipt_image_url": (lead.get("receipt_image_url") or "").strip() or None,
            "submitted_by_handle": (lead.get("telegram_username") or "").strip() or None,
            "submitted_by_telegram_id": str(lead.get("user_id") or "").strip() or None,
            "group_id": gid or None,
            "group_name": (grp or {}).get("group_name") or None,
            "group_telegram_id": (grp or {}).get("group_telegram_id") or None,
            "supervisory_telegram_id": (grp or {}).get("supervisory_telegram_id") or None,
            "created_at": lead.get("created_at"),
            "driver_history": history,
            "accepted_driver": accepted,
        }

    return out
