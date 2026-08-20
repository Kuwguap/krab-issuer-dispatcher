"""Simple Flask admin dashboard for managing groups, drivers, and supervisory IDs."""
from flask import Flask, render_template_string, request, jsonify, redirect, url_for
from flask_cors import CORS
import os
import re
import logging
from pathlib import Path
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load .env file explicitly (admin dashboard doesn't need Telegram bot token)
env_path = Path(__file__).parent / '.env'
load_dotenv(dotenv_path=env_path)

# Import Supabase directly - admin dashboard only needs Supabase, NOT Telegram
from supabase import create_client, Client

app = Flask(__name__)
# CORS: allow frontend from any origin (Vercel, localhost) so preflight and responses always have headers
CORS(app, resources={r"/api/*": {"origins": "*", "allow_headers": ["Content-Type", "Authorization"], "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"]}})


# Space-separated origins allowed to embed this Flask admin in an iframe (unified Krab Dispatch admin).
# Example: https://krabdispatch.vercel.app http://localhost:5500
ADMIN_FRAME_ANCESTORS = (os.getenv("ADMIN_FRAME_ANCESTORS") or "").strip()


@app.after_request
def add_cors_headers(response):
    """Ensure every response (including errors) has CORS headers so browser never blocks."""
    if request.path.startswith("/api/"):
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        response.headers["Access-Control-Max-Age"] = "86400"
    if (
        ADMIN_FRAME_ANCESTORS
        and response.status_code < 400
        and request.path == "/"
        and response.content_type
        and "html" in response.content_type.lower()
    ):
        origins = " ".join(ADMIN_FRAME_ANCESTORS.split())
        response.headers["Content-Security-Policy"] = f"frame-ancestors 'self' {origins}"
    return response


@app.route("/api/health", methods=["GET"])
def api_health():
    """Health check for Render / monitoring.

    Reports the deployed commit so "is my fix actually live?" is answerable without
    sending a message: every service in render.yaml deploys from the same push, so
    this commit is the one the bot worker is running too. Several rounds of bot fixes
    were debugged blind because nothing exposed the running build.
    """
    from config import Config

    return jsonify({
        "ok": True,
        "service": "krab-issuer-admin",
        "commit": (os.environ.get("RENDER_GIT_COMMIT") or "local")[:7],
        "lead_ingest_configured": Config.is_lead_ingest_configured(),
    })


@app.route("/api/<path:path>", methods=["OPTIONS"])
def api_options(path):
    """Handle CORS preflight for all /api/* so browser always gets CORS headers."""
    return "", 204


@app.errorhandler(Exception)
def handle_exception(e):
    """Catch unhandled exceptions and return JSON (so frontend always gets error message)."""
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    return jsonify({"success": False, "error": str(e)}), 500

# Create Supabase client directly (bypass Config to avoid Telegram dependencies)
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set in environment variables")

# Create a minimal database wrapper for admin dashboard
class AdminDatabase:
    """Minimal database wrapper for admin dashboard (no Telegram dependencies)."""
    def __init__(self):
        self.client: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
        self._tables_checked = False
        self._tables_exist = False
    
    def _check_tables_exist(self) -> bool:
        """Check if required tables exist."""
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
        """Get all groups."""
        if not self._check_tables_exist():
            return []
        try:
            response = self.client.table("groups").select("*").order("group_name").execute()
            return response.data or []
        except Exception:
            return []
    
    def get_all_drivers(self) -> list:
        """Get all drivers."""
        if not self._check_tables_exist():
            return []
        try:
            response = self.client.table("drivers").select("*").order("driver_name").execute()
            return response.data or []
        except Exception:
            return []
    
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
        except Exception:
            return False
    
    def create_driver(self, driver_name: str, driver_telegram_id: str, phone_number: str = None) -> bool:
        """Create a new driver. Raises on failure (table missing or insert error)."""
        if not self._check_tables_exist():
            raise ValueError("Database tables not found. Run the schema migrations in Supabase SQL Editor.")
        payload = {
            "driver_name": driver_name,
            "driver_telegram_id": str(driver_telegram_id).strip(),
        }
        if phone_number is not None and str(phone_number).strip():
            payload["phone_number"] = str(phone_number).strip()
        self.client.table("drivers").insert(payload).execute()
        return True
    
    def get_group_by_id(self, group_id: str):
        """Get a group by ID."""
        if not self._check_tables_exist():
            return None
        try:
            response = self.client.table("groups").select("*").eq("id", group_id).execute()
            return response.data[0] if response.data else None
        except Exception:
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
        except Exception:
            return False
    
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
        except Exception:
            return False

    # Group assistants (Telegram IDs that send leads into this group)
    def get_group_assistants(self, group_id: str) -> list:
        """Return list of telegram_id strings for assistants assigned to this group."""
        if not self._check_tables_exist():
            return []
        try:
            r = self.client.table("group_assistants").select("telegram_id").eq("group_id", group_id).execute()
            return [x["telegram_id"] for x in (r.data or [])]
        except Exception:
            return []

    def add_group_assistant(self, group_id: str, telegram_id: str) -> bool:
        """Assign an assistant (Telegram user ID) to a group."""
        if not self._check_tables_exist():
            return False
        try:
            self.client.table("group_assistants").insert({"group_id": group_id, "telegram_id": str(telegram_id).strip()}).execute()
            return True
        except Exception:
            return False

    def remove_group_assistant(self, group_id: str, telegram_id: str) -> bool:
        """Remove an assistant from a group."""
        if not self._check_tables_exist():
            return False
        try:
            self.client.table("group_assistants").delete().eq("group_id", group_id).eq("telegram_id", str(telegram_id).strip()).execute()
            return True
        except Exception:
            return False

    # Settings
    def get_setting(self, key: str) -> str:
        """Get setting value; returns '' if missing or table missing."""
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
        """Set setting (upsert)."""
        if not self._check_tables_exist():
            return False
        try:
            self.client.table("settings").upsert({"key": key, "value": str(value)}, on_conflict="key").execute()
            return True
        except Exception:
            return False

    # Contact info sources (for "Select the Contact info source for this client")
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

    # Driver–group assignments (group_drivers)
    def assign_driver_to_group(self, group_id: str, driver_id: str) -> bool:
        if not self._check_tables_exist():
            return False
        try:
            self.client.table("group_drivers").insert({"group_id": group_id, "driver_id": driver_id}).execute()
            return True
        except Exception:
            return False

    def get_all_assignments(self) -> list:
        """List all group–driver assignments: { id, group_id, driver_id, group_name, driver_name }."""
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

    # Lead stats: total leads, per-driver accepted and receipts
    def get_lead_stats(self) -> dict:
        """Return { total_leads: int, drivers: [ { driver_id, driver_name, leads_accepted, receipts_submitted } ] }."""
        out = {"total_leads": 0, "drivers": []}
        if not self._check_tables_exist():
            return out
        try:
            r = self.client.table("leads").select("id, exclude_from_count").execute()
            out["total_leads"] = len([
                x for x in (r.data or []) if not x.get("exclude_from_count")
            ])
        except Exception:
            pass
        try:
            drivers = self.client.table("drivers").select("id, driver_name").execute()
            assignments = self.client.table("lead_assignments").select("driver_id, lead_id").eq("status", "accepted").execute()
            lead_ids_with_receipt = set()
            try:
                leads = self.client.table("leads").select("id, receipt_image_url, exclude_from_count").execute()
                lead_ids_with_receipt = {
                    l["id"] for l in (leads.data or [])
                    if l.get("receipt_image_url") and not l.get("exclude_from_count")
                }
                excluded_lead_ids = {
                    l["id"] for l in (leads.data or []) if l.get("exclude_from_count")
                }
            except Exception:
                pass
            by_driver = {}
            for a in (assignments.data or []):
                did = a.get("driver_id")
                lid = a.get("lead_id")
                if lid and lid in excluded_lead_ids:
                    continue
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

    # Receipt debts (pending receipts) for admin penalty tooling
    def get_receipt_debts_summary(self, refs_per_driver: int = 5) -> dict:
        """
        Return summary for admin UI:
        {
          "drivers": [
            {
              "driver_id": "...",
              "driver_name": "...",
              "is_active": true/false,
              "owed_receipts": <int>,
              "pending_references": [
                { "assignment_id": "...", "reference_id": "...", "accepted_at": "..." }
              ]
            }
          ]
        }

        "Owed receipts" are accepted assignments where the joined lead has no receipt_image_url.
        Admin API: receipt_debts_summary (v1).
        """
        if not self._check_tables_exist():
            return {"drivers": []}

        try:
            drivers_resp = self.client.table("drivers").select("id, driver_name, is_active").order("driver_name").execute()
            drivers = drivers_resp.data or []

            # Pull all accepted assignments with receipt fields from the joined lead.
            # We'll filter to "receipt missing" in Python to keep the logic consistent with bot.py.
            assignments_resp = self.client.table("lead_assignments").select(
                "id, driver_id, accepted_at, lead:leads(reference_id, receipt_image_url)"
            ).eq("status", "accepted").execute()

            by_driver = {d.get("id"): {
                "driver_id": d.get("id"),
                "driver_name": d.get("driver_name", "N/A"),
                "is_active": d.get("is_active", True),
                "owed_receipts": 0,
                "pending_references": [],
            } for d in drivers}

            for row in (assignments_resp.data or []):
                driver_id = row.get("driver_id")
                if not driver_id or driver_id not in by_driver:
                    continue

                lead = row.get("lead") or {}
                # receipt_image_url empty or null => missing receipt
                if lead.get("receipt_image_url"):
                    continue

                by_driver[driver_id]["owed_receipts"] += 1

                # Keep only the first N refs for the "portion" shown in the table.
                if len(by_driver[driver_id]["pending_references"]) < (refs_per_driver or 0):
                    by_driver[driver_id]["pending_references"].append({
                        "assignment_id": row.get("id"),
                        "reference_id": lead.get("reference_id") or "N/A",
                        "accepted_at": row.get("accepted_at"),
                    })

            # Keep stable driver ordering (from drivers table).
            out_drivers = [by_driver.get(d.get("id")) for d in drivers if by_driver.get(d.get("id"))]
            return {"drivers": out_drivers}
        except Exception:
            # Don't break the whole admin page if Supabase is temporarily failing.
            return {"drivers": []}

    def get_driver_pending_receipts(self, driver_id: str) -> list:
        """
        Return full pending receipt items for a driver.
        Each item includes lead details so admin can click a reference and inspect it.
        """
        if not self._check_tables_exist():
            return []
        try:
            assignments_resp = self.client.table("lead_assignments").select(
                "id, driver_id, accepted_at, lead:leads("
                "id, reference_id, receipt_image_url, vehicle_details, delivery_details, extra_info, special_request_note, special_request_issuers, special_request_drivers, monday_status"
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
        """
        Delete a single pending receipt item by deleting its lead_assignments row.
        Only allowed when the joined lead has no receipt_image_url.
        """
        if not self._check_tables_exist():
            return False
        try:
            r = self.client.table("lead_assignments").select("id, lead:leads(receipt_image_url)").eq("id", assignment_id).limit(1).execute()
            if not r.data:
                return False

            lead = (r.data[0].get("lead") or {})
            if lead.get("receipt_image_url"):
                # Receipt already submitted; don't delete.
                return False

            self.client.table("lead_assignments").delete().eq("id", assignment_id).execute()
            return True
        except Exception:
            return False

    def delete_pending_receipts_for_driver(self, driver_id: str) -> int:
        """
        Clear all pending receipts (delete accepted assignments without receipts) for a driver.
        Returns number of deleted lead_assignments rows.
        """
        if not self._check_tables_exist():
            return 0
        try:
            r = self.client.table("lead_assignments").select("id, lead:leads(receipt_image_url)").eq("status", "accepted").eq(
                "driver_id", driver_id
            ).execute()

            pending_ids = []
            for row in (r.data or []):
                lead = row.get("lead") or {}
                if not lead.get("receipt_image_url"):
                    pending_ids.append(row.get("id"))

            deleted = 0
            for aid in pending_ids:
                if not aid:
                    continue
                ok = self.client.table("lead_assignments").delete().eq("id", aid).execute()
                # Supabase returns data depending on config; we just count attempted deletes that succeeded without exception.
                deleted += 1
            return deleted
        except Exception:
            return 0

    def get_submitted_receipts_recent(self, limit: int = 100) -> list:
        """Leads with a stored receipt image + accepting driver name (for admin review)."""
        if not self._check_tables_exist():
            return []
        cap = max(1, min(int(limit or 100), 500))
        try:
            r = self.client.table("leads").select(
                "id, reference_id, receipt_image_url, updated_at, group_id, appeal_status, exclude_from_count"
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
                    "appeal_status": lead.get("appeal_status"),
                    "exclude_from_count": bool(lead.get("exclude_from_count")),
                })
                if len(out) >= cap:
                    break
            return out
        except Exception:
            return []

