"""Sync DB steps for hiring a driver (issuer + dispatch)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from utils import recipients_db
from utils.database import Database


def driver_display_name(interview: dict) -> str:
    full = (interview.get("full_name") or "").strip()
    if full:
        return full
    fn = (interview.get("first_name") or "").strip()
    return fn or "Driver"


def validate_hire_ready(interview: dict) -> Tuple[bool, str]:
    driver_name = driver_display_name(interview)
    tid = (interview.get("telegram_id") or "").strip()
    em = (interview.get("email") or "").strip()
    if driver_name == "Driver" or not tid or not em:
        return False, "Full name, Telegram ID, and email are required to complete hire."
    return True, ""


def ensure_first_name_from_username(db: Database, interview: dict) -> dict:
    """Mirror bot hire: derive first_name from telegram username when full_name missing."""
    iid = str(interview.get("id") or "")
    if (interview.get("full_name") or "").strip() or (interview.get("first_name") or "").strip():
        return interview
    un = (interview.get("telegram_username") or "").lstrip("@").strip()
    derived = un.split()[0] if un else ""
    if derived and iid:
        db.update_interview(iid, {"first_name": derived})
        interview = dict(interview)
        interview["first_name"] = derived
    return interview


def hire_driver_records(
    db: Database,
    interview_id: str,
) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """
    Add driver to issuer + dispatch DBs and mark interview hired.
    Returns (interview, non-fatal errors). interview is None on fatal validation failure.
    """
    interview = db.get_interview_by_id(interview_id)
    if not interview:
        return None, ["Interview not found"]

    if (interview.get("status") or "").strip() == "hired":
        return interview, []

    interview = ensure_first_name_from_username(db, interview)
    ok, msg = validate_hire_ready(interview)
    if not ok:
        return None, [msg]

    driver_name = driver_display_name(interview)
    tid = (interview.get("telegram_id") or "").strip()
    em = (interview.get("email") or "").strip()

    errors: List[str] = []
    ok_d, err_d = db.create_driver(driver_name, tid, interview.get("phone_number"))
    ok_r, err_r = recipients_db.add_recipient(driver_name, em)
    if not ok_d and err_d:
        errors.append(f"Issuer drivers: {err_d}")
    if not ok_r and err_r:
        errors.append(f"Dispatch recipients: {err_r}")

    db.update_interview(interview_id, {"status": "hired"})
    updated = db.get_interview_by_id(interview_id) or interview
    updated["status"] = "hired"
    return updated, errors
