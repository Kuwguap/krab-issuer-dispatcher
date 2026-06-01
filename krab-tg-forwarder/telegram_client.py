"""Send formatted leads to a Telegram user or group."""
from __future__ import annotations

import logging

import httpx

from config import Config

logger = logging.getLogger(__name__)


def send_telegram_message(text: str) -> tuple[bool, str | None]:
    token = Config.TELEGRAM_BOT_TOKEN
    chat_id = Config.TELEGRAM_TARGET_CHAT_ID
    if not token or not chat_id:
        return False, "Telegram not configured"

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = httpx.post(
            url,
            json={
                "chat_id": chat_id,
                "text": text[:4096],
                "disable_web_page_preview": True,
            },
            timeout=30.0,
        )
    except httpx.RequestError as e:
        logger.warning("Telegram send failed: %s", e)
        return False, str(e)

    if r.status_code >= 400:
        logger.warning("Telegram API %s: %s", r.status_code, r.text[:300])
        return False, r.text[:500]
    return True, None
