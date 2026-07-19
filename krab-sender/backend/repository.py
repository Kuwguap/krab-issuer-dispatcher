from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import os
from typing import Iterable, List, Optional

from sqlalchemy import func
from zoneinfo import ZoneInfo

from .db import SessionLocal, TransactionORM, RecipientORM, IssuerGroupChatORM, UserGroupLinkORM
from bot.models import Transaction


NY_TZ = ZoneInfo("America/New_York")


def _get_highkage_handle_set() -> set[str]:
    raw = (os.getenv("HIGHKAGE_GROUP_HANDLES") or "").strip()
    handles = {
        h.strip().lower().lstrip("@")
        for h in raw.split(",")
        if h.strip()
    }
    # Stable default so highkage classification works if env is missing.
    if not handles:
        handles = {"haruhatsu"}
    return handles


@contextmanager
def get_session():
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def save_transaction(tx: Transaction) -> None:
    """
    Persist a Transaction from the bot into the database.
    """
    orm = TransactionORM(
        id=tx.id,
        telegram_name=tx.telegram_name,
        telegram_handle=tx.telegram_handle,
        filename=tx.filename,
        client_details=tx.client_details,
        recipient_name=tx.recipient_name,
        recipient_email=tx.recipient_email,
        issuer_group=tx.issuer_group,
        reference_id=tx.reference_id,
        timestamp_utc=tx.timestamp,
        delivery_status=tx.delivery_status,
    )
    with get_session() as session:
        session.add(orm)
        session.flush()  # Ensure the ORM is added before commit


def list_transactions(
    limit: int = 100,
    offset: int = 0,
    since_utc: Optional[datetime] = None,
) -> List[Transaction]:
    """
    Fetch a window of transactions ordered by most recent first.

    If ``since_utc`` is set, only rows with ``timestamp_utc >= since_utc`` are returned.
    """
    with get_session() as session:
        q = session.query(TransactionORM).order_by(TransactionORM.timestamp_utc.desc())
        if since_utc is not None:
            q = q.filter(TransactionORM.timestamp_utc >= since_utc)
        rows: Iterable[TransactionORM] = q.offset(offset).limit(limit).all()

        # Build Transaction objects while the session is still open to avoid
        # DetachedInstanceError when accessing attributes later.
        result = [
            Transaction(
                id=row.id,
                telegram_name=row.telegram_name,
                telegram_handle=row.telegram_handle,
                filename=row.filename,
                client_details=row.client_details,
                recipient_name=row.recipient_name,
                recipient_email=row.recipient_email,
                issuer_group=row.issuer_group,
                reference_id=row.reference_id,
                timestamp=row.timestamp_utc,
                delivery_status=row.delivery_status,
            )
            for row in rows
        ]

    return result


def get_latest_transaction() -> Optional[Transaction]:
    with get_session() as session:
        row: Optional[TransactionORM] = (
            session.query(TransactionORM)
            .order_by(TransactionORM.timestamp_utc.desc())
            .first()
        )
        if not row:
            return None

        tx = Transaction(
            id=row.id,
            telegram_name=row.telegram_name,
            telegram_handle=row.telegram_handle,
            filename=row.filename,
            client_details=row.client_details,
            recipient_name=row.recipient_name,
            recipient_email=row.recipient_email,
            issuer_group=row.issuer_group,
            reference_id=row.reference_id,
            timestamp=row.timestamp_utc,
            delivery_status=row.delivery_status,
        )

    return tx


