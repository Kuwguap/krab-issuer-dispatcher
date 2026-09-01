r"""This office runs on New York time, and so must every date it prints.

The bug these pin: `issue_date` is a Postgres timestamptz. The bot writes it
New York-aware, Postgres keeps the instant, and PostgREST returns it as +00:00.
Taking `.date()` off what comes back gave the UTC calendar date -- so between
8pm and midnight in New York, every tag printed TOMORROW's issue date and
expired a day late. Real leads it happened to are in EVENING_LEADS below.

Run:  venv\Scripts\python.exe -m pytest tests/test_new_york_time.py -q
"""
import os
import sys
import unittest
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy-key")

import bot                                                    # noqa: E402
from utils import tag_pdf                                     # noqa: E402
from utils.timezone import NY_TZ, ny_date, ny_now, ny_today, to_ny   # noqa: E402


# reference -> (what the database returns, the New York date it really was)
EVENING_LEADS = {
    "TVU3M1WR": ("2026-09-01T02:03:14.483849+00:00", date(2026, 8, 31)),
    "6EQJP4D4": ("2026-09-01T01:54:00.069037+00:00", date(2026, 8, 31)),
    "GDPHKCUL": ("2026-09-01T00:37:10.512004+00:00", date(2026, 8, 31)),
    # 4:59pm ET -- same UTC day, so this one was never wrong. It must stay right.
    "53UBM2A1": ("2026-08-31T20:59:56.199242+00:00", date(2026, 8, 31)),
}


class ATagPrintsTheNewYorkDateTest(unittest.TestCase):

    def test_the_evening_leads_no_longer_print_tomorrow(self):
        for ref, (stored, expected) in EVENING_LEADS.items():
            with self.subTest(reference=ref):
                got = bot._dt_from_lead_field(stored)
                self.assertEqual(expected, got.date(),
                                 f"{ref} still prints the UTC date")

    def test_the_whole_evening_window_is_covered(self):
        """8pm to midnight in New York is the window where UTC has already
        rolled over. Every minute of it belongs to the earlier day."""
        for hour in range(20, 24):
            with self.subTest(ny_hour=hour):
                ny = NY_TZ.localize(datetime(2026, 8, 31, hour, 30))
                stored = ny.astimezone(__import__("pytz").UTC).isoformat()
                self.assertEqual(date(2026, 8, 31),
                                 bot._dt_from_lead_field(stored).date())

    def test_a_morning_lead_is_untouched(self):
        stored = "2026-08-31T14:00:00+00:00"          # 10am ET
        self.assertEqual(date(2026, 8, 31), bot._dt_from_lead_field(stored).date())

    def test_the_expiry_moves_with_the_issue_date(self):
        """The expiry is printed from the issue date, so an issue date a day
        late took the expiry with it."""
        issued = bot._dt_from_lead_field(EVENING_LEADS["TVU3M1WR"][0]).date()
        expires = issued + __import__("datetime").timedelta(days=29)
        self.assertEqual(date(2026, 8, 31), issued)
        self.assertEqual(date(2026, 9, 29), expires)


class TheHelpersAgreeAboutNewYorkTest(unittest.TestCase):

    def test_an_aware_utc_stamp_is_converted(self):
        self.assertEqual(date(2026, 8, 31), ny_date("2026-09-01T02:03:14+00:00"))

    def test_a_trailing_z_is_understood(self):
        self.assertEqual(date(2026, 8, 31), ny_date("2026-09-01T02:03:14Z"))

    def test_a_postgrest_space_separated_stamp_is_understood(self):
        self.assertEqual(date(2026, 8, 31), ny_date("2026-09-01 02:03:14+00:00"))

    def test_a_naive_stamp_is_taken_as_new_york(self):
        """Every naive timestamp this codebase writes was written from New
        York's point of view, so re-reading one must not shift it."""
        got = to_ny("2026-08-31T21:00:00")
        self.assertEqual(date(2026, 8, 31), got.date())
        self.assertEqual(21, got.hour)

    def test_rubbish_is_not_a_time(self):
        for junk in (None, "", "   ", "not a date", "-"):
            with self.subTest(value=junk):
                self.assertIsNone(to_ny(junk))
                self.assertIsNone(ny_date(junk))

    def test_now_and_today_agree_with_each_other(self):
        self.assertEqual(ny_now().date(), ny_today())

    def test_today_is_new_yorks_today(self):
        self.assertEqual(datetime.now(NY_TZ).date(), ny_today())


class NoTimestampIsWrittenWithoutAZoneTest(unittest.TestCase):

    def test_nothing_calls_utcnow_any_more(self):
        """utcnow() is naive: nothing downstream can tell which zone it meant.

        Parsed rather than grepped, so the docstring that explains what it
        replaced does not count as a use of it.
        """
        import ast
        for name in ("bot.py", "insurance_card_view.py",
                     "utils/tag_pdf.py", "dispatch_web/tagpdf.py"):
            path = ROOT.joinpath(*name.split("/"))
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            calls = [n for n in ast.walk(tree)
                     if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Attribute)
                     and n.func.attr == "utcnow"]
            with self.subTest(module=name):
                self.assertEqual([], [n.lineno for n in calls],
                                 f"{name} still calls utcnow()")

    def test_the_stamp_helper_carries_a_zone(self):
        stamp = bot._ny_stamp()
        parsed = datetime.fromisoformat(stamp)
        self.assertIsNotNone(parsed.tzinfo, f"{stamp} is naive")
        self.assertEqual(ny_today(), parsed.date())

    def test_the_tag_builder_never_falls_back_to_the_servers_today(self):
        """The servers run in UTC, where today turns over at 8pm Eastern."""
        src = (ROOT / "utils" / "tag_pdf.py").read_text(encoding="utf-8")
        self.assertNotIn("_date.today()", src)
        self.assertIn("ny_today()", src)

    def test_the_card_view_uses_new_york_too(self):
        src = (ROOT / "insurance_card_view.py").read_text(encoding="utf-8")
        self.assertNotIn("return date.today()", src)
        self.assertIn("ny_today()", src)


class TheWebMirrorAgreesWithTheBotTest(unittest.TestCase):
    """dispatch_web prints the same tag; the two must not disagree about the
    day, or the same lead downloads with two different issue dates."""

    def test_both_parsers_give_the_same_new_york_date(self):
        from dispatch_web import tagpdf as web
        for ref, (stored, expected) in EVENING_LEADS.items():
            with self.subTest(reference=ref):
                self.assertEqual(bot._dt_from_lead_field(stored).date(),
                                 web._dt_from_lead_field(stored).date())
                self.assertEqual(expected, web._dt_from_lead_field(stored).date())


if __name__ == "__main__":
    unittest.main()
