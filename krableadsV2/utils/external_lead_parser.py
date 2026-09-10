"""Parse external 'New Lead' labeled text into krableadsV2 phase-1 state."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from utils.lead_validation import normalize_phone

VIN_PATTERN = re.compile(r"\b[A-Za-z0-9]{17}\b")

# Longest keys first so "Registration address" wins over "address" substrings.
_LABEL_KEYS: Tuple[Tuple[str, str], ...] = (
    ("registration address", "registration_address"),
    ("delivery address", "delivery_address_full"),
    ("delivery email", "email"),
    ("delivery method", "delivery_method"),
    ("policy #", "insurance_policy_number"),
    ("policy number", "insurance_policy_number"),
    ("order #", "external_order_id"),
    ("order", "external_order_id"),
    ("customer", "name"),
    ("phone", "phone"),
    ("vehicle", "vehicle"),
    ("insurance", "insurance_company"),
    ("service", "service"),
    ("price", "price"),
    ("vin", "vin"),
)

# The website's one signal that the customer PAID for our insurance. The
# ingest message carries this exact line for a "Plate + Insurance" or
# "Insurance Only" order and no Coverage line at all for a tag-only order
# (server/krableads-ingest.js in speedy-tags). Case and spacing are forgiven;
# the words are not -- anything else is not a paid cover.
PAID_COVERAGE_LINE = "Coverage: TriState insurance (paid)"
_COVERAGE_LINE_RE = re.compile(r"^coverage\s*:(.*)$", re.I)


def coverage_is_paid_tristate(value: Any) -> bool:
    """True only for the Coverage value "TriState insurance (paid)"."""
    squashed = re.sub(r"\s+", "", str(value or "")).casefold()
    return squashed == "tristateinsurance(paid)"


SAMPLE_MESSAGE = """🆕 New Lead
Order #bf6923ca
Customer: Zebin Fang Fang
Phone: 2138622301
Delivery email: zebinfang1002@gmail.com
Delivery method: Email Delivery
Registration address: 28 brookside rd quincy MA 02169
Delivery address: 28 brookside rd quincy MA 02169
VIN: SCA665C56HUX86704
Vehicle: 2017 ROLLS-ROYCE Wraith, black
Insurance: AC Insurance
Policy #: 279-06071-913
Service: 30-Day NJ Temp Tag
Price: $150.00"""


def _split_us_address(full: str) -> Tuple[str, str]:
    """Split a one-line US address into (street, city_state_zip)."""
    raw = (full or "").strip()
    if not raw:
        return "-", "-"
    zip_match = re.search(r"\b(\d{5})(?:-\d{4})?\b\s*$", raw)
    if not zip_match:
        return raw, "-"
    zip_code = zip_match.group(1)
    before = raw[: zip_match.start()].strip().rstrip(",")
    parts = [p for p in before.split() if p]
    if len(parts) >= 2 and re.fullmatch(r"[A-Za-z]{2}", parts[-1]):
        state = parts[-1].upper()
        if len(parts) >= 3:
            city = parts[-2]
            street = " ".join(parts[:-2])
        else:
            city = "-"
            street = parts[0] if parts else before
        csz = f"{city}, {state} {zip_code}"
        return (street or "-"), csz
    if len(parts) >= 2:
        city = parts[-1]
        street = " ".join(parts[:-1])
        return (street or "-"), f"{city}, {zip_code}"
    return before or "-", zip_code


def _parse_labeled_fields(text: str) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("🆕"):
            continue
        lower = line.lower()
        if lower.startswith("order #") or lower.startswith("order#"):
            val = re.sub(r"^order\s*#?\s*", "", line, flags=re.I).strip().lstrip("#").strip()
            if val:
                fields["external_order_id"] = val
            continue
        cov = _COVERAGE_LINE_RE.match(line)
        if cov:
            fields["coverage"] = cov.group(1).strip()
            continue
        for label, key in _LABEL_KEYS:
            prefix = f"{label}:"
            if lower.startswith(prefix):
                val = line[len(label) + 1 :].strip()
                if key == "external_order_id":
                    val = val.lstrip("#").strip()
                fields[key] = val
                break
    return fields


def _split_vehicle(vehicle: str) -> Tuple[str, str]:
    raw = (vehicle or "").strip()
    if not raw:
        return "-", "-"
    if "," in raw:
        car, color = raw.rsplit(",", 1)
        return car.strip() or "-", color.strip() or "-"
    return raw, "-"


def _apply_single_address_as_both(state: Dict[str, Any]) -> None:
    def _has(v: str) -> bool:
        return bool(v and str(v).strip() and str(v).strip() != "-")

    addr = (state.get("address") or "").strip()
    csz = (state.get("city_state_zip") or "").strip()
    daddr = (state.get("delivery_address") or "").strip()
    dcsz = (state.get("delivery_city_state_zip") or "").strip()
    has_reg = _has(addr) or _has(csz)
    has_del = _has(daddr) or _has(dcsz)
    if has_reg and not has_del:
        state["delivery_address"] = addr or "-"
        state["delivery_city_state_zip"] = csz or "-"
    elif has_del and not has_reg:
        state["address"] = daddr or "-"
        state["city_state_zip"] = dcsz or "-"


def _build_extra_info(fields: Dict[str, str]) -> str:
    parts: List[str] = []
    if fields.get("external_order_id"):
        parts.append(f"Order #{fields['external_order_id']}")
    if fields.get("delivery_method"):
        parts.append(f"Delivery method: {fields['delivery_method']}")
    if fields.get("service"):
        parts.append(f"Service: {fields['service']}")
    return " | ".join(parts) if parts else "-"


def build_vehicle_details_11(state: Dict[str, Any]) -> str:
    return "\n".join([
        state.get("name", "-"),
        state.get("address", "-"),
        state.get("city_state_zip", "-"),
        state.get("delivery_address", "-"),
        state.get("delivery_city_state_zip", "-"),
        state.get("vin", "-"),
        state.get("car", "-"),
        state.get("color", "-"),
        state.get("insurance_company", "-"),
        state.get("insurance_policy_number", "-"),
        state.get("extra_info", "-"),
    ])


def build_delivery_details(state: Dict[str, Any]) -> str:
    lines = [
        state.get("delivery_address", "-"),
        state.get("delivery_city_state_zip", "-"),
    ]
    return "\n".join(l for l in lines if l and l != "-") or "-"


def parse_external_lead_message(text: str) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """Parse labeled New Lead text. Returns (state_dict, errors)."""
    fields = _parse_labeled_fields(text or "")
    return parse_external_lead_fields(fields)


def _normalize_field_key(key: str) -> str:
    k = (key or "").strip().lower().replace("_", " ")
    mapping = {
        "name": "name",
        "customer": "name",
        "phone": "phone",
        "price": "price",
        "vin": "vin",
        "vehicle": "vehicle",
        "email": "email",
        "delivery email": "email",
        "insurance": "insurance_company",
        "insurance company": "insurance_company",
        "policy #": "insurance_policy_number",
        "policy number": "insurance_policy_number",
        "service": "service",
        "delivery method": "delivery_method",
        "registration address": "registration_address",
        "delivery address": "delivery_address_full",
        "external order id": "external_order_id",
        "order id": "external_order_id",
    }
    return mapping.get(k, k.replace(" ", "_"))


# ------------------------------------------------ an email typed into an address
#
# Lead 1K3AX0XS came from a checkout with an empty email and the customer's
# email typed at the end of the address ("113, East 84th Street, ..., 10028,
# United States, Josue_jeanette@icloud.com"). Carried in the address it rode
# into every driver offer, and its lone "_" broke the offer's Markdown, so no
# driver got it. On a website/API ingest an email typed into an address is an
# email: it becomes the lead's email if there is none, and it always leaves
# the address text. See move_address_emails_to_email.
#
# Conservative on purpose: name@a.dotted.domain ending in a 2-24 letter TLD,
# not run straight on into more address characters. "Unit @ rear" and
# "x@localhost" are not emails and stay exactly where they are.
_ADDRESS_EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+\-@])"
    r"[A-Za-z0-9._%+\-]+@"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?\.)+[A-Za-z]{2,24}"
    r"(?![A-Za-z0-9\-@]|\.[A-Za-z0-9])"
)
# An "Email:" / "e-mail address -" label left in front of a removed email.
_EMAIL_LABEL_TAIL_RE = re.compile(r"\be-?mail(?:\s+address)?\s*[:\-]?[ \t]*$", re.I)
# What joined an email to the address, cut out with it. Newlines included, so
# "5 Main St,\na@b.com" loses its comma too; "/" and "-" so " / a@b.com" and
# " - a@b.com" leave no stray separator behind.
_ADDRESS_JOIN_CHARS = " \t\r\n,;|/-"
_EMAIL_WRAPPERS = {"(": ")", "<": ">", "[": "]"}


def _unwrap_email(left: str, right: str) -> Tuple[str, str]:
    """Drop a bracket pair round a removed email: "(" + ... + ")"."""
    left_bare, right_bare = left.rstrip(" \t"), right.lstrip(" \t")
    if (left_bare and right_bare and left_bare[-1] in _EMAIL_WRAPPERS
            and right_bare[0] == _EMAIL_WRAPPERS[left_bare[-1]]):
        return left_bare[:-1], right_bare[1:]
    return left, right
# An address line 2 / apartment field, spelled as _normalize_field_key spells
# it: "Address line 2", "address2", "Apartment", "Apt #", "Delivery unit"...
_ADDRESS_LINE2_KEY_RE = re.compile(
    r"^(?:(?:delivery|registration)_)?"
    r"(?:address_?line_?2|address_?2|apartment|apt|unit|suite)"
    r"(?:_?(?:number|num|no|#))?$"
)


def pull_emails_from_address(text: Any) -> Tuple[str, List[str]]:
    """Split one address text into (the address without emails, the emails).

    Each email leaves with the comma or space that joined it -- and an
    "Email:" label or brackets round it -- so "..., United States, a_b@c.com"
    becomes "..., United States" and "5 Main St, a@b.com, Apt 2D" becomes
    "5 Main St, Apt 2D". Text holding no email (no "@" at all, or only a
    stray one) comes back exactly as given.
    """
    raw = "" if text is None else str(text)
    if "@" not in raw:
        return raw, []
    out = raw
    emails: List[str] = []
    match = _ADDRESS_EMAIL_RE.search(out)
    while match:
        emails.append(match.group(0))
        left, right = _unwrap_email(out[: match.start()], out[match.end():])
        left = _EMAIL_LABEL_TAIL_RE.sub("", left)
        # "(Email: a@b.com)": the brackets sit outside the label.
        left, right = _unwrap_email(left, right)
        left_kept = left.rstrip(_ADDRESS_JOIN_CHARS)
        right_kept = right.lstrip(_ADDRESS_JOIN_CHARS + ".")
        joint = left[len(left_kept):] + right[: len(right) - len(right_kept)]
        if not left_kept or not right_kept:
            glue = ""
        elif "\n" in joint:
            glue = "\n"
        elif "," in joint:
            glue = ", "
        else:
            glue = " "
        out = left_kept + glue + right_kept
        match = _ADDRESS_EMAIL_RE.search(out)
    if not emails:
        return raw, []
    return out.strip(), emails


def _address_rank(raw_key: Any) -> Optional[int]:
    """0 delivery address, 1 registration address, 2 a line 2 / apartment
    field, None for anything that is not an address."""
    nk = _normalize_field_key(str(raw_key))
    if nk == "delivery_address_full":
        return 0
    if nk == "registration_address":
        return 1
    if _ADDRESS_LINE2_KEY_RE.match(nk):
        return 2
    return None


def move_address_emails_to_email(fields: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """A copy of ``fields`` in which no address carries an email.

    Looks in the delivery address, the registration address and any address
    line 2 / apartment field -- keys matched the way parse_external_lead_fields
    matches them, so a raw ``fields`` body and _parse_labeled_fields output
    both work. Every email found is cut out of its text. The first one found
    becomes the lead's email only when the lead has none: an email already on
    the lead (anything holding an "@") always wins. A placeholder in the email
    field -- "-", "N/A", "none" -- is not an email and gives way, or the moved
    email would be lost altogether. A text left empty is dropped so the other address
    stands in for it; when no address is left at all the lead keeps a "-"
    address rather than being refused, because it was accepted before.
    ``fields`` itself is never changed.

    Only utils/lead_ingest.py calls this (new website/API leads). The parse_*
    functions are unchanged for every other caller.
    """
    work: Dict[str, Any] = dict(fields or {})
    keys = [k for k in work if _address_rank(k) is not None]
    keys.sort(key=_address_rank)  # stable: delivery, registration, line 2
    found: List[str] = []
    emptied: List[Any] = []
    for key in keys:
        if work[key] is None:
            continue
        kept, emails = pull_emails_from_address(work[key])
        if not emails:
            continue
        found.extend(emails)
        work[key] = kept if kept.strip() else ""
        if not kept.strip():
            emptied.append(key)
    if not found:
        return work
    emptied_main = [k for k in emptied if _address_rank(k) < 2]
    if emptied_main and not any(
            str(work[k] or "").strip() for k in keys if _address_rank(k) < 2):
        work[emptied_main[0]] = "-"
    email_keys = [k for k in work if _normalize_field_key(str(k)) == "email"]
    if any("@" in str(work[k] or "") for k in email_keys):
        return work  # an email already on the lead wins
    for k in email_keys:
        work[k] = ""  # a placeholder ("-", "N/A"), not an email
    work["email"] = found[0]
    return work


def parse_external_lead_fields(fields: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """Build phase-1 state from labeled field dict. Returns (state_dict, errors)."""
    errors: List[str] = []
    normalized: Dict[str, str] = {}
    for raw_key, raw_val in (fields or {}).items():
        if raw_val is None or not str(raw_val).strip():
            continue
        nk = _normalize_field_key(str(raw_key))
        normalized[nk] = str(raw_val).strip()
    f = normalized

    name = f.get("name", "")
    if not name:
        errors.append("Customer is required")

    phone_raw = f.get("phone", "")
    phone = normalize_phone(phone_raw)
    if not phone:
        errors.append("Phone is required (9–10 digit US number)")

    price = f.get("price", "")
    if not price or "$" not in price or not re.search(r"\d", price):
        errors.append("Price is required (must include $ and a number)")

    vin_raw = f.get("vin", "")
    vin_match = VIN_PATTERN.search(vin_raw)
    vin = vin_match.group(0).upper() if vin_match else ""
    if not vin:
        errors.append("VIN is required (17 characters)")

    vehicle_raw = f.get("vehicle", "")
    if not vehicle_raw:
        errors.append("Vehicle is required")
    car, color = _split_vehicle(vehicle_raw)

    reg_full = f.get("registration_address", "")
    del_full = f.get("delivery_address_full", "")
    if not reg_full and not del_full:
        errors.append("Registration address or Delivery address is required")

    address, city_state_zip = _split_us_address(reg_full) if reg_full else ("-", "-")
    delivery_address, delivery_city_state_zip = (
        _split_us_address(del_full) if del_full else ("-", "-")
    )

    state: Dict[str, Any] = {
        "name": name or "-",
        "address": address,
        "city_state_zip": city_state_zip,
        "delivery_address": delivery_address,
        "delivery_city_state_zip": delivery_city_state_zip,
        "vin": vin,
        "car": car,
        "color": color,
        "insurance_company": f.get("insurance_company", "-") or "-",
        "insurance_policy_number": f.get("insurance_policy_number", "-") or "-",
        "extra_info": _build_extra_info(f),
        "pending_phone_number": phone,
        "pending_price": price,
        "email": f.get("email") or None,
        "external_order_id": f.get("external_order_id") or None,
        # Only a paid cover arms our insurance on a website order.
        "wants_insurance": coverage_is_paid_tristate(f.get("coverage")),
    }
    _apply_single_address_as_both(state)
    state["vehicle_details"] = build_vehicle_details_11(state)
    state["delivery_details"] = build_delivery_details(state)

    if errors:
        return None, errors
    return state, []
