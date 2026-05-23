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
