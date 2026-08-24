r"""A refusal on ONE line must not become the client's name.

Reported: a driver's licence came back with line 1 as "I'm sorry, I can't assist
with that." and every other line read perfectly, so the card said

    First name: I'm
    Last name:  sorry, I can't assist with that.

above a correct address, VIN, car, colour, carrier, policy, phone, email and DL.

Two faults: the refusal detector did not know "can't assist" or a bare apology,
and it only ever judged the WHOLE reply — so one bad line could either poison a
field or throw away a good extraction. A bad value is now dropped on its own.

Run:  venv\Scripts\python.exe -m pytest tests/test_refusal_on_one_line.py -q
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy-key")

import utils.database as udb  # noqa: E402

if "bot" not in sys.modules:
    udb.Database = mock.MagicMock()
import bot  # noqa: E402
from utils import ai_vision  # noqa: E402

REFUSAL = "I'm sorry, I can't assist with that."

# The reply exactly as it came back, refusal on line 1 and good data below it.
REPORTED = "\n".join([
    REFUSAL,
    "325 Prospect St",
    "Perth Amboy, NJ 08861-4028",
    "19 Pennwood Dr",
    "Ewing, NJ 08638-4725",
    "WDDZF4JB4HA036041",
    "2017 M Benz E Class",
    "White",
    "Geico",
    "4570-22-55-83",
    "Effective 09/01/25, expires 03/01/26",
])


class TheReportedExtractionTest(unittest.TestCase):

    def setUp(self):
        self.card = bot.parse_phase1_structured(REPORTED)

    def test_the_apology_is_not_the_name(self):
        self.assertNotIn("sorry", str(self.card.get("name")).lower())
        self.assertNotIn("assist", str(self.card.get("name")).lower())

    def test_the_name_is_simply_blank(self):
        """Honest and editable beats a fabricated client."""
        self.assertIn(str(self.card.get("name") or ""), ("", "-"))

    def test_everything_else_survives(self):
        for key, want in (("address", "325 Prospect St"),
                          ("city_state_zip", "Perth Amboy, NJ 08861-4028"),
                          ("delivery_address", "19 Pennwood Dr"),
                          ("delivery_city_state_zip", "Ewing, NJ 08638-4725"),
                          ("vin", "WDDZF4JB4HA036041"),
                          ("car", "2017 M Benz E Class"),
                          ("insurance_company", "Geico"),
                          ("insurance_policy_number", "4570-22-55-83"),
                          ("extra_info", "Effective 09/01/25, expires 03/01/26")):
            with self.subTest(field=key):
                self.assertEqual(want, self.card.get(key))

    def test_the_colour_still_normalises(self):
        self.assertEqual("White", self.card.get("color"))

    def test_the_apology_reaches_no_field_at_all(self):
        """raw_text keeps the reply verbatim by design — the FIELDS must be clean."""
        fields = ("name", "address", "city_state_zip", "delivery_address",
                  "delivery_city_state_zip", "vin", "car", "color",
                  "insurance_company", "insurance_policy_number", "extra_info")
        for key in fields:
            with self.subTest(field=key):
                self.assertNotIn("sorry", str(self.card.get(key) or "").lower())


class TheDetectorKnowsAnApologyTest(unittest.TestCase):

    def test_the_reported_wording(self):
        self.assertTrue(bot._value_is_refusal(REFUSAL))

    def test_the_other_ways_a_model_declines(self):
        for said in ("I am unable to read this image.",
                     "Cannot provide that information.",
                     "I apologize, but I cannot help.",
                     "I apologise — I can't identify people in photos.",
                     "As an AI, I cannot identify people.",
                     "I'm unable to extract the name.",
                     "Please provide a clearer image."):
            with self.subTest(said=said):
                self.assertTrue(bot._value_is_refusal(said), said)

    def test_real_values_are_untouched(self):
        for said in ("John Damian", "325 Prospect St", "WDDZF4JB4HA036041",
                     "Geico", "White", "2017 M Benz E Class", "-", ""):
            with self.subTest(said=said):
                self.assertFalse(bot._value_is_refusal(said), said)

    def test_a_note_that_happens_to_say_sorry_survives(self):
        """A driver note is prose — it may apologise and still be a real note."""
        note = ("Sorry for the short notice, please call the client before you "
                "leave the shop and again when you are five minutes away, the gate "
                "code is 4432 and the dog is friendly but loud.")
        self.assertFalse(bot._value_is_refusal(note))

    def test_it_is_length_capped(self):
        self.assertFalse(bot._value_is_refusal("I'm sorry " + "x" * 200))


class TheWholeReplyCheckStillWorksTest(unittest.TestCase):
    """The two checks are separate on purpose and must both keep working."""

    def test_a_reply_that_is_nothing_but_prose_is_still_caught(self):
        self.assertTrue(bot._looks_like_ai_refusal(
            "I'm unable to extract any personal details from this image."))

    def test_a_real_block_is_not(self):
        self.assertFalse(bot._looks_like_ai_refusal(REPORTED.split("\n", 1)[1]))


class ThePromptDiscouragesItTest(unittest.TestCase):
    """Cheapest fix of all — ask the model not to do it."""

    def test_both_extraction_prompts_forbid_apologies(self):
        """The image prompt AND the typed-text one — a refusal can come from either."""
        for name in ("STRUCTURE_PROMPT", "TEXT_STRUCTURE_PROMPT", "MULTI_STRUCTURE_PROMPT"):
            with self.subTest(prompt=name):
                self.assertIn("NEVER answer with an apology", getattr(ai_vision, name), name)

    def test_they_say_what_to_do_instead(self):
        for name in ("STRUCTURE_PROMPT", "TEXT_STRUCTURE_PROMPT"):
            with self.subTest(prompt=name):
                self.assertIn('output "-" for that line', getattr(ai_vision, name))


if __name__ == "__main__":
    unittest.main()
