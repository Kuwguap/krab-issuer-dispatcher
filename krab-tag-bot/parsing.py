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
        # Value runs to the next label OR the end of the line, whichever comes
        # first. Capping at the newline stops a trailing label (e.g. "Name: Bob"
        # with no later label) from swallowing the following unlabeled lines,
        # while a same-line next label (City:/State:/Zip:) still bounds it.
        nl = s.find("\n", start)
        nl = nl if nl != -1 else len(s)
        nxt = matches[i + 1].start() if i + 1 < len(matches) else len(s)
        end = min(nl, nxt)
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


# ── Unlabeled / freeform inference ──────────────────────────────────────────
_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$", re.IGNORECASE)
_YEAR_RE = re.compile(r"^(19|20)\d{2}$")
_PHONE_RE = re.compile(r"^\+?1?[\s\-.()]*(?:\d[\s\-.()]*){10}$")
_STREET_RE = re.compile(
    r"\b(ave|avenue|st|street|rd|road|dr|drive|blvd|boulevard|ln|lane|ct|court|"
    r"pl|place|way|ter|terrace|cir|circle|hwy|highway|pkwy|parkway|sq|square|"
    r"loop|trail|trl|pike|row|walk|path|apt|unit|ste|suite|fl|floor|hwy)\b",
    re.IGNORECASE,
)
_COLOR_WORDS = {
    "white", "black", "red", "blue", "silver", "gray", "grey", "green", "gold",
    "tan", "brown", "beige", "orange", "yellow", "purple", "maroon", "burgundy",
    "navy", "charcoal", "bronze", "champagne", "pearl", "cream", "ivory",
    "pewter", "teal", "pink", "copper", "gunmetal", "sand", "olive", "magenta",
}
_MAKES = {
    "toyota", "honda", "ford", "chevrolet", "chevy", "gmc", "nissan", "infiniti",
    "lexus", "acura", "bmw", "mercedes", "mercedes-benz", "benz", "audi",
    "volkswagen", "vw", "hyundai", "kia", "subaru", "mazda", "jeep", "dodge",
    "ram", "chrysler", "buick", "cadillac", "lincoln", "volvo", "porsche",
    "jaguar", "rover", "range", "mitsubishi", "tesla", "mini", "genesis",
    "fiat", "scion", "saturn", "pontiac", "hummer", "suzuki", "isuzu",
    "freightliner", "kenworth", "peterbilt", "mack", "hino", "rivian", "lucid",
    "polestar", "maserati", "bentley", "ferrari", "lamborghini", "mclaren",
    "smart", "saab", "mercury", "oldsmobile", "plymouth",
}
_INSURERS = {
    "progressive", "geico", "allstate", "nationwide", "farmers", "usaa",
    "travelers", "esurance", "metlife", "safeco", "hartford", "aaa", "plymouth rock",
    "state farm", "liberty mutual", "american family", "national general",
    "the general", "root", "lemonade", "mercury", "kemper", "dairyland",
}


def _infer_unlabeled_line(s: str, out: dict) -> None:
    """Classify one unlabeled line by content and fill any still-blank field."""
    s = s.strip().strip(",").strip()
    if not s:
        return
    compact = s.replace(" ", "")

    if _VIN_RE.match(compact) and not out.get("vin"):
        out["vin"] = compact.upper()
        return

    # City/State/ZIP: a 2-letter state (or spelled name) together with a 5-digit ZIP.
    st = tagcore.tag_pdf.parse_state(s)
    if st and re.search(r"\b\d{5}\b", s) and not out.get("city"):
        city, zc = tagcore.tag_pdf.parse_city_zip(s, st)
        city, st, zc = tagcore.tag_pdf.normalize_city_state_zip(city or s, st, zc)
        if city:
            out.setdefault("city", city)
        if st:
            out.setdefault("state", st)
        if zc:
            out.setdefault("zip", zc)
        return

    if _PHONE_RE.match(s) and not out.get("phone"):
        d = re.sub(r"\D", "", s)
        out["phone"] = d[-10:] if len(d) >= 10 else d
        return

    if _YEAR_RE.match(s) and not out.get("year"):
        out["year"] = s
        return

    words = [w.strip(",") for w in s.split()]
    low = [w.lower() for w in words]
    # Vehicle line: contains a known make. Optional leading color word.
    if any(w in _MAKES for w in low):
        rest = words
        if low and low[0] in _COLOR_WORDS:
            out.setdefault("color", words[0])
            rest = words[1:]
        try:
            y, mk, md = tagcore.tag_pdf.parse_car_line(" ".join(rest))
            if y:
                out.setdefault("year", y)
            if mk:
                out.setdefault("make", mk)
            if md:
                out.setdefault("model", md)
        except Exception:
            pass
        return

    # Address: starts with a number and has letters (street name / suffix).
    if re.match(r"^\d", s) and re.search(r"[A-Za-z]", s) and not out.get("address"):
        out["address"] = s
        return

    # Insurance: a known carrier or an insurance-ish phrase.
    if not out.get("insurance_company") and (
        s.lower() in _INSURERS
        or re.search(r"insurance|casualty|mutual|indemnity|assurance", s, re.IGNORECASE)
    ):
        out["insurance_company"] = s
        return

    if s.lower() in _COLOR_WORDS and not out.get("color"):
        out["color"] = s
        return

    # Name: alphabetic words (a person's name).
    if re.fullmatch(r"[A-Za-z][A-Za-z.'\- ]+", s) and not out.get("name"):
        out["name"] = s
        return

    # Anything else with letters → insurance company (last resort).
    if not out.get("insurance_company") and re.search(r"[A-Za-z]", s):
        out["insurance_company"] = s


def parse_details(text: str) -> dict:
    """Labeled parse first, then infer any UNLABELED lines by content — so both
    "Name: Josue Pavon" and a bare "Josue Pavon" line work. Labeled values win."""
    out = parse_labeled(text)
    for line in (text or "").splitlines():
        if line.strip() and not _LABEL_RE.search(line):
            _infer_unlabeled_line(line, out)
    return out
