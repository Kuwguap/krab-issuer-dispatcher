"""Database utilities for Supabase integration."""
import logging
import re
import secrets
from supabase import create_client, Client
from config import Config
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

# Added in migrations — omit if DB is behind migration.
#   phase1_attached_files          → migration_phase1_attached_files.sql
#   email, driver_license_id,      → migration_email_dl_insurance_card.sql
#   insurance_card_*
_OPTIONAL_LEADS_WRITE_KEYS = frozenset({
    "phase1_attached_files",
    "email",
    "driver_license_id",
    "insurance_card_policy_number",
    "insurance_card_sent_to_email",
    "insurance_card_sent_at",
    "insurance_card_error",
    "portal_email",
    "portal_password",
    "ingest_dispatch_pending",
    "external_order_id",
})


def _retry_lead_write_without_phase1_files(exc: Exception, payload: Dict[str, Any]) -> bool:
    """True if PostgREST rejected one of the optional columns (column missing in schema cache).

    Caller pops any optional column whose key/name is mentioned in the error message,
    or — when PostgREST hides the column name (PGRST204) — pops all known optionals.
    """
    candidates = _OPTIONAL_LEADS_WRITE_KEYS.intersection(payload.keys())
    if not candidates:
        return False
    msg = str(exc).lower()
    matched_specific = False
    for key in list(candidates):
        if key in msg:
            payload.pop(key, None)
            matched_specific = True
    if matched_specific:
        return True
    # PostgREST sometimes omits the column name; PGRST204 = undefined column.
    if "pgrst204" in msg and "leads" in msg:
        for key in list(candidates):
            payload.pop(key, None)
        return True
    return False


def record_is_active(row: Optional[dict], key: str = "is_active") -> bool:
    """True unless the row explicitly has ``is_active`` = False.

    Supabase/Postgres often returns JSON ``null`` for unset booleans; ``row.get("is_active", True)``
    would then yield ``None``, which wrongly treated the driver/group as inactive.
    """
    if not row:
        return False
    return row.get(key) is not False


