"""Local, send-time capture of client fields from the PDF filename + typed notes.

Independent registration: every transaction must carry the client's name,
price, phone, and email WITHOUT depending on a matching lead in the Issuer
Supabase. These parsers use only what the issuer already provided; the lead
join remains an optional gap-filler at read time.

Pure functions — no network, unit-testable.
"""
from __future__ import annotations

import re
from pathlib import PurePath
from typing import Optional

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# NANP: area + exchange can't start with 0/1 — avoids VIN digit-runs and serials.
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?1[\s.\-]?)?\(?([2-9]\d{2})\)?[\s.\-]?([2-9]\d{2})[\s.\-]?(\d{4})(?!\d)"
)

_PRICE_RE = re.compile(r"\$\s*(\d{1,6}(?:,\d{3})*(?:\.\d{1,2})?)")
_PRICE_LABEL_RE = re.compile(r"\bprice\s*[:=]?\s*\$?\s*(\d{1,6}(?:\.\d{1,2})?)", re.IGNORECASE)

# NJ temp-tag serials print as CONTIGUOUS 10-digit runs starting with 9.
# Punctuated numbers like "(973) 555-1234" are phones, never serials, so only
# unbroken digit runs are blanked before phone matching.
_NJ_SERIAL_RE = re.compile(r"(?<!\d)9\d{9}(?!\d)")

_NAME_NOISE_TOKENS = {"tag", "temp", "pdf", "copy", "scan", "final", "new", "client"}
_NAME_TOKEN_RE = re.compile(r"^[A-Za-z][A-Za-z.'\-]*$")
# Reference tokens: 8 alnum chars containing at least one digit AND one letter
# (pure-alpha 8-char tokens like the surname CASTILLO are kept).
_REF_TOKEN_RE = re.compile(r"^(?=.*[0-9])(?=.*[A-Za-z])[A-Za-z0-9]{8}$")
_COUNTER_RE = re.compile(r"^\(\d+\)$")


def normalize_us_phone(raw: str | None) -> Optional[str]:
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"+1{digits}"
    return None


def extract_client_phone(text: str | None) -> Optional[str]:
    cleaned = _NJ_SERIAL_RE.sub(" ", str(text or ""))
    m = _PHONE_RE.search(cleaned)
    if m:
        return normalize_us_phone("".join(m.groups()))
    return None


def extract_client_email(text: str | None) -> Optional[str]:
    m = _EMAIL_RE.search(str(text or ""))
    return m.group(0) if m else None


def extract_price(text: str | None) -> Optional[str]:
    s = str(text or "")
    m = _PRICE_RE.search(s)
    if not m:
        m = _PRICE_LABEL_RE.search(s)
    if not m:
        return None
    return m.group(1).replace(",", "")


def _looks_like_name(candidate: str) -> Optional[str]:
    c = " ".join(str(candidate or "").split()).strip()
    if not c or len(c) > 40:
        return None
    if _EMAIL_RE.search(c) or any(ch.isdigit() for ch in c):
        return None
    tokens = c.split()
    if not (2 <= len(tokens) <= 5):
        return None
    if not all(_NAME_TOKEN_RE.match(t) for t in tokens):
        return None
    return " ".join(t if t.isupper() and len(t) == 1 else t.capitalize() for t in tokens)


def extract_client_name(filename: str | None, client_details: str = "") -> Optional[str]:
    stem = PurePath(str(filename or "")).stem
    stem = stem.replace("_", " ").replace("-", " ")
    kept = []
    for tok in stem.split():
        low = tok.lower()
        if _REF_TOKEN_RE.match(tok):
            continue
        if tok.isdigit() or _COUNTER_RE.match(tok):
            continue
        if low in _NAME_NOISE_TOKENS:
            continue
        kept.append(tok)
    name = _looks_like_name(" ".join(kept))
    if name:
        return name
    # Fallback: the Issuer intake format puts the client name on notes line 1.
    for line in str(client_details or "").splitlines():
        if line.strip():
            return _looks_like_name(line)
    return None


def capture_client_fields(
    filename: str | None,
    client_details: str | None,
    recipient_name: str | None,
    recipient_email: str | None,
) -> dict:
    """All four independent-registration fields, best-effort from local data."""
    details = str(client_details or "")
    email = extract_client_email(details)
    # Direct Email-Client sends: the recipient IS the client.
    if not email and str(recipient_name or "").startswith("Client 📧"):
        email = (recipient_email or "").strip() or None
    return {
        "client_name": extract_client_name(filename, details),
        "price": extract_price(details),
        "client_phone": extract_client_phone(details),
        "client_email": email,
    }
