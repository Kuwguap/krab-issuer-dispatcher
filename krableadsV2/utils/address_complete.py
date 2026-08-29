"""Fill in the parts of an address the operator did not type.

Leads arrive with whatever fitted in a text message: "3125 Park Ave Apt 11D /
Bronx New York" has no ZIP, "…10451" alone has no city. Both print badly on a
tag and neither geocodes reliably for the driver map.

This asks OpenStreetMap's Nominatim (free, no key, already used by the driver
tracking map) to resolve the address, then fills ONLY the pieces that were
missing. Anything the operator actually typed is left exactly as typed — a
geocoder guessing over a human is how you end up delivering to the wrong
borough.

Every failure path returns the input unchanged. An address that cannot be
completed is normal, not an error.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

# Nominatim asks for a real identifying UA and at most 1 request/second.
_UA = "krab-issuer-dispatcher/1.0 (lead address completion)"
_ENDPOINT = "https://nominatim.openstreetmap.org/search"
_TIMEOUT = 8

_ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")

_STATE_NAME_TO_ABBR = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC",
}
_STATE_ABBRS = set(_STATE_NAME_TO_ABBR.values())


def state_abbr(raw: str) -> str:
    """'NY' / 'New York' → 'NY'. Empty when it is not a US state."""
    t = " ".join((raw or "").strip().strip(",.").split()).lower()
    if not t:
        return ""
    if len(t) == 2 and t.upper() in _STATE_ABBRS:
        return t.upper()
    return _STATE_NAME_TO_ABBR.get(t, "")


def _query(params: dict) -> list:
    url = f"{_ENDPOINT}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    return data if isinstance(data, list) else []


# "3125 Park Ave Apt 11D" does not geocode — the unit designator has to go, or
# Nominatim returns nothing at all for an otherwise perfectly good address.
_UNIT_RE = re.compile(
    r"[,\s]+(?:apt|apartment|unit|ste|suite|fl|floor|rm|room|#)\s*[\w-]*\s*$",
    re.IGNORECASE,
)


def strip_unit(street: str) -> str:
    """'3125 Park Ave Apt 11D' → '3125 Park Ave'. Idempotent."""
    s = " ".join((street or "").split())
    prev = None
    while prev != s:
        prev = s
        s = _UNIT_RE.sub("", s).strip(" ,")
    return s


def _addr_of(hit: dict) -> dict:
    a = hit.get("address") or {}
    city = (
        a.get("city") or a.get("town") or a.get("village") or a.get("hamlet")
        or a.get("suburb") or a.get("city_district") or a.get("county") or ""
    )
    # New York City addresses come back as city="New York" with the borough in a
    # sub-field. Everyone here writes "Bronx", and that is what belongs on a tag.
    if str(city).strip().lower() in ("new york", "new york city"):
        borough = a.get("suburb") or a.get("city_district") or a.get("borough") or ""
        if borough:
            city = str(borough).strip()
            if city.lower().startswith("the "):
                city = city[4:]
    return {
        "city": str(city).strip(),
        "state": state_abbr(str(a.get("state") or "")),
        "zip": str(a.get("postcode") or "").strip()[:5],
    }


def lookup(street: str, city: str = "", state: str = "", postal: str = "") -> dict:
    """Best-effort structured geocode → {'city','state','zip'}; {} on any failure."""
    params = {"format": "jsonv2", "addressdetails": "1", "limit": "1", "countrycodes": "us"}
    street = strip_unit(street)
    if street:
        params["street"] = street
    if city:
        params["city"] = city
    if state:
        params["state"] = state
    if postal:
        params["postalcode"] = postal
    if len(params) == 4:  # nothing but the fixed options — nothing to search on
        return {}
    try:
        hits = _query(params)
    except Exception as e:  # network, rate limit, malformed JSON — all the same here
        logger.info("address completion lookup failed: %s", e)
        return {}
    if not hits:
        return {}
    return _addr_of(hits[0])


def complete_city_state_zip(street: str, csz: str) -> tuple[str, bool]:
    """Return (completed_csz, changed).

    Fills a missing ZIP, city or state and leaves everything else untouched.
    ``changed`` is False whenever nothing could be added, so callers can stay
    silent rather than announce a no-op.
    """
    from utils.insurance_card import parse_city_state_zip  # local: avoids a cycle

    parts = parse_city_state_zip(csz or "")
    city, st, zp = parts["city"], parts["state"], parts["zip"]

    def _rebuild(c: str, s: str, z: str) -> tuple[str, bool]:
        out = " ".join(p for p in (c, s, z) if p).strip()
        return (out, bool(out) and out != (csz or "").strip())

    # A ZIP hiding in the street line is worth taking before going to the network.
    if not zp:
        m = _ZIP_RE.search(street or "")
        if m:
            zp = m.group(1)

    if city and st and zp:
        # Complete — but say so if the ZIP came from the street line, or the
        # value we just recovered would be thrown away.
        return _rebuild(city, st, zp)

    try:
        found = lookup(street=street, city=city, state=st, postal=zp)
    except Exception as e:
        # `lookup` already swallows its own failures; this is the belt to its
        # braces, because a geocoder outage must never block entering a lead.
        logger.info("address completion lookup raised: %s", e)
        found = {}
    if not found:
        return _rebuild(city, st, zp) if zp else (csz, False)

    # Only ever ADD. What the operator typed always wins.
    new_city = city or found.get("city", "")
    new_state = st or found.get("state", "")
    new_zip = zp or found.get("zip", "")

    if (new_city, new_state, new_zip) == (city, st, zp):
        return (csz, False)

    rebuilt = " ".join(p for p in (new_city, new_state, new_zip) if p).strip()
    return (rebuilt, bool(rebuilt) and rebuilt != (csz or "").strip())
