r"""Reading "when is it due" out of what somebody typed.

The rule the module is built around: refuse rather than guess. A refusal costs
one more tap; a wrong guess puts a promise on a lead that nobody made and then
marks it late for missing it. So the refusal table below matters as much as the
acceptance one — and every refusal has to say what to type instead.

Run:  venv\Scripts\python.exe -m pytest tests/test_due_time_parsing.py -q
"""
import os
import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy-key")

from utils.due_time import parse_due  # noqa: E402
from utils.timezone import NY_TZ  # noqa: E402

# A Wednesday, mid-afternoon, well inside Eastern Daylight Time.
NOW = NY_TZ.localize(datetime(2026, 9, 9, 14, 30))


def at(text, now=NOW):
    return parse_due(text, now=now)


def ny(iso):
    return datetime.fromisoformat(iso).astimezone(NY_TZ)


class ItReadsATimeTest(unittest.TestCase):

    def test_the_forms_people_actually_type(self):
        for text, (mo, d, h, mi) in {
            "3pm": (9, 9, 15, 0),
            "3 PM": (9, 9, 15, 0),
            "2:30pm": (9, 9, 14, 30),
            "tomorrow 3pm": (9, 10, 15, 0),
            "tomorrow 2:30 pm": (9, 10, 14, 30),
            "tmrw 9am": (9, 10, 9, 0),
            "today 5pm": (9, 9, 17, 0),
            "9/10 2pm": (9, 10, 14, 0),
            "9-10 2pm": (9, 10, 14, 0),
            "2026-09-10 14:00": (9, 10, 14, 0),
            "sep 10 2pm": (9, 10, 14, 0),
            "september 10 at 2pm": (9, 10, 14, 0),
            "15:00": (9, 9, 15, 0),
            "at 4pm": (9, 9, 16, 0),
            "by 4:45pm": (9, 9, 16, 45),
            "friday 10am": (9, 11, 10, 0),
        }.items():
            iso, note = at(text)
            self.assertIsNotNone(iso, "%s -> %s" % (text, note))
            got = ny(iso)
            self.assertEqual((mo, d, h, mi),
                             (got.month, got.day, got.hour, got.minute), text)

    def test_midnight_and_noon_are_the_right_way_round(self):
        iso, _ = at("tomorrow 12am")
        self.assertEqual(0, ny(iso).hour)
        iso, _ = at("tomorrow 12pm")
        self.assertEqual(12, ny(iso).hour)

    def test_in_two_hours(self):
        iso, _ = at("in 2 hours")
        self.assertEqual((16, 30), (ny(iso).hour, ny(iso).minute))
        iso, _ = at("in 45 minutes")
        self.assertEqual((15, 15), (ny(iso).hour, ny(iso).minute))

    def test_a_weekday_means_the_next_one_never_today(self):
        """Said on a Wednesday, 'wednesday' is the one coming."""
        iso, _ = at("wednesday 9am")
        self.assertEqual(16, ny(iso).day)

    def test_everything_comes_back_as_new_york(self):
        iso, note = at("tomorrow 3pm")
        self.assertEqual(15, ny(iso).hour)
        self.assertIn("ET", note)


class ItSaysWhenItRolledForwardTest(unittest.TestCase):
    """A bare hour that has gone by means tomorrow — and must say so, because a
    lead quietly promised for tomorrow is how a wrong 'late' is born."""

    def test_an_hour_already_past_rolls_and_announces_it(self):
        iso, note = at("9am")
        self.assertEqual(10, ny(iso).day)
        self.assertIn("Tomorrow", note)

    def test_an_hour_still_ahead_stays_today_and_does_not(self):
        iso, note = at("6pm")
        self.assertEqual(9, ny(iso).day)
        self.assertNotIn("Tomorrow", note)

    def test_a_few_minutes_late_still_means_today(self):
        """Typing '2:30pm' at 2:32 means now, not this time tomorrow."""
        now = NY_TZ.localize(datetime(2026, 9, 9, 14, 32))
        iso, note = at("2:30pm", now=now)
        self.assertEqual(9, ny(iso).day)
        self.assertNotIn("Tomorrow", note)

    def test_an_explicit_day_is_never_rolled(self):
        iso, note = at("today 6pm")
        self.assertEqual(9, ny(iso).day)
        self.assertNotIn("Tomorrow", note)

    def test_an_explicit_day_in_the_past_is_refused_not_rolled(self):
        """Rolling it would silently turn "today 9am" into a promise for
        tomorrow that nobody made. Only a BARE hour rolls."""
        iso, note = at("today 9am")
        self.assertIsNone(iso)
        self.assertIn("past", note)


