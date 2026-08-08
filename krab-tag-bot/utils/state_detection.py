"""Detect NY vs NJ insurance card routing from lead address fields."""
from __future__ import annotations

import re
from typing import Any

_STATE_CODE_RE = re.compile(r"\b(NJ|NY)\b", re.IGNORECASE)
_STATE_NAME_RE = re.compile(
    r"\b(NEW\s+JERSEY|NEW\s+YORK)\b",
    re.IGNORECASE,
)


def extract_state_code(text: str) -> str | None:
    """Return ``NJ`` or ``NY`` when found in *text*, else ``None``."""
    s = (text or "").strip()
    if not s:
        return None
    m = _STATE_CODE_RE.search(s)
    if m:
        return m.group(1).upper()
    m = _STATE_NAME_RE.search(s)
    if m:
        name = m.group(1).upper().replace("  ", " ")
        if "JERSEY" in name:
            return "NJ"
        if "YORK" in name:
            return "NY"
    return None


def detect_card_state(lead: dict[str, Any], *, override: str | None = None) -> str:
    """Resolve card state from lead data.

    Priority: explicit *override* → delivery city/state/zip → registration
    city/state/zip → street address → delivery_details blob. Defaults to ``NY``.
    """
    if override in ("NJ", "NY"):
        return override

    vd = (lead.get("vehicle_details") or "").splitlines()

    def _ln(idx: int) -> str:
        return vd[idx].strip() if idx < len(vd) else ""

    candidates = [
        _ln(4),  # delivery_city_state_zip
        _ln(2),  # city_state_zip
        _ln(1),  # address
    ]
    for part in (lead.get("delivery_details") or "").splitlines():
        part = part.strip()
        if part:
            candidates.append(part)

    for text in candidates:
        state = extract_state_code(text)
        if state:
            return state
    return "NY"
