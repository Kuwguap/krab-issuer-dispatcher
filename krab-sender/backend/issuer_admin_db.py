"""
Krab Issuer Supabase admin operations (same tables as krableadsV2 Flask admin).
Uses service role key from ApiConfig (same project as Issuer bot).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from supabase import Client, create_client

logger = logging.getLogger(__name__)


class IssuerAdminDatabase:
    """Minimal database wrapper for unified Issuer admin (mirrors krableadsV2 AdminDatabase)."""

    def __init__(self, supabase_url: str, supabase_key: str) -> None:
        url = (supabase_url or "").strip().rstrip("/")
        key = (supabase_key or "").strip()
        if not url or not key:
            raise ValueError("Supabase URL and key required")
        self.client: Client = create_client(url, key)
        self._tables_checked = False
        self._tables_exist = False

    def _check_tables_exist(self) -> bool:
        if self._tables_checked:
            return self._tables_exist
        try:
            self.client.table("groups").select("id").limit(1).execute()
            self._tables_checked = True
            self._tables_exist = True
            return True
        except Exception:
            self._tables_checked = True
            self._tables_exist = False
            return False

    def get_all_groups(self) -> list:
        if not self._check_tables_exist():
            return []
        try:
            response = self.client.table("groups").select("*").order("group_name").execute()
            return response.data or []
        except Exception:
            return []

    def get_all_drivers(self) -> list:
        if not self._check_tables_exist():
            return []
        try:
            response = self.client.table("drivers").select("*").order("driver_name").execute()
            return response.data or []
        except Exception:
            return []

    def create_group(self, group_name: str, group_telegram_id: str, supervisory_telegram_id: str) -> bool:
        if not self._check_tables_exist():
            return False
        try:
            self.client.table("groups").insert({
                "group_name": group_name,
                "group_telegram_id": group_telegram_id,
                "supervisory_telegram_id": supervisory_telegram_id,
            }).execute()
            return True
        except Exception:
            return False

    def create_driver(
        self,
        driver_name: str,
        driver_telegram_id: str,
        phone_number: Optional[str] = None,
        email: Optional[str] = None,
    ) -> bool:
        if not self._check_tables_exist():
            raise ValueError("Database tables not found. Run the schema migrations in Supabase SQL Editor.")
        payload: dict[str, Any] = {
            "driver_name": driver_name,
            "driver_telegram_id": str(driver_telegram_id).strip(),
        }
        if phone_number is not None and str(phone_number).strip():
            payload["phone_number"] = str(phone_number).strip()
        if email is not None and str(email).strip():
            payload["email"] = str(email).strip()
        try:
            self.client.table("drivers").insert(payload).execute()
        except Exception as e:
            # drivers.email may not exist yet (migration only landed on the
            # leads side) — retry the insert without it so driver creation
            # still succeeds on older schemas.
            err = str(e).lower()
            if "email" in payload and "email" in err and any(
                x in err for x in ("column", "schema", "pgrst204")
            ):
                payload.pop("email", None)
                self.client.table("drivers").insert(payload).execute()
            else:
                raise
        return True

    def update_driver(
        self,
        driver_id: str,
        driver_name: Optional[str] = None,
        driver_telegram_id: Optional[str] = None,
        phone_number: Optional[str] = None,
    ) -> bool:
        """Update provided fields on a driver row. Returns False when not found."""
        if not self._check_tables_exist():
            return False
        payload: dict[str, Any] = {}
        if driver_name is not None and str(driver_name).strip():
            payload["driver_name"] = str(driver_name).strip()
        if driver_telegram_id is not None and str(driver_telegram_id).strip():
            payload["driver_telegram_id"] = str(driver_telegram_id).strip()
        if phone_number is not None:
            payload["phone_number"] = str(phone_number).strip() or None
        if not payload:
            existing = self.client.table("drivers").select("id").eq("id", driver_id).limit(1).execute()
            return bool(existing.data)
        r = self.client.table("drivers").update(payload).eq("id", driver_id).execute()
        return bool(r.data)

    def delete_driver(self, driver_id: str) -> bool:
        """Delete a driver row. FK errors (lead history) propagate to the caller."""
        if not self._check_tables_exist():
            return False
        r = self.client.table("drivers").delete().eq("id", driver_id).execute()
        return bool(r.data)

    def get_group_by_id(self, group_id: str):
        if not self._check_tables_exist():
            return None
        try:
            response = self.client.table("groups").select("*").eq("id", group_id).execute()
            return response.data[0] if response.data else None
        except Exception:
            return None

    def toggle_group_status(self, group_id: str) -> bool:
        if not self._check_tables_exist():
            return False
        try:
            group = self.get_group_by_id(group_id)
            if group:
                new_status = not group.get("is_active", True)
                self.client.table("groups").update({"is_active": new_status}).eq("id", group_id).execute()
                return True
            return False
        except Exception:
            return False

    def toggle_driver_status(self, driver_id: str) -> bool:
        if not self._check_tables_exist():
            return False
        try:
            driver = self.client.table("drivers").select("*").eq("id", driver_id).execute()
            if driver.data:
                current_status = driver.data[0].get("is_active", True)
                new_status = not current_status
                self.client.table("drivers").update({"is_active": new_status}).eq("id", driver_id).execute()
                return True
            return False
        except Exception:
            return False

    def get_group_assistants(self, group_id: str) -> list:
        if not self._check_tables_exist():
            return []
        try:
            r = self.client.table("group_assistants").select("telegram_id").eq("group_id", group_id).execute()
            return [x["telegram_id"] for x in (r.data or [])]
        except Exception:
            return []

    def add_group_assistant(self, group_id: str, telegram_id: str) -> bool:
        if not self._check_tables_exist():
            return False
        try:
            self.client.table("group_assistants").insert({
                "group_id": group_id,
                "telegram_id": str(telegram_id).strip(),
            }).execute()
            return True
        except Exception:
            return False

    def remove_group_assistant(self, group_id: str, telegram_id: str) -> bool:
        if not self._check_tables_exist():
            return False
        try:
            self.client.table("group_assistants").delete().eq("group_id", group_id).eq(
                "telegram_id", str(telegram_id).strip()
            ).execute()
            return True
        except Exception:
            return False

    def get_setting(self, key: str) -> str:
        if not self._check_tables_exist():
            return ""
        try:
            r = self.client.table("settings").select("value").eq("key", key).limit(1).execute()
            if r.data and len(r.data) > 0:
                return (r.data[0].get("value") or "").strip()
            return ""
        except Exception:
            return ""

    def set_setting(self, key: str, value: str) -> bool:
        if not self._check_tables_exist():
            return False
        try:
            self.client.table("settings").upsert({"key": key, "value": str(value)}, on_conflict="key").execute()
            return True
        except Exception:
            return False

    def get_contact_info_sources(self) -> list:
        if not self._check_tables_exist():
            return []
        try:
            r = self.client.table("contact_info_sources").select("*").order("sort_order").execute()
            return r.data or []
        except Exception:
            return []

    def create_contact_info_source(self, label: str, sort_order: int = 0) -> bool:
        if not self._check_tables_exist():
            return False
        try:
            self.client.table("contact_info_sources").insert({
                "label": label.strip(),
                "sort_order": sort_order,
                "is_active": True,
            }).execute()
            return True
        except Exception:
            return False

    def toggle_contact_source_status(self, source_id: str) -> bool:
        if not self._check_tables_exist():
            return False
        try:
            r = self.client.table("contact_info_sources").select("is_active").eq("id", source_id).limit(1).execute()
            if not r.data:
                return False
            new_status = not (r.data[0].get("is_active", True))
            self.client.table("contact_info_sources").update({"is_active": new_status}).eq("id", source_id).execute()
            return True
        except Exception:
            return False

    def get_bot_usage(self, limit: int = 100) -> list:
        if not self._check_tables_exist():
            return []
        try:
            r = self.client.table("bot_usage").select("*").order("created_at", desc=True).limit(limit).execute()
            return r.data or []
        except Exception:
            return []

    def assign_driver_to_group(self, group_id: str, driver_id: str) -> bool:
        if not self._check_tables_exist():
            return False
        try:
            self.client.table("group_drivers").insert({"group_id": group_id, "driver_id": driver_id}).execute()
            return True
        except Exception:
            return False

    def get_all_assignments(self) -> list:
        if not self._check_tables_exist():
            return []
        try:
            r = self.client.table("group_drivers").select(
                "id, group_id, driver_id, group:groups(group_name), driver:drivers(driver_name)"
            ).execute()
            out = []
            for row in (r.data or []):
                g = row.get("group") or {}
                d = row.get("driver") or {}
                out.append({
                    "id": row.get("id"),
                    "group_id": row.get("group_id"),
                    "driver_id": row.get("driver_id"),
                    "group_name": g.get("group_name", "N/A"),
                    "driver_name": d.get("driver_name", "N/A"),
                })
            return out
        except Exception:
            return []

    def remove_driver_from_group(self, assignment_id: str) -> bool:
        if not self._check_tables_exist():
            return False
        try:
            self.client.table("group_drivers").delete().eq("id", assignment_id).execute()
            return True
        except Exception:
            return False

    def get_lead_stats(self) -> dict:
        out: dict[str, Any] = {"total_leads": 0, "drivers": []}
        if not self._check_tables_exist():
            return out
        try:
            r = self.client.table("leads").select("id").execute()
            out["total_leads"] = len(r.data or [])
        except Exception:
            pass
        try:
            drivers = self.client.table("drivers").select("id, driver_name").execute()
            assignments = self.client.table("lead_assignments").select("driver_id, lead_id").eq("status", "accepted").execute()
            lead_ids_with_receipt = set()
            try:
                leads = self.client.table("leads").select("id, receipt_image_url").execute()
                lead_ids_with_receipt = {l["id"] for l in (leads.data or []) if l.get("receipt_image_url")}
            except Exception:
                pass
            by_driver: dict[str, dict] = {}
            for a in (assignments.data or []):
                did = a.get("driver_id")
                lid = a.get("lead_id")
                if did not in by_driver:
                    by_driver[did] = {"accepted": 0, "receipts": 0}
                by_driver[did]["accepted"] += 1
                if lid and lid in lead_ids_with_receipt:
                    by_driver[did]["receipts"] += 1
            for d in (drivers.data or []):
                did = d.get("id")
                out["drivers"].append({
                    "driver_id": did,
                    "driver_name": d.get("driver_name", "N/A"),
                    "leads_accepted": by_driver.get(did, {}).get("accepted", 0),
                    "receipts_submitted": by_driver.get(did, {}).get("receipts", 0),
                })
        except Exception:
            pass
        return out

    def get_receipt_debts_summary(self, refs_per_driver: int = 5) -> dict:
        if not self._check_tables_exist():
            return {"drivers": []}
        try:
            drivers_resp = self.client.table("drivers").select("id, driver_name, is_active").order("driver_name").execute()
            drivers = drivers_resp.data or []
            assignments_resp = self.client.table("lead_assignments").select(
                "id, driver_id, accepted_at, lead:leads(reference_id, receipt_image_url)"
            ).eq("status", "accepted").execute()
            by_driver = {
                d.get("id"): {
                    "driver_id": d.get("id"),
                    "driver_name": d.get("driver_name", "N/A"),
                    "is_active": d.get("is_active", True),
                    "owed_receipts": 0,
                    "pending_references": [],
                }
                for d in drivers
            }
            for row in (assignments_resp.data or []):
                driver_id = row.get("driver_id")
                if not driver_id or driver_id not in by_driver:
                    continue
                lead = row.get("lead") or {}
                if lead.get("receipt_image_url"):
                    continue
                by_driver[driver_id]["owed_receipts"] += 1
                if len(by_driver[driver_id]["pending_references"]) < (refs_per_driver or 0):
                    by_driver[driver_id]["pending_references"].append({
                        "assignment_id": row.get("id"),
                        "reference_id": lead.get("reference_id") or "N/A",
                        "accepted_at": row.get("accepted_at"),
                    })
            out_drivers = [by_driver.get(d.get("id")) for d in drivers if by_driver.get(d.get("id"))]
            return {"drivers": out_drivers}
        except Exception:
            return {"drivers": []}

    def get_driver_pending_receipts(self, driver_id: str) -> list:
        if not self._check_tables_exist():
            return []
        try:
            assignments_resp = self.client.table("lead_assignments").select(
                "id, driver_id, accepted_at, lead:leads("
                "id, reference_id, receipt_image_url, vehicle_details, delivery_details, extra_info, "
                "special_request_note, special_request_issuers, special_request_drivers, monday_status"
                ")"
            ).eq("status", "accepted").eq("driver_id", driver_id).order("accepted_at").execute()
            out = []
            for row in (assignments_resp.data or []):
                lead = row.get("lead") or {}
                if lead.get("receipt_image_url"):
                    continue
                out.append({
                    "assignment_id": row.get("id"),
                    "lead_id": lead.get("id"),
                    "reference_id": lead.get("reference_id") or "N/A",
                    "accepted_at": row.get("accepted_at"),
                    "monday_status": lead.get("monday_status"),
                    "vehicle_details": lead.get("vehicle_details"),
                    "delivery_details": lead.get("delivery_details"),
                    "extra_info": lead.get("extra_info"),
                    "special_request_note": lead.get("special_request_note"),
                    "special_request_issuers": lead.get("special_request_issuers"),
                    "special_request_drivers": lead.get("special_request_drivers"),
                })
            return out
        except Exception:
            return []

    def delete_pending_receipt_assignment(self, assignment_id: str) -> bool:
        if not self._check_tables_exist():
            return False
        try:
            r = self.client.table("lead_assignments").select("id, lead:leads(receipt_image_url)").eq(
                "id", assignment_id
            ).limit(1).execute()
            if not r.data:
                return False
            lead = (r.data[0].get("lead") or {})
            if lead.get("receipt_image_url"):
                return False
            self.client.table("lead_assignments").delete().eq("id", assignment_id).execute()
            return True
        except Exception:
            return False

    def delete_pending_receipts_for_driver(self, driver_id: str) -> int:
        if not self._check_tables_exist():
            return 0
        try:
            r = self.client.table("lead_assignments").select("id, lead:leads(receipt_image_url)").eq(
                "status", "accepted"
            ).eq("driver_id", driver_id).execute()
            pending_ids = []
            for row in (r.data or []):
                lead = row.get("lead") or {}
                if not lead.get("receipt_image_url"):
                    pending_ids.append(row.get("id"))
            deleted = 0
            for aid in pending_ids:
                if not aid:
                    continue
                self.client.table("lead_assignments").delete().eq("id", aid).execute()
                deleted += 1
            return deleted
        except Exception:
            return 0

    def get_submitted_receipts_recent(self, limit: int = 100) -> list:
        if not self._check_tables_exist():
            return []
        cap = max(1, min(int(limit or 100), 500))
        try:
            r = self.client.table("leads").select(
                "id, reference_id, receipt_image_url, updated_at, group_id"
            ).order("updated_at", desc=True).limit(cap * 3).execute()
            out = []
            for lead in (r.data or []):
                url = (lead.get("receipt_image_url") or "").strip()
                if not url:
                    continue
                lid = lead.get("id")
                driver_name = "—"
                try:
                    a = self.client.table("lead_assignments").select(
                        "driver:drivers(driver_name)"
                    ).eq("lead_id", lid).eq("status", "accepted").limit(1).execute()
                    if a.data:
                        dr = (a.data[0].get("driver") or {})
                        driver_name = dr.get("driver_name") or driver_name
                except Exception:
                    pass
                gname = "—"
                gid = lead.get("group_id")
                if gid:
                    g = self.get_group_by_id(str(gid))
                    if g:
                        gname = g.get("group_name") or gname
                out.append({
                    "lead_id": lid,
                    "reference_id": lead.get("reference_id") or "N/A",
                    "receipt_image_url": url,
                    "driver_name": driver_name,
                    "group_name": gname,
                    "updated_at": lead.get("updated_at"),
                })
                if len(out) >= cap:
                    break
            return out
        except Exception:
            return []

    def get_upcoming_renewals(self) -> list:
        if not self._check_tables_exist():
            return []
        try:
            r = self.client.table("lead_renewals").select(
                "id, lead_id, renewal_due_at, status, original_group_id, original_driver_id, "
                "group_accepted_by_id, driver_accepted_by_id, "
                "lead:leads(reference_id, telegram_username, vehicle_details)"
            ).in_("status", ["pending", "group_phase", "driver_phase"]).order("renewal_due_at").execute()
            rows = r.data or []
            groups_cache: dict[str, str] = {}
            drivers_cache: dict[str, str] = {}
            now = datetime.now(timezone.utc)
            out = []
            for row in rows:
                lead = row.get("lead") or {}
                due_str = row.get("renewal_due_at") or ""
                days_left = None
                if due_str:
                    try:
                        due_dt = datetime.fromisoformat(due_str.replace("Z", "+00:00"))
                        days_left = max(0, (due_dt - now).days)
                    except Exception:
                        pass
                gid = row.get("group_accepted_by_id") or row.get("original_group_id")
                did = row.get("driver_accepted_by_id") or row.get("original_driver_id")
                gname = "—"
                if gid:
                    if gid not in groups_cache:
                        try:
                            gr = self.client.table("groups").select("group_name").eq("id", gid).limit(1).execute()
                            groups_cache[gid] = (gr.data[0].get("group_name") if gr.data else "—")
                        except Exception:
                            groups_cache[gid] = "—"
                    gname = groups_cache[gid]
                dname = "—"
                if did:
                    if did not in drivers_cache:
                        try:
                            dr = self.client.table("drivers").select("driver_name").eq("id", did).limit(1).execute()
                            drivers_cache[did] = (dr.data[0].get("driver_name") if dr.data else "—")
                        except Exception:
                            drivers_cache[did] = "—"
                    dname = drivers_cache[did]
                out.append({
                    "id": row.get("id"),
                    "reference_id": lead.get("reference_id", "—"),
                    "client_name": lead.get("telegram_username") or "—",
                    "vehicle": (lead.get("vehicle_details") or "")[:120],
                    "group_name": gname,
                    "driver_name": dname,
                    "days_left": days_left,
                    "status": row.get("status"),
                    "renewal_due_at": due_str,
                })
            return out
        except Exception as e:
            logger.error("get_upcoming_renewals: %s", e)
            return []
