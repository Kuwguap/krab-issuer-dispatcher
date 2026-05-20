"""Parse user-supplied datetimes for appointments and scheduled announcements."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

import pytz


def parse_user_datetime(text: str, tz_name: str) -> Optional[datetime]:
    """
    Parse strings like ``2026-05-25 14:30``, ``5/25/2026 2:30pm``, ``tomorrow 3pm``.
    Returns timezone-aware datetime in ``tz_name``, or None.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        tz = pytz.timezone(tz_name)
    except Exception:
        tz = pytz.timezone("America/New_York")

    lower = raw.lower()
    now = datetime.now(tz)

    if lower.startswith("tomorrow"):
        base = now + __import__("datetime").timedelta(days=1)
        rest = lower.replace("tomorrow", "").strip()
        if not rest:
            return base.replace(hour=9, minute=0, second=0, microsecond=0)
        raw = rest

    patterns = [
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%m/%d/%Y %H:%M",
        "%m/%d/%Y %I:%M %p",
        "%m/%d/%Y %I:%M%p",
        "%Y/%m/%d %H:%M",
    ]
    for fmt in patterns:
        try:
            dt = datetime.strptime(raw, fmt)
            return tz.localize(dt)
        except ValueError:
            continue

    m = re.match(
        r"^(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\s+(\d{1,2}):(\d{2})\s*(am|pm)?$",
        lower,
    )
    if m:
        mo, da, yr, hr, mi, ap = m.groups()
        year = int(yr)
        if year < 100:
            year += 2000
        hour = int(hr)
        minute = int(mi)
        if ap == "pm" and hour < 12:
            hour += 12
        if ap == "am" and hour == 12:
            hour = 0
        try:
            dt = datetime(year, int(mo), int(da), hour, minute)
            return tz.localize(dt)
        except ValueError:
            return None

    return None


def format_dt_display(dt: datetime, tz_name: str) -> str:
    try:
        tz = pytz.timezone(tz_name)
    except Exception:
        tz = pytz.timezone("America/New_York")
    if dt.tzinfo is None:
        dt = tz.localize(dt)
    else:
        dt = dt.astimezone(tz)
    return dt.strftime("%Y-%m-%d %H:%M %Z")
