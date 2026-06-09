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

        with psycopg.connect(dsn, connect_timeout=5) as conn:
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


def _connect():
    dsn = (Config.KRAB_SENDER_DATABASE_URL or "").strip()
    if not dsn:
        return None, "KRAB_SENDER_DATABASE_URL is not configured"
    dsn = dsn.replace("postgresql+psycopg://", "postgresql://")
    try:
        import psycopg

        return psycopg.connect(dsn, connect_timeout=5), None
    except Exception as e:
        logger.error("recipients_db connect failed: %s", e)
        return None, str(e)


def delete_recipient_by_email(email: str) -> tuple[bool, Optional[str]]:
    """Remove dispatch recipient by email (krab-sender)."""
    em = (email or "").strip().lower()
    if not em:
        return False, "No email to match"
    conn, err = _connect()
    if not conn:
        return False, err
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM recipients WHERE lower(trim(email)) = %s",
                    (em,),
                )
                deleted = cur.rowcount
        if deleted:
            return True, None
        return False, "No dispatch recipient found for that email"
    except Exception as e:
        logger.error("delete_recipient_by_email failed: %s", e)
        return False, str(e)


def delete_recipient_by_name(name: str) -> tuple[bool, Optional[str]]:
    """Fallback: remove dispatch recipient by exact name match."""
    nm = (name or "").strip()
    if not nm:
        return False, "No name to match"
    conn, err = _connect()
    if not conn:
        return False, err
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM recipients WHERE trim(name) = %s",
                    (nm,),
                )
                deleted = cur.rowcount
        if deleted:
            return True, None
        return False, "No dispatch recipient found for that name"
    except Exception as e:
        logger.error("delete_recipient_by_name failed: %s", e)
        return False, str(e)
