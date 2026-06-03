"""Interview draft persistence for the public web form (IP-hash keyed)."""
from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from backend.config import BundleConfig

logger = logging.getLogger(__name__)


def ip_hash_from_request(forwarded_for: Optional[str], remote_addr: Optional[str]) -> str:
    """Stable visitor key without storing raw IP."""
    ip = ""
    if forwarded_for:
        ip = forwarded_for.split(",")[0].strip()
    elif remote_addr:
        ip = remote_addr.strip()
    if not ip:
        ip = "unknown"
    salt = (BundleConfig.IP_HASH_SALT or "krab-interviewer").strip()
    return hashlib.sha256(f"{ip}{salt}".encode("utf-8")).hexdigest()


def new_draft_cookie() -> str:
    return secrets.token_urlsafe(24)


class DraftsDatabase:
    def __init__(self, client) -> None:
        self.client = client
        self.table_name = BundleConfig.DRAFTS_TABLE

    def get_by_ip_hash(self, ip_hash: str) -> Optional[Dict[str, Any]]:
        try:
            r = (
                self.client.table(self.table_name)
                .select("*")
                .eq("ip_hash", ip_hash)
                .limit(1)
                .execute()
            )
            return r.data[0] if r.data else None
        except Exception as e:
            logger.error("get_by_ip_hash failed: %s", e)
            return None

    def get_by_id(self, draft_id: str) -> Optional[Dict[str, Any]]:
        try:
            r = (
                self.client.table(self.table_name)
                .select("*")
                .eq("id", draft_id)
                .limit(1)
                .execute()
            )
            return r.data[0] if r.data else None
        except Exception as e:
            logger.error("get_by_id failed: %s", e)
            return None

    def create(
        self,
        *,
        ip_hash: str,
        draft_cookie: str,
        user_agent: str = "",
        payload: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        row = {
            "ip_hash": ip_hash,
            "draft_cookie": draft_cookie,
            "user_agent": user_agent or "",
            "payload": payload or {},
            "status": "draft",
        }
        try:
            r = self.client.table(self.table_name).insert(row).execute()
            return r.data[0] if r.data else None
        except Exception as e:
            logger.error("create draft failed: %s", e)
            return None

    def update(
        self,
        draft_id: str,
        *,
        payload: Optional[Dict[str, Any]] = None,
        drivers_license_file_url: Optional[str] = None,
        status: Optional[str] = None,
        submitted_interview_id: Optional[str] = None,
        submitted_at: Optional[str] = None,
        last_seen_at: Optional[datetime] = None,
    ) -> bool:
        updates: Dict[str, Any] = {
            "last_seen_at": (last_seen_at or datetime.now(timezone.utc)).isoformat(),
        }
        if payload is not None:
            updates["payload"] = payload
        if drivers_license_file_url is not None:
            updates["drivers_license_file_url"] = drivers_license_file_url
        if status is not None:
            updates["status"] = status
        if submitted_interview_id is not None:
            updates["submitted_interview_id"] = submitted_interview_id
        if submitted_at is not None:
            updates["submitted_at"] = submitted_at
        try:
            self.client.table(self.table_name).update(updates).eq("id", draft_id).execute()
            return True
        except Exception as e:
            logger.error("update draft failed: %s", e)
            return False

    def mark_submitted(self, draft_id: str, interview_id: str) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        return self.update(
            draft_id,
            status="submitted",
            submitted_interview_id=interview_id,
            submitted_at=now,
        )

    def touch(self, draft_id: str) -> bool:
        return self.update(draft_id, last_seen_at=datetime.now(timezone.utc))

    def list_for_admin(
        self,
        *,
        status_filter: str = "",
        query: str = "",
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        try:
            q = self.client.table(self.table_name).select("*")
            if status_filter in ("draft", "submitted", "abandoned"):
                q = q.eq("status", status_filter)
            q = q.order("last_seen_at", desc=True).limit(min(limit, 500))
            r = q.execute()
            rows = r.data or []
        except Exception as e:
            logger.error("list_for_admin failed: %s", e)
            return []

        interview_ids = [
            str(row["submitted_interview_id"])
            for row in rows
            if row.get("submitted_interview_id")
        ]
        interviews_by_id: Dict[str, Dict[str, Any]] = {}
        if interview_ids:
            try:
                ir = (
                    self.client.table(BundleConfig.INTERVIEWS_TABLE)
                    .select(
                        "id, status, full_name, telegram_username, phone_number, email, drivers_license_file_url"
                    )
                    .in_("id", interview_ids)
                    .execute()
                )
                for inv in ir.data or []:
                    interviews_by_id[str(inv["id"])] = inv
            except Exception as e:
                logger.warning("list_for_admin interview join: %s", e)

        needle = (query or "").strip().lower()
        out: List[Dict[str, Any]] = []
        now = datetime.now(timezone.utc)
        one_hour_ago = now - timedelta(hours=1)

        for row in rows:
            payload = row.get("payload") or {}
            inv = interviews_by_id.get(str(row.get("submitted_interview_id") or ""), {})
            name = (inv.get("full_name") or payload.get("full_name") or "").strip()
            tg = (inv.get("telegram_username") or payload.get("telegram_username") or "").strip()
            phone = (inv.get("phone_number") or payload.get("phone_number") or "").strip()
            email = (inv.get("email") or payload.get("email") or "").strip()
            license_url = (
                inv.get("drivers_license_file_url")
                or row.get("drivers_license_file_url")
                or ""
            ).strip()

            if needle:
                hay = " ".join([name, tg, phone, email, str(row.get("id", ""))]).lower()
                if needle not in hay:
                    continue

            badge = _compute_badge(row, one_hour_ago, inv)
            last_seen = row.get("last_seen_at") or ""
            out.append(
                {
                    "draftId": str(row["id"]),
                    "createdAt": row.get("created_at"),
                    "name": name or "-",
                    "telegramUsername": tg or "-",
                    "phone": phone or "-",
                    "email": email or "-",
                    "licenseUrl": license_url or None,
                    "status": row.get("status"),
                    "badge": badge,
                    "interviewStatus": inv.get("status") if row.get("status") == "submitted" else None,
                    "ipHashShort": (row.get("ip_hash") or "")[:12],
                    "lastSeenAt": last_seen,
                    "submittedInterviewId": row.get("submitted_interview_id"),
                }
            )

        grouped: Dict[int, List[Dict[str, Any]]] = {}
        for item in out:
            pri = item.get("badge", {}).get("priority", 99)
            grouped.setdefault(pri, []).append(item)
        ordered: List[Dict[str, Any]] = []
        for pri in sorted(grouped.keys()):
            chunk = grouped[pri]
            chunk.sort(key=lambda x: str(x.get("lastSeenAt") or ""), reverse=True)
            ordered.extend(chunk)
        return ordered[:limit]

    def mark_abandoned_older_than_days(self, days: int = 7) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        try:
            r = (
                self.client.table(self.table_name)
                .update({"status": "abandoned"})
                .eq("status", "draft")
                .lt("last_seen_at", cutoff)
                .is_("submitted_interview_id", "null")
                .execute()
            )
            return len(r.data or [])
        except Exception as e:
            logger.error("mark_abandoned_older_than_days failed: %s", e)
            return 0


def _compute_badge(
    row: Dict[str, Any],
    one_hour_ago: datetime,
    interview: Dict[str, Any],
) -> Dict[str, Any]:
    status = (row.get("status") or "draft").strip()
    if status == "submitted":
        ist = (interview.get("status") or "pending").strip()
        return {
            "color": "green",
            "label": f"Submitted ({ist})",
            "priority": 2,
        }
    if status == "abandoned":
        return {"color": "gray", "label": "Abandoned", "priority": 3}

    last_seen_raw = row.get("last_seen_at")
    try:
        last_seen = datetime.fromisoformat(str(last_seen_raw).replace("Z", "+00:00"))
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        last_seen = datetime.now(timezone.utc)

    if status == "draft" and not row.get("submitted_interview_id"):
        if last_seen < one_hour_ago:
            return {"color": "red", "label": "Started but never finished", "priority": 0}
        return {"color": "yellow", "label": "Currently filling out", "priority": 1}
    return {"color": "gray", "label": status, "priority": 3}
