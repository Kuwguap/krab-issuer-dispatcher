"""Telegram HTTP notifications for web form submissions."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import requests

from config import Config

logger = logging.getLogger(__name__)


def _supervisory_chat_ids() -> List[int]:
    out: List[int] = []
    seen: set = set()
    raw = Config.SUPERVISORY_TELEGRAM_ID or ""
    for part in str(raw).split(","):
        t = part.strip().lstrip("=").strip()
        if not t:
            continue
        try:
            cid = int(t)
        except ValueError:
            continue
        if cid in seen:
            continue
        seen.add(cid)
        out.append(cid)
    return out


def notify_supervisors_new_web_interview(interview: Dict[str, Any]) -> int:
    """Legacy: pending application awaiting supervisor /open (when auto-hire is off)."""
    token = (Config.TELEGRAM_BOT_TOKEN or "").strip()
    if not token:
        logger.warning("notify_supervisors: no TELEGRAM_BOT_TOKEN")
        return 0

    iid = str(interview.get("id") or "")
    name = (interview.get("full_name") or interview.get("first_name") or "Driver").strip()
    tg = (interview.get("telegram_username") or "-").strip()
    phone = (interview.get("phone_number") or "-").strip()

    text = (
        "🌐 New driver application (web form)\n\n"
        f"👤 {name}\n"
        f"💬 {tg}\n"
        f"📱 {phone}\n\n"
        f"Open: /open {iid}"
    )
    return _send_supervisor_text(token, text)


def notify_supervisors_web_auto_hired(
    interview: Dict[str, Any],
    db_errors: Optional[List[str]] = None,
) -> int:
    """Web form submitted and driver auto-hired (DB records created)."""
    token = (Config.TELEGRAM_BOT_TOKEN or "").strip()
    if not token:
        logger.warning("notify_supervisors: no TELEGRAM_BOT_TOKEN")
        return 0

    iid = str(interview.get("id") or "")
    name = (interview.get("full_name") or interview.get("first_name") or "Driver").strip()
    tg = (interview.get("telegram_username") or "-").strip()
    em = (interview.get("email") or "-").strip()
    tid = (interview.get("telegram_id") or "-").strip()
    issuer = Config.KRAB_ISSUER_BOT_USERNAME.lstrip("@")
    dispatch = Config.KRAB_DISPATCH_BOT_USERNAME.lstrip("@")

    text = (
        "🌐 Web application — AUTO-HIRED ✅\n\n"
        f"👤 {name}\n"
        f"💬 {tg}\n"
        f"📧 {em}\n"
        f"🆔 Telegram ID: {tid}\n\n"
        f"Added to @{issuer} + @{dispatch}.\n"
        "Driver welcome DM, channel, papers, and training are being sent…\n\n"
        f"Record: /open {iid}"
    )
    errs = [e for e in (db_errors or []) if e]
    if errs:
        text += "\n\n⚠️ DB warnings:\n" + "\n".join(f"• {e}" for e in errs)
    return _send_supervisor_text(token, text)


def notify_supervisors_hire_complete(
    interview: Dict[str, Any],
    hire_msg: str,
    warnings: Optional[List[str]] = None,
    *,
    source: str = "web",
) -> int:
    """Follow-up when async hire onboarding finishes with warnings."""
    warn = [w for w in (warnings or []) if w]
    if not warn:
        return 0
    token = (Config.TELEGRAM_BOT_TOKEN or "").strip()
    if not token:
        return 0
    iid = str(interview.get("id") or "")
    text = (
        f"⚠️ Hire onboarding warnings ({source})\n\n"
        f"Record: /open {iid}\n\n"
        + hire_msg
        + "\n\n⚠️ Warnings:\n"
        + "\n".join(f"• {w}" for w in warn)
    )
    return _send_supervisor_text(token, text)


def _send_supervisor_text(token: str, text: str) -> int:
    api = f"https://api.telegram.org/bot{token}/sendMessage"
    sent = 0
    for chat_id in _supervisory_chat_ids():
        try:
            r = requests.post(
                api,
                json={"chat_id": chat_id, "text": text},
                timeout=15,
            )
            if r.status_code == 200 and r.json().get("ok"):
                sent += 1
            else:
                logger.warning(
                    "notify_supervisors chat %s: %s %s",
                    chat_id,
                    r.status_code,
                    r.text[:200],
                )
        except Exception as e:
            logger.error("notify_supervisors chat %s failed: %s", chat_id, e)
    return sent
