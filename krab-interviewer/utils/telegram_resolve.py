"""Resolve @telegram_username to numeric Telegram user id for web interviews."""
from __future__ import annotations

import re
from typing import Any, Optional, Tuple
from urllib.parse import quote

from config import Config

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")


def normalize_telegram_username(raw: str) -> str:
    un = (raw or "").strip().lstrip("@").lower()
    if not un or not _USERNAME_RE.match(un):
        return ""
    return un


def normalize_telegram_username_display(raw: str) -> str:
    un = normalize_telegram_username(raw)
    return f"@{un}" if un else ""


def telegram_connect_url(start: str = "web") -> str:
    bot = (getattr(Config, "KRAB_INTERVIEWER_BOT_USERNAME", None) or "krabinterviewerbot").strip().lstrip("@")
    return f"https://t.me/{quote(bot)}?start={quote(start)}"


def _lookup_from_db(db: Any, drafts_db: Any, username: str) -> Tuple[Optional[str], str]:
    if db:
        for lookup, source in (
            (db.lookup_telegram_id_from_directory, "directory"),
            (db.lookup_telegram_id_from_interviews, "interview"),
            (db.lookup_telegram_id_from_drivers, "driver"),
        ):
            tid = lookup(username)
            if tid:
                return str(tid), source

    if drafts_db:
        tid = drafts_db.lookup_telegram_id_from_drafts(username)
        if tid:
            return str(tid), "draft"

    return None, ""


def resolve_telegram_id_for_username(
    db: Any,
    username: str,
    drafts_db: Any = None,
) -> Tuple[Optional[str], str, str]:
    """
    Returns (telegram_id, source, message).

    IDs come from: bot /start directory, past interviews/drivers, or web drafts
    linked when the user started the bot after typing their username.
    """
    un = normalize_telegram_username(username)
    if not un:
        return (
            None,
            "none",
            "Enter a valid Telegram username (5–32 characters: letters, numbers, underscore). @ is optional.",
        )

    tid, source = _lookup_from_db(db, drafts_db, un)
    if tid:
        return tid, source, f"Telegram ID {tid} found for @{un}."

    bot = (getattr(Config, "KRAB_INTERVIEWER_BOT_USERNAME", None) or "krabinterviewerbot").strip().lstrip("@")
    return (
        None,
        "none",
        f'Username @{un} saved. Open @{bot} in Telegram, tap Start once, then return here - your ID will appear automatically.',
    )


def telegram_resolve_meta(db: Any, username: str, drafts_db: Any = None) -> dict:
    """JSON-friendly resolve result for API responses."""
    tid, source, message = resolve_telegram_id_for_username(db, username or "", drafts_db=drafts_db)
    return {
        "ok": bool(tid),
        "telegramId": tid,
        "source": source,
        "message": message,
        "needsBotStart": not bool(tid),
        "connectUrl": telegram_connect_url("web") if not tid else None,
    }
