"""
VIN decode: same return shape from multiple providers.
Returns dict with year, make, model, car_line ("Year Make Model").
Providers: nhtsa (free, no key) or api_ninjas (premium, needs key).
"""
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

VIN_ALNUM_17 = re.compile(r"^[A-Za-z0-9]{17}$")

# Provider names (use in .env VIN_PROVIDER)
PROVIDER_NHTSA = "nhtsa"
PROVIDER_API_NINJAS = "api_ninjas"


def normalize_vin(vin: str) -> str:
    """Return VIN as 17-char alphanumeric uppercase, or empty if invalid."""
    if not vin or not isinstance(vin, str):
        return ""
    raw = re.sub(r"\s+", "", vin.strip()).upper()
    if len(raw) != 17 or not raw.isalnum():
        return ""
    return raw


def _result(year: str, make: str, model: str) -> dict | None:
    """Build standard result dict; return None if nothing useful."""
    year = str(year).strip() if year else ""
    make = str(make).strip() if make else ""
    model = str(model).strip() if model else ""
    if not year and not make and not model:
        return None
    parts = [p for p in (year, make, model) if p]
    car_line = " ".join(parts) if parts else ""
    return {"year": year, "make": make, "model": model, "car_line": car_line}


def vin_lookup_nhtsa(vin: str) -> dict | None:
    """
    Free NHTSA vPIC API (no key). Returns same shape: year, make, model, car_line.
    """
    vin = normalize_vin(vin)
    if not vin:
        return None
    url = f"https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/{urllib.parse.quote(vin)}?format=json"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        logger.warning("VIN lookup NHTSA error: %s", e)
        return None
    results = data.get("Results") if isinstance(data, dict) else None
    if not results or not isinstance(results, list) or len(results) == 0:
        return None
    r = results[0]
    if not isinstance(r, dict):
        return None
    year = r.get("ModelYear") or r.get("Model_Year") or ""
    make = r.get("Make") or ""
    model = r.get("Model") or ""
    return _result(year, make, model)


def vin_lookup_api_ninjas(vin: str, api_key: str) -> dict | None:
    """
    API Ninjas VIN lookup (premium). Returns same shape: year, make, model, car_line.
    """
    vin = normalize_vin(vin)
    if not vin or not api_key:
        return None
    url = f"https://api.api-ninjas.com/v1/vinlookup?vin={urllib.parse.quote(vin)}"
    req = urllib.request.Request(url, headers={"X-Api-Key": api_key})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code in (404, 400):
            return None
        logger.warning("VIN lookup API Ninjas HTTP error %s: %s", e.code, e.read())
        return None
    except Exception as e:
        logger.warning("VIN lookup API Ninjas error: %s", e)
        return None
    if not data or not isinstance(data, dict):
        return None
    year = data.get("year")
    make = data.get("make") or ""
    model = data.get("model") or ""
    return _result(year, make, model)


def vin_lookup(vin: str, provider: str, api_key: str | None = None) -> dict | None:
    """
    Single entry point. provider: "nhtsa" (free) or "api_ninjas" (needs api_key).
    Returns dict with year, make, model, car_line or None.
    """
    provider = (provider or "").strip().lower()
    if provider == PROVIDER_API_NINJAS and api_key:
        return vin_lookup_api_ninjas(vin, api_key)
    if provider == PROVIDER_NHTSA or not provider:
        return vin_lookup_nhtsa(vin)
    # fallback to free if unknown provider
    return vin_lookup_nhtsa(vin)