def get_rolling_summary_ny(
    days: Optional[int] = 7,
    reference_utc: Optional[datetime] = None,
    max_items: Optional[int] = 5000,
) -> dict:
    """
    Build a summary for the last `days` days in America/New_York time.

    - Window is [now_NY - days, now_NY].
    - Can be generated on demand at any time.
    - The Saturday 12 AM NJ cron will also call this, effectively
      generating the last 7 days as of that moment.
    """
    if reference_utc is None:
        reference_utc = datetime.now(timezone.utc)

    # Convert reference time into NJ (ET) timezone
    ref_ny = reference_utc.astimezone(NY_TZ)

    # Rolling window bounds in NY
    start_ny = (
        (ref_ny - timedelta(days=days)).replace(microsecond=0)
        if days is not None
        else None
    )
    end_ny = ref_ny.replace(microsecond=0)

    # Convert back to UTC for querying
    start_utc = start_ny.astimezone(timezone.utc) if start_ny else None
    end_utc = end_ny.astimezone(timezone.utc)

    # Cap row payload for the dashboard: large windows (e.g. 6m) used to load every
    # ORM row and OOM/timeout on Render, which surfaced as 502/CORS in the browser.
    with get_session() as session:
        base = session.query(TransactionORM).filter(
            TransactionORM.timestamp_utc <= end_utc
        )
        if start_utc is not None:
            base = base.filter(TransactionORM.timestamp_utc >= start_utc)

        status_u = func.upper(func.coalesce(TransactionORM.delivery_status, "PENDING"))
        total = base.count()
        delivered = base.filter(status_u == "DELIVERED").count()
        pending = base.filter(status_u == "PENDING").count()
        failed = max(0, total - delivered - pending)

        group_counts: dict = {
            "sensei_group": {"issued": 0, "sent": 0},
            "highkage_group": {"issued": 0, "sent": 0},
        }
        # Canonical issuer split: classify by sender telegram_handle.
        # Historical issuer_group values can be stale/incorrect.
        highkage_handles = _get_highkage_handle_set()
        handle_norm = func.lower(
            func.replace(func.coalesce(TransactionORM.telegram_handle, ""), "@", "")
        )
        if highkage_handles:
            highkage_q = base.filter(handle_norm.in_(tuple(highkage_handles)))
        else:
            # Defensive fallback (helper currently always returns at least one handle).
            highkage_q = base.filter(False)
        highkage_issued = highkage_q.count()
        highkage_sent = highkage_q.filter(status_u == "DELIVERED").count()

        group_counts["highkage_group"]["issued"] = highkage_issued
        group_counts["highkage_group"]["sent"] = highkage_sent
        group_counts["sensei_group"]["issued"] = max(0, total - highkage_issued)
        group_counts["sensei_group"]["sent"] = max(0, delivered - highkage_sent)

        # Most-recent N rows for the table (chronological within the cap).
        row_query = base.order_by(TransactionORM.timestamp_utc.desc())
        if max_items is not None:
            row_query = row_query.limit(max_items)
        rows: List[TransactionORM] = row_query.all()
        rows = list(reversed(rows))

        items = [
            {
                "id": r.id,
                "telegram_name": r.telegram_name,
                "telegram_handle": r.telegram_handle,
                "filename": r.filename,
                "recipient_name": r.recipient_name,
                "recipient_email": r.recipient_email,
                "issuer_group": r.issuer_group,
                "reference_id": r.reference_id,
                "client_details": r.client_details,
                "timestamp_ny": r.timestamp_utc.astimezone(NY_TZ).isoformat(),
                "delivery_status": r.delivery_status,
            }
            for r in rows
        ]

    summary = {
        "period_start_ny": start_ny.isoformat() if start_ny else None,
        "period_end_ny": end_ny.isoformat(),
        "window_days": days,
        "total_transactions": total,
        "delivered": delivered,
        "pending": pending,
        "failed": failed,
        "group_counts": group_counts,
        "items": items,
        "items_omitted": max(0, total - max_items) if max_items is not None else 0,
    }

    return summary


# Recipient management functions
def list_recipients() -> List[dict]:
    """
    Fetch all recipients ordered by name.
    """
    with get_session() as session:
        rows = session.query(RecipientORM).order_by(RecipientORM.name.asc()).all()
        return [
            {
                "id": row.id,
                "name": row.name,
                "email": row.email,
                "created_at_utc": row.created_at_utc.isoformat(),
            }
            for row in rows
        ]
def get_recipient_by_id(recipient_id: str) -> Optional[dict]:
    """
    Fetch a recipient by ID.
    """
    with get_session() as session:
        row = session.query(RecipientORM).filter(RecipientORM.id == recipient_id).first()
        if not row:
            return None
        return {
            "id": row.id,
            "name": row.name,
            "email": row.email,
            "created_at_utc": row.created_at_utc.isoformat(),
        }
def create_recipient(name: str, email: str) -> dict:
    """
    Create a new recipient.
    """
    import uuid
    recipient_id = str(uuid.uuid4())
    with get_session() as session:
        orm = RecipientORM(
            id=recipient_id,
            name=name,
            email=email,
            created_at_utc=datetime.now(timezone.utc),
        )
        session.add(orm)
        return {
            "id": orm.id,
            "name": orm.name,
            "email": orm.email,
            "created_at_utc": orm.created_at_utc.isoformat(),
        }
