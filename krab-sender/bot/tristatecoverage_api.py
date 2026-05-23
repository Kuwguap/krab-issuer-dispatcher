"""TriStateCoverage integrations API — POST /api/integrations/clients."""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)


@dataclass
class CreatePortalClientResult:
    ok: bool
    status_code: int
    error: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None


def _friendly_error(status_code: int, body: Any) -> str:
    if status_code == 401:
        return "Invalid INTEGRATIONS_API_KEY (401 Unauthorized)."
    if status_code == 503:
        return (
            "TriStateCoverage server missing INTEGRATIONS_API_KEY (503). "
            "Configure the key on Vercel and redeploy."
        )
    if status_code == 409:
        return "Portal account already exists for this email (409)."
    if status_code == 400 and isinstance(body, dict):
        issues = body.get("issues") or body.get("errors") or body.get("details")
        if issues:
            return f"Validation failed: {issues}"
        msg = (body.get("message") or body.get("error") or "").strip()
        if msg:
            return msg
        return "Validation failed (400)."
    if isinstance(body, dict):
        msg = (body.get("message") or body.get("error") or "").strip()
        if msg:
            return msg
    if status_code >= 500:
        return f"TriStateCoverage server error ({status_code})."
    return f"Portal create failed ({status_code})."


def create_portal_client(
    payload: Dict[str, Any],
    pdf_bytes: bytes | None,
    *,
    api_key: str,
    api_base: str,
) -> CreatePortalClientResult:
    """Create a portal client; bot sends welcome email via Resend separately."""
    key = (api_key or "").strip()
    if not key:
        return CreatePortalClientResult(
            False,
            503,
            "INTEGRATIONS_API_KEY is not configured on the bot.",
        )

    base = (api_base or "https://tristatecoverage.com").strip().rstrip("/")
    url = f"{base}/api/integrations/clients"

    body = dict(payload)
    if pdf_bytes:
        body["insuranceCardPdfBase64"] = base64.b64encode(pdf_bytes).decode("ascii")

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    try:
        resp = httpx.post(url, json=body, headers=headers, timeout=20.0)
    except httpx.RequestError as e:
        logger.warning("create_portal_client request failed: %s", e)
        return CreatePortalClientResult(False, 502, str(e))

    try:
        data = resp.json() if resp.content else {}
    except Exception:
        data = {}

    if 200 <= resp.status_code < 300:
        if isinstance(data, dict) and data.get("ok") is False:
            return CreatePortalClientResult(
                False,
                resp.status_code,
                _friendly_error(resp.status_code, data),
                data if isinstance(data, dict) else None,
            )
        return CreatePortalClientResult(
            True, resp.status_code, None, data if isinstance(data, dict) else None
        )

    return CreatePortalClientResult(
        False,
        resp.status_code,
        _friendly_error(resp.status_code, data),
        data if isinstance(data, dict) else None,
    )
