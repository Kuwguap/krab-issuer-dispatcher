"""Supabase database layer for Krab Interviewer."""
from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from config import Config
from supabase import Client, create_client

from utils.ai_vision import INTERVIEW_FIELD_KEYS, normalize_interview_data

logger = logging.getLogger(__name__)


class Database:
    def __init__(self) -> None:
        self.client: Client = create_client(Config.SUPABASE_URL, Config.SUPABASE_KEY)

    # --- interviews ---

    def create_interview(
        self,
        fields: Dict[str, Any],
        *,
        created_by_telegram_id: str,
        is_supervisor_created: bool = False,
        status: str = "pending",
    ) -> Optional[Dict[str, Any]]:
        row = normalize_interview_data(fields)
        payload = {
            "status": status,
            "created_by_telegram_id": str(created_by_telegram_id),
            "is_supervisor_created": bool(is_supervisor_created),
            **{k: row.get(k) or None for k in INTERVIEW_FIELD_KEYS},
            "first_name": row.get("first_name") or None,
        }
        try:
            r = self.client.table("interviews").insert(payload).execute()
            return r.data[0] if r.data else None
        except Exception as e:
            logger.error("create_interview failed: %s", e)
            return None

    def get_interview_by_id(self, interview_id: str) -> Optional[Dict[str, Any]]:
        try:
            r = (
                self.client.table("interviews")
                .select("*")
                .eq("id", interview_id)
                .limit(1)
                .execute()
            )
            return r.data[0] if r.data else None
        except Exception as e:
            logger.error("get_interview_by_id: %s", e)
            return None

    def list_interviews(self, limit: int = 50) -> List[Dict[str, Any]]:
        try:
            r = (
                self.client.table("interviews")
                .select("*")
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
            return r.data or []
        except Exception as e:
            logger.error("list_interviews: %s", e)
            return []

    def update_interview(self, interview_id: str, updates: Dict[str, Any]) -> bool:
        updates = dict(updates)
        updates["updated_at"] = datetime.now(timezone.utc).isoformat()
        try:
            self.client.table("interviews").update(updates).eq("id", interview_id).execute()
            return True
        except Exception as e:
            logger.error("update_interview: %s", e)
            return False

    def get_latest_interview_for_telegram_id(self, telegram_id: str) -> Optional[Dict[str, Any]]:
        try:
            r = (
                self.client.table("interviews")
                .select("*")
                .eq("telegram_id", str(telegram_id).strip())
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
            return r.data[0] if r.data else None
        except Exception as e:
            logger.error("get_latest_interview_for_telegram_id: %s", e)
            return None

    # --- appointments ---

    def create_appointment(
        self,
        interview_id: str,
        scheduled_at: datetime,
        created_by_telegram_id: str,
    ) -> Optional[Dict[str, Any]]:
        payload = {
            "interview_id": interview_id,
            "scheduled_at": scheduled_at.isoformat(),
            "created_by_telegram_id": str(created_by_telegram_id),
            "status": "pending",
        }
        try:
            r = self.client.table("appointments").insert(payload).execute()
            return r.data[0] if r.data else None
        except Exception as e:
            logger.error("create_appointment: %s", e)
            return None

    def get_appointment_by_id(self, appointment_id: str) -> Optional[Dict[str, Any]]:
        try:
            r = (
                self.client.table("appointments")
                .select("*")
                .eq("id", appointment_id)
                .limit(1)
                .execute()
            )
            return r.data[0] if r.data else None
        except Exception as e:
            logger.error("get_appointment_by_id: %s", e)
            return None

    def list_pending_appointments_before(self, before_iso: str) -> List[Dict[str, Any]]:
        try:
            r = (
                self.client.table("appointments")
                .select("*")
                .eq("status", "pending")
                .lte("scheduled_at", before_iso)
                .execute()
            )
            return r.data or []
        except Exception as e:
            logger.error("list_pending_appointments_before: %s", e)
            return []

    def list_future_pending_appointments(self) -> List[Dict[str, Any]]:
        now = datetime.now(timezone.utc).isoformat()
        try:
            r = (
                self.client.table("appointments")
                .select("*")
                .eq("status", "pending")
                .gt("scheduled_at", now)
                .execute()
            )
            return r.data or []
        except Exception as e:
            logger.error("list_future_pending_appointments: %s", e)
            return []

    def mark_appointment_reminded(self, appointment_id: str) -> bool:
        return self._update_appointment(
            appointment_id,
            {"status": "reminded", "reminder_sent_at": datetime.now(timezone.utc).isoformat()},
        )

    def _update_appointment(self, appointment_id: str, updates: Dict[str, Any]) -> bool:
        try:
            self.client.table("appointments").update(updates).eq("id", appointment_id).execute()
            return True
        except Exception as e:
            logger.error("update appointment: %s", e)
            return False

    def get_appointment_for_interview(self, interview_id: str) -> Optional[Dict[str, Any]]:
        try:
            r = (
                self.client.table("appointments")
                .select("*")
                .eq("interview_id", interview_id)
                .eq("status", "pending")
                .order("scheduled_at", desc=True)
                .limit(1)
                .execute()
            )
            if r.data:
                return r.data[0]
            r2 = (
                self.client.table("appointments")
                .select("*")
                .eq("interview_id", interview_id)
                .order("scheduled_at", desc=True)
                .limit(1)
                .execute()
            )
            return r2.data[0] if r2.data else None
        except Exception as e:
            logger.error("get_appointment_for_interview: %s", e)
            return None

    # --- announcement_jobs ---

    def create_announcement_job(
        self,
        *,
        kind: str,
        body: str,
        media_file_id: Optional[str],
        created_by_telegram_id: str,
        scheduled_at: Optional[datetime] = None,
        status: str = "pending",
    ) -> Optional[Dict[str, Any]]:
        payload = {
            "kind": kind,
            "body": body or "",
            "media_file_id": media_file_id,
            "created_by_telegram_id": str(created_by_telegram_id),
            "status": status,
            "scheduled_at": scheduled_at.isoformat() if scheduled_at else None,
            "sent_at": datetime.now(timezone.utc).isoformat() if status == "sent" else None,
        }
        try:
            r = self.client.table("announcement_jobs").insert(payload).execute()
            return r.data[0] if r.data else None
        except Exception as e:
            logger.error("create_announcement_job: %s", e)
            return None

    def get_announcement_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        try:
            r = (
                self.client.table("announcement_jobs")
                .select("*")
                .eq("id", job_id)
                .limit(1)
                .execute()
            )
            return r.data[0] if r.data else None
        except Exception as e:
            logger.error("get_announcement_job: %s", e)
            return None

    def list_pending_announcements_before(self, before_iso: str) -> List[Dict[str, Any]]:
        try:
            r = (
                self.client.table("announcement_jobs")
                .select("*")
                .eq("status", "pending")
                .execute()
            )
            out = []
            for row in r.data or []:
                sched = row.get("scheduled_at")
                if sched and str(sched) <= before_iso:
                    out.append(row)
            return out
        except Exception as e:
            logger.error("list_pending_announcements_before: %s", e)
            return []

    def list_future_pending_announcements(self) -> List[Dict[str, Any]]:
        now = datetime.now(timezone.utc).isoformat()
        try:
            r = (
                self.client.table("announcement_jobs")
                .select("*")
                .eq("status", "pending")
                .execute()
            )
            out = []
            for row in r.data or []:
                sched = row.get("scheduled_at")
                if sched and str(sched) > now:
                    out.append(row)
            return out
        except Exception as e:
            logger.error("list_future_pending_announcements: %s", e)
            return []

    def mark_announcement_sent(self, job_id: str) -> bool:
        try:
            self.client.table("announcement_jobs").update(
                {
                    "status": "sent",
                    "sent_at": datetime.now(timezone.utc).isoformat(),
                }
            ).eq("id", job_id).execute()
            return True
        except Exception as e:
            logger.error("mark_announcement_sent: %s", e)
            return False

    # --- drivers (issuer shared table) ---

    def create_driver(
        self, driver_name: str, driver_telegram_id: str, phone_number: Optional[str] = None
    ) -> tuple[bool, Optional[str]]:
        try:
            self.client.table("drivers").insert(
                {
                    "driver_name": driver_name,
                    "driver_telegram_id": str(driver_telegram_id).strip(),
                    "phone_number": phone_number,
                }
            ).execute()
            return True, None
        except Exception as e:
            err = str(e)
            if "duplicate" in err.lower() or "unique" in err.lower():
                return True, None
            logger.error("create_driver: %s", e)
            return False, err

    def get_all_drivers(self) -> List[Dict[str, Any]]:
        try:
            r = self.client.table("drivers").select("*").order("driver_name").execute()
            return r.data or []
        except Exception as e:
            logger.error("get_all_drivers: %s", e)
            return []

    def get_driver_by_id(self, driver_id: str) -> Optional[Dict[str, Any]]:
        try:
            r = (
                self.client.table("drivers")
                .select("*")
                .eq("id", driver_id)
                .limit(1)
                .execute()
            )
            return r.data[0] if r.data else None
        except Exception as e:
            logger.error("get_driver_by_id: %s", e)
            return None

    def update_driver(self, driver_id: str, updates: Dict[str, Any]) -> bool:
        if not updates:
            return False
        try:
            self.client.table("drivers").update(updates).eq("id", driver_id).execute()
            return True
        except Exception as e:
            logger.error("update_driver: %s", e)
            return False

    def delete_driver(self, driver_id: str) -> bool:
        try:
            self.client.table("drivers").delete().eq("id", driver_id).execute()
            return True
        except Exception as e:
            logger.error("delete_driver: %s", e)
            return False

    # --- storage ---

    def upload_driver_license_to_storage(
        self,
        interview_id: str,
        file_bytes: bytes,
        original_name: str,
    ) -> Optional[str]:
        if not file_bytes:
            return None
        lower = (original_name or "").lower()
        ext = "jpg"
        if lower.endswith(".png"):
            ext = "png"
        elif lower.endswith(".webp"):
            ext = "webp"
        content_type = {
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "png": "image/png",
            "webp": "image/webp",
        }.get(ext, "image/jpeg")
        safe_id = re.sub(r"[^A-Za-z0-9_-]+", "_", str(interview_id))[:36]
        path = f"{safe_id}/{secrets.token_hex(4)}.{ext}"
        try:
            bucket = self.client.storage.from_("driver_licenses")
            bucket.upload(path, file_bytes, file_options={"content-type": content_type})
            return bucket.get_public_url(path)
        except Exception as e:
            logger.warning("upload_driver_license_to_storage failed: %s", e)
            return None
