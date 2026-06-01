"""HTTP client to krab-tg-forwarder."""
from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from config import Config
from state import bump_pending_attempt, delete_pending, enqueue_forward, list_pending_forwards

logger = logging.getLogger(__name__)


def _headers() -> dict[str, str]:
    return {"X-Forwarder-Key": Config.FORWARDER_API_KEY, "Content-Type": "application/json"}


def forward_lead(payload: dict[str, Any]) -> tuple[bool, Optional[str], Optional[str]]:
    """
    POST lead to forwarder. Returns (ok, reference_id, error).
    """
    base = Config.FORWARDER_BASE_URL
    key = Config.FORWARDER_API_KEY
    if not base or not key:
        return False, None, "Forwarder not configured (FORWARDER_BASE_URL / FORWARDER_API_KEY)"

    url = f"{base}/leads"
    try:
        r = httpx.post(url, json=payload, headers=_headers(), timeout=30.0)
    except httpx.RequestError as e:
        logger.warning("Forwarder request failed: %s", e)
        return False, None, str(e)

    if r.status_code >= 400:
        err = r.text[:500]
        logger.warning("Forwarder %s: %s", r.status_code, err)
        return False, None, err

    try:
        data = r.json()
    except Exception:
        return False, None, "Invalid forwarder response"
    ref = data.get("reference_id") if isinstance(data, dict) else None
    return True, ref, None


def queue_if_failed(psid: str, payload: dict[str, Any], error: Optional[str]) -> None:
    enqueue_forward(psid, payload, error=error)


def retry_pending() -> int:
    """Retry dead-letter queue. Returns count forwarded."""
    if not Config.FORWARDER_API_KEY:
        return 0
    forwarded = 0
    for item in list_pending_forwards():
        ok, ref, err = forward_lead(item["payload"])
        if ok:
            delete_pending(item["id"])
            forwarded += 1
            logger.info("Retried forward %s -> %s", item["id"], ref)
        else:
            bump_pending_attempt(item["id"], err or "unknown")
    return forwarded
