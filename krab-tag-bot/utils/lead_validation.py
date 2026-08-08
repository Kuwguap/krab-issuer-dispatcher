"""Shared phone/price validation for bot and external lead ingest."""
from __future__ import annotations

import re

PHONE_PRICE_PLACEHOLDERS = frozenset(
    ("-", "—", "–", "n/a", "na", "none", "null", "?", "unknown", "tbd", "pending")
)


def normalize_phone(raw: str) -> str:
    """Return 10-digit US phone or empty string."""
    p = (raw or "").strip()
    if not p or p.lower() in PHONE_PRICE_PLACEHOLDERS:
        return ""
    d = re.sub(r"\D", "", p)
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    if len(d) not in (9, 10) or not d.isdigit():
        return ""
    return d


def is_valid_pending_phone(raw) -> bool:
    return bool(normalize_phone(str(raw or "")))


def is_valid_pending_price(raw) -> bool:
    p = (raw or "").strip()
    if not p or p.lower() in PHONE_PRICE_PLACEHOLDERS:
        return False
    return bool("$" in p and re.search(r"\d", p))