class ItRefusesRatherThanGuessTest(unittest.TestCase):

    def _why(self, text):
        iso, note = at(text)
        self.assertIsNone(iso, "%r was accepted as %s" % (text, note))
        self.assertTrue(note and note.strip(), "refused %r with no reason" % text)
        return note

    def test_a_bare_hour_is_two_different_times(self):
        for text in ("3", "9", "11"):
            self._why(text)

    def test_a_colon_does_not_resolve_am_or_pm(self):
        """'9:00' is as much nine in the evening as nine in the morning."""
        self._why("9:00")

    def test_a_date_with_no_hour_says_to_add_one(self):
        for text in ("tomorrow", "9/12", "friday", "sep 10"):
            why = self._why(text)
            self.assertIn("not a time", why, text)

    def test_a_stretch_of_the_day_is_not_a_moment(self):
        for text in ("morning", "afternoon", "end of day", "asap", "whenever"):
            self._why(text)

    def test_two_times_are_a_question_not_an_answer(self):
        why = self._why("3pm or 5pm")
        self.assertIn("more than one time", why)

    def test_a_phone_number_is_not_a_time(self):
        why = self._why("7325550000")
        self.assertIn("phone", why)

    def test_a_year_typed_wrong_is_caught_before_it_lands(self):
        why = self._why("2027-09-10 14:00")
        self.assertIn("month away", why)

    def test_the_past_is_refused(self):
        why = self._why("2026-09-01 14:00")
        self.assertIn("past", why)

    def test_a_sentence_is_refused_before_it_is_mined_for_digits(self):
        self._why("leave it at the gate for the neighbour to collect later on")

    def test_nothing_at_all(self):
        for text in ("", "   ", None):
            iso, note = parse_due(text, now=NOW)
            self.assertIsNone(iso)
            self.assertTrue(note)

    def test_every_refusal_tells_them_what_to_type(self):
        """A refusal that does not say the shape of the answer is a dead end."""
        for text in ("3", "9:00", "morning", "", "tomorrow"):
            why = self._why(text) if text else parse_due(text, now=NOW)[1]
            self.assertRegex(why, r"\d\s*(?:pm|am)|hour|time|when",
                             "%r -> %r" % (text, why))


class ItSurvivesDaylightSavingTest(unittest.TestCase):

    def test_spring_forward(self):
        """2026-03-08 is the US switch. 'tomorrow 3pm' on the 7th is 3pm on the
        8th — not 2pm, which is what adding 24 hours to an aware value gives."""
        now = NY_TZ.localize(datetime(2026, 3, 7, 10, 0))
        iso, _ = parse_due("tomorrow 3pm", now=now)
        got = ny(iso)
        self.assertEqual((3, 8, 15, 0), (got.month, got.day, got.hour, got.minute))

    def test_fall_back(self):
        """2026-11-01 is the switch back."""
        now = NY_TZ.localize(datetime(2026, 10, 31, 10, 0))
        iso, _ = parse_due("tomorrow 3pm", now=now)
        got = ny(iso)
        self.assertEqual((11, 1, 15, 0), (got.month, got.day, got.hour, got.minute))

    def test_the_offset_actually_changes_across_the_switch(self):
        """Proves the two dates really are on opposite sides of the boundary,
        so the tests above are testing what they claim to."""
        a, _ = parse_due("2026-03-07 15:00", now=NY_TZ.localize(datetime(2026, 3, 1)))
        b, _ = parse_due("2026-03-09 15:00", now=NY_TZ.localize(datetime(2026, 3, 1)))
        self.assertNotEqual(ny(a).utcoffset(), ny(b).utcoffset())


class ItAddsNoDependencyTest(unittest.TestCase):

    def test_it_parses_dates_without_dateparser_or_dateutil(self):
        src = (ROOT / "utils" / "due_time.py").read_text(encoding="utf-8")
        for line in src.splitlines():
            head = line.strip()
            if head.startswith(("import ", "from ")):
                self.assertNotIn("dateparser", head, head)
                self.assertNotIn("dateutil", head, head)
        req = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
        self.assertNotIn("dateparser", req)
        self.assertNotIn("python-dateutil", req)

    def test_it_localises_through_the_one_timezone_layer(self):
        """utils/timezone.py:1-17 records the tag-expiry bug that came from
        doing this arithmetic anywhere else."""
        src = (ROOT / "utils" / "due_time.py").read_text(encoding="utf-8")
        self.assertIn("from utils.timezone import ny_now, to_ny", src)
        self.assertNotIn("NY_TZ.localize", src)
        self.assertNotIn("datetime.now()", src)


if __name__ == "__main__":
    unittest.main()
