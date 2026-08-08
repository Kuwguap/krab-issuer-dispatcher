"""Free-text label parser shared by the Telegram bot and the HTTP /api/tag
endpoint, so tristatetags.com/tag and the bot parse client details identically.
"""
from __future__ import annotations

import re

import tagcore

# Label → canonical field. Longer aliases first so "insurance company" wins over
# "insurance", "policy number" over "policy", "body style" over "body".
_LABEL_ALIASES = {
    "customer name": "name", "customer": "name", "name": "name", "client": "name",
    "phone": "phone", "phone number": "phone", "tel": "phone", "mobile": "phone",
    "vin": "vin", "vin number": "vin",
    "year": "year", "yr": "year",
    "make": "make", "model": "model",
    "vehicle": "vehicle", "car": "vehicle", "vehicle info": "vehicle",
    "color": "color", "colour": "color",
    "body style": "body", "bodystyle": "body", "body type": "body", "body": "body",
    "registration address": "address", "reg address": "address",
    "mailing address": "address", "street address": "address",
    "street": "address", "address": "address",
    "city": "city",
    "state": "state", "st": "state",
    "zip": "zip", "zipcode": "zip", "zip code": "zip", "postal": "zip", "postal code": "zip",
    "city state zip": "city_state_zip", "city/state/zip": "city_state_zip",
    "insurance company": "insurance_company", "insurance carrier": "insurance_company",
    "carrier": "insurance_company", "insurer": "insurance_company", "insurance": "insurance_company",
    "policy number": "policy", "policy no": "policy", "policy #": "policy",
    "binder number": "policy", "binder": "policy", "policy": "policy",
    "issue date": "issued", "issued": "issued", "date issued": "issued",
    "expiration date": "expires", "expiration": "expires", "expires": "expires", "exp": "expires",
}
_LABELS_SORTED = sorted(_LABEL_ALIASES, key=len, reverse=True)
# A label is a known alias followed by ":" / "-" / "#" separators. The lookbehind
# stops it matching inside a word; matching anywhere (not just line start) lets a
# single line carry several fields, e.g. "City: Bronx  State: NY  Zip: 10465".
_LABEL_RE = re.compile(
    r"(?i)(?<![A-Za-z])(" + "|".join(re.escape(l) for l in _LABELS_SORTED) + r")\s*[:#\-]+\s*"
)


def parse_labeled(text: str) -> dict:
    """Tokenize free-form labeled text into canonical fields.

    Robust like krableadsV2's ingest parser: aliases, multiple fields per line,
    and a combined "Vehicle: 2013 Infiniti JX35" line split into year/make/model.
    First occurrence of a field wins.
    """
    s = text or ""
    matches = list(_LABEL_RE.finditer(s))
    out: dict = {}
    for i, m in enumerate(matches):
        key = _LABEL_ALIASES[m.group(1).lower()]
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(s)
        val = s[start:end].strip().strip(",").strip()
        if val and key not in out:
            out[key] = val

    # Combined "Vehicle: 2013 Infiniti JX35" → year/make/model (build_fields also
    # VIN-decodes any that are still blank).
    if out.get("vehicle") and not (out.get("year") and out.get("make") and out.get("model")):
        try:
            y, mk, md = tagcore.tag_pdf.parse_car_line(out["vehicle"])
            out.setdefault("year", y)
            out.setdefault("make", mk)
            out.setdefault("model", md)
        except Exception:
            pass
    out.pop("vehicle", None)
    return out
