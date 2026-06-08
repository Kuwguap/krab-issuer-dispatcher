"""Verify Telegram Login Widget callback (https://core.telegram.org/widgets/login)."""
from __future__ import annotations

import hashlib
import hmac
import threading
import time
from typing import Any, Dict, Optional, Tuple

# Fields Telegram signs for Login Widget (https://core.telegram.org/widgets/login).
_TELEGRAM_LOGIN_DATA_FIELDS = frozenset(
    {"id", "first_name", "last_name", "username", "photo_url", "auth_date"}
)

_VERIFIED_LOGIN_CACHE_TTL_SEC = 600
_verified_login_cache: Dict[str, Tuple[Dict[str, str], float]] = {}
_cache_lock = threading.Lock()


def telegram_login_return_query_params(data: Dict[str, Any]) -> Dict[str, str]:
    """All signed Telegram fields + hash for redirect back to the frontend."""
    out = extract_telegram_login_data(data)
    received_hash = str(data.get("hash") or "").strip()
    if received_hash:
        out["hash"] = received_hash
    return out


def create_telegram_login_handoff(data: Dict[str, Any], bot_token: str, ttl_sec: int = 600) -> str:
    """Short-lived token proving callback already verified this Telegram login."""
    token = (bot_token or "").strip()
    payload = extract_telegram_login_data(data)
    received_hash = str(data.get("hash") or "").strip()
    if not token or not payload or not received_hash:
        return ""
    exp = int(time.time()) + ttl_sec
    msg = f"{exp}:{payload['id']}:{payload['auth_date']}:{received_hash}"
    secret = hashlib.sha256(token.encode()).digest()
    sig = hmac.new(secret, msg.encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def verify_telegram_login_handoff(data: Dict[str, Any], bot_token: str) -> Optional[Dict[str, str]]:
    """Accept login_handoff from callback redirect when hash fields were trimmed client-side."""
    handoff = str(data.get("login_handoff") or "").strip()
    if not handoff or "." not in handoff:
        return None
    token = (bot_token or "").strip()
    if not token:
        return None
    exp_text, sig = handoff.split(".", 1)
    try:
        exp = int(exp_text)
    except (TypeError, ValueError):
        return None
    if exp <= int(time.time()):
        return None

    payload = extract_telegram_login_data(data)
    received_hash = str(data.get("hash") or "").strip()
    if not payload or not received_hash:
        return None
    try:
        req_id = str(int(data.get("id") or 0))
        req_auth_date = str(int(data.get("auth_date") or 0))
    except (TypeError, ValueError):
        return None
    if req_id != str(payload.get("id") or "") or req_auth_date != str(payload.get("auth_date") or ""):
        return None

    msg = f"{exp}:{payload['id']}:{payload['auth_date']}:{received_hash}"
    secret = hashlib.sha256(token.encode()).digest()
    expected = hmac.new(secret, msg.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    return _verified_login_result(payload)


def extract_telegram_login_data(data: Dict[str, Any]) -> Dict[str, str]:
    """Strip app-specific query params (e.g. return_url) before hash verification."""
    out: Dict[str, str] = {}
    for key in _TELEGRAM_LOGIN_DATA_FIELDS:
        if key not in data:
            continue
        value = data[key]
        if value is None:
            continue
        text = str(value).strip()
        if text:
            out[key] = text
    return out


def _verified_login_result(payload: Dict[str, str]) -> Dict[str, str]:
    username = str(payload.get("username") or "").strip().lstrip("@").lower()
    first_name = str(payload.get("first_name") or "").strip()
    return {
        "telegram_id": str(int(payload["id"])),
        "telegram_username": f"@{username}" if username else "",
        "first_name": first_name,
    }


def _prune_verified_login_cache(now: Optional[float] = None) -> None:
    ts = now if now is not None else time.time()
    for key, (_, expires_at) in list(_verified_login_cache.items()):
        if expires_at <= ts:
            del _verified_login_cache[key]


def remember_verified_telegram_login(data: Dict[str, Any], verified: Dict[str, str]) -> None:
    """Cache a login verified on widget callback so re-verify can use hash+id only."""
    received_hash = str(data.get("hash") or "").strip()
    if not received_hash or not verified.get("telegram_id"):
        return
    payload = extract_telegram_login_data(data)
    payload["hash"] = received_hash
    expires_at = time.time() + _VERIFIED_LOGIN_CACHE_TTL_SEC
    with _cache_lock:
        _prune_verified_login_cache()
        _verified_login_cache[received_hash] = (payload, expires_at)


def lookup_verified_telegram_login(data: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Accept a partial frontend payload when callback already verified the same hash."""
    received_hash = str(data.get("hash") or "").strip()
    if not received_hash:
        return None
    try:
        req_id = str(int(data.get("id") or 0))
    except (TypeError, ValueError):
        return None
    if not req_id or req_id == "0":
        return None

    with _cache_lock:
        entry = _verified_login_cache.get(received_hash)
        if not entry:
            return None
        payload, expires_at = entry
        if time.time() > expires_at:
            del _verified_login_cache[received_hash]
            return None

    if str(payload.get("id") or "") != req_id:
        return None
    try:
        req_auth_date = str(int(data.get("auth_date") or 0))
    except (TypeError, ValueError):
        return None
    if req_auth_date != str(payload.get("auth_date") or ""):
        return None
    return _verified_login_result(payload)


def verify_telegram_login(data: Dict[str, Any], bot_token: str, max_age_sec: int = 86400) -> Optional[Dict[str, str]]:
    """
    Returns {telegram_id, telegram_username, first_name} on success, else None.
    """
    token = (bot_token or "").strip()
    if not token or not data:
        return None

    payload = extract_telegram_login_data(data)
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

    return _verified_login_result(payload)
