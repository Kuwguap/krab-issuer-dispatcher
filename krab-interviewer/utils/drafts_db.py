"""Interview draft persistence for the public web form (IP-hash keyed)."""
from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from config import Config
from utils.telegram_resolve import normalize_telegram_username, normalize_telegram_username_display

logger = logging.getLogger(__name__)


def _client_ip(forwarded_for: Optional[str], remote_addr: Optional[str]) -> str:
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    if remote_addr:
        return remote_addr.strip()
    return ""


def request_ip_is_reliable(forwarded_for: Optional[str], remote_addr: Optional[str]) -> bool:
    ip = _client_ip(forwarded_for, remote_addr)
    return bool(ip) and ip not in ("unknown", "127.0.0.1", "::1")


def ip_hash_from_request(forwarded_for: Optional[str], remote_addr: Optional[str]) -> str:
    """Stable visitor key without storing raw IP."""
    ip = _client_ip(forwarded_for, remote_addr) or "unknown"
    salt = (Config.IP_HASH_SALT or "krab-interviewer").strip()
    return hashlib.sha256(f"{ip}{salt}".encode("utf-8")).hexdigest()


def storage_ip_hash(
    forwarded_for: Optional[str],
    remote_addr: Optional[str],
    draft_cookie: str,
) -> str:
    """Per-session storage key when the client IP is missing (Vercel/proxy without X-Forwarded-For)."""
    if request_ip_is_reliable(forwarded_for, remote_addr):
        return ip_hash_from_request(forwarded_for, remote_addr)
    salt = (Config.IP_HASH_SALT or "krab-interviewer").strip()
    token = (draft_cookie or "").strip() or "anon"
    return hashlib.sha256(f"session:{token}{salt}".encode("utf-8")).hexdigest()


def new_draft_cookie() -> str:
    return secrets.token_urlsafe(24)


