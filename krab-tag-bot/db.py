"""Minimal Supabase wrapper for krab-tag-bot.

Only what the tag service needs: the shared atomic plate allocator (same RPC
krableadsV2 uses, so plate sequences stay unified) and the active dispatch
groups (for the Telegram "send to group" picker). Falls back gracefully when
Supabase is unset so the HTTP generator still works with caller-supplied plates.
"""
from __future__ import annotations

import logging
import secrets
from typing import Any, Dict, List, Optional

from config import Config

logger = logging.getLogger(__name__)


class Database:
    def __init__(self) -> None:
        self.client = None
        if Config.SUPABASE_URL and Config.SUPABASE_KEY:
            try:
                from supabase import create_client
                self.client = create_client(Config.SUPABASE_URL, Config.SUPABASE_KEY)
            except Exception as e:
                logger.warning("Supabase client init failed: %s", e)

    def allocate_temp_plate(self, is_nj: bool) -> Dict[str, str]:
        """NJ resident → ``H######``; non-resident → ``######V``; 10-digit control.
        Uses the atomic ``allocate_temp_plate`` RPC (migration_tag_plates.sql);
        random fallback on any error so tag generation never fails."""
        if self.client is not None:
            try:
                resp = self.client.rpc("allocate_temp_plate", {"p_is_nj": bool(is_nj)}).execute()
                data = resp.data
                if isinstance(data, dict) and data.get("plate") and data.get("car"):
                    return {"plate": str(data["plate"]), "control_number": str(data["car"])}
            except Exception as e:
                logger.warning("allocate_temp_plate RPC failed, using random fallback: %s", e)
        n = secrets.randbelow(900000) + 100000
        plate = f"H{n:06d}" if is_nj else f"{n:06d}V"
        control = str(secrets.randbelow(9_000_000_000) + 1_000_000_000)
        return {"plate": plate, "control_number": control}

    def list_active_groups(self) -> List[Dict[str, Any]]:
        """Active dispatch groups (id, group_name, group_telegram_id)."""
        if self.client is None:
            return []
        try:
            resp = self.client.table("groups").select(
                "id, group_name, group_telegram_id, is_active"
            ).execute()
            rows = resp.data or []
            return [
                g for g in rows
                if g.get("group_telegram_id") and g.get("is_active", True)
            ]
        except Exception as e:
            logger.warning("list_active_groups failed: %s", e)
            return []

    # ── Plate/control counters (tag_plate_settings) ─────────────────────────
    _PLATE_FIELDS = {
        "nj_plate_next_number", "non_nj_plate_next_number",
        "nj_car_next_number", "non_nj_car_next_number",
        "nj_plate_prefix", "non_nj_plate_suffix", "plate_digits",
    }

    def get_plate_settings(self) -> Optional[Dict[str, Any]]:
        if self.client is None:
            return None
        try:
            r = self.client.table("tag_plate_settings").select("*").eq("id", 1).limit(1).execute()
            return (r.data or [None])[0]
        except Exception as e:
            logger.warning("get_plate_settings failed: %s", e)
            return None

    def update_plate_settings(self, updates: Dict[str, Any]) -> bool:
        """Update whitelisted counters/prefix columns on the singleton row."""
        if self.client is None:
            return False
        clean = {k: v for k, v in (updates or {}).items() if k in self._PLATE_FIELDS}
        if not clean:
            return False
        try:
            r = self.client.table("tag_plate_settings").update(clean).eq("id", 1).execute()
            return bool(r.data)
        except Exception as e:
            logger.warning("update_plate_settings failed: %s", e)
            return False

    # ── Group management (shared groups table) ──────────────────────────────
    def list_groups(self) -> List[Dict[str, Any]]:
        """All groups (active and inactive) for the settings UI."""
        if self.client is None:
            return []
        try:
            r = self.client.table("groups").select(
                "id, group_name, group_telegram_id, is_active"
            ).order("group_name").execute()
            return r.data or []
        except Exception as e:
            logger.warning("list_groups failed: %s", e)
            return []

    def add_group(self, group_name: str, group_telegram_id: str,
                  supervisory_telegram_id: str = "") -> bool:
        if self.client is None:
            return False
        try:
            self.client.table("groups").insert({
                "group_name": group_name,
                "group_telegram_id": str(group_telegram_id),
                "supervisory_telegram_id": str(supervisory_telegram_id or group_telegram_id),
                "is_active": True,
            }).execute()
            return True
        except Exception as e:
            logger.warning("add_group failed: %s", e)
            return False

    def set_group_active(self, group_id: str, active: bool) -> bool:
        if self.client is None:
            return False
        try:
            r = self.client.table("groups").update({"is_active": bool(active)}).eq("id", group_id).execute()
            return bool(r.data)
        except Exception as e:
            logger.warning("set_group_active failed: %s", e)
            return False

    def upload_tag_to_storage(self, plate: str, pdf_bytes: bytes) -> Optional[str]:
        """Optional: store the PDF in the public ``order-documents`` bucket and
        return its URL (used by ?store=1). Returns None on failure."""
        if self.client is None or not pdf_bytes:
            return None
        safe = "".join(c for c in str(plate) if c.isalnum()) or "tag"
        path = f"tags/{safe}_{secrets.token_hex(4)}.pdf"
        try:
            bucket = self.client.storage.from_("order-documents")
            bucket.upload(path, pdf_bytes, file_options={"content-type": "application/pdf"})
            return bucket.get_public_url(path)
        except Exception as e:
            logger.warning("upload_tag_to_storage failed: %s", e)
            return None
