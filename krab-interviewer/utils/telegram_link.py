"""Link Telegram users from bot /start to pending web application drafts."""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def on_bot_user_started(drafts_db: Any, telegram_id: str, username: str) -> int:
    """Match @username from /start against web drafts waiting for an ID. Returns drafts updated."""
    if not drafts_db or not username or not telegram_id:
        return 0
    try:
        return int(drafts_db.link_telegram_id_for_username(username, str(telegram_id)))
    except Exception as e:
        logger.warning("on_bot_user_started: %s", e)
        return 0
