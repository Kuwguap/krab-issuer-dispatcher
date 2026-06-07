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

    def list_interviews_by_status(self, status: str, limit: int = 50) -> List[Dict[str, Any]]:
        st = (status or "").strip()
        if not st:
            return []
        try:
            r = (
                self.client.table("interviews")
                .select("*")
                .eq("status", st)
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
            return r.data or []
        except Exception as e:
            logger.error("list_interviews_by_status: %s", e)
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

    def find_pending_interview_by_telegram_username(
        self, username: str, *, exclude_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        un = (username or "").strip().lstrip("@").lower()
        if not un:
            return None
        try:
            r = (
                self.client.table("interviews")
                .select("*")
                .in_("status", ["pending", "scheduled"])
                .order("created_at", desc=True)
                .limit(50)
                .execute()
            )
            for row in r.data or []:
                if exclude_id and str(row.get("id")) == str(exclude_id):
                    continue
                row_un = (row.get("telegram_username") or "").strip().lstrip("@").lower()
                if row_un == un:
                    return row
        except Exception as e:
            logger.error("find_pending_interview_by_telegram_username: %s", e)
        return None

    def lookup_telegram_id_from_directory(self, username: str) -> Optional[str]:
        un = (username or "").strip().lstrip("@").lower()
        if not un:
            return None
        try:
            r = (
                self.client.table("telegram_user_directory")
                .select("telegram_id")
                .ilike("telegram_username", un)
                .limit(5)
                .execute()
            )
            for row in r.data or []:
                row_un = (row.get("telegram_username") or "").strip().lstrip("@").lower()
                if row_un == un:
                    tid = (row.get("telegram_id") or "").strip()
                    if tid:
                        return tid
        except Exception as e:
            if "telegram_user_directory" not in str(e):
                logger.error("lookup_telegram_id_from_directory: %s", e)
        return None

    def upsert_telegram_user_directory(self, telegram_id: str, username: str) -> bool:
        un = (username or "").strip().lstrip("@").lower()
        tid = str(telegram_id).strip()
        if not un or not tid:
            return False
        try:
            self.client.table("telegram_user_directory").upsert(
                {
                    "telegram_id": tid,
                    "telegram_username": un,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
                on_conflict="telegram_id",
            ).execute()
            return True
        except Exception as e:
            logger.error("upsert_telegram_user_directory: %s", e)
            return False

    def lookup_telegram_id_from_interviews(self, username: str) -> Optional[str]:
        un = (username or "").strip().lstrip("@").lower()
        if not un:
            return None
        try:
            r = (
                self.client.table("interviews")
                .select("telegram_id, telegram_username")
                .not_.is_("telegram_id", "null")
                .order("created_at", desc=True)
                .limit(100)
                .execute()
            )
            for row in r.data or []:
                row_un = (row.get("telegram_username") or "").strip().lstrip("@").lower()
                tid = (row.get("telegram_id") or "").strip()
                if row_un == un and tid and tid != "-":
                    return tid
        except Exception as e:
            logger.error("lookup_telegram_id_from_interviews: %s", e)
        return None

    def lookup_telegram_id_from_drivers(self, username: str) -> Optional[str]:
        """Best-effort: drivers table stores telegram id, not username — skip unless we add mapping later."""
        return None

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

    def delete_driver_by_telegram_id(self, telegram_id: str) -> bool:
        tid = str(telegram_id or "").strip()
        if not tid:
            return False
        try:
            self.client.table("drivers").delete().eq("driver_telegram_id", tid).execute()
            return True
        except Exception as e:
            logger.error("delete_driver_by_telegram_id: %s", e)
            return False

    def find_interviews_by_full_name(self, name: str, limit: int = 20) -> List[Dict[str, Any]]:
        needle = (name or "").strip()
        if not needle:
            return []
        try:
            r = (
                self.client.table("interviews")
                .select("*")
                .ilike("full_name", f"%{needle}%")
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
            return r.data or []
        except Exception as e:
            logger.error("find_interviews_by_full_name: %s", e)
            return []

    def cancel_interview(self, interview_id: str) -> bool:
        return self.update_interview(interview_id, {"status": "cancelled"})

    def delete_interview(self, interview_id: str) -> bool:
        try:
            self.client.table("interviews").delete().eq("id", interview_id).execute()
            return True
        except Exception as e:
            logger.error("delete_interview: %s", e)
            return False

    # --- storage ---

    def _ensure_driver_license_bucket(self) -> None:
        """Create public bucket if missing (service role key required)."""
        base = (Config.SUPABASE_URL or "").rstrip("/")
        key = (Config.SUPABASE_KEY or "").strip()
        if not base or not key:
            return
        try:
            buckets = self.client.storage.list_buckets() or []
            names = set()
            for b in buckets:
                if isinstance(b, dict):
                    nm = b.get("name")
                else:
                    nm = getattr(b, "name", None)
                if nm:
                    names.add(nm)
            if "driver_licenses" in names:
                return
        except Exception as e:
            logger.warning("_ensure_driver_license_bucket list: %s", e)

        try:
            self.client.storage.create_bucket(
                "driver_licenses",
                options={"public": True},
            )
            logger.info("Created Supabase storage bucket driver_licenses")
            return
        except Exception as e:
            logger.warning("_ensure_driver_license_bucket create: %s", e)

        try:
            import requests

            r = requests.post(
                f"{base}/storage/v1/bucket",
                headers={"Authorization": f"Bearer {key}", "apikey": key},
                json={"id": "driver_licenses", "name": "driver_licenses", "public": True},
                timeout=30,
            )
            if r.status_code in (200, 201):
                logger.info("Created driver_licenses bucket via REST")
            else:
                logger.warning(
                    "REST create bucket failed: %s %s",
                    r.status_code,
                    (r.text or "")[:300],
                )
        except Exception as e:
            logger.warning("_ensure_driver_license_bucket REST: %s", e)

    def _upload_license_via_rest(
        self,
        path: str,
        file_bytes: bytes,
        content_type: str,
    ) -> Optional[str]:
        base = (Config.SUPABASE_URL or "").rstrip("/")
        key = (Config.SUPABASE_KEY or "").strip()
        if not base or not key:
            return None
        try:
            import requests

            upload_url = f"{base}/storage/v1/object/driver_licenses/{path}"
            r = requests.post(
                upload_url,
                headers={
                    "Authorization": f"Bearer {key}",
                    "apikey": key,
                    "Content-Type": content_type,
                    "x-upsert": "true",
                },
                data=file_bytes,
                timeout=60,
            )
            if r.status_code in (200, 201):
                return f"{base}/storage/v1/object/public/driver_licenses/{path}"
            logger.warning(
                "REST license upload failed: %s %s",
                r.status_code,
                (r.text or "")[:300],
            )
        except Exception as e:
            logger.warning("_upload_license_via_rest: %s", e)
        return None

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
        self._ensure_driver_license_bucket()
        try:
            bucket = self.client.storage.from_("driver_licenses")
            bucket.upload(
                path,
                file_bytes,
                file_options={"content-type": content_type, "upsert": "true"},
            )
            return bucket.get_public_url(path)
        except Exception as e:
            logger.warning("upload_driver_license_to_storage client failed: %s", e)
        return self._upload_license_via_rest(path, file_bytes, content_type)
