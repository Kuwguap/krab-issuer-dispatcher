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


def purge_driver_everywhere(
    db: Database,
    driver: dict,
    *,
    drafts_db=None,
) -> tuple[bool, list[str]]:
    """Remove driver from Issuer, Dispatch, and interview records. Returns (ok, errors)."""
    errors: list[str] = []
    did = str(driver.get("id") or "").strip()
    tid = str(driver.get("driver_telegram_id") or "").strip()
    driver_name = (driver.get("driver_name") or "").strip()

    interview = db.get_latest_interview_for_telegram_id(tid) if tid else None
    email = (interview.get("email") or "").strip() if interview else ""

    if tid:
        for inv in db.list_interviews_for_telegram_id(tid):
            iid = str(inv.get("id") or "")
            if not iid:
                continue
            if drafts_db:
                try:
                    for drow in drafts_db.find_drafts_by_submitted_interview(iid):
                        drafts_db.reset_submission(str(drow["id"]))
                except Exception as e:
                    errors.append(f"Draft reset: {e}")
            if not db.delete_interview(iid):
                errors.append(f"Interview {iid[:8]}… not deleted")

    issuer_ok = False
    if did:
        issuer_ok = db.delete_driver(did)
    elif tid:
        issuer_ok = db.delete_driver_by_telegram_id(tid)

    dispatch_ok = False
    if email:
        dispatch_ok, err = recipients_db.delete_recipient_by_email(email)
        if not dispatch_ok and err:
            errors.append(f"Dispatch: {err}")
    elif driver_name:
        dispatch_ok, err = recipients_db.delete_recipient_by_name(driver_name)
        if not dispatch_ok and err:
            errors.append(f"Dispatch: {err}")

    if not issuer_ok and not dispatch_ok and not tid:
        errors.append("Nothing was removed")
    return issuer_ok or dispatch_ok, errors