def delete_recipient(recipient_id: str) -> bool:
    """
    Delete a recipient by ID. Returns True if deleted, False if not found.
    """
    with get_session() as session:
        row = session.query(RecipientORM).filter(RecipientORM.id == recipient_id).first()
        if not row:
            return False
        session.delete(row)
        return True


# Group-attach module (/groupattach): registered group chats + user→group links
def _group_to_dict(row: IssuerGroupChatORM) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "chat_id": row.chat_id,
        "created_at_utc": row.created_at_utc.isoformat() if row.created_at_utc else None,
    }


def list_group_chats() -> List[dict]:
    """All registered group chats, ordered by name."""
    with get_session() as session:
        rows = session.query(IssuerGroupChatORM).order_by(IssuerGroupChatORM.name.asc()).all()
        return [_group_to_dict(r) for r in rows]


def get_group_chat_by_id(group_id: str) -> Optional[dict]:
    with get_session() as session:
        row = session.query(IssuerGroupChatORM).filter(IssuerGroupChatORM.id == group_id).first()
        return _group_to_dict(row) if row else None


def register_group_chat(name: str, chat_id: str) -> dict:
    """
    Register (or rename) a group chat by its Telegram chat id. Idempotent:
    running /groupattach again in the same group just refreshes the name.
    """
    import uuid
    chat_id = str(chat_id)
    with get_session() as session:
        row = session.query(IssuerGroupChatORM).filter(IssuerGroupChatORM.chat_id == chat_id).first()
        if row:
            row.name = name or row.name
            return _group_to_dict(row)
        row = IssuerGroupChatORM(
            id=str(uuid.uuid4()),
            name=name or chat_id,
            chat_id=chat_id,
            created_at_utc=datetime.now(timezone.utc),
        )
        session.add(row)
        return _group_to_dict(row)


def delete_group_chat(group_id: str) -> bool:
    """Delete a registered group and any user links pointing at it."""
    with get_session() as session:
        row = session.query(IssuerGroupChatORM).filter(IssuerGroupChatORM.id == group_id).first()
        if not row:
            return False
        session.query(UserGroupLinkORM).filter(UserGroupLinkORM.group_id == group_id).delete()
        session.delete(row)
        return True


def set_user_group(telegram_user_id, group_id: str, telegram_name: str | None = None) -> None:
    """Attach a user's Telegram id to a registered group (one group per user)."""
    uid = str(telegram_user_id)
    with get_session() as session:
        row = session.query(UserGroupLinkORM).filter(UserGroupLinkORM.telegram_user_id == uid).first()
        if row:
            row.group_id = group_id
            if telegram_name:
                row.telegram_name = telegram_name
        else:
            session.add(UserGroupLinkORM(
                telegram_user_id=uid,
                telegram_name=telegram_name,
                group_id=group_id,
                created_at_utc=datetime.now(timezone.utc),
            ))


def clear_user_group(telegram_user_id) -> bool:
    """Detach a user from their group. Returns True if a link was removed."""
    uid = str(telegram_user_id)
    with get_session() as session:
        n = session.query(UserGroupLinkORM).filter(UserGroupLinkORM.telegram_user_id == uid).delete()
        return n > 0


def get_user_group(telegram_user_id) -> Optional[dict]:
    """The group dict a user is attached to, or None."""
    uid = str(telegram_user_id)
    with get_session() as session:
        link = session.query(UserGroupLinkORM).filter(UserGroupLinkORM.telegram_user_id == uid).first()
        if not link:
            return None
        row = session.query(IssuerGroupChatORM).filter(IssuerGroupChatORM.id == link.group_id).first()
        return _group_to_dict(row) if row else None


def list_group_members(group_id: str) -> List[dict]:
    """Users attached to a group."""
    with get_session() as session:
        rows = (
            session.query(UserGroupLinkORM)
            .filter(UserGroupLinkORM.group_id == group_id)
            .order_by(UserGroupLinkORM.created_at_utc.asc())
            .all()
        )
        return [
            {"telegram_user_id": r.telegram_user_id, "telegram_name": r.telegram_name}
            for r in rows
        ]