db = AdminDatabase()

# Simple HTML template for the dashboard
DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
    <title>Krab Issuer Admin</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
            -webkit-text-size-adjust: 100%;
        }
        .container {
            max-width: 1200px;
            margin: 0 auto;
            background: white;
            border-radius: 12px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            padding: 30px;
        }
        h1 {
            color: #333;
            margin-bottom: 30px;
            text-align: center;
            font-size: clamp(1.25rem, 4vw, 1.75rem);
        }
        .section {
            margin-bottom: 40px;
            padding: 20px;
            background: #f8f9fa;
            border-radius: 8px;
        }
        .section h2 {
            color: #667eea;
            margin-bottom: 20px;
            border-bottom: 2px solid #667eea;
            padding-bottom: 10px;
            font-size: clamp(1rem, 3vw, 1.25rem);
        }
        .form-group {
            margin-bottom: 15px;
        }
        label {
            display: block;
            margin-bottom: 5px;
            color: #555;
            font-weight: 500;
        }
        input, select {
            width: 100%;
            padding: 12px 10px;
            border: 2px solid #ddd;
            border-radius: 6px;
            font-size: 16px;
            transition: border-color 0.3s;
        }
        input:focus, select:focus {
            outline: none;
            border-color: #667eea;
        }
        button {
            background: #667eea;
            color: white;
            padding: 12px 24px;
            border: none;
            border-radius: 6px;
            cursor: pointer;
            font-size: 14px;
            font-weight: 500;
            transition: background 0.3s;
            min-height: 44px;
        }
        button:hover {
            background: #5568d3;
        }
        .btn-danger {
            background: #dc3545;
        }
        .btn-danger:hover {
            background: #c82333;
        }
        .table-wrapper {
            overflow-x: auto;
            -webkit-overflow-scrolling: touch;
            margin-top: 15px;
        }
        .table-wrapper table {
            min-width: 320px;
        }
        table {
            width: 100%;
            border-collapse: collapse;
        }
        th, td {
            padding: 12px 8px;
            text-align: left;
            border-bottom: 1px solid #ddd;
            font-size: clamp(0.8125rem, 2.5vw, 0.9375rem);
        }
        th {
            background: #667eea;
            color: white;
            font-weight: 600;
        }
        tr:hover {
            background: #f5f5f5;
        }
        .status-active {
            color: #28a745;
            font-weight: 600;
        }
        .status-inactive {
            color: #dc3545;
            font-weight: 600;
        }
        .message {
            padding: 12px;
            border-radius: 6px;
            margin-bottom: 20px;
        }
        .message-success {
            background: #d4edda;
            color: #155724;
            border: 1px solid #c3e6cb;
        }
        .message-error {
            background: #f8d7da;
            color: #721c24;
            border: 1px solid #f5c6cb;
        }
        .assistant-form-row {
            display: flex;
            gap: 8px;
            align-items: center;
            flex-wrap: wrap;
        }
        .assistant-form-row input {
            flex: 1;
            min-width: 0;
        }
        .assistant-form-row button {
            flex-shrink: 0;
        }
        @media (max-width: 768px) {
            body { padding: 10px; }
            .container { padding: 16px; border-radius: 8px; }
            .section { padding: 14px; margin-bottom: 24px; }
            .section h2 { margin-bottom: 14px; }
            th, td { padding: 10px 6px; }
            button { width: 100%; min-height: 48px; }
            .assistant-form-row { flex-direction: column; align-items: stretch; }
            .assistant-form-row button { width: 100%; }
        }
        @media (max-width: 480px) {
            h1 { margin-bottom: 20px; }
            .section h2 { font-size: 1rem; }
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>🚀 Krab Issuer Admin</h1>
        
        {% if message %}
        <div class="message message-{{ message_type }}">{{ message }}</div>
        {% endif %}
        
        <!-- Settings: Allow assistants to choose group -->
        <div class="section">
            <h2>⚙️ Lead flow</h2>
            <p style="margin-bottom: 10px; color: #555;">When <strong>Allow assistants to choose group</strong> is ON, anyone can send leads and will choose a group (and then a driver). When OFF, assistants use their assigned group.</p>
            <p style="margin-bottom: 12px;"><strong>Current:</strong> {{ 'Allow assistants to choose group' if assistants_choose_group else 'Use assigned groups only' }}</p>
            <form method="POST" action="/set_assistants_choose_group" style="display: inline;">
                <input type="hidden" name="value" value="{{ '0' if assistants_choose_group else '1' }}">
                <button type="submit" style="padding: 8px 16px;">{{ 'Use assigned groups only' if assistants_choose_group else 'Allow assistants to choose group' }}</button>
            </form>
            <div style="margin-top: 18px; padding-top: 14px; border-top: 1px solid #dee2e6;">
                <h3 style="margin: 0 0 8px 0; font-size: 1rem;">Driver receipt detection (AI)</h3>
                <p style="margin-bottom: 10px; color: #555; font-size: 0.95rem;"><strong>Strict</strong>: receipt image must show a visible <strong>$</strong> (no amount vs lead check). <strong>Lax</strong>: also require paid total to match the lead price when set.</p>
                <p style="margin-bottom: 10px;"><strong>Current:</strong> {{ 'Strict ($ on receipt)' if receipt_detection_mode == 'strict' else 'Lax (match amount to lead)' }}</p>
                <form method="POST" action="/set_receipt_detection_mode" style="display: inline; margin-right: 8px;">
                    <input type="hidden" name="mode" value="strict">
                    <button type="submit" style="padding: 8px 16px;">Use strict ($ only)</button>
                </form>
                <form method="POST" action="/set_receipt_detection_mode" style="display: inline;">
                    <input type="hidden" name="mode" value="lax">
                    <button type="submit" style="padding: 8px 16px;">Use lax (match amount)</button>
                </form>
            </div>
        </div>
        
        <!-- ST Telegram ID: notify on every successful lead send -->
        <div class="section">
            <h2>📬 ST Telegram ID</h2>
            <p style="margin-bottom: 10px; color: #555;">When a lead is successfully sent, the bot notifies this Telegram chat (user or group ID). Leave empty to disable.</p>
            <form method="POST" action="/set_st_telegram_id">
                <div class="form-group">
                    <label>ST Telegram ID:</label>
                    <input type="text" name="st_telegram_id" value="{{ st_telegram_id or '' }}" placeholder="e.g., 123456789 or -1001234567890">
                </div>
                <button type="submit">Save</button>
            </form>
        </div>
        
        <!-- Bot usage: who used the bot and who they sent to -->
        <div class="section">
            <h2>📱 Bot usage</h2>
            <p style="margin-bottom: 10px; color: #555;">Who used the bot and which driver(s) / group they sent to (from the bot, not server logs).</p>
            <div class="table-wrapper">
            <table>
                <thead>
                    <tr>
                        <th>When</th>
                        <th>User</th>
                        <th>Group</th>
                        <th>Sent to driver(s)</th>
                    </tr>
                </thead>
                <tbody>
                    {% for u in (bot_usage or []) %}
                    <tr>
                        <td>{{ u.created_at[:19] if u.created_at else 'N/A' }}</td>
                        <td>@{{ u.telegram_username or u.user_telegram_id }}</td>
                        <td>{{ u.group_name or 'N/A' }}</td>
                        <td>{{ u.driver_names or 'N/A' }}</td>
                    </tr>
                    {% endfor %}
                    {% if not (bot_usage or []) %}
                    <tr><td colspan="4" style="text-align: center; color: #888;">No usage yet</td></tr>
                    {% endif %}
                </tbody>
            </table>
            </div>
        </div>
        
        <!-- Lead stats -->
        <div class="section">
            <h2>📊 Lead stats</h2>
            <p style="margin-bottom: 12px;"><strong>Total leads sent:</strong> {{ lead_stats.get('total_leads', 0) }}</p>
            <div class="table-wrapper">
            <table>
                <thead>
                    <tr>
                        <th>Driver</th>
                        <th>Leads accepted</th>
                        <th>Receipts submitted</th>
                    </tr>
                </thead>
                <tbody>
                    {% for d in (lead_stats.get('drivers') or []) %}
                    <tr>
                        <td>{{ d.driver_name }}</td>
                        <td>{{ d.leads_accepted }}</td>
                        <td>{{ d.receipts_submitted }}</td>
                    </tr>
                    {% endfor %}
                    {% if not (lead_stats.get('drivers') or []) %}
                    <tr><td colspan="3" style="text-align: center; color: #888;">No drivers</td></tr>
                    {% endif %}
                </tbody>
            </table>
            </div>
        </div>
        
        <!-- Add Group Section -->
        <div class="section">
            <h2>➕ Add New Group</h2>
            <form method="POST" action="/add_group">
                <div class="form-group">
                    <label>Group Name:</label>
                    <input type="text" name="group_name" required placeholder="e.g., Group A">
                </div>
                <div class="form-group">
                    <label>Group Telegram ID:</label>
                    <input type="text" name="group_telegram_id" required placeholder="e.g., -1001234567890">
                </div>
                <div class="form-group">
                    <label>Supervisory Telegram ID:</label>
                    <input type="text" name="supervisory_telegram_id" required placeholder="e.g., 123456789">
                </div>
                <button type="submit">Add Group</button>
            </form>
        </div>
        
        <!-- Contact info sources: "Select the Contact info source for this client" -->
        <div class="section">
            <h2>📋 Contact info sources</h2>
            <p style="margin-bottom: 10px; color: #555;">Options shown in the bot after sending a lead to drivers. Users pick one per lead.</p>
            <form method="POST" action="/add_contact_source" style="margin-bottom: 16px;">
                <div class="form-group">
                    <label>New option label:</label>
                    <input type="text" name="label" required placeholder="e.g., Facebook, Referral, Website">
                </div>
                <button type="submit">Add</button>
            </form>
            <div class="table-wrapper">
            <table>
                <thead>
                    <tr>
                        <th>Label</th>
                        <th>Status</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody>
                    {% for s in (contact_sources or []) %}
                    <tr>
                        <td>{{ s.label }}</td>
                        <td>
                            <span class="status-{{ 'active' if s.is_active else 'inactive' }}">
                                {{ 'Active' if s.is_active else 'Inactive' }}
                            </span>
                        </td>
                        <td>
                            <a href="/toggle_contact_source/{{ s.id }}">
                                <button class="btn-danger" style="padding: 6px 12px; font-size: 12px;">
                                    {{ 'Deactivate' if s.is_active else 'Activate' }}
                                </button>
                            </a>
                        </td>
                    </tr>
                    {% endfor %}
                    {% if not (contact_sources or []) %}
                    <tr><td colspan="3" style="text-align: center; color: #888;">No sources yet. Add one above.</td></tr>
                    {% endif %}
                </tbody>
            </table>
            </div>
        </div>
        
        <!-- Add Driver Section -->
        <div class="section">
            <h2>👤 Add New Driver</h2>
            <form method="POST" action="/add_driver">
                <div class="form-group">
                    <label>Driver Name:</label>
                    <input type="text" name="driver_name" required placeholder="e.g., John Doe">
                </div>
                <div class="form-group">
                    <label>Driver Telegram ID:</label>
                    <input type="text" name="driver_telegram_id" required placeholder="e.g., 123456789">
                </div>
                <div class="form-group">
                    <label>Phone Number (optional):</label>
                    <input type="text" name="phone_number" placeholder="e.g., +1234567890">
                </div>
                <button type="submit">Add Driver</button>
            </form>
        </div>
        
        <!-- Groups List -->
        <div class="section">
            <h2>📋 Groups</h2>
            <div class="table-wrapper">
            <table>
                <thead>
                    <tr>
                        <th>Name</th>
                        <th>Group ID</th>
                        <th>Supervisory ID</th>
                        <th>Status</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody>
                    {% for group in groups %}
                    <tr>
                        <td>{{ group.group_name }}</td>
                        <td><code>{{ group.group_telegram_id }}</code></td>
                        <td><code>{{ group.supervisory_telegram_id }}</code></td>
                        <td>
                            <span class="status-{{ 'active' if group.is_active else 'inactive' }}">
                                {{ 'Active' if group.is_active else 'Inactive' }}
                            </span>
                        </td>
                        <td>
                            <a href="/toggle_group/{{ group.id }}">
                                <button class="btn-danger" style="padding: 6px 12px; font-size: 12px;">
                                    {{ 'Deactivate' if group.is_active else 'Activate' }}
                                </button>
                            </a>
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            </div>
        </div>
        
        <!-- Drivers List -->
        <div class="section">
            <h2>🚗 Drivers</h2>
            <div class="table-wrapper">
            <table>
                <thead>
                    <tr>
                        <th>Name</th>
                        <th>Telegram ID</th>
                        <th>Phone</th>
                        <th>Status</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody>
                    {% for driver in drivers %}
                    <tr>
                        <td>{{ driver.driver_name }}</td>
                        <td><code>{{ driver.driver_telegram_id }}</code></td>
                        <td>{{ driver.phone_number or 'N/A' }}</td>
                        <td>
                            <span class="status-{{ 'active' if driver.is_active else 'inactive' }}">
                                {{ 'Active' if driver.is_active else 'Inactive' }}
                            </span>
                        </td>
                        <td>
                            <a href="/toggle_driver/{{ driver.id }}">
                                <button class="btn-danger" style="padding: 6px 12px; font-size: 12px;">
                                    {{ 'Deactivate' if driver.is_active else 'Activate' }}
                                </button>
                            </a>
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            </div>
        </div>
        
        <!-- Group Assistants: Telegram IDs that send leads into each group -->
        <div class="section">
            <h2>👥 Group Assistants</h2>
            <p style="margin-bottom: 15px; color: #555;">Assistants use the bot like normal; their leads go to the group they are assigned to.</p>
            {% for group in groups %}
            <div style="margin-bottom: 24px; padding: 12px; background: #fff; border-radius: 6px; border: 1px solid #ddd;">
                <strong>{{ group.group_name }}</strong> — Assistants (Telegram IDs):
                <ul style="margin: 8px 0; padding-left: 20px;">
                    {% for tid in (groups_assistants.get(group.id) or []) %}
                    <li><code>{{ tid }}</code> <a href="/remove_group_assistant/{{ group.id }}/{{ tid }}" style="margin-left: 8px; color: #dc3545; font-size: 12px;">Remove</a></li>
                    {% endfor %}
                    {% if not (groups_assistants.get(group.id) or []) %}
                    <li style="color: #888;">None yet</li>
                    {% endif %}
                </ul>
                <form method="POST" action="/add_group_assistant" class="assistant-form-row">
                    <input type="hidden" name="group_id" value="{{ group.id }}">
                    <input type="text" name="telegram_id" placeholder="Assistant Telegram ID (e.g. 123456789)">
                    <button type="submit" style="padding: 8px 16px;">Add Assistant</button>
                </form>
            </div>
            {% endfor %}
        </div>
        
    </div>
</body>
</html>
"""


@app.route('/')
def dashboard():
    """Main dashboard page."""
    try:
        groups = db.get_all_groups()
        drivers = db.get_all_drivers()
        groups_assistants = {}
        for g in (groups or []):
            groups_assistants[g['id']] = db.get_group_assistants(g['id'])
        assistants_choose_group = (db.get_setting("assistants_choose_group") or "").lower() in ("true", "1", "yes")
        st_telegram_id = (db.get_setting("st_telegram_id") or "").strip()
        receipt_detection_mode = (db.get_setting("receipt_detection_mode") or "lax").strip().lower()
        if receipt_detection_mode not in ("strict", "lax"):
            receipt_detection_mode = "lax"
        contact_sources = db.get_contact_info_sources()
        bot_usage = db.get_bot_usage()
        lead_stats = db.get_lead_stats()
        return render_template_string(
            DASHBOARD_HTML,
            groups=groups or [],
            drivers=drivers or [],
            groups_assistants=groups_assistants,
            assistants_choose_group=assistants_choose_group,
            st_telegram_id=st_telegram_id,
            receipt_detection_mode=receipt_detection_mode,
            contact_sources=contact_sources or [],
            bot_usage=bot_usage or [],
            lead_stats=lead_stats,
            assignments=[],
            message=request.args.get('message'),
            message_type=request.args.get('type', 'success')
        )
    except Exception as e:
        return f"Error loading dashboard: {str(e)}", 500


@app.route('/add_group', methods=['POST'])
def add_group():
    """Add a new group."""
    try:
        group_name = request.form.get('group_name')
        group_telegram_id = request.form.get('group_telegram_id')
        supervisory_telegram_id = request.form.get('supervisory_telegram_id')
        
        if db.create_group(group_name, group_telegram_id, supervisory_telegram_id):
            return redirect(url_for('dashboard', message='Group added successfully!', type='success'))
        else:
            return redirect(url_for('dashboard', message='Error adding group', type='error'))
    except Exception as e:
        return redirect(url_for('dashboard', message=f'Error: {str(e)}', type='error'))


@app.route('/add_driver', methods=['POST'])
def add_driver():
    """Add a new driver."""
    try:
        driver_name = request.form.get('driver_name')
        driver_telegram_id = request.form.get('driver_telegram_id')
        phone_number = request.form.get('phone_number') or None
        
        if db.create_driver(driver_name, driver_telegram_id, phone_number):
            return redirect(url_for('dashboard', message='Driver added successfully!', type='success'))
        else:
            return redirect(url_for('dashboard', message='Error adding driver', type='error'))
    except Exception as e:
        return redirect(url_for('dashboard', message=f'Error: {str(e)}', type='error'))


@app.route('/toggle_group/<group_id>')
def toggle_group(group_id):
    """Toggle group active status."""
    try:
        if db.toggle_group_status(group_id):
            return redirect(url_for('dashboard', message='Group status updated!', type='success'))
        else:
            return redirect(url_for('dashboard', message='Error updating group', type='error'))
    except Exception as e:
        return redirect(url_for('dashboard', message=f'Error: {str(e)}', type='error'))


@app.route('/toggle_driver/<driver_id>')
def toggle_driver(driver_id):
    """Toggle driver active status."""
    try:
        if db.toggle_driver_status(driver_id):
            return redirect(url_for('dashboard', message='Driver status updated!', type='success'))
        else:
            return redirect(url_for('dashboard', message='Error updating driver', type='error'))
    except Exception as e:
        return redirect(url_for('dashboard', message=f'Error: {str(e)}', type='error'))


@app.route('/set_assistants_choose_group', methods=['POST'])
def set_assistants_choose_group():
    """Toggle setting: value=1 means allow assistants to choose group, 0 means use assigned only."""
    try:
        val = (request.form.get("value") or "0").strip()
        db.set_setting("assistants_choose_group", "true" if val == "1" else "false")
        return redirect(url_for('dashboard', message='Setting updated!', type='success'))
    except Exception as e:
        return redirect(url_for('dashboard', message=f'Error: {str(e)}', type='error'))


@app.route('/set_st_telegram_id', methods=['POST'])
def set_st_telegram_id():
    """Set ST Telegram ID (notified when a lead is successfully sent)."""
    try:
        st_id = (request.form.get("st_telegram_id") or "").strip()
        db.set_setting("st_telegram_id", st_id)
        return redirect(url_for('dashboard', message='ST Telegram ID saved!', type='success'))
    except Exception as e:
        return redirect(url_for('dashboard', message=f'Error: {str(e)}', type='error'))


@app.route('/set_receipt_detection_mode', methods=['POST'])
def set_receipt_detection_mode():
    """strict = visible $ on receipt; lax = match amount to lead when price set."""
    try:
        mode = (request.form.get("mode") or "lax").strip().lower()
        if mode not in ("strict", "lax"):
            mode = "lax"
        db.set_setting("receipt_detection_mode", mode)
        return redirect(url_for('dashboard', message='Receipt detection mode updated!', type='success'))
    except Exception as e:
        return redirect(url_for('dashboard', message=f'Error: {str(e)}', type='error'))


@app.route('/add_contact_source', methods=['POST'])
def add_contact_source():
    """Add a contact info source option."""
    try:
        label = (request.form.get("label") or "").strip()
        if not label:
            return redirect(url_for('dashboard', message='Label required', type='error'))
        if db.create_contact_info_source(label):
            return redirect(url_for('dashboard', message='Contact source added!', type='success'))
        return redirect(url_for('dashboard', message='Error adding contact source', type='error'))
    except Exception as e:
        return redirect(url_for('dashboard', message=f'Error: {str(e)}', type='error'))


@app.route('/toggle_contact_source/<source_id>')
def toggle_contact_source(source_id):
    """Toggle contact info source active status."""
    try:
        if db.toggle_contact_source_status(source_id):
            return redirect(url_for('dashboard', message='Contact source status updated!', type='success'))
        return redirect(url_for('dashboard', message='Error updating contact source', type='error'))
    except Exception as e:
        return redirect(url_for('dashboard', message=f'Error: {str(e)}', type='error'))


@app.route('/add_group_assistant', methods=['POST'])
def add_group_assistant():
    """Add an assistant (Telegram ID) to a group (legacy form)."""
    try:
        group_id = request.form.get('group_id')
        telegram_id = (request.form.get('telegram_id') or '').strip()
        if not group_id or not telegram_id:
            return redirect(url_for('dashboard', message='Missing group or Telegram ID', type='error'))
        if db.add_group_assistant(group_id, telegram_id):
            return redirect(url_for('dashboard', message='Assistant added!', type='success'))
        return redirect(url_for('dashboard', message='Error adding assistant', type='error'))
    except Exception as e:
        return redirect(url_for('dashboard', message=f'Error: {str(e)}', type='error'))


@app.route('/remove_group_assistant/<group_id>/<telegram_id>')
def remove_group_assistant(group_id, telegram_id):
    """Remove an assistant from a group (legacy link)."""
    try:
        if db.remove_group_assistant(group_id, telegram_id):
            return redirect(url_for('dashboard', message='Assistant removed!', type='success'))
        return redirect(url_for('dashboard', message='Error removing assistant', type='error'))
    except Exception as e:
        return redirect(url_for('dashboard', message=f'Error: {str(e)}', type='error'))


# --- JSON API for Vercel frontend (no iframe) ---

def _get_json_or_form():
    """Get request data from JSON body or form (for API and legacy)."""
    if request.is_json:
        return request.get_json(silent=True) or {}
    return request.form


@app.route('/api/groups', methods=['GET', 'POST'])
def api_groups():
    if request.method == 'GET':
        try:
            groups = db.get_all_groups()
            return jsonify(groups or [])
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    # POST: create group
    try:
        data = _get_json_or_form()
        group_name = data.get('group_name') or request.form.get('group_name')
        group_telegram_id = data.get('group_telegram_id') or request.form.get('group_telegram_id')
        supervisory_telegram_id = data.get('supervisory_telegram_id') or request.form.get('supervisory_telegram_id')
        if not all([group_name, group_telegram_id, supervisory_telegram_id]):
            return jsonify({"success": False, "error": "Missing group_name, group_telegram_id, or supervisory_telegram_id"}), 400
        if db.create_group(group_name, group_telegram_id, supervisory_telegram_id):
            return jsonify({"success": True, "message": "Group added successfully!"})
        return jsonify({"success": False, "error": "Error adding group"}), 500
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/drivers', methods=['GET', 'POST'])
def api_drivers():
    if request.method == 'GET':
        try:
            drivers = db.get_all_drivers()
            return jsonify(drivers or [])
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    # POST: create driver
    try:
        data = _get_json_or_form()
        driver_name = (data.get('driver_name') or request.form.get('driver_name') or '').strip()
        driver_telegram_id = (data.get('driver_telegram_id') or request.form.get('driver_telegram_id') or '').strip()
        phone_number = data.get('phone_number') or request.form.get('phone_number')
        if phone_number is not None:
            phone_number = str(phone_number).strip() or None
        if not driver_name or not driver_telegram_id:
            return jsonify({"success": False, "error": "Missing driver_name or driver_telegram_id"}), 400
        try:
            if db.create_driver(driver_name, driver_telegram_id, phone_number):
                return jsonify({"success": True, "message": "Driver added successfully!"})
        except Exception as db_err:
            err_msg = str(db_err).lower()
            logger.exception("POST /api/drivers create_driver failed")
            if any(x in err_msg for x in ("unique", "duplicate", "already exists", "23505", "unique constraint", "violates")):
                return jsonify({"success": False, "error": "A driver with this Telegram ID already exists."}), 409
            return jsonify({"success": False, "error": str(db_err)}), 500
    except Exception as e:
        logger.exception("POST /api/drivers error")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/groups/<group_id>/assistants', methods=['GET'])
def api_group_assistants(group_id):
    """List assistants (Telegram IDs) for a group."""
    try:
        ids = db.get_group_assistants(group_id)
        return jsonify(ids)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/groups/<group_id>/assistants', methods=['POST'])
def api_add_group_assistant(group_id):
    """Add an assistant (Telegram user ID) to a group. Body: {"telegram_id": "123456789"}."""
    try:
        data = _get_json_or_form()
        telegram_id = (data.get('telegram_id') or request.form.get('telegram_id') or '').strip()
        if not telegram_id:
            return jsonify({"success": False, "error": "Missing telegram_id"}), 400
        if db.add_group_assistant(group_id, telegram_id):
            return jsonify({"success": True, "message": "Assistant added!"})
        return jsonify({"success": False, "error": "Error adding assistant"}), 500
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/groups/<group_id>/assistants/<telegram_id>', methods=['DELETE'])
def api_remove_group_assistant(group_id, telegram_id):
    """Remove an assistant from a group."""
    try:
        if db.remove_group_assistant(group_id, telegram_id):
            return jsonify({"success": True, "message": "Assistant removed!"})
        return jsonify({"success": False, "error": "Error removing assistant"}), 500
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/settings', methods=['GET'])
def api_get_settings():
    """Get settings: assistants_choose_group, st_telegram_id, receipt_detection_mode."""
    try:
        val = db.get_setting("assistants_choose_group")
        st_id = (db.get_setting("st_telegram_id") or "").strip()
        rec_mode = (db.get_setting("receipt_detection_mode") or "lax").strip().lower()
        if rec_mode not in ("strict", "lax"):
            rec_mode = "lax"
        return jsonify({
            "assistants_choose_group": (val or "").lower() in ("true", "1", "yes"),
            "st_telegram_id": st_id,
            "receipt_detection_mode": rec_mode,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/settings', methods=['POST'])
def api_set_settings():
    """Update settings. Body: assistants_choose_group, st_telegram_id, receipt_detection_mode (strict|lax) — any key optional."""
    try:
        data = _get_json_or_form()
        if data.get("assistants_choose_group") is not None:
            v = data.get("assistants_choose_group")
            db.set_setting("assistants_choose_group", "true" if v in (True, "true", "1", "yes") else "false")
        if "st_telegram_id" in data:
            db.set_setting("st_telegram_id", str(data.get("st_telegram_id", "")).strip())
        if data.get("receipt_detection_mode") is not None:
            rm = str(data.get("receipt_detection_mode", "")).strip().lower()
            if rm not in ("strict", "lax"):
                rm = "lax"
            db.set_setting("receipt_detection_mode", rm)
        return jsonify({"success": True, "message": "Settings updated!"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/contact_sources', methods=['GET'])
def api_contact_sources_list():
    """List all contact info sources."""
    try:
        sources = db.get_contact_info_sources()
        return jsonify(sources or [])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/contact_sources', methods=['POST'])
def api_contact_sources_add():
    """Add a contact source. Body: { \"label\": \"Blue FB\", \"sort_order\": 0 }."""
    try:
        data = _get_json_or_form()
        label = (data.get("label") or "").strip()
        if not label:
            return jsonify({"success": False, "error": "Missing label"}), 400
        sort_order = int(data.get("sort_order", 0) or 0)
        if db.create_contact_info_source(label, sort_order):
            return jsonify({"success": True, "message": "Contact source added!"})
        return jsonify({"success": False, "error": "Error adding contact source"}), 500
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/contact_sources/<source_id>/toggle', methods=['POST'])
def api_contact_sources_toggle(source_id):
    """Toggle contact source active status."""
    try:
        if db.toggle_contact_source_status(source_id):
            return jsonify({"success": True, "message": "Contact source updated!"})
        return jsonify({"success": False, "error": "Error updating contact source"}), 500
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/assignments', methods=['GET'])
def api_assignments_list():
    """List all group–driver assignments."""
    try:
        assignments = db.get_all_assignments()
        return jsonify(assignments or [])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/assignments', methods=['POST'])
def api_assignments_add():
    """Assign a driver to a group. Body: { \"group_id\": \"uuid\", \"driver_id\": \"uuid\" }."""
    try:
        data = _get_json_or_form()
        group_id = (data.get("group_id") or "").strip()
        driver_id = (data.get("driver_id") or "").strip()
        if not group_id or not driver_id:
            return jsonify({"success": False, "error": "Missing group_id or driver_id"}), 400
        if db.assign_driver_to_group(group_id, driver_id):
            return jsonify({"success": True, "message": "Driver assigned to group!"})
        return jsonify({"success": False, "error": "Error assigning driver (maybe already assigned)"}), 500
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/assignments/<assignment_id>', methods=['DELETE'])
def api_assignments_remove(assignment_id):
    """Remove a driver from a group (by assignment id)."""
    try:
        if db.remove_driver_from_group(assignment_id):
            return jsonify({"success": True, "message": "Driver removed from group!"})
        return jsonify({"success": False, "error": "Error removing assignment"}), 500
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/stats', methods=['GET'])
def api_stats():
    """Get lead stats: total_leads, drivers with leads_accepted and receipts_submitted."""
    try:
        return jsonify(db.get_lead_stats())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/receipt_debts/summary', methods=['GET'])
def api_receipt_debts_summary():
    """Get per-driver count of owed receipts + a small portion of pending references."""
    try:
        return jsonify(db.get_receipt_debts_summary())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/receipt_debts/drivers/<driver_id>', methods=['GET'])
def api_receipt_debts_driver(driver_id):
    """Get the full list of pending receipt items for a given driver."""
    try:
        return jsonify(db.get_driver_pending_receipts(driver_id))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/receipt_debts/drivers/<driver_id>/pending', methods=['DELETE'])
def api_receipt_debts_clear_driver(driver_id):
    """Clear pending receipts for a driver by deleting unsent receipt assignments."""
    try:
        deleted = db.delete_pending_receipts_for_driver(driver_id)
        return jsonify({"success": True, "deleted": deleted})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/receipt_debts/assignments/<assignment_id>', methods=['DELETE'])
def api_receipt_debts_delete_assignment(assignment_id):
    """Delete a single pending receipt assignment row."""
    try:
        ok = db.delete_pending_receipt_assignment(assignment_id)
        if ok:
            return jsonify({"success": True})
        return jsonify({"success": False, "error": "Assignment not found or receipt already submitted."}), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/receipts/submitted', methods=['GET'])
def api_receipts_submitted():
    """Recent leads with driver-uploaded receipt images (for admin dashboard gallery)."""
    try:
        raw_limit = request.args.get("limit", "100")
        limit = int(raw_limit) if str(raw_limit).isdigit() else 100
        return jsonify(db.get_submitted_receipts_recent(limit=limit))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Driver Track (live map tab) ────────────────────────────────────────────
# Native port of the driver-track site's admin data endpoints so the dashboard
# renders the map itself (no iframe). Free services: Nominatim + OSRM.

_TRACK_GEO_CACHE = {"geocode": {}, "reverse": {}, "route": {}}
_TRACK_ROUTE_TTL_S = 45
_NOMINATIM_HEADERS = {"User-Agent": "krab-issuer-admin/1.0 (driver track tab)"}


def _track_round3(v):
    try:
        return round(float(v), 3)
    except (TypeError, ValueError):
        return None


@app.route('/api/tracking/overview', methods=['GET'])
def api_tracking_overview():
    """Sessions + last-known driver positions + movement trails."""
    try:
        from datetime import datetime, timedelta, timezone
        try:
            hours = max(1, min(48, int(request.args.get("hours", "8"))))
        except ValueError:
            hours = 8
        now = datetime.now(timezone.utc)
        s_since = (now - timedelta(hours=48)).isoformat()
        p_since = (now - timedelta(hours=hours)).isoformat()

        srows = (
            db.client.table("driver_tracking_sessions").select("*")
            .gte("created_at", s_since).order("created_at", desc=True).limit(200).execute()
        ).data or []
        prows = (
            db.client.table("driver_location_pings")
            .select("driver_id,lat,lng,accuracy,created_at")
            .gte("created_at", p_since).order("created_at").limit(4000).execute()
        ).data or []

        sessions = [{
            "token": s.get("token"), "kind": s.get("kind"),
            "reference_id": s.get("reference_id"), "driver_name": s.get("driver_name"),
            "status": s.get("status"), "created_at": s.get("created_at"),
            "located_at": s.get("located_at"), "details_sent_at": s.get("details_sent_at"),
            "delivery_address": s.get("delivery_address"),
            "dest_lat": s.get("dest_lat"), "dest_lng": s.get("dest_lng"),
        } for s in srows]

        name_by, sess_by = {}, {}
        for s in srows:
            did = s.get("driver_id")
            if did is not None and str(did) not in name_by:
                name_by[str(did)] = s.get("driver_name")
                sess_by[str(did)] = s.get("token")

        latest = {}
        trails = {}
        for p in prows:
            key = "unknown" if p.get("driver_id") is None else str(p.get("driver_id"))
            trails.setdefault(key, []).append([p.get("lat"), p.get("lng"), p.get("created_at")])
            if p.get("driver_id") is not None:
                latest[key] = p
        for k in trails:
            trails[k] = trails[k][-500:]

        drivers = []
        for did, ping in latest.items():
            try:
                age = (now - datetime.fromisoformat(str(ping.get("created_at")).replace("Z", "+00:00"))).total_seconds()
            except Exception:
                age = 1e9
            drivers.append({
                "driver_id": did,
                "driver_name": name_by.get(did),
                "session_token": sess_by.get(did),
                "last_ping": {"lat": ping.get("lat"), "lng": ping.get("lng"),
                              "accuracy": ping.get("accuracy"), "created_at": ping.get("created_at")},
                "live": age < 120,
            })
        return jsonify({"sessions": sessions, "drivers": drivers, "trails": trails})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/tracking/route', methods=['GET'])
def api_tracking_route():
    """From/To addresses + driving route + ETA for one tracking session."""
    import time as _time
    import requests as _rq
    try:
        token = (request.args.get("token") or "").strip()
        if not token:
            return jsonify({"error": "token required"}), 400
        srows = (
            db.client.table("driver_tracking_sessions").select("*")
            .eq("token", token).limit(1).execute()
        ).data or []
        if not srows:
            return jsonify({"error": "session not found"}), 404
        s = srows[0]

        prow = (
            db.client.table("driver_location_pings").select("lat,lng,created_at")
            .eq("session_token", token).order("created_at", desc=True).limit(1).execute()
        ).data or []
        if not prow and s.get("driver_id"):
            prow = (
                db.client.table("driver_location_pings").select("lat,lng,created_at")
                .eq("driver_id", str(s.get("driver_id"))).order("created_at", desc=True).limit(1).execute()
            ).data or []
        frm = None
        if prow:
            frm = {"lat": prow[0].get("lat"), "lng": prow[0].get("lng"), "at": prow[0].get("created_at")}

        addr = (s.get("delivery_address") or "").strip() or None
        to = None
        if s.get("dest_lat") is not None and s.get("dest_lng") is not None:
            to = {"lat": s.get("dest_lat"), "lng": s.get("dest_lng"), "address": addr}
        elif addr:
            hit = _TRACK_GEO_CACHE["geocode"].get(addr)
            if hit is None and addr not in _TRACK_GEO_CACHE["geocode"]:
                try:
                    g = _rq.get(
                        "https://nominatim.openstreetmap.org/search",
                        params={"format": "json", "limit": 1, "countrycodes": "us", "q": addr},
                        headers=_NOMINATIM_HEADERS, timeout=15,
                    ).json()
                    hit = {"lat": float(g[0]["lat"]), "lng": float(g[0]["lon"])} if g else None
                except Exception:
                    hit = None
                _TRACK_GEO_CACHE["geocode"][addr] = hit
            if hit:
                to = {"lat": hit["lat"], "lng": hit["lng"], "address": addr}
                try:
                    db.client.table("driver_tracking_sessions").update(
                        {"dest_lat": hit["lat"], "dest_lng": hit["lng"]}
                    ).eq("token", token).execute()
                except Exception:
                    pass

        if frm:
            rkey = (_track_round3(frm["lat"]), _track_round3(frm["lng"]))
            cached = _TRACK_GEO_CACHE["reverse"].get(rkey)
            if cached is None and rkey not in _TRACK_GEO_CACHE["reverse"]:
                try:
                    rv = _rq.get(
                        "https://nominatim.openstreetmap.org/reverse",
                        params={"format": "json", "zoom": 16, "lat": frm["lat"], "lon": frm["lng"]},
                        headers=_NOMINATIM_HEADERS, timeout=15,
                    ).json()
                    a = rv.get("address") or {}
                    parts = [
                        " ".join(x for x in (a.get("house_number"), a.get("road")) if x),
                        a.get("city") or a.get("town") or a.get("village") or a.get("county"),
                        (f"{a.get('state')} {a.get('postcode') or ''}".strip() if a.get("state") else a.get("postcode")),
                    ]
                    cached = ", ".join(p for p in parts if p) or rv.get("display_name")
                except Exception:
                    cached = None
                _TRACK_GEO_CACHE["reverse"][rkey] = cached
            frm["address"] = cached

        route = None
        if frm and to:
            key = (_track_round3(frm["lat"]), _track_round3(frm["lng"]),
                   _track_round3(to["lat"]), _track_round3(to["lng"]))
            hit = _TRACK_GEO_CACHE["route"].get(key)
            if hit and _time.time() - hit[0] < _TRACK_ROUTE_TTL_S:
                route = hit[1]
            else:
                try:
                    rj = _rq.get(
                        f"https://router.project-osrm.org/route/v1/driving/"
                        f"{frm['lng']},{frm['lat']};{to['lng']},{to['lat']}",
                        params={"overview": "full", "geometries": "geojson"},
                        timeout=20,
                    ).json()
                    r0 = (rj.get("routes") or [None])[0]
                    if r0:
                        route = {
                            "duration": r0.get("duration") or 0,
                            "distance": r0.get("distance") or 0,
                            "coords": [[c[1], c[0]] for c in (r0.get("geometry") or {}).get("coordinates", [])],
                        }
                except Exception:
                    route = None
                _TRACK_GEO_CACHE["route"][key] = (_time.time(), route)

        def _eta(sec):
            m = int(round((sec or 0) / 60))
            if m < 1:
                return "under 1 min"
            if m < 60:
                return f"{m} min"
            return f"{m // 60}h {m % 60}m" if m % 60 else f"{m // 60}h"

        return jsonify({
            "ok": True, "token": token,
            "reference_id": s.get("reference_id"), "driver_name": s.get("driver_name"),
            "status": s.get("status"),
            "from": frm,
            "to": to or ({"address": addr} if addr else None),
            "eta_seconds": route and route["duration"],
            "eta_label": _eta(route["duration"]) if route else None,
            "distance_meters": route and route["distance"],
            "distance_label": (f"{route['distance'] / 1609.344:.1f} mi" if route else None),
            "geometry": route and route["coords"],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/receipts/image/<lead_id>', methods=['GET'])
def api_receipt_image(lead_id):
    """Receipt image resolver — STREAMS the image bytes (no redirects).

    Redirects break behind the Vercel /api/backend proxy (it drops the
    Location header), so we fetch the image server-side and return the bytes.
    Storage URLs are fetched directly; expired Telegram file URLs are
    re-signed first via getFile using the #tgfid= fragment the bot stores.
    """
    import requests as _rq
    from flask import Response

    def _fetch(u):
        try:
            r = _rq.get(u, timeout=25)
            if r.ok and r.content:
                ct = r.headers.get("content-type") or "image/jpeg"
                if "text/html" in ct.lower():
                    return None
                return Response(
                    r.content,
                    mimetype=ct,
                    headers={"Cache-Control": "private, max-age=300"},
                )
        except Exception as fe:
            logger.warning("receipt image fetch failed for %s: %s", lead_id, fe)
        return None

    try:
        row = db.client.table("leads").select("receipt_image_url").eq("id", lead_id).limit(1).execute()
        url = ((row.data or [{}])[0].get("receipt_image_url") or "").strip()
        if not url:
            return jsonify({"error": "no receipt image for this lead"}), 404
        marker = "https://api.telegram.org/file/bot"
        base, _, frag = url.partition("#")
        if marker not in url:
            resp = _fetch(base)
            if resp:
                return resp
            return jsonify({"error": "image not reachable"}), 502
        # Telegram-hosted: re-sign first (stored URLs expire after ~1h).
        tgfid = None
        for part in frag.split("&"):
            if part.startswith("tgfid="):
                tgfid = part[len("tgfid="):].strip()
        # file_ids are bot-scoped: prefer the uploading bot's token (embedded
        # in the stored URL, LAST occurrence to agree with the doubled-prefix
        # normalizer); cascade to the env token if that one fails (rotation).
        hits = re.findall(r"https://api\.telegram\.org/file/bot([^/]+)/", base)
        embedded = hits[-1].strip() if hits else ""
        env_tok = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
        tokens = [embedded] if embedded else []
        if env_tok and env_tok != embedded:
            tokens.append(env_tok)
        if tgfid:
            for token in tokens:
                r = _rq.get(
                    f"https://api.telegram.org/bot{token}/getFile",
                    params={"file_id": tgfid},
                    timeout=15,
                )
                fp = ""
                if r.ok:
                    fp = str((((r.json() or {}).get("result") or {}).get("file_path") or "")).strip()
                if fp:
                    resp = _fetch(f"{marker}{token}/{fp}")
                    if resp:
                        return resp
        # Last resort: the stored URL as-is (works within the first hour).
        resp = _fetch(base)
        if resp:
            return resp
        return jsonify({"error": "image expired and could not be re-signed"}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/groups/<group_id>/toggle', methods=['POST'])
def api_toggle_group(group_id):
    """Toggle group active status."""
    try:
        if db.toggle_group_status(group_id):
            return jsonify({"success": True, "message": "Group status updated!"})
        return jsonify({"success": False, "error": "Error updating group"}), 500
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/drivers/<driver_id>/toggle', methods=['POST'])
def api_toggle_driver(driver_id):
    """Toggle driver active status."""
    try:
        if db.toggle_driver_status(driver_id):
            return jsonify({"success": True, "message": "Driver status updated!"})
        return jsonify({"success": False, "error": "Error updating driver"}), 500
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/renewals/upcoming', methods=['GET'])
def api_upcoming_renewals():
    """Return upcoming (pending) renewals with days remaining, client/group/driver names."""
    try:
        from datetime import datetime, timezone
        r = db.client.table("lead_renewals").select(
            "id, lead_id, renewal_due_at, status, original_group_id, original_driver_id, "
            "group_accepted_by_id, driver_accepted_by_id, "
            "lead:leads(reference_id, telegram_username, vehicle_details)"
        ).in_("status", ["pending", "group_phase", "driver_phase"]).order("renewal_due_at").execute()
        rows = r.data or []
        groups_cache = {}
        drivers_cache = {}
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
                        gr = db.client.table("groups").select("group_name").eq("id", gid).limit(1).execute()
                        groups_cache[gid] = (gr.data[0].get("group_name") if gr.data else "—")
                    except Exception:
                        groups_cache[gid] = "—"
                gname = groups_cache[gid]
            dname = "—"
            if did:
                if did not in drivers_cache:
                    try:
                        dr = db.client.table("drivers").select("driver_name").eq("id", did).limit(1).execute()
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
        return jsonify(out)
    except Exception as e:
        logger.error("Error fetching upcoming renewals: %s", e)
        return jsonify({"error": str(e)}), 500


def _require_lead_ingest_auth() -> tuple[bool, tuple]:
    """Validate Bearer token for external lead ingest."""
    from config import Config

    expected = (Config.LEAD_INGEST_API_KEY or "").strip()
    if not expected:
        return False, (jsonify({"ok": False, "error": "Lead ingest API is not configured"}), 503)
    auth = (request.headers.get("Authorization") or "").strip()
    if not auth.lower().startswith("bearer "):
        return False, (jsonify({"ok": False, "error": "Missing Authorization: Bearer token"}), 401)
    token = auth[7:].strip()
    if token != expected:
        return False, (jsonify({"ok": False, "error": "Invalid API key"}), 401)
    return True, ()


@app.route("/api/v1/leads/ingest", methods=["POST"])
def api_v1_leads_ingest():
    """
    Accept external 'New Lead' messages and create krableadsV2 leads.
    Authorization: Bearer <LEAD_INGEST_API_KEY>
    Body: text/plain message or JSON { "message": "..." } or { "fields": { ... } }.
    JSON bodies may also carry "files": [{"url": <https>, "label": <caption>}] —
    checkout documents attached to the lead and AI-read to fill blank fields.
    """
    ok, err_resp = _require_lead_ingest_auth()
    if not ok:
        return err_resp

    from utils.database import Database
    from utils.lead_ingest import ingest_external_lead

    ingest_db = Database()
    content_type = (request.content_type or "").lower()
    message = None
    fields = None
    files = None

    if "application/json" in content_type:
        body = request.get_json(silent=True) or {}
        if isinstance(body, dict):
            if body.get("message"):
                message = str(body["message"])
            elif body.get("fields") and isinstance(body["fields"], dict):
                fields = body["fields"]
            else:
                return jsonify({"ok": False, "errors": ["JSON body needs message or fields"]}), 422
            if isinstance(body.get("files"), list):
                files = body["files"]
        else:
            return jsonify({"ok": False, "errors": ["Invalid JSON body"]}), 422
    else:
        message = request.get_data(as_text=True) or ""
        if not message.strip():
            return jsonify({"ok": False, "errors": ["Empty message body"]}), 422

    result, errors = ingest_external_lead(ingest_db, message=message, fields=fields, files=files)
    if errors:
        return jsonify({"ok": False, "errors": errors}), 422
    if not result:
        return jsonify({"ok": False, "errors": ["Ingest failed"]}), 500

    return jsonify({"ok": True, **result}), 201


def _send_attachment_via_bot_api(chat_id, entry: dict) -> bool:
    """Send one URL attachment straight through the Telegram HTTP API.

    Used by the attach endpoint when the lead's group already accepted —
    the bot process isn't involved, so we deliver late-arriving checkout
    documents ourselves. Photo failures fall back to sendDocument (covers
    PDFs mislabeled as photos and oversized images).
    """
    import requests as _rq
    from config import Config

    token = (Config.TELEGRAM_BOT_TOKEN or "").strip()
    url = str(entry.get("file_id") or "").strip()
    if not token or not chat_id or not url:
        return False
    caption = (entry.get("caption") or "").strip() or None
    ftype = (entry.get("type") or "photo").lower()
    methods = ["sendDocument"] if ftype == "document" else ["sendPhoto", "sendDocument"]
    for method in methods:
        payload = {"chat_id": chat_id, ("document" if method == "sendDocument" else "photo"): url}
        if caption:
            payload["caption"] = caption
        try:
            r = _rq.post(
                f"https://api.telegram.org/bot{token}/{method}",
                json=payload,
                timeout=30,
            )
            if r.ok and (r.json() or {}).get("ok"):
                return True
            logger.warning("Attach %s failed (%s): %s", method, r.status_code, r.text[:160])
        except Exception as e:
            logger.warning("Attach %s error: %s", method, e)
    return False


@app.route("/api/v1/leads/attach", methods=["POST"])
def api_v1_leads_attach():
    """
    Attach checkout documents to an existing lead (documents upload after the
    order ingest on tristatetags.com, so they arrive as a second call).
    Authorization: Bearer <LEAD_INGEST_API_KEY>
    JSON body: { "reference_id" and/or "external_order_id",
                 "files": [{"url": <https>, "label": <caption>}] }

    If a group already accepted the lead the files are sent to that group
    chat immediately; otherwise they are stored on the lead and the existing
    accept flow forwards them. Blank lead fields (VIN, insurance, policy #,
    email, license ID) are best-effort filled by AI-reading the documents.
    """
    ok, err_resp = _require_lead_ingest_auth()
    if not ok:
        return err_resp

    from utils.database import Database
    from utils.ingest_files import extract_fields_from_files, normalize_ingest_files

    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"ok": False, "errors": ["Invalid JSON body"]}), 422
    attachments = normalize_ingest_files(body.get("files"))
    if not attachments:
        return jsonify({"ok": False, "errors": ["files must contain at least one valid https url"]}), 422

    attach_db = Database()
    lead = None
    ref = str(body.get("reference_id") or "").strip()
    ext = str(body.get("external_order_id") or "").strip()
    if ref:
        lead = attach_db.get_lead_by_reference_id(ref)
    if not lead and ext:
        lead = attach_db.get_lead_by_external_order_id(ext)
    if not lead:
        return jsonify({"ok": False, "errors": ["Lead not found for reference_id/external_order_id"]}), 404
    lead_id = str(lead.get("id"))

    # AI gap-fill for fields the customer never typed. The VIN lives on line 6
    # of the 11-line vehicle_details block; insurance and policy are lines 9-10.
    ai_filled = []
    try:
        vd_lines = (lead.get("vehicle_details") or "").split("\n")
        while len(vd_lines) < 11:
            vd_lines.append("-")
        wanted = []
        if (vd_lines[5] or "-").strip() in ("", "-"):
            wanted.append("vin")
        if (vd_lines[8] or "-").strip() in ("", "-"):
            wanted.append("insurance_company")
        if (vd_lines[9] or "-").strip() in ("", "-"):
            wanted.append("insurance_policy_number")
        if not (lead.get("email") or "").strip():
            wanted.append("email")
        if not (lead.get("driver_license_id") or "").strip():
            wanted.append("driver_license_id")
        if wanted:
            extracted = extract_fields_from_files(attachments, wanted_keys=wanted)
            updates = {}
            if extracted.get("vin"):
                vd_lines[5] = extracted["vin"]
            if extracted.get("insurance_company"):
                vd_lines[8] = extracted["insurance_company"]
            if extracted.get("insurance_policy_number"):
                vd_lines[9] = extracted["insurance_policy_number"]
            if any(k in extracted for k in ("vin", "insurance_company", "insurance_policy_number")):
                updates["vehicle_details"] = "\n".join(vd_lines)
            if extracted.get("email"):
                updates["email"] = extracted["email"]
            if extracted.get("driver_license_id"):
                updates["driver_license_id"] = extracted["driver_license_id"]
            if updates:
                attach_db.update_lead(lead_id, updates)
                ai_filled = sorted(extracted.keys())
    except Exception as e:
        logger.warning("Attach AI gap-fill failed for lead %s: %s", lead_id, e)

    # Deliver: straight to the accepting group if one exists, else queue on the
    # lead so handle_accept_group_offer forwards them on accept.
    delivered = "queued"
    acc = attach_db.get_accepted_group_for_lead(lead_id)
    group = attach_db.get_group_by_id(str(acc.get("group_id"))) if acc else None
    group_chat = (group or {}).get("group_telegram_id")
    if group_chat:
        sent = sum(1 for entry in attachments if _send_attachment_via_bot_api(group_chat, entry))
        if sent:
            delivered = f"sent_to_group:{sent}/{len(attachments)}"
        if sent < len(attachments):
            logger.warning(
                "Attach: only %d/%d files delivered to group for lead %s",
                sent, len(attachments), lead_id,
            )
    else:
        existing = lead.get("phase1_attached_files")
        existing = existing if isinstance(existing, list) else []
        known = {str(e.get("file_id")) for e in existing if isinstance(e, dict)}
        merged = existing + [a for a in attachments if a["file_id"] not in known]
        attach_db.update_lead(lead_id, {"phase1_attached_files": merged})

    return jsonify({
        "ok": True,
        "lead_id": lead_id,
        "reference_id": lead.get("reference_id"),
        "delivered": delivered,
        "ai_filled": ai_filled,
    }), 200


if __name__ == '__main__':
    # Render sets PORT for web services; fallback to ADMIN_PORT or 5000
    port = int(os.getenv('PORT', os.getenv('ADMIN_PORT', 5000)))
    app.run(host='0.0.0.0', port=port, debug=os.getenv('FLASK_DEBUG', 'false').lower() == 'true')

