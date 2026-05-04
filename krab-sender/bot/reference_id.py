"""Extract Krab Issuer–style 8-character reference IDs from filenames and client notes."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

# Issuer uses secrets from uppercase A-Z + digits (see krableadsV2 generate_reference_id).
_REF8_VALID = re.compile(r"^[A-Z0-9]{8}$")

# Start of basename: REF8 then end / sep / space (case-insensitive).
_PREFIX_ICASE = re.compile(r"^([A-Za-z0-9]{8})(?:$|[_\-\s\.])", re.IGNORECASE)

# Any isolated 8-character alphanumeric token (case-insensitive → normalized to upper).
_REF8_TOKEN = re.compile(r"(?<![A-Za-z0-9])([A-Za-z0-9]{8})(?![A-Za-z0-9])")

# What users paste from Issuer / Telegram (emoji, backticks, HTML-style rarely).
_LABELED_REF_PATTERNS = [
    # 📋 Ref ID: `AB12CD34` or 📋 Reference ID: AB12CD34
    re.compile(
        r"📋\s*Ref(?:erence)?(?:\s+ID)?\s*:\s*`?\s*([A-Za-z0-9]{8})\s*`?",
        re.IGNORECASE,
    ),
    re.compile(r"📋\s*Ref\s*ID\s*:\s*`?\s*([A-Za-z0-9]{8})\s*`?", re.IGNORECASE),
    re.compile(r"🆔\s*Reference\s*ID\s*:\s*<?\s*([A-Za-z0-9]{8})`?", re.IGNORECASE),
    re.compile(r"Reference\s*:\s*`?\s*([A-Za-z0-9]{8})\s*`?", re.IGNORECASE),
    re.compile(r"Ref\s*ID\s*:\s*`?\s*([A-Za-z0-9]{8})\s*`?", re.IGNORECASE),
    # Line starts common in copied messages
    re.compile(
        r"(?:^|[\n\r])[^\n]*?(?:reference|ref)\s*(?:id)?\s*[:#=]\s*`?\s*([A-Za-z0-9]{8})",
        re.IGNORECASE,
    ),
    # Backtick code only
    re.compile(r"`([A-Za-z0-9]{8})`"),
]


def _normalize_ref_token(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    u = raw.strip().upper()
    if len(u) != 8:
        return None
    if not _REF8_VALID.match(u):
        return None
    return u


def extract_reference_id_from_filename(file_name: str) -> Optional[str]:
    """First strong match from filename alone (prefix, then any token in stem)."""
    c = collect_reference_candidates(file_name, None)
    return c[0] if c else None


def extract_reference_id_from_client_details(text: str) -> Optional[str]:
    if not (text or "").strip():
        return None
    c = collect_reference_candidates("", text)
    return c[0] if c else None


def collect_reference_candidates(file_name: str, client_details: Optional[str] = None) -> list[str]:
    """
    Ordered unique refs: filename tokens first, then labeled lines in client notes, then full-text tokens.
    Works when Telegram renames files to lowercase or users paste Issuer lines with emoji/backticks.
    """
    ordered: list[str] = []
    seen: set[str] = set()

    def add_raw(raw: Optional[str]) -> None:
        tok = _normalize_ref_token(raw)
        if tok and tok not in seen:
            seen.add(tok)
            ordered.append(tok)

    def scan_tokens(text: Optional[str]) -> None:
        if not text:
            return
        for m in _REF8_TOKEN.finditer(text):
            add_raw(m.group(1))

    stem = Path(file_name or "").stem.strip()
    if stem:
        pm = _PREFIX_ICASE.match(stem)
        if pm:
            add_raw(pm.group(1))
        scan_tokens(stem)

    # Full filename string (some clients embed ref before extension oddly)
    scan_tokens(file_name or "")

    cd = client_details or ""
    if cd.strip():
        for pat in _LABELED_REF_PATTERNS:
            for m in pat.finditer(cd):
                add_raw(m.group(1))
        scan_tokens(cd)

    return ordered
