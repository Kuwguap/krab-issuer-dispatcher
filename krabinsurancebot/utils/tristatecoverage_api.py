"""TriStateCoverage integrations API — POST /api/integrations/clients."""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)


@dataclass
class CreatePortalClientResult:
    ok: bool
    status_code: int
    error: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None
    """When the server took the "add another vehicle to an existing customer"
    path instead of creating a new account, this is set to ``"vehicle"``. Bots
    can use it to show "Added to existing account" UI copy."""
    added: Optional[str] = None


def _looks_like_duplicate_email(text: str) -> bool:
    lowered = (text or "").lower()
    return (
        "already been registered" in lowered
        or "already registered" in lowered
        or "already exists" in lowered
        or "duplicate key" in lowered
    )


def _friendly_error(status_code: int, body: Any) -> str:
    if status_code == 401:
        return "Invalid INTEGRATIONS_API_KEY (401 Unauthorized)."
    if status_code == 503:
        return (
            "TriStateCoverage server missing INTEGRATIONS_API_KEY (503). "
            "Configure the key on Vercel and redeploy."
        )
    if status_code == 409:
        # Modern portal returns 200 with `added: "vehicle"` instead of 409.
        # If we still see 409, the portal deploy is old — surface a hint.
        return (
            "Portal returned 409 for a duplicate email. The TriStateCoverage "
            "portal appears to be running an older build — redeploy the "
            "b_H821T7ehlpo app so duplicates are added as a second vehicle."
        )
    if status_code == 400 and isinstance(body, dict):
        issues = body.get("issues") or body.get("errors") or body.get("details")
        if issues:
            return f"Validation failed: {issues}"
        msg = (body.get("message") or body.get("error") or "").strip()
        if _looks_like_duplicate_email(msg):
            # Same as 409: newer portal handles this transparently.
            return (
                "Portal returned 400 for a duplicate email. Redeploy the "
                "TriStateCoverage portal (v2/b_H821T7ehlpo) so this customer's "
                "additional policy is added to their existing account."
            )
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
    pdf_bytes: bytes | None = None,
) -> CreatePortalClientResult:
    """
    Create a portal client via POST /api/integrations/clients.

    Does not send the welcome email — the bot sends that via Resend with portal credentials.
    """
    try:
        from config import Config
    except Exception:
        return CreatePortalClientResult(False, 503, "Config not available.")

    api_key = (getattr(Config, "INTEGRATIONS_API_KEY", None) or "").strip()
    if not api_key:
        return CreatePortalClientResult(
            False,
            503,
            "INTEGRATIONS_API_KEY is not configured on the bot.",
        )

    base = (getattr(Config, "TRISTATECOVERAGE_API_BASE", None) or "https://tristatecoverage.com").rstrip("/")
    url = f"{base}/api/integrations/clients"

    body = dict(payload)
    if pdf_bytes:
        body["insuranceCardPdfBase64"] = base64.b64encode(pdf_bytes).decode("ascii")

    body.setdefault("skipWelcomeEmail", True)
    body.setdefault("sendWelcomeEmail", False)
    body.setdefault("welcomeEmail", False)
    body.setdefault("sendEmail", False)
    body.setdefault("notifyClient", False)
    body.setdefault("silent", True)
    body.setdefault("suppressEmail", True)
    body.setdefault("disableEmail", True)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(url, json=body, headers=headers, timeout=20)
    except requests.RequestException as e:
        logger.warning("create_portal_client request failed: %s", e)
        return CreatePortalClientResult(False, 502, str(e))

    try:
        data = resp.json() if resp.content else {}
    except Exception:
        data = {}

    if resp.status_code >= 200 and resp.status_code < 300:
        if isinstance(data, dict) and data.get("ok") is False:
            return CreatePortalClientResult(
                False,
                resp.status_code,
                _friendly_error(resp.status_code, data),
                data if isinstance(data, dict) else None,
            )
        added = None
        if isinstance(data, dict):
            raw_added = data.get("added")
            if isinstance(raw_added, str) and raw_added.strip():
                added = raw_added.strip()
        return CreatePortalClientResult(
            True,
            resp.status_code,
            None,
            data if isinstance(data, dict) else None,
            added,
        )

    return CreatePortalClientResult(
        False,
        resp.status_code,
        _friendly_error(resp.status_code, data),
        data if isinstance(data, dict) else None,
    )