class Database:
    """Supabase database client wrapper."""
    
    def __init__(self):
        self.client: Client = create_client(Config.SUPABASE_URL, Config.SUPABASE_KEY)
        self._tables_checked = False
        self._tables_exist = False
        self._error_logged = False
    
    def _check_tables_exist(self) -> bool:
        """Check if required tables exist in the database."""
        if self._tables_checked:
            return self._tables_exist
        
        try:
            # Try a simple query to check if tables exist
            self.client.table("states").select("user_id").limit(1).execute()
            self._tables_checked = True
            self._tables_exist = True
            return True
        except Exception as e:
            error_msg = str(e)
            if ("Could not find the table" in error_msg or "PGRST205" in error_msg) and not self._error_logged:
                logger.error(
                    "\n" + "="*60 + "\n"
                    "DATABASE ERROR: Tables not found!\n\n"
                    "Please run the SQL schema from 'database/schema.sql' in your Supabase SQL Editor.\n"
                    "Go to: https://supabase.com/dashboard -> Your Project -> SQL Editor\n"
                    "="*60
                )
                self._error_logged = True
            self._tables_checked = True
            self._tables_exist = False
            return False
    
    def get_user_state(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Get the current state for a user."""
        if not self._check_tables_exist():
            return None
        
        try:
            response = self.client.table("states").select("*").eq("user_id", user_id).execute()
            if response.data:
                return response.data[0]
            return None
        except Exception as e:
            error_msg = str(e)
            if "Could not find the table" not in error_msg and "PGRST205" not in error_msg:
                logger.error(f"Error getting user state: {e}")
            return None
    
    def set_user_state(self, user_id: int, state: str, data: Optional[Dict[str, Any]] = None) -> bool:
        """Set or update the state for a user."""
        if not self._check_tables_exist():
            return False
        
        try:
            state_data = {
                "user_id": user_id,
                "state": state,
                "data": data or {}
            }
            
            # Try to update first
            existing = self.get_user_state(user_id)
            if existing:
                self.client.table("states").update(state_data).eq("user_id", user_id).execute()
            else:
                self.client.table("states").insert(state_data).execute()
            
            return True
        except Exception as e:
            error_msg = str(e)
            if "Could not find the table" not in error_msg and "PGRST205" not in error_msg:
                logger.error(f"Error setting user state: {e}")
            return False
    
    def clear_user_state(self, user_id: int) -> bool:
        """Clear the state for a user."""
        if not self._check_tables_exist():
            return False
        
        try:
            self.client.table("states").delete().eq("user_id", user_id).execute()
            return True
        except Exception as e:
            error_msg = str(e)
            if "Could not find the table" not in error_msg and "PGRST205" not in error_msg:
                logger.error(f"Error clearing user state: {e}")
            return False
    
    def create_lead(self, lead_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Create a new lead record."""
        if not self._check_tables_exist():
            return None

        payload = dict(lead_data)
        for attempt in (0, 1, 2):
            try:
                response = self.client.table("leads").insert(payload).execute()
                if response.data:
                    return response.data[0]
                return None
            except Exception as e:
                if attempt < 2 and _retry_lead_write_without_phase1_files(e, payload):
                    logger.warning(
                        "create_lead: retrying with optional columns dropped — "
                        "run migrations under database/ to enable them: %s",
                        e,
                    )
                    continue
                error_msg = str(e)
                if "Could not find the table" not in error_msg and "PGRST205" not in error_msg:
                    logger.error(f"Error creating lead: {e}")
                return None
        return None

    def update_lead(self, lead_id: str, updates: Dict[str, Any]) -> bool:
        """Update a lead record."""
        if not self._check_tables_exist():
            return False

        payload = dict(updates)
        for attempt in (0, 1, 2):
            try:
                self.client.table("leads").update(payload).eq("id", lead_id).execute()
                return True
            except Exception as e:
                if attempt < 2 and _retry_lead_write_without_phase1_files(e, payload):
                    logger.warning(
                        "update_lead: retrying with optional columns dropped — "
                        "run migrations under database/: %s",
                        e,
                    )
                    continue
                error_msg = str(e)
                if "Could not find the table" not in error_msg and "PGRST205" not in error_msg:
                    logger.error(f"Error updating lead: {e}")
                return False
        return False
    
    def get_lead_by_id(self, lead_id: str) -> Optional[Dict[str, Any]]:
        """Get a lead by ID."""
        if not self._check_tables_exist():
            return None
        
        try:
            response = self.client.table("leads").select("*").eq("id", lead_id).execute()
            if response.data:
                return response.data[0]
            return None
        except Exception as e:
            error_msg = str(e)
            if "Could not find the table" not in error_msg and "PGRST205" not in error_msg:
                logger.error(f"Error getting lead by ID: {e}")
            return None
    
    def list_leads_pending_ingest_dispatch(self, limit: int = 10) -> list:
        """Leads created via HTTP ingest awaiting bot worker group broadcast."""
        if not self._check_tables_exist():
            return []
        try:
            response = (
                self.client.table("leads")
                .select("*")
                .eq("ingest_dispatch_pending", True)
                .order("created_at")
                .limit(max(1, min(limit, 25)))
                .execute()
            )
            return response.data or []
        except Exception as e:
            msg = str(e).lower()
            if "ingest_dispatch_pending" in msg or "pgrst204" in msg:
                logger.warning(
                    "list_leads_pending_ingest_dispatch: column missing — run migration_lead_api_ingest.sql"
                )
                return []
            logger.error("list_leads_pending_ingest_dispatch: %s", e)
            return []

    def lookup_telegram_id_by_username(self, username: str) -> Optional[int]:
        """Best-effort: resolve @username to Telegram user id from bot_usage / past leads."""
        un = (username or "").strip().lstrip("@").lower()
        if not un:
            return None
        try:
            r = (
                self.client.table("bot_usage")
                .select("user_telegram_id, telegram_username")
                .order("created_at", desc=True)
                .limit(500)
                .execute()
            )
            for row in r.data or []:
                row_un = (row.get("telegram_username") or "").strip().lstrip("@").lower()
                if row_un == un:
                    uid = row.get("user_telegram_id")
                    if uid is not None:
                        return int(uid)
        except Exception as e:
            logger.error("lookup_telegram_id_by_username bot_usage: %s", e)
        try:
            r = (
                self.client.table("leads")
                .select("user_id, telegram_username")
                .order("created_at", desc=True)
                .limit(500)
                .execute()
            )
            for row in r.data or []:
                row_un = (row.get("telegram_username") or "").strip().lstrip("@").lower()
                if row_un == un:
                    uid = row.get("user_id")
                    if uid is not None:
                        return int(uid)
        except Exception as e:
            logger.error("lookup_telegram_id_by_username leads: %s", e)
        return None

    def get_lead_by_monday_id(self, monday_item_id: int) -> Optional[Dict[str, Any]]:
        """Get a lead by Monday.com item ID."""
        if not self._check_tables_exist():
            return None
        
        try:
            response = self.client.table("leads").select("*").eq("monday_item_id", monday_item_id).execute()
            if response.data:
                return response.data[0]
            return None
        except Exception as e:
            error_msg = str(e)
            if "Could not find the table" not in error_msg and "PGRST205" not in error_msg:
                logger.error(f"Error getting lead by Monday ID: {e}")
            return None
    
    def get_lead_by_reference_id(self, reference_id: str) -> Optional[Dict[str, Any]]:
        """Get a lead by reference ID."""
        if not self._check_tables_exist():
            return None
        
        try:
            response = self.client.table("leads").select("*").eq("reference_id", reference_id).execute()
            if response.data:
                return response.data[0]
            return None
        except Exception as e:
            error_msg = str(e)
            if "Could not find the table" not in error_msg and "PGRST205" not in error_msg:
                logger.error(f"Error getting lead by reference ID: {e}")
            return None
    
    def upload_receipt_to_storage(
        self,
        lead_id: str,
        reference_id: str,
        file_bytes: bytes,
        original_name: str,
    ) -> Optional[str]:
        """
        Upload receipt image to Storage bucket `receipts`. Returns public URL, or None on failure.
        Requires bucket from database/migration_receipts_storage.sql; use service_role key for the bot.
        """
        if not file_bytes or not Config.SUPABASE_URL or not Config.SUPABASE_KEY:
            return None
        lower = (original_name or "").lower()
        ext = "jpg"
        if lower.endswith(".png"):
            ext = "png"
        elif lower.endswith(".webp"):
            ext = "webp"
        elif lower.endswith(".gif"):
            ext = "gif"
        content_type = {
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "png": "image/png",
            "webp": "image/webp",
            "gif": "image/gif",
        }.get(ext, "image/jpeg")
        safe_ref = re.sub(r"[^A-Za-z0-9_-]+", "_", str(reference_id))[:36]
        path = f"{lead_id}/{safe_ref}_{secrets.token_hex(4)}.{ext}"
        try:
            bucket = self.client.storage.from_("receipts")
            bucket.upload(
                path,
                file_bytes,
                file_options={
                    "content-type": content_type,
                },
            )
            return bucket.get_public_url(path)
        except Exception as e:
            logger.warning("upload_receipt_to_storage failed (bucket missing or RLS?): %s", e)
            return None
    
    def update_lead_receipt(
        self, lead_id: str, receipt_image_url: str, receipt_price: str | None = None
    ) -> bool:
        """Update lead with receipt image URL (and optional receipt_price) and set status to Paid."""
        if not self._check_tables_exist():
            return False
        # Persist URL first so a failing monday_status/constraint does not block the receipt.
        updates = {"receipt_image_url": receipt_image_url}
        if receipt_price is not None and str(receipt_price).strip():
            updates["receipt_price"] = str(receipt_price).strip()
        if not self.update_lead(lead_id, updates):
            # Forward-step: if the DB doesn't have receipt_price yet, retry with URL only.
            if "receipt_price" in updates:
                if not self.update_lead(lead_id, {"receipt_image_url": receipt_image_url}):
                    return False
            else:
                return False
        self.update_lead(lead_id, {"monday_status": "Paid"})
        return True
    
    # Group management methods
    def create_group(self, group_name: str, group_telegram_id: str, supervisory_telegram_id: str) -> bool:
        """Create a new group."""
        if not self._check_tables_exist():
            return False
        
        try:
            self.client.table("groups").insert({
                "group_name": group_name,
                "group_telegram_id": group_telegram_id,
                "supervisory_telegram_id": supervisory_telegram_id
            }).execute()
            return True
        except Exception as e:
            logger.error(f"Error creating group: {e}")
            return False
    
    def get_all_groups(self) -> list:
        """Get all groups."""
        if not self._check_tables_exist():
            return []
        
        try:
            response = self.client.table("groups").select("*").order("group_name").execute()
            return response.data or []
        except Exception as e:
            logger.error(f"Error getting groups: {e}")
            return []
    
    def get_group_by_id(self, group_id: str) -> Optional[Dict[str, Any]]:
        """Get a group by ID."""
        if not self._check_tables_exist():
            return None
        
        try:
            response = self.client.table("groups").select("*").eq("id", group_id).execute()
            if response.data:
                return response.data[0]
            return None
        except Exception as e:
            logger.error(f"Error getting group: {e}")
            return None
    
    def toggle_group_status(self, group_id: str) -> bool:
        """Toggle group active status."""
        if not self._check_tables_exist():
            return False
        
        try:
            group = self.get_group_by_id(group_id)
            if group:
                new_status = not group.get('is_active', True)
                self.client.table("groups").update({"is_active": new_status}).eq("id", group_id).execute()
                return True
            return False
        except Exception as e:
            logger.error(f"Error toggling group status: {e}")
            return False
    
    # Group assistants: Telegram IDs that send leads into this group
    def get_group_by_assistant_telegram_id(self, telegram_id: str) -> Optional[Dict[str, Any]]:
        """Return the group that has this telegram_id as an assistant (first if multiple)."""
        if not self._check_tables_exist():
            return None
        try:
            r = self.client.table("group_assistants").select("group_id").eq("telegram_id", str(telegram_id)).limit(1).execute()
            if not r.data:
                return None
            return self.get_group_by_id(r.data[0]["group_id"])
        except Exception as e:
            logger.error(f"Error getting group by assistant: {e}")
            return None

    def get_group_assistants(self, group_id: str) -> list:
        """Return list of {telegram_id} for assistants assigned to this group."""
        if not self._check_tables_exist():
            return []
        try:
            r = self.client.table("group_assistants").select("telegram_id").eq("group_id", group_id).execute()
            return [x["telegram_id"] for x in (r.data or [])]
        except Exception as e:
            logger.error(f"Error getting group assistants: {e}")
            return []

    def add_group_assistant(self, group_id: str, telegram_id: str) -> bool:
        """Assign an assistant (Telegram user ID) to a group."""
        if not self._check_tables_exist():
            return False
        try:
            self.client.table("group_assistants").insert({"group_id": group_id, "telegram_id": str(telegram_id).strip()}).execute()
            return True
        except Exception as e:
            logger.error(f"Error adding group assistant: {e}")
            return False

    def remove_group_assistant(self, group_id: str, telegram_id: str) -> bool:
        """Remove an assistant from a group."""
        if not self._check_tables_exist():
            return False
        try:
            self.client.table("group_assistants").delete().eq("group_id", group_id).eq("telegram_id", str(telegram_id).strip()).execute()
            return True
        except Exception as e:
            logger.error(f"Error removing group assistant: {e}")
            return False

    # Settings (e.g. assistants_choose_group)
    def get_setting(self, key: str) -> Optional[str]:
        """Get a setting value by key. Returns None if not found or table missing."""
        if not self._check_tables_exist():
            return None
        try:
            r = self.client.table("settings").select("value").eq("key", key).limit(1).execute()
            if r.data and len(r.data) > 0:
                return (r.data[0].get("value") or "").strip()
            return None
        except Exception as e:
            logger.error(f"Error getting setting {key}: {e}")
            return None

    def set_setting(self, key: str, value: str) -> bool:
        """Set a setting value. Upserts."""
        if not self._check_tables_exist():
            return False
        try:
            self.client.table("settings").upsert({"key": key, "value": str(value)}, on_conflict="key").execute()
            return True
        except Exception as e:
            logger.error(f"Error setting {key}: {e}")
            return False

    # Driver management methods
    def create_driver(self, driver_name: str, driver_telegram_id: str, phone_number: Optional[str] = None) -> bool:
        """Create a new driver."""
        if not self._check_tables_exist():
            return False
        
        try:
            self.client.table("drivers").insert({
                "driver_name": driver_name,
                "driver_telegram_id": driver_telegram_id,
                "phone_number": phone_number
            }).execute()
            return True
        except Exception as e:
            logger.error(f"Error creating driver: {e}")
            return False
    
    def get_all_drivers(self) -> list:
        """Get all drivers."""
        if not self._check_tables_exist():
            return []
        
        try:
            response = self.client.table("drivers").select("*").order("driver_name").execute()
            return response.data or []
        except Exception as e:
            logger.error(f"Error getting drivers: {e}")
            return []

    def get_driver_by_telegram_id(self, telegram_user_id: str) -> Optional[Dict[str, Any]]:
        """Single-row lookup by driver_telegram_id (indexed). Prefer over scanning get_all_drivers()."""
        if not self._check_tables_exist():
            return None
        uid = str(telegram_user_id).strip()
        try:
            response = self.client.table("drivers").select("*").eq("driver_telegram_id", uid).limit(1).execute()
            return response.data[0] if response.data else None
        except Exception as e:
            logger.error(f"Error getting driver by telegram id: {e}")
            return None
    
    def get_group_driver_rows_for_group(self, group_id: str) -> list:
        """All drivers linked to a group via group_drivers (any is_active)."""
        if not self._check_tables_exist():
            return []
        try:
            response = self.client.table("group_drivers").select(
                "driver:drivers(*)"
            ).eq("group_id", group_id).execute()
            out = []
            for item in response.data or []:
                driver = item.get("driver")
                if driver:
                    out.append(driver)
            return out
        except Exception as e:
            logger.error(f"Error getting group driver rows: {e}")
            return []

    def get_active_drivers_for_group(self, group_id: str) -> list:
        """Get all active drivers for a specific group."""
        if not self._check_tables_exist():
            return []
        return [d for d in self.get_group_driver_rows_for_group(group_id) if record_is_active(d)]
    
    def toggle_driver_status(self, driver_id: str) -> bool:
        """Toggle driver active status."""
        if not self._check_tables_exist():
            return False
        
        try:
            driver = self.client.table("drivers").select("*").eq("id", driver_id).execute()
            if driver.data:
                current_status = driver.data[0].get('is_active', True)
                new_status = not current_status
                self.client.table("drivers").update({"is_active": new_status}).eq("id", driver_id).execute()
                return True
            return False
        except Exception as e:
            logger.error(f"Error toggling driver status: {e}")
            return False
    
    # Assignment methods
    def assign_driver_to_group(self, group_id: str, driver_id: str) -> bool:
        """Assign a driver to a group."""
        if not self._check_tables_exist():
            return False
        
        try:
            self.client.table("group_drivers").insert({
                "group_id": group_id,
                "driver_id": driver_id
            }).execute()
            return True
        except Exception as e:
            logger.error(f"Error assigning driver to group: {e}")
            return False
    
    def get_all_assignments(self) -> list:
        """Get all group-driver assignments with names."""
        if not self._check_tables_exist():
            return []
        
        try:
            response = self.client.table("group_drivers").select(
                "id, group:groups(group_name), driver:drivers(driver_name)"
            ).execute()
            
            assignments = []
            for item in response.data or []:
                assignments.append({
                    "id": item.get("id"),
                    "group_name": item.get("group", {}).get("group_name", "N/A"),
                    "driver_name": item.get("driver", {}).get("driver_name", "N/A")
                })
            return assignments
        except Exception as e:
            logger.error(f"Error getting assignments: {e}")
            return []
    
    def remove_driver_from_group(self, assignment_id: str) -> bool:
        """Remove a driver from a group."""
        if not self._check_tables_exist():
            return False
        
        try:
            self.client.table("group_drivers").delete().eq("id", assignment_id).execute()
            return True
        except Exception as e:
            logger.error(f"Error removing driver from group: {e}")
            return False
    
    # Lead assignment methods
    def create_lead_assignment(self, lead_id: str, driver_id: str, group_id: str) -> bool:
        """Create a lead assignment (when sent to driver)."""
        if not self._check_tables_exist():
            return False
        try:
            self.client.table("lead_assignments").insert({
                "lead_id": lead_id,
                "driver_id": driver_id,
                "group_id": group_id,
                "status": "pending",
            }).execute()
            return True
        except Exception as e:
            logger.error(f"Error creating lead assignment: {e}")
            return False

    def lead_has_assignments(self, lead_id: str) -> bool:
        """True if any driver assignment exists for this lead (sender already picked drivers)."""
        if not self._check_tables_exist():
            return False
        try:
            r = self.client.table("lead_assignments").select("id").eq("lead_id", lead_id).limit(1).execute()
            return bool(r.data)
        except Exception as e:
            logger.error(f"Error checking lead assignments: {e}")
            return False

    # Group lead offer methods (broadcast lead to many groups; first accept wins)
    def create_group_lead_offer(
        self,
        lead_id: str,
        group_id: str,
        group_chat_id: str | None = None,
        group_message_id: int | None = None,
    ) -> bool:
        """Create a pending group offer record for a lead."""
        if not self._check_tables_exist():
            return False
        try:
            row = {
                "lead_id": lead_id,
                "group_id": group_id,
                "status": "pending",
            }
            if group_chat_id is not None:
                row["group_chat_id"] = str(group_chat_id)
            if group_message_id is not None:
                row["group_message_id"] = int(group_message_id)
            self.client.table("group_lead_offers").insert(row).execute()
            return True
        except Exception as e:
            logger.error(f"Error creating group lead offer: {e}")
            return False

    def update_group_lead_offer_message(
        self,
        lead_id: str,
        group_id: str,
        group_chat_id: str | None,
        group_message_id: int | None,
    ) -> bool:
        """Persist the group chat/message IDs for an offer after sending."""
        if not self._check_tables_exist():
            return False
        try:
            updates = {}
            if group_chat_id is not None:
                updates["group_chat_id"] = str(group_chat_id)
            if group_message_id is not None:
                updates["group_message_id"] = int(group_message_id)
            if not updates:
                return True
            self.client.table("group_lead_offers").update(updates).eq("lead_id", lead_id).eq("group_id", group_id).execute()
            return True
        except Exception as e:
            logger.error(f"Error updating group lead offer message: {e}")
            return False

    def get_group_lead_offers(self, lead_id: str) -> list:
        """Return all group offers for a lead."""
        if not self._check_tables_exist():
            return []
        try:
            r = self.client.table("group_lead_offers").select("*").eq("lead_id", lead_id).execute()
            return r.data or []
        except Exception as e:
            logger.error(f"Error getting group lead offers: {e}")
            return []

    def get_accepted_group_for_lead(self, lead_id: str) -> Optional[Dict[str, Any]]:
        """Return accepted group offer for a lead (if any)."""
        if not self._check_tables_exist():
            return None
        try:
            r = self.client.table("group_lead_offers").select("*").eq("lead_id", lead_id).eq("status", "accepted").limit(1).execute()
            return r.data[0] if r.data else None
        except Exception as e:
            logger.error(f"Error getting accepted group for lead: {e}")
            return None

    def accept_group_lead_offer(self, lead_id: str, group_id: str, accepted_by_telegram_id: str) -> bool:
        """Accept a group offer (first group to accept wins); declines all other pending offers.

        Concurrency: a read-then-update race allowed two groups' rows to both become
        ``accepted``. We now atomically ``UPDATE … WHERE status='pending'`` for this row only,
        and require DB partial unique index ``(lead_id) WHERE status='accepted'`` so a second
        group cannot commit accepted (see migration_group_lead_offers_one_accepted_per_lead.sql).

        Idempotency: if this group's row is already ``accepted``, return False so the bot does
        not re-send notifications.
        """
        if not self._check_tables_exist():
            return False
        try:
            cur = (
                self.client.table("group_lead_offers")
                .select("status")
                .eq("lead_id", lead_id)
                .eq("group_id", group_id)
                .limit(1)
                .execute()
            )
            if not cur.data:
                return False
            st = (cur.data[0].get("status") or "").lower()
            if st == "accepted":
                return False
            if st != "pending":
                return False

            self.client.table("group_lead_offers").update(
                {
                    "status": "accepted",
                    "accepted_by_telegram_id": str(accepted_by_telegram_id),
                    "accepted_at": "now()",
                }
            ).eq("lead_id", lead_id).eq("group_id", group_id).eq("status", "pending").execute()

            verify = (
                self.client.table("group_lead_offers")
                .select("status")
                .eq("lead_id", lead_id)
                .eq("group_id", group_id)
                .limit(1)
                .execute()
            )
            if not verify.data or verify.data[0].get("status") != "accepted":
                return False

            self.client.table("group_lead_offers").update({"status": "declined"}).eq(
                "lead_id", lead_id
            ).eq("status", "pending").neq("group_id", group_id).execute()
            return True
        except Exception as e:
            err = str(e)
            if "23505" in err or "unique" in err.lower() or "duplicate" in err.lower():
                logger.info(
                    "Group lead accept lost race (lead=%s group=%s): %s",
                    lead_id,
                    group_id,
                    err[:200],
                )
            else:
                logger.error(f"Error accepting group lead offer: {e}")
            return False

    def decline_group_lead_offer(self, lead_id: str, group_id: str) -> bool:
        """Decline a group offer."""
        if not self._check_tables_exist():
            return False
        try:
            self.client.table("group_lead_offers").update({"status": "declined"}).eq("lead_id", lead_id).eq("group_id", group_id).execute()
            return True
        except Exception as e:
            logger.error(f"Error declining group lead offer: {e}")
            return False

    def delete_group_lead_offers_for_lead(self, lead_id: str) -> bool:
        """Remove all group offer rows for a lead (e.g. before re-routing to another group)."""
        if not self._check_tables_exist():
            return False
        try:
            self.client.table("group_lead_offers").delete().eq("lead_id", lead_id).execute()
            return True
        except Exception as e:
            logger.error(f"delete_group_lead_offers_for_lead: {e}")
            return False

    def get_leads_pending_group_accept_timeout(self, minutes: int = 5) -> list:
        """Leads still awaiting a group Accept, no driver assignments, pending offer older than ``minutes``."""
        if not self._check_tables_exist():
            return []
        try:
            from datetime import datetime, timedelta
            import pytz
            now_utc = datetime.now(pytz.UTC)
            cutoff = now_utc - timedelta(minutes=minutes)
            r = self.client.table("leads").select(
                "id, user_id, reference_id, awaiting_group_accept, group_accept_timeout_notified_at"
            ).eq("awaiting_group_accept", True).execute()
            out = []
            for lead in r.data or []:
                lid = lead.get("id")
                if not lid or lead.get("group_accept_timeout_notified_at"):
                    continue
                if self.lead_has_assignments(lid):
                    continue
                if self.get_accepted_group_for_lead(lid):
                    continue
                offers = [
                    o for o in self.get_group_lead_offers(lid)
                    if (o.get("status") or "").lower() == "pending"
                ]
                if not offers:
                    continue
                oldest = None
                for o in offers:
                    c = o.get("created_at")
                    if not c:
                        continue
                    try:
                        # ISO8601 from Postgres
                        ts = datetime.fromisoformat(str(c).replace("Z", "+00:00"))
                        if ts.tzinfo is None:
                            ts = pytz.UTC.localize(ts)
                        if oldest is None or ts < oldest:
                            oldest = ts
                    except (ValueError, TypeError):
                        continue
                if oldest is None:
                    continue
                if oldest > cutoff:
                    continue
                out.append({
                    "lead_id": lid,
                    "user_id": lead.get("user_id"),
                    "reference_id": lead.get("reference_id") or "N/A",
                })
            return out
        except Exception as e:
            err = str(e)
            if "awaiting_group_accept" in err or "42703" in err or "column" in err.lower():
                logger.warning(
                    "get_leads_pending_group_accept_timeout: migration_group_accept_gate.sql may be missing — %s",
                    err[:200],
                )
            else:
                logger.error("get_leads_pending_group_accept_timeout: %s", e)
            return []

    def mark_group_accept_timeout_notified(self, lead_id: str) -> bool:
        if not self._check_tables_exist():
            return False
        try:
            from datetime import datetime
            import pytz
            now_utc = datetime.now(pytz.UTC).strftime("%Y-%m-%dT%H:%M:%S") + "Z"
            self.client.table("leads").update({
                "group_accept_timeout_notified_at": now_utc,
            }).eq("id", lead_id).execute()
            return True
        except Exception as e:
            logger.error("mark_group_accept_timeout_notified: %s", e)
            return False

    def get_driver_ids_with_pending_receipt_count_at_least(self, threshold: int) -> set:
        """Single-query alternative to N× get_driver_pending_receipts for suspension checks."""
        if not self._check_tables_exist() or threshold <= 0:
            return set()
        try:
            r = self.client.table("lead_assignments").select(
                "driver_id, lead:leads(receipt_image_url)"
            ).eq("status", "accepted").execute()
            counts: dict[str, int] = {}
            for row in r.data or []:
                lead = row.get("lead") or {}
                if lead.get("receipt_image_url"):
                    continue
                did = row.get("driver_id")
                if not did:
                    continue
                ds = str(did)
                counts[ds] = counts.get(ds, 0) + 1
            return {did for did, c in counts.items() if c >= threshold}
        except Exception as e:
            logger.error("get_driver_ids_with_pending_receipt_count_at_least: %s", e)
            return set()

    def accept_lead_assignment(self, lead_id: str, driver_id: str) -> Optional[Dict[str, Any]]:
        """Accept a lead assignment (first driver to accept).

        Returns the accepted assignment row (at least ``id``, ``lead_id``, ``driver_id``), or ``None``
        if the lead was already accepted by someone else, no matching pending row exists, or on error.
        """
        if not self._check_tables_exist():
            return None

        try:
            existing = self.client.table("lead_assignments").select("id").eq(
                "lead_id", lead_id
            ).eq("status", "accepted").limit(1).execute()

            if existing.data:
                return None  # Already accepted by someone else

            self.client.table("lead_assignments").update({
                "status": "accepted",
                "accepted_at": "now()"
            }).eq("lead_id", lead_id).eq("driver_id", driver_id).execute()

            self.client.table("lead_assignments").update({
                "status": "declined"
            }).eq("lead_id", lead_id).eq("status", "pending").neq("driver_id", driver_id).execute()

            r = self.client.table("lead_assignments").select("id, lead_id, driver_id, status").eq(
                "lead_id", lead_id
            ).eq("driver_id", driver_id).eq("status", "accepted").limit(1).execute()
            return r.data[0] if r.data else None
        except Exception as e:
            logger.error(f"Error accepting lead assignment: {e}")
            return None
    
    def decline_lead_assignment(self, lead_id: str, driver_id: str) -> bool:
        """Decline a lead assignment."""
        if not self._check_tables_exist():
            return False
        
        try:
            self.client.table("lead_assignments").update({
                "status": "declined"
            }).eq("lead_id", lead_id).eq("driver_id", driver_id).execute()
            return True
        except Exception as e:
            logger.error(f"Error declining lead assignment: {e}")
            return False
    
    def get_lead_assignment_status(self, lead_id: str) -> Optional[Dict[str, Any]]:
        """Get the accepted assignment for a lead."""
        if not self._check_tables_exist():
            return None
        
        try:
            response = self.client.table("lead_assignments").select(
                "*, driver:drivers(*)"
            ).eq("lead_id", lead_id).eq("status", "accepted").execute()
            
            if response.data:
                return response.data[0]
            return None
        except Exception as e:
            logger.error(f"Error getting lead assignment status: {e}")
            return None

    def _norm_paper_driver_id(self, driver_id: str) -> str:
        return str(driver_id).strip()

    @staticmethod
    def _norm_uuid_str(val) -> str:
        """Normalize UUID strings so paper_inventory / paper_processed rows match across clients."""
        if val is None:
            return ""
        s = str(val).strip()
        if not s:
            return ""
        try:
            import uuid

            return str(uuid.UUID(s))
        except (ValueError, TypeError, AttributeError):
            return s

    def _paper_ensure_inventory_row(self, driver_id: str) -> None:
        did = self._norm_uuid_str(self._norm_paper_driver_id(driver_id))
        try:
            existing = self.client.table("paper_inventory").select("id").eq("driver_id", did).limit(1).execute()
            if not existing.data:
                self.client.table("paper_inventory").insert({"driver_id": did, "current_count": 0}).execute()
        except Exception as e:
            logger.error("_paper_ensure_inventory_row: %s", e)

    def paper_was_low_alert_sent(self, driver_id: str) -> bool:
        did = self._norm_uuid_str(self._norm_paper_driver_id(driver_id))
        try:
            r = self.client.table("paper_inventory").select("low_alert_sent").eq("driver_id", did).limit(1).execute()
            return bool(r.data and r.data[0].get("low_alert_sent"))
        except Exception:
            return False

    def paper_mark_low_alert_sent(self, driver_id: str) -> None:
        did = self._norm_uuid_str(self._norm_paper_driver_id(driver_id))
        try:
            self.client.table("paper_inventory").update({"low_alert_sent": True}).eq("driver_id", did).execute()
        except Exception:
            pass

    def apply_paper_on_lead_accept(self, driver_id: str, assignment_id: str, reference_id: str) -> Optional[int]:
        """Subtract one paper when a driver accepts a lead (Paper Investigator shared tables).

        Idempotent: skips if ``paper_processed_assignments`` already has this assignment.

        Returns the new paper balance, or ``None`` if skipped (already counted) or paper tables unavailable.
        """
        did = self._norm_uuid_str(self._norm_paper_driver_id(driver_id))
        aid = self._norm_uuid_str(assignment_id)
        if not aid:
            return None
        try:
            pr = self.client.table("paper_processed_assignments").select("assignment_id").eq("assignment_id", aid).limit(1).execute()
            if pr.data:
                return None
            self._paper_ensure_inventory_row(did)
            r = self.client.table("paper_inventory").select("current_count").eq("driver_id", did).limit(1).execute()
            current = int(r.data[0]["current_count"] or 0) if r.data else 0
            new_balance = max(0, current - 1)
            self.client.table("paper_inventory").update({"current_count": new_balance}).eq("driver_id", did).execute()
            ref = (reference_id or "").strip() or None
            self.client.table("paper_transactions").insert({
                "driver_id": did,
                "type": "subtract_order",
                "amount": -1,
                "balance_after": new_balance,
                "reference_id": ref,
                "note": "Order accepted in Krab Issuer",
                "created_by": None,
            }).execute()
            self.client.table("paper_processed_assignments").insert({
                "assignment_id": aid,
                "driver_id": did,
            }).execute()
            return new_balance
        except Exception as e:
            err = str(e)
            if "Could not find the table" in err or "PGRST205" in err:
                logger.warning("Paper tables missing — skipping paper subtract: %s", e)
            else:
                logger.error("apply_paper_on_lead_accept: %s", e)
            return None

    def get_accepted_leads_without_receipt_over_24h(self) -> list:
        """Accepted assignments where lead has no receipt and accepted_at is 24+ hours ago; reminder not yet sent."""
        if not self._check_tables_exist():
            return []
        try:
            from datetime import datetime, timedelta
            import pytz
            eastern = pytz.timezone("America/New_York")
            now_eastern = datetime.now(eastern)
            cutoff_dt = (now_eastern - timedelta(hours=24)).astimezone(pytz.UTC)
            cutoff = cutoff_dt.strftime("%Y-%m-%dT%H:%M:%S") + "Z"
            response = self.client.table("lead_assignments").select(
                "id, lead_id, driver_id, accepted_at, "
                "lead:leads(reference_id, receipt_image_url), "
                "driver:drivers(driver_telegram_id, driver_name)"
            ).eq("status", "accepted").is_("receipt_reminder_sent_at", "null").lt(
                "accepted_at", cutoff
            ).execute()
            out = []
            for row in (response.data or []):
                lead = row.get("lead") or {}
                if lead.get("receipt_image_url"):
                    continue
                driver = row.get("driver") or {}
                if not driver.get("driver_telegram_id"):
                    continue
                out.append({
                    "assignment_id": row.get("id"),
                    "lead_id": row.get("lead_id"),
                    "driver_id": row.get("driver_id"),
                    "reference_id": lead.get("reference_id") or "N/A",
                    "driver_telegram_id": driver.get("driver_telegram_id"),
                    "driver_name": driver.get("driver_name", "Driver"),
                })
            return out
        except Exception as e:
            logger.error(f"Error getting overdue receipt assignments: {e}")
            return []

    def get_leads_pending_driver_timeout(self, minutes: int = 10) -> list:
        """Leads where all assignments are still pending, oldest assignment 10+ min ago, timeout not yet notified."""
        if not self._check_tables_exist():
            return []
        try:
            from datetime import datetime, timedelta
            import pytz
            eastern = pytz.timezone("America/New_York")
            now_eastern = datetime.now(eastern)
            cutoff_dt = (now_eastern - timedelta(minutes=minutes)).astimezone(pytz.UTC)
            cutoff = cutoff_dt.strftime("%Y-%m-%dT%H:%M:%S") + "Z"
            r = self.client.table("lead_assignments").select(
                "lead_id, created_at, driver:drivers(driver_telegram_id, driver_name)"
            ).eq("status", "pending").lt("created_at", cutoff).execute()
            if not r.data:
                return []
            by_lead = {}
            for row in r.data or []:
                lid = row.get("lead_id")
                if lid not in by_lead:
                    by_lead[lid] = {"drivers": [], "min_created": row.get("created_at")}
                driver = row.get("driver") or {}
                by_lead[lid]["drivers"].append({
                    "driver_telegram_id": driver.get("driver_telegram_id"),
                    "driver_name": driver.get("driver_name", "Driver"),
                })
                c = row.get("created_at")
                if c and (not by_lead[lid]["min_created"] or c < by_lead[lid]["min_created"]):
                    by_lead[lid]["min_created"] = c
            lead_ids = list(by_lead.keys())
            if not lead_ids:
                return []
            leads_resp = self.client.table("leads").select(
                "id, user_id, reference_id, driver_timeout_notified_at"
            ).in_("id", lead_ids).execute()
            leads_by_id = {str(row["id"]): row for row in (leads_resp.data or [])}
            acc_resp = self.client.table("lead_assignments").select("lead_id").in_(
                "lead_id", lead_ids
            ).eq("status", "accepted").execute()
            accepted_ids = {str(row["lead_id"]) for row in (acc_resp.data or [])}

            out = []
            for lead_id, info in by_lead.items():
                sid = str(lead_id)
                lead = leads_by_id.get(sid)
                if not lead:
                    continue
                if lead.get("driver_timeout_notified_at"):
                    continue
                if sid in accepted_ids:
                    continue
                out.append({
                    "lead_id": lead_id,
                    "user_id": lead.get("user_id"),
                    "reference_id": lead.get("reference_id") or "N/A",
                    "drivers": info["drivers"],
                })
            return out
        except Exception as e:
            logger.error("get_leads_pending_driver_timeout: %s", e)
            return []

    def mark_driver_timeout_notified(self, lead_id: str) -> bool:
        """Mark that we sent driver timeout notification for this lead."""
        if not self._check_tables_exist():
            return False
        try:
            from datetime import datetime
            import pytz
            now_utc = datetime.now(pytz.UTC).strftime("%Y-%m-%dT%H:%M:%S") + "Z"
            self.client.table("leads").update({
                "driver_timeout_notified_at": now_utc
            }).eq("id", lead_id).execute()
            return True
        except Exception as e:
            logger.error("mark_driver_timeout_notified: %s", e)
            return False

    def _lead_excluded_from_receipt_count(self, lead: dict) -> bool:
        """True when appeals migration marked the lead excluded (column may be absent)."""
        return bool((lead or {}).get("exclude_from_count"))

    def get_driver_pending_receipts(self, driver_id: str) -> list:
        """Accepted assignments for this driver where lead has no receipt. Returns list of {reference_id, lead_id, lead}."""
        if not self._check_tables_exist():
            return []
        try:
            # Do not select appeal_status / exclude_from_count here — production DBs that
            # have not run migration_lead_appeals.sql will 42703 and return [] for all drivers.
            r = self.client.table("lead_assignments").select(
                "lead_id, lead:leads(reference_id, receipt_image_url, vehicle_details, delivery_details, extra_info, special_request_note, special_request_issuers, special_request_drivers)"
            ).eq("driver_id", driver_id).eq("status", "accepted").execute()
            out = []
            for row in r.data or []:
                lead = row.get("lead") or {}
                if lead.get("receipt_image_url"):
                    continue
                if self._lead_excluded_from_receipt_count(lead):
                    continue
                out.append({
                    "lead_id": row.get("lead_id"),
                    "reference_id": lead.get("reference_id") or "N/A",
                    "lead": lead,
                })
            return out
        except Exception as e:
            logger.error("get_driver_pending_receipts: %s", e)
            return []

    def _fetch_rows_by_id(
        self,
        table: str,
        ids: list,
        columns: str,
        *,
        chunk_size: int = 100,
    ) -> dict[str, dict]:
        """Batch-fetch rows by primary key ``id`` (chunks for PostgREST ``in_`` limits)."""
        out: dict[str, dict] = {}
        clean = [str(x).strip() for x in (ids or []) if x]
        if not clean:
            return out
        for i in range(0, len(clean), chunk_size):
            chunk = clean[i : i + chunk_size]
            try:
                r = self.client.table(table).select(columns).in_("id", chunk).execute()
            except Exception as e:
                logger.error("_fetch_rows_by_id(%s): %s", table, e)
                continue
            for row in r.data or []:
                rid = row.get("id")
                if rid:
                    out[str(rid)] = row
        return out

    def get_all_pending_receipts(self, limit: int = 90) -> list:
        """Accepted assignments missing receipts (all drivers). For supervisor /receipts menu."""
        if not self._check_tables_exist():
            return []
        try:
            # Flat queries — nested ``lead:leads(...)`` embeds can come back empty on bulk
            # selects (PostgREST/RLS), which made the supervisor /receipts menu show
            # "No outstanding receipts" even while per-driver pending counts were > 0.
            r = self.client.table("lead_assignments").select(
                "lead_id, driver_id"
            ).eq("status", "accepted").execute()
            rows = r.data or []
            if not rows:
                return []

            lead_ids = [row.get("lead_id") for row in rows if row.get("lead_id")]
            driver_ids = [row.get("driver_id") for row in rows if row.get("driver_id")]
            leads_by_id = self._fetch_rows_by_id(
                "leads",
                lead_ids,
                "id, reference_id, receipt_image_url",
            )
            drivers_by_id = self._fetch_rows_by_id(
                "drivers",
                driver_ids,
                "id, driver_name",
            )

            out = []
            for row in rows:
                lid = str(row.get("lead_id") or "").strip()
                did = str(row.get("driver_id") or "").strip()
                if not lid or not did:
                    continue
                lead = leads_by_id.get(lid) or {}
                if lead.get("receipt_image_url"):
                    continue
                if self._lead_excluded_from_receipt_count(lead):
                    continue
                ref = (lead.get("reference_id") or "").strip()
                if not ref or ref.upper() == "N/A":
                    continue
                driver = drivers_by_id.get(did) or {}
                out.append({
                    "lead_id": lid,
                    "driver_id": did,
                    "reference_id": ref,
                    "driver_name": (driver.get("driver_name") or "Driver").strip() or "Driver",
                    "lead": lead,
                })
            if not out and rows:
                logger.warning(
                    "get_all_pending_receipts: %d accepted assignment(s) but flat join found "
                    "0 pending — falling back to per-driver scan",
                    len(rows),
                )
                out = self._get_all_pending_receipts_per_driver(limit=0)
            if limit and len(out) > limit:
                return out[:limit]
            return out
        except Exception as e:
            logger.error("get_all_pending_receipts: %s", e)
            return self._get_all_pending_receipts_per_driver(limit)

    def _get_all_pending_receipts_per_driver(self, limit: int = 90) -> list:
        """Fallback: aggregate ``get_driver_pending_receipts`` across all drivers."""
        out: list = []
        for drv in self.get_all_drivers():
            did = drv.get("id")
            if not did:
                continue
            dname = (drv.get("driver_name") or "Driver").strip() or "Driver"
            for row in self.get_driver_pending_receipts(str(did)):
                ref = (row.get("reference_id") or "").strip()
                if not ref or ref.upper() == "N/A":
                    continue
                out.append({
                    "lead_id": row.get("lead_id"),
                    "driver_id": str(did),
                    "reference_id": ref,
                    "driver_name": dname,
                    "lead": row.get("lead") or {},
                })
        if limit and len(out) > limit:
            return out[:limit]
        return out

    def mark_receipt_reminder_sent(self, assignment_id: str) -> bool:
        """Mark that we sent the receipt reminder for this assignment."""
        if not self._check_tables_exist():
            return False
        try:
            from datetime import datetime
            import pytz
            now_eastern = datetime.now(pytz.timezone("America/New_York"))
            now_utc = now_eastern.astimezone(pytz.UTC).strftime("%Y-%m-%dT%H:%M:%S") + "Z"
            self.client.table("lead_assignments").update({
                "receipt_reminder_sent_at": now_utc,
            }).eq("id", assignment_id).execute()
            return True
        except Exception as e:
            logger.error(f"Error marking receipt reminder sent: {e}")
            return False

    # Contact info sources (for "Select the Contact info source for this client")
    def get_contact_info_sources(self) -> list:
        """Get all active contact info sources, ordered by sort_order."""
        if not self._check_tables_exist():
            return []
        try:
            r = self.client.table("contact_info_sources").select("*").eq(
                "is_active", True
            ).order("sort_order").execute()
            return r.data or []
        except Exception as e:
            logger.error(f"Error getting contact info sources: {e}")
            return []

    def get_contact_info_source_by_id(self, source_id: str) -> Optional[Dict[str, Any]]:
        """Get a single contact info source by id."""
        if not self._check_tables_exist():
            return None
        try:
            r = self.client.table("contact_info_sources").select("*").eq("id", source_id).limit(1).execute()
            return r.data[0] if r.data else None
        except Exception as e:
            logger.error(f"Error getting contact info source: {e}")
            return None

    def get_bot_usage(self, limit: int = 100) -> list:
        """Get recent bot usage (who sent to whom) for admin view."""
        if not self._check_tables_exist():
            return []
        try:
            r = self.client.table("bot_usage").select("*").order("created_at", desc=True).limit(limit).execute()
            return r.data or []
        except Exception as e:
            logger.error(f"Error getting bot usage: {e}")
            return []

    def record_bot_usage(
        self,
        user_telegram_id: int,
        telegram_username: str,
        lead_id: Optional[str],
        group_name: str,
        driver_names: str,
    ) -> bool:
        """Record that a user sent a lead to a group and driver(s) (for admin usage view)."""
        if not self._check_tables_exist():
            return False
        try:
            self.client.table("bot_usage").insert({
                "user_telegram_id": user_telegram_id,
                "telegram_username": telegram_username or "",
                "lead_id": lead_id,
                "group_name": group_name or "",
                "driver_names": driver_names or "",
            }).execute()
            return True
        except Exception as e:
            logger.error(f"Error recording bot usage: {e}")
            return False

    def get_lead_sender_telegram_ids(self) -> list:
        """Distinct telegram user IDs who have sent at least one lead (from bot_usage)."""
        if not self._check_tables_exist():
            return []
        try:
            r = self.client.table("bot_usage").select("user_telegram_id").execute()
            seen = set()
            out = []
            for row in (r.data or []):
                uid = row.get("user_telegram_id")
                if uid is not None and uid not in seen:
                    seen.add(uid)
                    out.append(uid)
            return out
        except Exception as e:
            logger.error(f"Error getting lead sender IDs: {e}")
            return []

    def get_lead_sender_stats(self) -> list:
        """List of {user_id, last_lead_at, leads_count_7d} for lead senders (from leads table)."""
        if not self._check_tables_exist():
            return []
        try:
            from datetime import datetime, timedelta
            import pytz
            now_eastern = datetime.now(pytz.timezone("America/New_York"))
            cutoff_7d = (now_eastern - timedelta(days=7)).astimezone(pytz.UTC).strftime("%Y-%m-%dT%H:%M:%S") + "Z"
            r = self.client.table("leads").select("user_id, created_at, exclude_from_count").order("created_at", desc=True).execute()
            by_user = {}
            for row in (r.data or []):
                if row.get("exclude_from_count"):
                    continue
                uid = row.get("user_id")
                if uid is None:
                    continue
                created = row.get("created_at") or ""
                if uid not in by_user:
                    by_user[uid] = {"last_lead_at": created, "leads_count_7d": 0}
                if created >= cutoff_7d:
                    by_user[uid]["leads_count_7d"] += 1
            return list(by_user.values())
        except Exception as e:
            logger.error(f"Error getting lead sender stats: {e}")
            return []

    def get_motivation_recipients(self) -> list:
        """For daily rotation: list of dicts with user_id, last_lead_at, leads_count_7d. Uses leads table."""
        if not self._check_tables_exist():
            return []
        try:
            from datetime import datetime, timedelta
            import pytz
            now_eastern = datetime.now(pytz.timezone("America/New_York"))
            cutoff_7d = (now_eastern - timedelta(days=7)).astimezone(pytz.UTC).strftime("%Y-%m-%dT%H:%M:%S") + "Z"
            cutoff_24h = (now_eastern - timedelta(hours=24)).astimezone(pytz.UTC).strftime("%Y-%m-%dT%H:%M:%S") + "Z"
            r = self.client.table("leads").select("user_id, created_at, exclude_from_count").execute()
            by_user = {}
            for row in (r.data or []):
                if row.get("exclude_from_count"):
                    continue
                uid = row.get("user_id")
                if uid is None:
                    continue
                created = row.get("created_at") or ""
                if uid not in by_user:
                    by_user[uid] = {"user_id": uid, "last_lead_at": created, "leads_count_7d": 0}
                if created > (by_user[uid]["last_lead_at"] or ""):
                    by_user[uid]["last_lead_at"] = created
                if created >= cutoff_7d:
                    by_user[uid]["leads_count_7d"] += 1
            out = list(by_user.values())
            for x in out:
                x["no_lead_24h"] = (x.get("last_lead_at") or "") < cutoff_24h
            return out
        except Exception as e:
            logger.error(f"Error getting motivation recipients: {e}")
            return []

    # ── Renewal helpers ──────────────────────────────────────────────────

    def resolve_renewal_group_id(self, lead_id: str, lead: dict | None = None) -> Optional[str]:
        """Best-effort issuing group for renewal routing (lead row → assignment → group offer)."""
        if not self._check_tables_exist():
            return None
        lead = lead or self.get_lead_by_id(lead_id) or {}
        gid = lead.get("group_id")
        if gid:
            return str(gid)
        st = self.get_lead_assignment_status(lead_id)
        if st and st.get("group_id"):
            return str(st["group_id"])
        acc = self.get_accepted_group_for_lead(lead_id)
        if acc and acc.get("group_id"):
            return str(acc["group_id"])
        return None

    def ensure_renewal_original_group(self, renewal: dict) -> dict:
        """Backfill ``original_group_id`` on older renewal rows when missing."""
        if not renewal or renewal.get("original_group_id"):
            return renewal or {}
        lead_id = renewal.get("lead_id")
        gid = self.resolve_renewal_group_id(lead_id, renewal.get("lead"))
        if gid and renewal.get("id"):
            self.update_renewal(str(renewal["id"]), {"original_group_id": gid})
            renewal = dict(renewal)
            renewal["original_group_id"] = gid
        return renewal

    def schedule_renewal(self, lead_id: str, group_id: str | None, driver_id: str | None, renewal_due_at: str) -> Optional[Dict[str, Any]]:
        """Create a pending renewal record for a lead (28-day cycle)."""
        if not self._check_tables_exist():
            return None
        try:
            if not group_id:
                group_id = self.resolve_renewal_group_id(lead_id)
            row = {
                "lead_id": lead_id,
                "renewal_due_at": renewal_due_at,
                "status": "pending",
                "group_status": "pending",
                "driver_status": "pending",
            }
            if group_id:
                row["original_group_id"] = group_id
            if driver_id:
                row["original_driver_id"] = driver_id
            r = self.client.table("lead_renewals").insert(row).execute()
            return r.data[0] if r.data else None
        except Exception as e:
            logger.error(f"Error scheduling renewal: {e}")
            return None

    def claim_renewal_for_group_dispatch(self, renewal_id: str) -> bool:
        """Atomically pending → group_phase. Only one worker should get True."""
        if not self._check_tables_exist():
            return False
        try:
            from datetime import datetime, timezone
            now_iso = datetime.now(timezone.utc).isoformat()
            r = (
                self.client.table("lead_renewals")
                .update({
                    "status": "group_phase",
                    "group_status": "sent",
                    "group_sent_at": now_iso,
                })
                .eq("id", renewal_id)
                .eq("status", "pending")
                .execute()
            )
            return bool(r.data)
        except Exception as e:
            logger.error("claim_renewal_for_group_dispatch: %s", e)
            return False

    def claim_renewal_for_driver_dispatch(self, renewal_id: str, original_group_id: str | None) -> bool:
        """Atomically pending → driver_phase for driver-first renewal routing.

        Skips the group-accept step entirely: the original group is auto-accepted so the
        existing driver-accept path works, and the offer goes straight to the original driver.
        Only one worker should get True.
        """
        if not self._check_tables_exist():
            return False
        try:
            from datetime import datetime, timezone
            now_iso = datetime.now(timezone.utc).isoformat()
            update = {
                "status": "driver_phase",
                "group_status": "accepted",
                "driver_status": "sent",
                "driver_sent_at": now_iso,
            }
            if original_group_id:
                update["group_accepted_by_id"] = original_group_id
            r = (
                self.client.table("lead_renewals")
                .update(update)
                .eq("id", renewal_id)
                .eq("status", "pending")
                .execute()
            )
            return bool(r.data)
        except Exception as e:
            logger.error("claim_renewal_for_driver_dispatch: %s", e)
            return False

    def get_due_renewals(self) -> list:
        """Return renewals whose due date has passed and are still 'pending'."""
        if not self._check_tables_exist():
            return []
        try:
            from datetime import datetime, timezone
            now_iso = datetime.now(timezone.utc).isoformat()
            r = (
                self.client.table("lead_renewals")
                .select("*, lead:leads(reference_id, vehicle_details, delivery_details, extra_info, "
                        "special_request_issuers, special_request_drivers, phone_number, price, encrypted_link, "
                        "onetimesecret_token, onetimesecret_secret_key, telegram_username, issue_date, expiration_date)")
                .eq("status", "pending")
                .lte("renewal_due_at", now_iso)
                .execute()
            )
            return r.data or []
        except Exception as e:
            logger.error(f"Error fetching due renewals: {e}")
            return []

    def get_renewal_by_id(self, renewal_id: str) -> Optional[Dict[str, Any]]:
        if not self._check_tables_exist():
            return None
        try:
            r = (
                self.client.table("lead_renewals")
                .select("*, lead:leads(reference_id, vehicle_details, delivery_details, extra_info, "
                        "special_request_issuers, special_request_drivers, phone_number, price, encrypted_link, "
                        "onetimesecret_token, onetimesecret_secret_key, telegram_username, issue_date, expiration_date)")
                .eq("id", renewal_id)
                .limit(1)
                .execute()
            )
            return r.data[0] if r.data else None
        except Exception as e:
            logger.error(f"Error fetching renewal: {e}")
            return None

    def update_renewal(self, renewal_id: str, data: dict) -> bool:
        """Generic update for a renewal row."""
        if not self._check_tables_exist():
            return False
        try:
            self.client.table("lead_renewals").update(data).eq("id", renewal_id).execute()
            return True
        except Exception as e:
            logger.error(f"Error updating renewal: {e}")
            return False

    def accept_renewal_group(self, renewal_id: str, group_id: str) -> str:
        """Team-level renewal accept.

        Returns one of: ``ok``, ``already_accepted``, ``wrong_team``, ``not_found``, ``error``.

        Before escalation (``group_status`` pending/sent) only the original issuing group
        may accept. After escalation any team that received the offer may accept (FCFS).
        """
        if not self._check_tables_exist():
            return "error"
        try:
            existing = self.get_renewal_by_id(renewal_id)
            if not existing:
                return "not_found"
            gs = (existing.get("group_status") or "").strip().lower()
            if gs == "accepted":
                return "already_accepted"
            existing = self.ensure_renewal_original_group(existing)
            orig_gid = existing.get("original_group_id")
            if gs in ("pending", "sent") and orig_gid and str(group_id) != str(orig_gid):
                return "wrong_team"
            r = (
                self.client.table("lead_renewals")
                .update({
                    "group_status": "accepted",
                    "group_accepted_by_id": group_id,
                    "status": "driver_phase",
                })
                .eq("id", renewal_id)
                .neq("group_status", "accepted")
                .execute()
            )
            if not r.data:
                return "already_accepted"
            return "ok"
        except Exception as e:
            logger.error(f"Error accepting renewal group: {e}")
            return "error"

    def accept_renewal_driver(self, renewal_id: str, driver_id: str) -> str:
        """Driver-level renewal accept (FCFS among drivers in the accepted group).

        Returns one of: ``ok``, ``already_accepted``, ``wrong_driver``, ``not_found``, ``error``.
        """
        if not self._check_tables_exist():
            return "error"
        try:
            existing = self.get_renewal_by_id(renewal_id)
            if not existing:
                return "not_found"
            if (existing.get("driver_status") or "").strip().lower() == "accepted":
                return "already_accepted"
            if (existing.get("group_status") or "").strip().lower() != "accepted":
                return "error"
            # After escalation the renewal is offered to all drivers everywhere (FCFS),
            # so skip the group-membership gate. Before escalation, only the original
            # group's drivers (i.e. the original driver) may grab it.
            ds = (existing.get("driver_status") or "").strip().lower()
            group_id = existing.get("group_accepted_by_id") or existing.get("original_group_id")
            if ds != "escalated" and group_id:
                allowed = {
                    str(d.get("id"))
                    for d in (self.get_active_drivers_for_group(str(group_id)) or [])
                    if d.get("id")
                }
                # Always allow the original driver we routed the renewal to, even if they
                # aren't linked to this group (drivers can work across all groups).
                orig_did = existing.get("original_driver_id")
                if orig_did:
                    allowed.add(str(orig_did))
                if allowed and str(driver_id) not in allowed:
                    return "wrong_driver"
            from datetime import datetime, timezone
            r = (
                self.client.table("lead_renewals")
                .update({
                    "driver_status": "accepted",
                    "driver_accepted_by_id": driver_id,
                    "status": "completed",
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                })
                .eq("id", renewal_id)
                .neq("driver_status", "accepted")
                .execute()
            )
            if not r.data:
                return "already_accepted"
            return "ok"
        except Exception as e:
            logger.error(f"Error accepting renewal driver: {e}")
            return "error"

    def get_active_renewal_for_lead(self, lead_id: str) -> Optional[Dict[str, Any]]:
        """Check if a non-completed renewal already exists for this lead."""
        if not self._check_tables_exist():
            return None
        try:
            r = (
                self.client.table("lead_renewals")
                .select("id, status")
                .eq("lead_id", lead_id)
                .in_("status", ["pending", "group_phase", "driver_phase"])
                .limit(1)
                .execute()
            )
            return r.data[0] if r.data else None
        except Exception as e:
            logger.error(f"Error checking active renewal: {e}")
            return None

    # ── Client follow-ups ──────────────────────────────────────────────

    def create_client_followup(
        self,
        user_id: str,
        telegram_username: str | None,
        client_name: str | None,
        phone_number: str | None,
        notes: str | None,
        frequency: str | None = None,
        start_at: str | None = None,
        next_reminder_at: str | None = None,
        email: str | None = None,
        contact_client: bool = False,
        end_at: str | None = None,
        kind: str = "followup",
    ) -> Optional[Dict[str, Any]]:
        """Create a prospective-client follow-up record.

        With no ``frequency``/``next_reminder_at`` this is just a stored contact
        (status='closed'); with a schedule it's an active reminder (status='open').
        """
        if not self._check_tables_exist():
            return None
        try:
            row = {
                "user_id": str(user_id),
                "telegram_username": telegram_username,
                "client_name": client_name,
                "phone_number": phone_number,
                "notes": notes,
                "status": "open" if next_reminder_at else "closed",
            }
            if frequency:
                row["frequency"] = frequency
            if start_at:
                row["start_at"] = start_at
            if next_reminder_at:
                row["next_reminder_at"] = next_reminder_at
            if email:
                row["email"] = email
            if contact_client:
                row["contact_client"] = True
            if end_at:
                row["end_at"] = end_at
            if kind and kind != "followup":
                row["kind"] = kind
            r = self.client.table("client_followups").insert(row).execute()
            return r.data[0] if r.data else None
        except Exception as e:
            logger.error(f"Error creating client follow-up: {e}")
            return None

    def get_due_client_followups(self) -> list:
        """Return open follow-ups whose next reminder time has passed."""
        if not self._check_tables_exist():
            return []
        try:
            from datetime import datetime, timezone
            now_iso = datetime.now(timezone.utc).isoformat()
            r = (
                self.client.table("client_followups")
                .select("*")
                .eq("status", "open")
                .lte("next_reminder_at", now_iso)
                .execute()
            )
            return r.data or []
        except Exception as e:
            logger.error(f"Error fetching due client follow-ups: {e}")
            return []

    def get_client_followup_by_id(self, followup_id: str) -> Optional[Dict[str, Any]]:
        if not self._check_tables_exist():
            return None
        try:
            r = (
                self.client.table("client_followups")
                .select("*")
                .eq("id", followup_id)
                .limit(1)
                .execute()
            )
            return r.data[0] if r.data else None
        except Exception as e:
            logger.error(f"Error fetching client follow-up: {e}")
            return None

    def update_client_followup(self, followup_id: str, data: dict) -> bool:
        if not self._check_tables_exist():
            return False
        try:
            self.client.table("client_followups").update(data).eq("id", followup_id).execute()
            return True
        except Exception as e:
            logger.error(f"Error updating client follow-up: {e}")
            return False

    def advance_client_followup(self, followup_id: str, next_reminder_at: str, last_reminded_at: str) -> bool:
        """Record that a reminder fired and schedule the next one."""
        return self.update_client_followup(followup_id, {
            "next_reminder_at": next_reminder_at,
            "last_reminded_at": last_reminded_at,
        })

    def close_client_followup(self, followup_id: str) -> bool:
        """Mark a follow-up as closed (sale done or reminders stopped)."""
        return self.update_client_followup(followup_id, {"status": "closed"})

    def get_followups_for_user(self, user_id: str, limit: int = 30) -> list:
        """Full follow-up history for a user (open + closed), newest first."""
        if not self._check_tables_exist():
            return []
        try:
            r = (
                self.client.table("client_followups")
                .select("*")
                .eq("user_id", str(user_id))
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
            return r.data or []
        except Exception as e:
            logger.error(f"Error fetching follow-up history for user: {e}")
            return []

    def get_open_followups_for_user(self, user_id: str) -> list:
        if not self._check_tables_exist():
            return []
        try:
            r = (
                self.client.table("client_followups")
                .select("*")
                .eq("user_id", str(user_id))
                .eq("status", "open")
                .order("next_reminder_at")
                .execute()
            )
            return r.data or []
        except Exception as e:
            logger.error(f"Error fetching open follow-ups for user: {e}")
            return []

    def get_all_open_followups(self) -> list:
        """All open follow-ups across every agent (supervisor backend view)."""
        if not self._check_tables_exist():
            return []
        try:
            r = (
                self.client.table("client_followups")
                .select("*")
                .eq("status", "open")
                .order("next_reminder_at")
                .execute()
            )
            return r.data or []
        except Exception as e:
            logger.error(f"Error fetching all open follow-ups: {e}")
            return []

    def auto_close_followups_matching_lead(self, lead: dict) -> list:
        """Close open follow-ups whose client matches a new dispatch order.

        Match by phone digits (last 10), email (case-insensitive), or full
        client name (first line of vehicle_details). Closed — never deleted.
        Returns the closed rows so the bot can notify the owning agents.
        """
        import re as _re

        def digits(s):
            return _re.sub(r"\D", "", str(s or ""))[-10:]

        lead_phone = digits(lead.get("phone_number"))
        lead_email = str(lead.get("email") or "").strip().lower()
        vd = str(lead.get("vehicle_details") or "").strip()
        lead_name = vd.splitlines()[0].strip().lower() if vd else ""

        if not (lead_phone or lead_email or lead_name):
            return []
        closed = []
        for f in self.get_all_open_followups():
            fp = digits(f.get("phone_number"))
            fe = str(f.get("email") or "").strip().lower()
            fn = str(f.get("client_name") or "").strip().lower()
            hit = (
                (lead_phone and fp and fp == lead_phone)
                or (lead_email and fe and fe == lead_email)
                or (lead_name and fn and fn == lead_name)
            )
            if hit and self.close_client_followup(f.get("id")):
                closed.append(f)
        return closed

    def delete_client_followup(self, followup_id: str) -> bool:
        """Permanently delete a follow-up record (supervisor backend action)."""
        if not self._check_tables_exist():
            return False
        try:
            self.client.table("client_followups").delete().eq("id", followup_id).execute()
            return True
        except Exception as e:
            logger.error(f"Error deleting client follow-up: {e}")
            return False

    # ── Driver GPS tracking sessions ───────────────────────────────────
    # Hard location gate: delivery details are only DM'd after the driver's
    # tracking session flips pending -> located (or a supervisor overrides).

    def create_tracking_session(
        self,
        *,
        token: str,
        kind: str,
        chat_id: str,
        driver_id: str | None = None,
        driver_name: str | None = None,
        lead_id: str | None = None,
        renewal_id: str | None = None,
        reference_id: str | None = None,
        delivery_address: str | None = None,
    ) -> Optional[Dict[str, Any]]:
        """Create a pending tracking session. Returns row or None on failure.

        ``delivery_address`` (v2 column) powers the admin map's destination/ETA;
        if the v2 migration hasn't run yet, the insert retries without it.
        """
        if not self._check_tables_exist():
            return None
        row = {
            "token": token,
            "kind": kind,
            "chat_id": str(chat_id),
            "status": "pending",
        }
        if driver_id:
            row["driver_id"] = str(driver_id)
        if driver_name:
            row["driver_name"] = driver_name
        if lead_id:
            row["lead_id"] = str(lead_id)
        if renewal_id:
            row["renewal_id"] = str(renewal_id)
        if reference_id:
            row["reference_id"] = str(reference_id)
        if delivery_address:
            row["delivery_address"] = delivery_address
        for attempt in (0, 1):
            try:
                r = self.client.table("driver_tracking_sessions").insert(row).execute()
                return r.data[0] if r.data else None
            except Exception as e:
                if attempt == 0 and "delivery_address" in row and "delivery_address" in str(e):
                    logger.warning(
                        "Tracking insert retrying without delivery_address — "
                        "run database/migration_driver_tracking_v2.sql: %s", e
                    )
                    row.pop("delivery_address", None)
                    continue
                logger.error(
                    "Error creating tracking session (run database/migration_driver_tracking.sql?): %s", e
                )
                return None
        return None

    def get_tracking_sessions_located_unsent(self) -> list:
        """Sessions whose location arrived but details haven't been sent yet."""
        if not self._check_tables_exist():
            return []
        try:
            r = (
                self.client.table("driver_tracking_sessions")
                .select("*")
                .eq("status", "located")
                .order("located_at")
                .limit(50)
                .execute()
            )
            return r.data or []
        except Exception as e:
            logger.error(f"Error fetching located tracking sessions: {e}")
            return []

    def get_tracking_sessions_pending_reminder(self, timeout_minutes: int) -> list:
        """Still-pending sessions past the reminder window, not yet reminded."""
        if not self._check_tables_exist():
            return []
        try:
            from datetime import datetime, timedelta, timezone
            cutoff = (datetime.now(timezone.utc) - timedelta(minutes=timeout_minutes)).isoformat()
            r = (
                self.client.table("driver_tracking_sessions")
                .select("*")
                .eq("status", "pending")
                .is_("reminder_sent_at", "null")
                .lt("created_at", cutoff)
                .limit(50)
                .execute()
            )
            return r.data or []
        except Exception as e:
            logger.error(f"Error fetching pending tracking reminders: {e}")
            return []

    def claim_tracking_details_sent(self, session_id: str) -> bool:
        """Conditionally claim located->details_sent. False if already claimed."""
        if not self._check_tables_exist():
            return False
        try:
            from datetime import datetime, timezone
            r = (
                self.client.table("driver_tracking_sessions")
                .update({
                    "status": "details_sent",
                    "details_sent_at": datetime.now(timezone.utc).isoformat(),
                })
                .eq("id", session_id)
                .eq("status", "located")
                .execute()
            )
            return bool(r.data)
        except Exception as e:
            logger.error(f"Error claiming tracking details_sent: {e}")
            return False

    def claim_tracking_override(self, session_id: str) -> bool:
        """Supervisor override: pending->overridden. False if no longer pending."""
        if not self._check_tables_exist():
            return False
        try:
            from datetime import datetime, timezone
            r = (
                self.client.table("driver_tracking_sessions")
                .update({
                    "status": "overridden",
                    "details_sent_at": datetime.now(timezone.utc).isoformat(),
                })
                .eq("id", session_id)
                .eq("status", "pending")
                .execute()
            )
            return bool(r.data)
        except Exception as e:
            logger.error(f"Error claiming tracking override: {e}")
            return False

    def mark_tracking_reminder_sent(self, session_id: str) -> bool:
        if not self._check_tables_exist():
            return False
        try:
            from datetime import datetime, timezone
            self.client.table("driver_tracking_sessions").update(
                {"reminder_sent_at": datetime.now(timezone.utc).isoformat()}
            ).eq("id", session_id).execute()
            return True
        except Exception as e:
            logger.error(f"Error marking tracking reminder sent: {e}")
            return False

    def get_tracking_session_by_id(self, session_id: str) -> Optional[Dict[str, Any]]:
        if not self._check_tables_exist():
            return None
        try:
            r = (
                self.client.table("driver_tracking_sessions")
                .select("*")
                .eq("id", session_id)
                .limit(1)
                .execute()
            )
            return r.data[0] if r.data else None
        except Exception as e:
            logger.error(f"Error fetching tracking session: {e}")
            return None

    # ── Lead appeals ───────────────────────────────────────────────────

    def upload_appeal_proof_to_storage(
        self,
        lead_id: str,
        reference_id: str,
        file_bytes: bytes,
        original_name: str,
    ) -> Optional[str]:
        """Upload appeal proof image to Storage bucket `receipts` under appeals/. Returns public URL."""
        if not file_bytes or not Config.SUPABASE_URL or not Config.SUPABASE_KEY:
            return None
        lower = (original_name or "").lower()
        ext = "jpg"
        if lower.endswith(".png"):
            ext = "png"
        elif lower.endswith(".webp"):
            ext = "webp"
        elif lower.endswith(".gif"):
            ext = "gif"
        content_type = {
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "png": "image/png",
            "webp": "image/webp",
            "gif": "image/gif",
        }.get(ext, "image/jpeg")
        safe_ref = re.sub(r"[^A-Za-z0-9_-]+", "_", str(reference_id))[:36]
        path = f"appeals/{lead_id}/{safe_ref}_{secrets.token_hex(4)}.{ext}"
        try:
            bucket = self.client.storage.from_("receipts")
            bucket.upload(
                path,
                file_bytes,
                file_options={"content-type": content_type},
            )
            return bucket.get_public_url(path)
        except Exception as e:
            logger.warning("upload_appeal_proof_to_storage failed: %s", e)
            return None

    def get_pending_appeal_for_lead(self, lead_id: str) -> Optional[Dict[str, Any]]:
        if not self._check_tables_exist():
            return None
        try:
            r = (
                self.client.table("lead_appeals")
                .select("*")
                .eq("lead_id", lead_id)
                .eq("status", "pending")
                .limit(1)
                .execute()
            )
            return r.data[0] if r.data else None
        except Exception as e:
            logger.error("get_pending_appeal_for_lead: %s", e)
            return None

    def get_appeal_by_id(self, appeal_id: str) -> Optional[Dict[str, Any]]:
        if not self._check_tables_exist():
            return None
        try:
            r = self.client.table("lead_appeals").select("*").eq("id", appeal_id).limit(1).execute()
            return r.data[0] if r.data else None
        except Exception as e:
            logger.error("get_appeal_by_id: %s", e)
            return None

    def create_lead_appeal(
        self,
        *,
        lead_id: str,
        reference_id: str,
        proof_image_url: str | None,
        proof_file_id: str | None,
        submitted_by_telegram_id: int,
        submitted_by_username: str | None,
        submitted_by_display_name: str | None,
        lead_client_name: str | None,
    ) -> Optional[Dict[str, Any]]:
        if not self._check_tables_exist():
            return None
        try:
            row = {
                "lead_id": lead_id,
                "reference_id": reference_id,
                "status": "pending",
                "proof_image_url": proof_image_url,
                "proof_file_id": proof_file_id,
                "submitted_by_telegram_id": submitted_by_telegram_id,
                "submitted_by_username": submitted_by_username,
                "submitted_by_display_name": submitted_by_display_name,
                "lead_client_name": lead_client_name,
            }
            r = self.client.table("lead_appeals").insert(row).execute()
            if not r.data:
                return None
            self.update_lead(lead_id, {"appeal_status": "pending"})
            return r.data[0]
        except Exception as e:
            logger.error("create_lead_appeal: %s", e)
            return None

    def resolve_lead_appeal(
        self,
        appeal_id: str,
        *,
        status: str,
        reviewed_by_telegram_id: int,
        reviewed_by_display_name: str | None,
    ) -> Optional[Dict[str, Any]]:
        """Accept or decline appeal. On accept, marks lead exclude_from_count."""
        if status not in ("accepted", "declined"):
            return None
        if not self._check_tables_exist():
            return None
        try:
            from datetime import datetime
            import pytz
            now = datetime.now(pytz.UTC).isoformat()
            appeal = self.get_appeal_by_id(appeal_id)
            if not appeal or (appeal.get("status") or "") != "pending":
                return None
            r = (
                self.client.table("lead_appeals")
                .update({
                    "status": status,
                    "reviewed_by_telegram_id": reviewed_by_telegram_id,
                    "reviewed_by_display_name": reviewed_by_display_name,
                    "reviewed_at": now,
                    "updated_at": now,
                })
                .eq("id", appeal_id)
                .eq("status", "pending")
                .execute()
            )
            if not r.data:
                return None
            lead_id = appeal.get("lead_id")
            if lead_id:
                lead_updates: Dict[str, Any] = {"appeal_status": status}
                if status == "accepted":
                    lead_updates["exclude_from_count"] = True
                self.update_lead(str(lead_id), lead_updates)
            return r.data[0]
        except Exception as e:
            logger.error("resolve_lead_appeal: %s", e)
            return None

    def _lead_excluded_from_count(self, lead: dict) -> bool:
        return bool(lead.get("exclude_from_count"))
