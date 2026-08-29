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

    @property
    def warning(self) -> Optional[str]:
        """What the portal did NOT manage to do, despite answering ok.

        A globally-duplicate policy number, or dates it could not parse, make it
        skip the policy row (and with it the invoice, the stored card and the
        coverage flags) and return 200 with a warning instead. The account still
        exists, so the member shows on the coverage receipts board with no
        policy against them -- which is the shape of "the insurance never
        appeared". Reading `ok` alone cannot see it."""
        if not isinstance(self.payload, dict):
            return None
        w = self.payload.get("warning")
        return str(w).strip() or None if w else None

    @property
    def policy_registered(self) -> bool:
        """True when the portal really does hold a policy for this client."""
        return bool(self.ok) and not self.warning


_DUPLICATE_MARKERS = (
    "already exists",
    "already registered",
    "already has an account",
    "duplicate",
    "email_exists",
    "email already",
    "user_already_exists",
)


def _looks_like_duplicate(body: Any) -> bool:
    """True when the portal is telling us the email is already registered.

    Checked by TEXT as well as by 409 because the endpoint has answered 400 and
    200-with-ok:false for the same condition, and every one of those spellings used
    to abort an insurance issue that only needed the account to exist."""
    if not isinstance(body, dict):
        return False
    blob = " ".join(
        str(body.get(k) or "") for k in ("error", "message", "code", "detail", "details")
    ).lower()
    return any(m in blob for m in _DUPLICATE_MARKERS)


def _friendly_error(status_code: int, body: Any) -> str:
    if status_code == 401:
        return "Invalid INTEGRATIONS_API_KEY (401 Unauthorized)."
    if status_code == 503:
        return (
            "TriStateCoverage server missing INTEGRATIONS_API_KEY (503). "
            "Configure the key on Vercel and redeploy."
        )
    if status_code == 409:
        # create_portal_client no longer routes 409 here (a duplicate is success),
        # but other callers of this mapper still need the wording.
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
    pdf_bytes: bytes | None = None,
) -> CreatePortalClientResult:
    """
    Create a portal client via POST /api/integrations/clients.

    The caller must pass ``skipWelcomeEmail: True`` in the payload — the bot
    sends the welcome email itself via Resend, with the portal credentials in
    it. Omitting the flag makes the portal send its own "policy issued" mail as
    well, so the client gets two emails for one purchase and the portal's copy
    carries no login.
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

    # An account that already exists IS the outcome we wanted. The portal answers
    # 409 for a duplicate email, and treating that as a failure aborted the whole
    # insurance issue for every repeat customer — and for a second car on the same
    # household email. The caller needs an account to exist, not to have been
    # created by this particular call.
    if resp.status_code == 409 or _looks_like_duplicate(data):
        logger.info("Portal account already exists for this email — continuing.")
        merged = dict(data) if isinstance(data, dict) else {}
        merged["alreadyExisted"] = True
        return CreatePortalClientResult(True, resp.status_code, None, merged)

    if resp.status_code >= 200 and resp.status_code < 300:
        if isinstance(data, dict) and data.get("ok") is False:
            return CreatePortalClientResult(
                False,
                resp.status_code,
                _friendly_error(resp.status_code, data),
                data if isinstance(data, dict) else None,
            )
        return CreatePortalClientResult(True, resp.status_code, None, data if isinstance(data, dict) else None)

    return CreatePortalClientResult(
        False,
        resp.status_code,
        _friendly_error(resp.status_code, data),
        data if isinstance(data, dict) else None,
    )
