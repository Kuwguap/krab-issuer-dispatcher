"""When a tag is expected to arrive, read out of what somebody typed.

One definition of a due time, shared by the bot's gate and the receipts board's
editor, so the two can never disagree about what "tomorrow 3pm" means.

The rule this module is built around: **refuse rather than guess.** A refusal
costs one more tap. A wrong guess puts a promise on a lead that nobody made and
then marks it late for missing it. So "3" is not a time -- 3am and 3pm are both
plausible -- and neither is "tomorrow", which is a date. Every refusal comes
back with a reason, so the person is told what to type instead of being told no.

Everything goes through utils.timezone.to_ny. The office runs on New York time
and the servers run on UTC; utils/timezone.py:1-17 records the tag-expiry bug
that came from doing this arithmetic anywhere else. And a day is never added to
an aware datetime to mean "the same wall clock tomorrow" -- across a DST switch
that is off by an hour -- the date is rolled and the wall clock rebuilt.

No new dependency: dateparser and python-dateutil are not in requirements.txt
and one command is not the reason to add either.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from utils.timezone import ny_now, to_ny

# A time typed at 3:02 that says "3pm" means today, not tomorrow. Beyond this
# it has genuinely passed and rolls forward -- and the caller says so out loud.
GRACE_MINUTES = 15
# A promise further out than this is a typo'd year, not a plan. Catching it here
# is cheaper than a lead that reads "late" for the next eleven months.
MAX_DAYS_AHEAD = 30
MAX_TEXT = 40

_WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2,
    "wed": 2, "weds": 2, "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}
_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
# Words that name a stretch of the day rather than a moment in it. Naming them
# lets the refusal say what is wrong instead of "I did not understand".
_VAGUE = {
    "morning", "afternoon", "evening", "night", "noon", "midday", "midnight",
    "eod", "end of day", "cob", "asap", "now", "whenever", "anytime",
    "any time", "soon", "later", "today", "tomorrow", "tonight",
}

_TIME_RE = re.compile(
    r"(?<![\d:])(?P<h>\d{1,2})(?::(?P<mi>\d{2}))?\s*"
    r"(?P<ap>a\.?m\.?|p\.?m\.?)?(?![\d:])", re.I)
_REL_RE = re.compile(
    r"^\s*in\s+(?P<n>\d{1,3})\s*(?P<unit>min(?:ute)?s?|hr?s?|hours?|days?)\s*$", re.I)
_ISO_DATE_RE = re.compile(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)")
_US_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?(?!\d)")
_MONTH_DAY_RE = re.compile(
    r"\b(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\.?\s+(\d{1,2})\b", re.I)


def _fmt(dt) -> str:
    """'Wed Sep 10, 3:00 PM ET' — %-I is not portable, so the hour is built."""
    clock = dt.strftime("%I:%M %p").lstrip("0")
    return f"{dt.strftime('%a %b')} {dt.day}, {clock} ET"


def _stamp(day: date, hour: int, minute: int):
    """A wall clock on a day, localised once. Never arithmetic on an aware value."""
    return to_ny(datetime(day.year, day.month, day.day, hour, minute))


def _read_time(text: str):
    """(hour, minute, matched_span) or (None, None, reason)."""
    hits = list(_TIME_RE.finditer(text))
    hits = [m for m in hits if m.group("ap") or ":" in m.group(0)
            or int(m.group("h")) >= 13]
    if not hits:
        return None, None, None
    if len(hits) > 1:
        return None, None, ("that names more than one time — send just the one "
                            "it is due")
    m = hits[0]
    h = int(m.group("h"))
    mi = int(m.group("mi") or 0)
    ap = (m.group("ap") or "").replace(".", "").lower()
    if mi > 59:
        return None, None, "that is not a real time"
    if ap:
        if h < 1 or h > 12:
            return None, None, "that is not a real time"
        if ap == "am":
            h = 0 if h == 12 else h
        else:
            h = 12 if h == 12 else h + 12
    else:
        # No am/pm. Only a 24-hour reading is unambiguous, and only above noon:
        # "9:00" is as much nine in the evening as nine in the morning.
        if h < 13 or h > 23:
            return None, None, None
    return h, mi, m


def _read_day(text: str, today: date):
    """(date, matched_text) or (None, None). Never invents a year backwards."""
    m = _ISO_DATE_RE.search(text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))), m.group(0)
        except ValueError:
            return None, None
    m = _US_DATE_RE.search(text)
    if m:
        mo, dy, yr = int(m.group(1)), int(m.group(2)), m.group(3)
        if yr:
            y = int(yr)
            y += 2000 if y < 100 else 0
        else:
            y = today.year
        try:
            d = date(y, mo, dy)
        except ValueError:
            return None, None
        # No year written and the date has gone by: they mean next year.
        if not yr and d < today:
            try:
                d = date(y + 1, mo, dy)
            except ValueError:
                return None, None
        return d, m.group(0)
    m = _MONTH_DAY_RE.search(text)
    if m:
        mo = _MONTHS[m.group(1).lower()]
        try:
            d = date(today.year, mo, int(m.group(2)))
        except ValueError:
            return None, None
        if d < today:
            d = date(today.year + 1, mo, int(m.group(2)))
        return d, m.group(0)
    low = text.lower()
    if re.search(r"\btomorrow\b|\btmrw\b|\btmr\b|\btom\b", low):
        return today + timedelta(days=1), "tomorrow"
    if re.search(r"\btoday\b|\btonight\b", low):
        return today, "today"
    for name, idx in _WEEKDAYS.items():
        if re.search(r"\b" + name + r"\b", low):
            ahead = (idx - today.weekday()) % 7 or 7   # strictly after today
            return today + timedelta(days=ahead), name
    return None, None


def parse_due(text, *, now=None):
    """(iso, note). iso is None when it was refused, and note says why.

    On success the note is how it was read back -- "Tomorrow, Wed Sep 10,
    3:00 PM ET" -- because a bare time that rolled to tomorrow must say so.
    Rolling silently is exactly what produces a lead that reads late.
    """
    raw = str(text or "").strip()
    if not raw:
        return None, "Say when it is due, like 'tomorrow 3pm'."
    if len(raw) > MAX_TEXT:
        return None, ("that is too long to be a time — try 'tomorrow 3pm' or "
                      "'9/10 2:30pm'")
    now = now or ny_now()
    today = now.date()

    m = _REL_RE.match(raw)
    if m:
        n, unit = int(m.group("n")), m.group("unit").lower()
        if unit.startswith("d"):
            delta = timedelta(days=n)
        elif unit.startswith("m"):
            delta = timedelta(minutes=n)
        else:
            delta = timedelta(hours=n)
        # Elapsed time from now: arithmetic on an aware value is right here,
        # because "in two hours" means two hours, not the same clock tomorrow.
        when = now + delta
        if when > now + timedelta(days=MAX_DAYS_AHEAD):
            return None, "that is more than a month away — check the date"
        return when.isoformat(), _fmt(when)

    low = raw.lower()
    # A run of digits long enough to be a phone number is not a time.
    if re.search(r"\d{7,}", low):
        return None, "that looks like a phone number, not a time"

    h, mi, err = _read_time(raw)
    if err and isinstance(err, str):
        return None, err
    day, day_text = _read_day(raw, today)

    if h is None:
        stripped = low
        for token in (day_text or "",):
            if token:
                stripped = stripped.replace(token.lower(), " ")
        stripped = re.sub(r"\b(on|by|at|due|arrive|arriving|deliver|delivery)\b",
                          " ", stripped).strip(" ,.@")
        if day is not None:
            return None, (f"'{day_text}' is a date, not a time — add the hour, "
                          f"like '{day_text} 3pm'")
        if stripped in _VAGUE or any(w in low for w in ("asap", "whenever", "anytime")):
            return None, ("that is a stretch of the day, not a moment — give an "
                          "hour, like '3pm'")
        return None, ("I could not read a time in that. Try '3pm', "
                      "'tomorrow 2:30pm', or '9/10 14:00'.")

    rolled = False
    if day is None:
        day = today
        cand = _stamp(day, h, mi)
        if cand < now - timedelta(minutes=GRACE_MINUTES):
            # Rebuild on the next date rather than adding a day to an aware
            # value: across a DST switch that would land an hour out.
            day = today + timedelta(days=1)
            rolled = True

    when = _stamp(day, h, mi)
    if when > now + timedelta(days=MAX_DAYS_AHEAD):
        return None, "that is more than a month away — check the date"
    if when < now - timedelta(hours=1):
        return None, "that is in the past — say when it is due to arrive"

    note = _fmt(when)
    if rolled:
        # Said out loud on purpose: they typed a bare hour that had gone by, and
        # a lead quietly promised for tomorrow is how a wrong "late" is born.
        note = "Tomorrow — " + note
    return when.isoformat(), note


# A day word is only ever a QUALIFIER here -- it sharpens a clock reading that
# follows it. On its own it names a date, not a moment.
_DAY_WORD = (r"(?:today|tonight|tomorrow|tmr|tmrw|"
             + "|".join(sorted(_WEEKDAYS, key=len, reverse=True)) + r")")
# 3pm, 3:30pm, 15:00. An hour with no am/pm and no colon is NOT here on purpose:
# "call 3" is not three o'clock.
_CLOCK = r"(?:\d{1,2}\s*(?:a\.?m\.?|p\.?m\.?)|\d{1,2}:\d{2}\s*(?:a\.?m\.?|p\.?m\.?)?)"
_ELAPSED = r"in\s+\d{1,3}\s*(?:min(?:ute)?s?|hrs?|hours?|days?)"

_STATED_RE = re.compile(
    r"(?:(?:by|before|at|around|due|deliver(?:y|ed)?|eta|needs?\s+to\s+be\s+there)\s+)?"
    r"(?:" + _DAY_WORD + r"\s+)?" + _CLOCK
    + r"|" + _ELAPSED,
    re.I,
)
# A run this long is an account number or a phone, whatever else surrounds it.
_LONG_DIGITS_RE = re.compile(r"\d{5,}")


def find_due_in_text(text, *, now=None):
    """(iso, note) for a time STATED in free-form notes, else (None, reason).

    Every candidate goes through parse_due, so a fragment this finds but that
    module refuses is refused here too. Finding is the only new part.
    """
    raw = str(text or "").strip()
    if not raw:
        return None, "nothing written down"
    # Work line by line: a time on one line and a date on another are not one
    # answer, and joining them invents a promise.
    for line in re.split(r"[\n;\u2022|]+", raw):
        line = line.strip()
        if not line:
            continue
        for m in _STATED_RE.finditer(line):
            frag = m.group(0).strip(" .,-\u2013\u2014")
            if not frag or _LONG_DIGITS_RE.search(frag):
                continue
            # Strip the lead-in word: parse_due reads "3pm", not "by 3pm".
            frag = re.sub(
                r"^(?:by|before|at|around|due|delivery|delivered|deliver|eta|"
                r"needs?\s+to\s+be\s+there)\s+", "", frag, flags=re.I).strip()
            if not frag:
                continue
            iso, note = parse_due(frag, now=now)
            if iso:
                return iso, note
    return None, "no clear time in the notes"
