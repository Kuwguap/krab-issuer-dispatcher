"""NJ Temporary Evidence of Insurance — upstream barcode-app client."""
from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import requests


def generate_nj_policy_number() -> str:
    """``ABP63<8 digits>`` (e.g. ``ABP6300173880``)."""
    n = random.randint(0, 99_999_999)
    return f"ABP63{n:08d}"

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")
_DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")

_5XX_RETRY_MAX = 2
_5XX_BACKOFF_START_S = 1.0


@dataclass
class NJCardResult:
    ok: bool
    status_code: int
    email: Optional[str] = None
    policy_number: Optional[str] = None
    portal_warning: Optional[str] = None
    error: Optional[str] = None


def _parse_error(resp: requests.Response) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict):
            err = (body.get("error") or body.get("message") or "").strip()
            if err:
                return err
    except Exception:
        pass
    text = (resp.text or "").strip()
    if text:
        return text[:500]
    return resp.reason or f"HTTP {resp.status_code}"


def validate_nj_payload(payload: dict[str, Any]) -> tuple[bool, Optional[str]]:
    required = (
        "policyNumber",
        "effectiveMmDdYyyy",
        "expirationMmDdYyyy",
        "vehicleYear",
        "vehicleMake",
        "vehicleModel",
        "vin",
        "insuredNameUpper",
        "email",
    )
    for key in required:
        val = payload.get(key)
        if val is None or (isinstance(val, str) and not str(val).strip()):
            return False, f"Missing required field: {key}"

    vin = str(payload["vin"]).strip().upper()
    if not _VIN_RE.match(vin):
        return False, "Invalid VIN: must be 17 characters with no I, O, or Q."

    for date_key in ("effectiveMmDdYyyy", "expirationMmDdYyyy"):
        d = str(payload[date_key]).strip()
        if not _DATE_RE.match(d):
            return False, f"Invalid date for {date_key}: use MM/DD/YYYY."

    email = str(payload["email"]).strip()
    if not _EMAIL_RE.match(email):
        return False, "Invalid email address."

    return True, None


def build_nj_email_payload(
    *,
    policy_number: str,
    effective_mm_dd_yyyy: str,
    expiration_mm_dd_yyyy: str,
    vehicle_year: str,
    vehicle_make: str,
    vehicle_model: str,
    vin: str,
    insured_name_upper: str,
    insured_address_lines: list[str],
    email: str,
    first_name: str | None = None,
    phone: str | None = None,
    annual_premium: float | None = None,
    skip_portal_account: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "policyNumber": policy_number,
        "effectiveMmDdYyyy": effective_mm_dd_yyyy,
        "expirationMmDdYyyy": expiration_mm_dd_yyyy,
        "vehicleYear": vehicle_year,
        "vehicleMake": vehicle_make,
        "vehicleModel": vehicle_model,
        "vin": vin.strip().upper(),
        "insuredNameUpper": insured_name_upper.strip().upper(),
        "insuredAddressLines": [ln.strip() for ln in insured_address_lines if ln and ln.strip()],
        "email": email.strip(),
        "skipPortalAccount": skip_portal_account,
    }
    if first_name:
        payload["firstName"] = first_name
    if phone:
        payload["phone"] = phone
    if annual_premium is not None and annual_premium > 0:
        payload["annualPremium"] = annual_premium
    return payload


def send_nj_insurance_email(payload: Dict[str, Any]) -> NJCardResult:
    """POST JSON to ``/api/insurance-card-nj/email`` with 5xx-only retry."""
    try:
        from config import Config
    except Exception:
        return NJCardResult(False, 503, error="Config not available.")

    base = (getattr(Config, "BARCODE_APP_BASE_URL", None) or "").strip().rstrip("/")
    if not base:
        return NJCardResult(
            False,
            503,
            error="BARCODE_APP_BASE_URL is not configured on the bot.",
        )

    ok, err = validate_nj_payload(payload)
    if not ok:
        return NJCardResult(False, 400, error=err)

    url = f"{base}/api/insurance-card-nj/email"
    headers = {"Content-Type": "application/json"}

    resp: requests.Response | None = None
    backoff = _5XX_BACKOFF_START_S
    for attempt in range(_5XX_RETRY_MAX + 1):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=120)
        except requests.RequestException as exc:
            logger.exception("NJ card API request failed")
            return NJCardResult(False, 0, error=str(exc))

        if resp.status_code < 500:
            break
        if attempt < _5XX_RETRY_MAX:
            logger.warning(
                "NJ card API %s — retry %d/%d after %.1fs",
                resp.status_code,
                attempt + 1,
                _5XX_RETRY_MAX,
                backoff,
            )
            time.sleep(backoff)
            backoff *= 2

    assert resp is not None

    if resp.status_code == 200:
        try:
            body = resp.json()
        except Exception:
            return NJCardResult(False, 200, error="Invalid JSON in success response.")
        if isinstance(body, dict) and body.get("ok"):
            warning = (body.get("portalWarning") or "").strip() or None
            if warning:
                logger.info("NJ card issued with portalWarning: %s", warning)
            return NJCardResult(
                True,
                200,
                email=body.get("email"),
                policy_number=body.get("policyNumber"),
                portal_warning=warning,
            )
        return NJCardResult(False, resp.status_code, error=_parse_error(resp))

    return NJCardResult(False, resp.status_code, error=_parse_error(resp))
