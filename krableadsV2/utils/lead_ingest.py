"""Create leads from external HTTP ingest (no Telegram dependency)."""
from __future__ import annotations

import logging
import secrets
import string
from typing import Any, Dict, List, Optional, Tuple

from config import Config
from utils.database import Database, record_is_active
from utils.external_lead_parser import (
    build_vehicle_details_11,
    build_delivery_details,
    parse_external_lead_fields,
    parse_external_lead_message,
)
from utils.lead_validation import is_valid_pending_phone, is_valid_pending_price
from utils.onetimesecret import OneTimeSecret
from utils.api_lead_user import resolve_api_lead_user_sync

logger = logging.getLogger(__name__)


def generate_reference_id() -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(8))


def ingest_external_lead(
    db: Database,
    *,
    message: Optional[str] = None,
    fields: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """
    Parse, validate, optionally encrypt phone, and create a lead with ingest_dispatch_pending=true.
    Returns ({lead_id, reference_id, external_order_id}, []) or (None, errors).
    """
    if fields:
        state, parse_errors = parse_external_lead_fields(fields)
    elif message:
        state, parse_errors = parse_external_lead_message(message)
    else:
        return None, ["message or fields is required"]

    if parse_errors:
        return None, parse_errors

    assert state is not None
    phone = state.get("pending_phone_number")
    price = state.get("pending_price")
    if not is_valid_pending_phone(phone):
        return None, ["Invalid phone number"]
    if not is_valid_pending_price(price):
        return None, ["Invalid price"]

    api_user = (Config.API_LEAD_USER_ID or "tristatetag").strip()
    user_id, issuer_username, resolve_err = resolve_api_lead_user_sync(db, api_user)
    if resolve_err or user_id is None:
        return None, [resolve_err or "Could not resolve API_LEAD_USER_ID"]

    groups = db.get_all_groups()
    active_groups = [g for g in groups if record_is_active(g)]
    if not active_groups:
        return None, ["No active dispatch groups configured"]

    phone_str = str(phone)
    ots = OneTimeSecret()
    encrypted = ots.encrypt_phone(phone_str)
    if encrypted:
        ots_token = encrypted.get("secret_key")
        ots_meta = encrypted.get("metadata_key")
        ots_link = encrypted.get("link")
    else:
        # OTS optional for API ingest — store raw phone and still dispatch Accept buttons.
        err = (ots.last_error or "Phone encryption skipped").strip()
        logger.warning("Lead ingest: skipping phone encryption — %s", err)
        ots_token = None
        ots_meta = None
        ots_link = phone_str  # copy-paste block shows number when no secret link

    reference_id = generate_reference_id()
    vehicle_details = build_vehicle_details_11(state)
    delivery_details = build_delivery_details(state)
    source_label = (Config.LEAD_INGEST_SOURCE_LABEL or "External API").strip()

    lead_payload = {
        "user_id": user_id,
        "telegram_username": issuer_username,
        "vehicle_details": vehicle_details,
        "delivery_details": delivery_details,
        "phone_number": phone_str,
        "price": str(price),
        "onetimesecret_token": ots_token,
        "onetimesecret_secret_key": ots_meta,
        "encrypted_link": ots_link,
        "reference_id": reference_id,
        "group_id": active_groups[0]["id"],
        "extra_info": state.get("extra_info", "") or "",
        "special_request_issuers": "",
        "special_request_drivers": "",
        "special_request_note": "",
        "email": state.get("email") or None,
        "driver_license_id": None,
        "contact_info_source": source_label,
        "phase1_attached_files": [],
        "ingest_dispatch_pending": True,
        "external_order_id": state.get("external_order_id"),
        "awaiting_group_accept": False,
    }

    lead = db.create_lead(lead_payload)
    if not lead:
        return None, ["Failed to save lead to database (run migration_lead_api_ingest.sql?)"]

    return {
        "lead_id": str(lead.get("id")),
        "reference_id": lead.get("reference_id") or reference_id,
        "external_order_id": state.get("external_order_id"),
    }, []
