"""Resolve API_LEAD_USER_ID (numeric id or @username like tristatetag)."""
from __future__ import annotations

import logging
import re
from typing import Optional, Tuple

import requests

from config import Config
from utils.database import Database

logger = logging.getLogger(__name__)

DEFAULT_API_LEAD_USER = "tristatetag"


def api_lead_user_ref() -> str:
    return (Config.API_LEAD_USER_ID or DEFAULT_API_LEAD_USER).strip() or DEFAULT_API_LEAD_USER


def normalize_username_label(raw: str) -> str:
    return (raw or "").strip().lstrip("@") or DEFAULT_API_LEAD_USER


def _telegram_get_user_id_by_username(username: str) -> Optional[int]:
    token = (getattr(Config, "TELEGRAM_BOT_TOKEN", None) or "").strip()
    if not token:
        return None
    un = normalize_username_label(username)
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{token}/getChat",
            params={"chat_id": f"@{un}"},
            timeout=10,
        )
        if not resp.ok:
            return None
        data = resp.json()
        if not data.get("ok"):
            return None
        chat_id = (data.get("result") or {}).get("id")
        return int(chat_id) if chat_id is not None else None
    except Exception as e:
        logger.debug("Telegram getChat for @%s failed: %s", un, e)
        return None


def resolve_api_lead_user_sync(
    db: Database,
    raw: Optional[str] = None,
) -> Tuple[Optional[int], str, Optional[str]]:
    """
    Resolve API_LEAD_USER_ID to (telegram_user_id, username_label, error).
    Accepts numeric id or username (default tristatetag).
    """
    ref = (raw or api_lead_user_ref()).strip()
    if not ref:
        return None, DEFAULT_API_LEAD_USER, "API_LEAD_USER_ID is not configured"

    if re.fullmatch(r"\d+", ref):
        uid = int(ref)
        return uid, ref, None

    username = normalize_username_label(ref)
    uid = db.lookup_telegram_id_by_username(username)
    if uid is None:
        uid = _telegram_get_user_id_by_username(username)
    if uid is None:
        return (
            None,
            username,
            f"Could not resolve Telegram user for @{username}. "
            f"Have @{username} open a DM with the issuer bot and tap Start, "
            "or set API_LEAD_USER_ID to their numeric Telegram id.",
        )
    return uid, username, None
