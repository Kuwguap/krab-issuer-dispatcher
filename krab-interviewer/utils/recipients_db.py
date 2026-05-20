"""Direct insert into krab-sender Postgres ``recipients`` table on Hire."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from config import Config

logger = logging.getLogger(__name__)


def add_recipient(name: str, email: str) -> tuple[bool, Optional[str]]:
    """
    Insert a dispatch email recipient (krab-sender).
    Returns (success, error_message).
    """
    dsn = (Config.KRAB_SENDER_DATABASE_URL or "").strip()
    if not dsn:
        return False, "KRAB_SENDER_DATABASE_URL is not configured"
    nm = (name or "").strip()
    em = (email or "").strip()
    if not nm or not em:
        return False, "Name and email are required for krab-sender recipient"

    # psycopg expects postgresql:// or postgres:// (not SQLAlchemy +psycopg dialect)
    dsn = dsn.replace("postgresql+psycopg://", "postgresql://")

    try:
        import psycopg

        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO recipients (id, name, email, created_at_utc)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (str(uuid.uuid4()), nm, em, datetime.now(timezone.utc)),
                )
            conn.commit()
        return True, None
    except Exception as e:
        logger.error("add_recipient failed: %s", e)
        return False, str(e)
