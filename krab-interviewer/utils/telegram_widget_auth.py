"""Verify Telegram Login Widget callback (https://core.telegram.org/widgets/login)."""
from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any, Dict, Optional


def verify_telegram_login(data: Dict[str, Any], bot_token: str, max_age_sec: int = 86400) -> Optional[Dict[str, str]]:
    """
    Returns {telegram_id, telegram_username, first_name} on success, else None.
    """
    token = (bot_token or "").strip()
    if not token or not data:
        return None

    payload = {k: v for k, v in data.items() if v is not None and k != "hash"}
    received_hash = str(data.get("hash") or "").strip()
    if not received_hash:
        return None

    try:
        auth_date = int(payload.get("auth_date") or 0)
    except (TypeError, ValueError):
        return None
    if auth_date <= 0 or time.time() - auth_date > max_age_sec:
        return None

    check_lines = [f"{k}={payload[k]}" for k in sorted(payload.keys())]
    check_string = "\n".join(check_lines)
    secret = hashlib.sha256(token.encode()).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        return None

    try:
        tid = str(int(payload["id"]))
    except (KeyError, TypeError, ValueError):
        return None

    username = str(payload.get("username") or "").strip().lstrip("@").lower()
    first_name = str(payload.get("first_name") or "").strip()
    return {
        "telegram_id": tid,
        "telegram_username": f"@{username}" if username else "",
        "first_name": first_name,
    }
