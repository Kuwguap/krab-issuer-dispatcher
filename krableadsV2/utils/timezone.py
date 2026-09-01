"""New York time, in one place.

This office runs on New York time. Every date a client can read -- a tag's issue
and expiry, an insurance card's effective date -- is a New York date, and every
timestamp this bot writes means a New York moment.

The database does not agree by default, and that is the whole reason this module
exists. `issue_date` is a Postgres ``timestamptz``: the bot writes
``datetime.now(NY).isoformat()``, Postgres stores the instant, and PostgREST
hands it back normalised to ``+00:00``. Calling ``.date()`` on what comes back
therefore yields the UTC calendar date -- and between 8pm and midnight in New
York that is TOMORROW. Tags issued on an August evening printed a September
issue date and a September expiry, on a legal document, every night.

So: convert before you take a date, and never take a naive local one. `ny_today`
is the only correct "today" in this codebase, because the servers run in UTC.
"""
from __future__ import annotations

from datetime import date, datetime

import pytz

NY_TZ = pytz.timezone("America/New_York")


def ny_now() -> datetime:
    """The current moment, as New York sees it."""
    return datetime.now(NY_TZ)


def ny_today() -> date:
    """Today's date in New York.

    Not ``date.today()``: the servers run in UTC, so after 8pm Eastern that
    answers with tomorrow.
    """
    return ny_now().date()


def to_ny(value):
    """``value`` as a New York-aware datetime, or None if it is not a time.

    Accepts a datetime or an ISO string (including the trailing ``Z`` and the
    ``YYYY-MM-DD HH:MM:SS`` shape PostgREST sometimes returns). An AWARE value is
    converted -- that is the fix for timestamptz coming back as UTC. A NAIVE one
    is taken to be New York already, because every naive timestamp this codebase
    writes was written from New York's point of view.
    """
    dt = value
    if dt is None:
        return None
    if not isinstance(dt, datetime):
        s = str(dt).strip()
        if not s:
            return None
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        for candidate in (s, s.replace(" ", "T", 1), s[:19], s[:19].replace(" ", "T", 1)):
            try:
                dt = datetime.fromisoformat(candidate)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        return NY_TZ.localize(dt)
    return dt.astimezone(NY_TZ)


def ny_date(value):
    """The New York calendar date of ``value``, or None.

    The one to reach for whenever a date is being PRINTED. ``dt.date()`` on a
    row straight out of the database is the bug this exists to prevent.
    """
    dt = to_ny(value)
    return dt.date() if dt else None
