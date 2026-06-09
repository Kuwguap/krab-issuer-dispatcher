"""Supabase helpers for paper_shipments and bot_settings."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from config import Config
from supabase import Client, create_client

logger = logging.getLogger(__name__)

_client: Optional[Client] = None


def _get_client() -> Client:
    global _client
    if _client is None:
        _client = create_client(Config.SUPABASE_URL, Config.SUPABASE_KEY)
    return _client


def create_shipment(
    *,
    interview_id: Optional[str],
    driver_name: str,
    driver_address: Optional[str],
    driver_phone: Optional[str],
    driver_email: Optional[str],
    driver_telegram_id: Optional[str],
    quantity: int,
    created_by_telegram_id: str,
    status: str = "awaiting_tracking",
    driver_city: Optional[str] = None,
    driver_zip: Optional[str] = None,
    offer_messages: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    payload = {
        "interview_id": interview_id,
        "driver_name": driver_name.strip(),
        "driver_address": (driver_address or "").strip() or None,
        "driver_phone": (driver_phone or "").strip() or None,
        "driver_email": (driver_email or "").strip() or None,
        "driver_telegram_id": (driver_telegram_id or "").strip() or None,
        "quantity": int(quantity),
        "status": status,
        "created_by_telegram_id": str(created_by_telegram_id),
        "driver_city": (driver_city or "").strip() or None,
        "driver_zip": (driver_zip or "").strip() or None,
        "offer_messages": offer_messages or [],
    }
    try:
        r = _get_client().table("paper_shipments").insert(payload).execute()
        return r.data[0] if r.data else None
    except Exception as e:
        logger.error("create_shipment failed: %s", e)
        return None


def get_shipment(shipment_id: str) -> Optional[Dict[str, Any]]:
    try:
        r = (
            _get_client()
            .table("paper_shipments")
            .select("*")
            .eq("id", shipment_id)
            .limit(1)
            .execute()
        )
        return r.data[0] if r.data else None
    except Exception as e:
        logger.error("get_shipment: %s", e)
        return None


def update_shipment(shipment_id: str, updates: Dict[str, Any]) -> bool:
    updates = dict(updates)
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    try:
        _get_client().table("paper_shipments").update(updates).eq("id", shipment_id).execute()
        return True
    except Exception as e:
        logger.error("update_shipment: %s", e)
        return False


def list_shipments(limit: int = 25, status: Optional[str] = None) -> List[Dict[str, Any]]:
    try:
        q = _get_client().table("paper_shipments").select("*").order("created_at", desc=True).limit(limit)
        if status:
            q = q.eq("status", status)
        r = q.execute()
        return r.data or []
    except Exception as e:
        logger.error("list_shipments: %s", e)
        return []


def try_accept_shipment(
    shipment_id: str,
    *,
    telegram_id: str,
    accepted_by_name: str,
) -> Optional[Dict[str, Any]]:
    """First accept wins — atomic update only when status is pending_accept."""
    now = datetime.now(timezone.utc).isoformat()
    updates = {
        "status": "awaiting_tracking",
        "accepted_by_telegram_id": str(telegram_id).strip(),
        "accepted_by_name": (accepted_by_name or "").strip() or None,
        "accepted_at": now,
        "updated_at": now,
    }
    try:
        r = (
            _get_client()
            .table("paper_shipments")
            .update(updates)
            .eq("id", shipment_id)
            .eq("status", "pending_accept")
            .execute()
        )
        return r.data[0] if r.data else None
    except Exception as e:
        logger.error("try_accept_shipment: %s", e)
        return None


def save_offer_messages(shipment_id: str, messages: List[Dict[str, Any]]) -> bool:
    return update_shipment(shipment_id, {"offer_messages": messages})


def append_offer_message(shipment_id: str, chat_id: int, message_id: int) -> bool:
    row = get_shipment(shipment_id)
    if not row:
        return False
    msgs = list(row.get("offer_messages") or [])
    msgs.append({"chat_id": chat_id, "message_id": message_id})
    return update_shipment(shipment_id, {"offer_messages": msgs})


def set_bot_setting(
    key: str,
    *,
    value: Optional[str] = None,
    media_kind: Optional[str] = None,
    media_file_id: Optional[str] = None,
    caption: Optional[str] = None,
) -> bool:
    payload = {
        "key": key,
        "value": value,
        "media_kind": media_kind,
        "media_file_id": media_file_id,
        "caption": caption,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        _get_client().table("bot_settings").upsert(payload).execute()
        return True
    except Exception as e:
        logger.error("set_bot_setting: %s", e)
        return False


def get_bot_setting(key: str) -> Optional[Dict[str, Any]]:
    try:
        r = (
            _get_client()
            .table("bot_settings")
            .select("*")
            .eq("key", key)
            .limit(1)
            .execute()
        )
        return r.data[0] if r.data else None
    except Exception as e:
        logger.error("get_bot_setting: %s", e)
        return None


def add_training_video(
    *,
    media_kind: str,
    media_file_id: str,
    caption: Optional[str] = None,
    added_by_telegram_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    payload = {
        "media_kind": media_kind,
        "media_file_id": media_file_id,
        "caption": (caption or "").strip() or None,
        "added_by_telegram_id": (
            str(added_by_telegram_id).strip() if added_by_telegram_id else None
        ),
    }
    try:
        r = _get_client().table("training_videos").insert(payload).execute()
        return r.data[0] if r.data else None
    except Exception as e:
        logger.error("add_training_video: %s", e)
        return None


def list_training_videos(limit: int = 50) -> List[Dict[str, Any]]:
    try:
        r = (
            _get_client()
            .table("training_videos")
            .select("*")
            .order("created_at", desc=False)
            .limit(limit)
            .execute()
        )
        return r.data or []
    except Exception as e:
        logger.error("list_training_videos: %s", e)
        return []


def get_training_video(video_id: str) -> Optional[Dict[str, Any]]:
    try:
        r = (
            _get_client()
            .table("training_videos")
            .select("*")
            .eq("id", video_id)
            .limit(1)
            .execute()
        )
        return r.data[0] if r.data else None
    except Exception as e:
        logger.error("get_training_video: %s", e)
        return None


def delete_training_video(video_id: str) -> bool:
    try:
        _get_client().table("training_videos").delete().eq("id", video_id).execute()
        return True
    except Exception as e:
        logger.error("delete_training_video: %s", e)
        return False


def set_supervisor_email(telegram_id: str, email: str) -> bool:
    payload = {
        "telegram_id": str(telegram_id).strip(),
        "email": (email or "").strip(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        _get_client().table("supervisor_emails").upsert(payload).execute()
        return True
    except Exception as e:
        logger.error("set_supervisor_email: %s", e)
        return False


def get_supervisor_email(telegram_id: str) -> Optional[str]:
    try:
        r = (
            _get_client()
            .table("supervisor_emails")
            .select("email")
            .eq("telegram_id", str(telegram_id).strip())
            .limit(1)
            .execute()
        )
        if r.data:
            return (r.data[0].get("email") or "").strip() or None
        return None
    except Exception as e:
        logger.error("get_supervisor_email: %s", e)
        return None


def list_supervisor_emails() -> List[Dict[str, Any]]:
    try:
        r = _get_client().table("supervisor_emails").select("*").execute()
        return r.data or []
    except Exception as e:
        logger.error("list_supervisor_emails: %s", e)
        return []