class DraftsDatabase:
    def __init__(self, client) -> None:
        self.client = client

    def get_by_ip_hash(self, ip_hash: str) -> Optional[Dict[str, Any]]:
        try:
            r = (
                self.client.table("interview_drafts")
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
                self.client.table("interview_drafts")
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
            r = self.client.table("interview_drafts").insert(row).execute()
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
            self.client.table("interview_drafts").update(updates).eq("id", draft_id).execute()
            return True
        except Exception as e:
            logger.error("update draft failed: %s", e)
            return False

    def release_ip_hash(self, draft_id: str) -> bool:
        """Free the unique ip_hash slot so the visitor can start a new application."""
        draft = self.get_by_id(draft_id)
        if not draft:
            return False
        ip = (draft.get("ip_hash") or "").strip()
        if not ip or ":archived:" in ip:
            return True
        return self.update(draft_id, ip_hash=f"{ip}:archived:{draft_id}")

    def mark_submitted(self, draft_id: str, interview_id: str) -> bool:
        """Mark submitted, strip form answers (keep license bytes), release ip_hash."""
        now = datetime.now(timezone.utc).isoformat()
        draft = self.get_by_id(draft_id) or {}
        payload = draft.get("payload") or {}
        license_only: Dict[str, Any] = {}
        for key in ("_license_b64", "_license_mime"):
            if payload.get(key):
                license_only[key] = payload[key]
        ok = self.update(
            draft_id,
            status="submitted",
            submitted_interview_id=interview_id,
            submitted_at=now,
            payload=license_only,
        )
        if ok:
            self.release_ip_hash(draft_id)
        return ok

    def archive_draft(self, draft_id: str) -> bool:
        """Free the visitor ip_hash slot and stop auto-resuming this draft."""
        draft = self.get_by_id(draft_id)
        if not draft:
            return False
        ip = (draft.get("ip_hash") or "").strip()
        if not ip or ":archived:" in ip:
            return True
        archived_ip = f"{ip}:archived:{draft_id}"
        status = draft.get("status") or "draft"
        if status == "draft":
            status = "abandoned"
        return self.update(draft_id, ip_hash=archived_ip, status=status, payload={})

    def archive_visitor_drafts(self, ip_hash: str, cookie_draft_id: Optional[str] = None) -> None:
        """Abandon in-progress drafts for a fresh application."""
        seen: set[str] = set()
        by_ip = self.get_by_ip_hash(ip_hash)
        if by_ip and by_ip.get("id"):
            seen.add(str(by_ip["id"]))
            self.archive_draft(str(by_ip["id"]))
        if cookie_draft_id and cookie_draft_id not in seen:
            self.archive_draft(str(cookie_draft_id))

    def reset_submission(self, draft_id: str) -> bool:
        """Clear submitted state so the visitor can submit again."""
        try:
            self.client.table("interview_drafts").update(
                {
                    "status": "draft",
                    "submitted_interview_id": None,
                    "submitted_at": None,
                    "last_seen_at": datetime.now(timezone.utc).isoformat(),
                }
            ).eq("id", draft_id).execute()
            return True
        except Exception as e:
            logger.error("reset_submission failed: %s", e)
            return False

    def find_drafts_by_submitted_interview(self, interview_id: str) -> List[Dict[str, Any]]:
        try:
            r = (
                self.client.table("interview_drafts")
                .select("*")
                .eq("submitted_interview_id", str(interview_id))
                .execute()
            )
            return r.data or []
        except Exception as e:
            logger.error("find_drafts_by_submitted_interview failed: %s", e)
            return []

    def touch(self, draft_id: str) -> bool:
        return self.update(draft_id, last_seen_at=datetime.now(timezone.utc))

    def register_pending_telegram_username(self, username: str, draft_id: str) -> None:
        """Remember username from web form until user taps /start on the bot."""
        un = normalize_telegram_username(username)
        if not un or not draft_id:
            return
        try:
            self.client.table("telegram_username_pending").upsert(
                {
                    "username_lower": un,
                    "draft_id": draft_id,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            ).execute()
        except Exception as e:
            if "telegram_username_pending" not in str(e):
                logger.warning("register_pending_telegram_username: %s", e)

    def lookup_telegram_id_from_drafts(self, username: str) -> Optional[str]:
        """Find telegram_id already linked on a draft for this username."""
        un = normalize_telegram_username(username)
        if not un:
            return None
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
            r = (
                self.client.table("interview_drafts")
                .select("payload")
                .eq("status", "draft")
                .gte("last_seen_at", cutoff)
                .order("last_seen_at", desc=True)
                .limit(300)
                .execute()
            )
            for row in r.data or []:
                payload = row.get("payload") or {}
                if normalize_telegram_username(payload.get("telegram_username") or "") != un:
                    continue
                tid = str(payload.get("telegram_id") or "").strip()
                if tid and tid != "-":
                    return tid
        except Exception as e:
            logger.error("lookup_telegram_id_from_drafts: %s", e)
        return None

    def link_telegram_id_for_username(self, username: str, telegram_id: str) -> int:
        """Set telegram_id on drafts that have this username but no ID yet."""
        un = normalize_telegram_username(username)
        if not un or not telegram_id:
            return 0
        display = normalize_telegram_username_display(un)
        draft_ids: set[str] = set()

        try:
            r = (
                self.client.table("telegram_username_pending")
                .select("draft_id")
                .eq("username_lower", un)
                .execute()
            )
            for row in r.data or []:
                if row.get("draft_id"):
                    draft_ids.add(str(row["draft_id"]))
            self.client.table("telegram_username_pending").delete().eq("username_lower", un).execute()
        except Exception as e:
            if "telegram_username_pending" not in str(e):
                logger.warning("link_telegram_id pending table: %s", e)

        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
            r = (
                self.client.table("interview_drafts")
                .select("id, payload")
                .eq("status", "draft")
                .gte("last_seen_at", cutoff)
                .order("last_seen_at", desc=True)
                .limit(300)
                .execute()
            )
            for row in r.data or []:
                payload = row.get("payload") or {}
                if normalize_telegram_username(payload.get("telegram_username") or "") != un:
                    continue
                tid = str(payload.get("telegram_id") or "").strip()
                if tid and tid != "-":
                    continue
                draft_ids.add(str(row["id"]))
        except Exception as e:
            logger.warning("link_telegram_id draft scan: %s", e)

        linked = 0
        for did in draft_ids:
            draft = self.get_by_id(did)
            if not draft or draft.get("status") != "draft":
                continue
            payload = dict(draft.get("payload") or {})
            payload["telegram_username"] = display
            payload["telegram_id"] = str(telegram_id)
            if self.update(did, payload=payload):
                linked += 1
        return linked

    def list_for_admin(
        self,
        *,
        status_filter: str = "",
        query: str = "",
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        try:
            q = self.client.table("interview_drafts").select("*")
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
                    self.client.table("interviews")
                    .select("id, status, full_name, telegram_username, phone_number, email, drivers_license_file_url")
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
            name = (
                inv.get("full_name")
                or payload.get("full_name")
                or ""
            ).strip()
            tg = (
                inv.get("telegram_username")
                or payload.get("telegram_username")
                or ""
            ).strip()
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
            sort_key = _badge_sort_key(badge)

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
                    "_priority": sort_key,
                    "_last_seen": last_seen,
                }
            )

        def _sort_key(item: Dict[str, Any]) -> tuple:
            pri = item.get("_priority", 99)
            ls = str(item.get("_last_seen") or "")
            return (pri, ls)

        out.sort(key=_sort_key)
        for item in out:
            item.pop("_priority", None)
            item.pop("_last_seen", None)
        # Newest last_seen first within same badge tier
        grouped: Dict[int, List[Dict[str, Any]]] = {}
        for item in out:
            pri = item.get("badge", {}).get("priority", 99)
            grouped.setdefault(pri, []).append(item)
        out = []
        for pri in sorted(grouped.keys()):
            chunk = grouped[pri]
            chunk.sort(key=lambda x: str(x.get("lastSeenAt") or ""), reverse=True)
            out.extend(chunk)
        return out[:limit]

    def mark_abandoned_older_than_days(self, days: int = 7) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        try:
            r = (
                self.client.table("interview_drafts")
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
            return {
                "color": "red",
                "label": "Started but never finished",
                "priority": 0,
            }
        return {
            "color": "yellow",
            "label": "Currently filling out",
            "priority": 1,
        }
    return {"color": "gray", "label": status, "priority": 3}


def _badge_sort_key(badge: Dict[str, Any]) -> int:
    return int(badge.get("priority", 99))
