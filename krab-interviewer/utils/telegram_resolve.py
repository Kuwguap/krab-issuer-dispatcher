"""Resolve @telegram_username to numeric Telegram user id for web interviews."""
from __future__ import annotations

import logging
import re
from typing import Any, Optional, Tuple

import requests

from config import Config

logger = logging.getLogger(__name__)

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")


def normalize_telegram_username(raw: str) -> str:
    un = (raw or "").strip().lstrip("@").lower()
    if not un or not _USERNAME_RE.match(un):
        return ""
    return un


def normalize_telegram_username_display(raw: str) -> str:
    un = normalize_telegram_username(raw)
    return f"@{un}" if un else ""


def _get_chat_via_bot_api(username: str) -> Optional[str]:
    token = (Config.TELEGRAM_BOT_TOKEN or "").strip()
    if not token:
        return None
    chat_id = f"@{username}"
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{token}/getChat",
            params={"chat_id": chat_id},
            timeout=12,
        )
        data = r.json() if r.ok else {}
        if not data.get("ok"):
            return None
        result = data.get("result") or {}
        tid = result.get("id")
        if tid is not None:
            return str(int(tid))
    except Exception as e:
        logger.warning("getChat failed for %s: %s", chat_id, e)
    return None


def resolve_telegram_id_for_username(db: Any, username: str) -> Tuple[Optional[str], str, str]:
    """
    Returns (telegram_id, source, message).
    source: directory | interview | driver | bot_api | none
    """
    un = normalize_telegram_username(username)
    if not un:
        return None, "none", "Enter a valid Telegram username (5–32 characters, letters, numbers, underscore)."

    if db:
        tid = db.lookup_telegram_id_from_directory(un)
        if tid:
            return tid, "directory", "Linked via Krab Interviewer bot."

        tid = db.lookup_telegram_id_from_interviews(un)
        if tid:
            return tid, "interview", "Found from a previous application."

        tid = db.lookup_telegram_id_from_drivers(un)
        if tid:
            return tid, "driver", "Found from driver records."

    tid = _get_chat_via_bot_api(un)
    if tid:
        if db:
            db.upsert_telegram_user_directory(tid, un)
        return tid, "bot_api", "Verified with Telegram."

    bot = (getattr(Config, "KRAB_INTERVIEWER_BOT_USERNAME", None) or "krabinterviewerbot").strip().lstrip("@")
    return (
        None,
        "none",
        f"Open @{bot} in Telegram, tap Start, then return here and verify your username.",
    )
