r"""Every lead gets a due time without anyone being asked for one.

Asked for: "after sending a lead it asks when is the tag due to reach the client
/ stop sending this message then send the lead (make default 1 hour) unless
stated otherwise in the lead".

Precedence, and each line of it is a decision:

  1. A value already on the payload wins. The picker and the /receipts editor
     still set real promised times, and a default that overwrote them would
     quietly erase somebody's promise.
  2. A time STATED in the lead's own "Delivery Date/Time & Notes".
  3. One hour from now.

The reason (2) is not simply `parse_due(extra_info)`: parse_due answers a
question somebody was ASKED -- short, deliberate, capped at 40 characters. A
real note is prose with a gate code and a phone number in it, so parse_due
refuses nearly all of them and everything silently becomes the default. So the
fragments that look like a stated time are pulled out first and each is handed
to the same parse_due. The strictness did not move; only the search is new.

And the guard that earns its keep: a number is not a time. "Gate code 4432",
"$180", "2 adults", a phone number, a house number and a bare "3" are all
digits that must NOT become a promise the office is then measured against.

This also reverses a decision recorded at the bot's insert -- that extra_info is
deliberately NOT parsed into this column. The note is still stored verbatim and
still shown; what changed is that a time written there seeds the column instead
of being ignored while the issuer was asked the same question again.

Run:  venv\Scripts\python.exe -m pytest tests/test_due_time_default.py -q
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy-key")

import importlib.util  # noqa: E402

# A private copy: many suites replace utils.database.Database with a MagicMock at
# import and never restore it, so a name bound from the module is a mock in a
# full run and passes alone. Same reason as tests/test_suspension_alert_buttons.
_spec = importlib.util.spec_from_file_location(
    "_real_db_for_due_default", ROOT / "utils" / "database.py")
udb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(udb)

from utils.due_time import find_due_in_text  # noqa: E402
from utils.timezone import ny_now, to_ny  # noqa: E402

NOW = to_ny("2026-09-12T14:00:00-04:00")


def _apply(payload):
    return udb.apply_default_expected_delivery(dict(payload))


class ATimeStatedInTheLeadIsHonouredTest(unittest.TestCase):

    def _read(self, text):
        iso, _ = find_due_in_text(text, now=NOW)
        return to_ny(iso) if iso else None

    def test_a_note_that_names_an_hour(self):
        for text, hour in (
            ("Delivery by 5pm, gate code 4432", 17),
            ("ETA 15:00", 15),
            ("deliver at 9:30am", 9),
            ("needs to be there by 6pm", 18),
            ("gate 1234 by 6pm", 18),
        ):
            with self.subTest(text=text):
                got = self._read(text)
                self.assertIsNotNone(got, text)
                self.assertEqual(hour, got.hour, text)

    def test_tomorrow_plus_an_hour_lands_tomorrow(self):
        got = self._read("Deliver tomorrow at 9:30am please")
        self.assertEqual(13, got.day)
        self.assertEqual(9, got.hour)
        self.assertEqual(30, got.minute)

    def test_an_elapsed_time(self):
        got = self._read("client says in 2 hours")
        self.assertEqual(16, got.hour)

    def test_a_weekday_is_read(self):
        got = self._read("friday 9am")
        self.assertEqual(4, got.weekday())
        self.assertEqual(9, got.hour)


class ANumberIsNotATimeTest(unittest.TestCase):
    """The promise-nobody-made failure. Every one of these is a real thing that
    appears in a real note."""

    def test_nothing_here_becomes_a_promise(self):
        for text in (
            "gate code 4432",
            "$180 collected",
            "2 adults, apt 4B",
            "call 3",
            "phone 9734941210",
            "123 Main St, Newark NJ 07102",
            "unit 12",
            "Policy 2035252790",
            "VIN 1HGCM82633A004352",
        ):
            with self.subTest(text=text):
                iso, why = find_due_in_text(text, now=NOW)
                self.assertIsNone(iso, f"{text!r} became {iso}")
                self.assertTrue(why)

    def test_a_day_with_no_hour_is_a_date_not_a_moment(self):
        """Inventing an hour for "tomorrow" is exactly the promise nobody made."""
        for text in ("tomorrow", "today", "friday", "tonight"):
            with self.subTest(text=text):
                self.assertIsNone(find_due_in_text(text, now=NOW)[0])

    def test_vague_words_are_not_times(self):
        for text in ("asap", "whenever", "end of day", "morning"):
            with self.subTest(text=text):
                self.assertIsNone(find_due_in_text(text, now=NOW)[0])

    def test_nothing_written_down(self):
        for text in ("", None, "   "):
            with self.subTest(text=text):
                self.assertIsNone(find_due_in_text(text, now=NOW)[0])

    def test_a_time_and_a_date_on_separate_lines_are_not_one_answer(self):
        """Joining them would invent a promise neither line made."""
        got = find_due_in_text("next friday\nsome other note", now=NOW)[0]
        self.assertIsNone(got)


class TheDefaultIsOneHourTest(unittest.TestCase):

    def test_a_lead_with_nothing_stated(self):
        before = ny_now()
        got = to_ny(_apply({"extra_info": "gate code 4432"})["expected_delivery_at"])
        self.assertAlmostEqual(
            udb.DEFAULT_DUE_HOURS * 3600,
            (got - before).total_seconds(), delta=60)

    def test_it_says_the_time_was_assumed(self):
        """The board can tell a promise from an assumption without a second
        column -- and must, or an assumed time reads as something a human said."""
        self.assertEqual(udb.DUE_ASSUMED,
                         _apply({})["expected_delivery_set_by"])

    def test_a_lead_with_no_notes_at_all(self):
        for payload in ({}, {"extra_info": None}, {"extra_info": ""}):
            with self.subTest(payload=payload):
                out = _apply(payload)
                self.assertTrue(out["expected_delivery_at"])
                self.assertEqual(udb.DUE_ASSUMED, out["expected_delivery_set_by"])

    def test_the_default_is_one_hour(self):
        self.assertEqual(1, udb.DEFAULT_DUE_HOURS)


class WhatSomebodyChoseIsNeverOverwrittenTest(unittest.TestCase):

    def test_an_existing_value_survives(self):
        out = _apply({"expected_delivery_at": "2026-01-01T00:00:00-05:00",
                      "extra_info": "by 5pm"})
        self.assertEqual("2026-01-01T00:00:00-05:00", out["expected_delivery_at"])

    def test_and_so_does_who_set_it(self):
        out = _apply({"expected_delivery_at": "2026-01-01T00:00:00-05:00",
                      "expected_delivery_set_by": "Kazeem"})
        self.assertEqual("Kazeem", out["expected_delivery_set_by"])

    def test_a_stated_time_credits_the_lead_not_a_person(self):
        out = _apply({"extra_info": "by 5pm"})
        self.assertEqual(udb.DUE_FROM_NOTES, out["expected_delivery_set_by"])
        self.assertEqual(17, to_ny(out["expected_delivery_at"]).hour)

    def test_a_blank_existing_value_is_treated_as_absent(self):
        for empty in ("", "   ", None):
            with self.subTest(empty=empty):
                out = _apply({"expected_delivery_at": empty})
                self.assertTrue(out["expected_delivery_at"])


class ItCanNeverCostTheLeadTest(unittest.TestCase):
    """A lead that fails to save over a due-time detail is a far worse outcome
    than a due time that is merely an assumption."""

    def test_a_parser_that_explodes_still_yields_a_default(self):
        with mock.patch("utils.due_time.find_due_in_text",
                        side_effect=RuntimeError("boom")):
            out = _apply({"extra_info": "by 5pm"})
        self.assertTrue(out["expected_delivery_at"])
        self.assertEqual(udb.DUE_ASSUMED, out["expected_delivery_set_by"])

    def test_a_payload_that_is_not_a_dict_does_not_raise(self):
        udb.apply_default_expected_delivery({})

    def test_notes_that_are_not_a_string(self):
        for junk in (123, ["by 5pm"], {"a": 1}, object()):
            with self.subTest(junk=type(junk).__name__):
                out = _apply({"extra_info": junk})
                self.assertTrue(out["expected_delivery_at"])


class EveryLeadGoesThroughItTest(unittest.TestCase):
    """There are five create sites in bot.py plus the website/API ingest. A
    default applied in the bot's funnel would have left website orders exactly
    as they were -- with no promised time at all."""

    SRC = (ROOT / "utils" / "database.py").read_text(encoding="utf-8")

    def test_create_lead_applies_it(self):
        i = self.SRC.index("def create_lead(self")
        body = self.SRC[i:self.SRC.index("\n    def ", i + 10)]
        self.assertIn("apply_default_expected_delivery(payload)", body)

    def test_it_runs_before_the_insert(self):
        i = self.SRC.index("def create_lead(self")
        body = self.SRC[i:self.SRC.index("\n    def ", i + 10)]
        self.assertLess(body.index("apply_default_expected_delivery("),
                        body.index('table("leads").insert'))

    def test_the_column_is_still_optional(self):
        """It is behind migration_lead_expected_delivery.sql. create_lead drops
        optional columns and retries, so a database without that migration must
        still save leads."""
        self.assertIn('"expected_delivery_at"', self.SRC)
        self.assertIn('"expected_delivery_set_by"', self.SRC)


class TheNoteItselfIsStillStoredTest(unittest.TestCase):

    def test_reading_a_time_out_of_it_does_not_consume_it(self):
        """Where the note and the column disagree the office should still see
        both -- which was the whole point of not parsing it before."""
        out = _apply({"extra_info": "Delivery by 5pm, gate code 4432"})
        self.assertEqual("Delivery by 5pm, gate code 4432", out["extra_info"])


if __name__ == "__main__":
    unittest.main()
