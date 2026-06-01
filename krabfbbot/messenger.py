"""Facebook Graph API Messenger send."""
from __future__ import annotations

import logging

import httpx

from config import Config

logger = logging.getLogger(__name__)


def send_message(psid: str, text: str) -> bool:
    token = Config.META_PAGE_ACCESS_TOKEN
    if not token:
        logger.warning("META_PAGE_ACCESS_TOKEN missing")
        return False
    url = "https://graph.facebook.com/v19.0/me/messages"
    try:
        r = httpx.post(
            url,
            params={"access_token": token},
            json={"recipient": {"id": psid}, "message": {"text": text[:2000]}},
            timeout=30.0,
        )
    except httpx.RequestError as e:
        logger.warning("Graph send failed: %s", e)
        return False
    if r.status_code >= 400:
        logger.warning("Graph API %s: %s", r.status_code, r.text[:300])
        return False
    return True
