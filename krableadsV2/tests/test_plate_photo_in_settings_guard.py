"""A tag photo sent in /settings must never turn into a lead.

Reported: on the Plate Numbers screen a photo of a car's temp tag started a NEW LEAD,
with the model's refusal text ("I'm unable to extract any personal details from this
image…") showing as the client's first and last name.

Three faults, all covered here:
  * the read failed because the prompt rejected "a photo of a car" — which is exactly
    how a tag is photographed;
  * a failed read then fell through to the lead flow (correct for a forwarded
    screenshot, wrong inside /settings);
  * a prose refusal was parsed into the name fields instead of being treated as
    "nothing read".

Run:  venv\\Scripts\\python.exe -m pytest tests/test_plate_photo_in_settings_guard.py -q
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

# Verbatim from the report.
REFUSAL = ("I'm unable to extract any personal details from this image. Please provide "
           "a document or image with the required details visible for further assistance.")


class RefusalIsNotALeadTest(unittest.TestCase):
    """A prose refusal must never populate the card."""

    def test_the_reported_refusal_is_recognised(self):
        self.assertTrue(bot._looks_like_ai_refusal(REFUSAL))

    def test_other_phrasings_are_recognised(self):
        for text in ("I am unable to read this image.",
                     "Cannot extract the required fields.",
                     "No personal details are visible in this photo.",
                     "This image does not contain any personal information.",
                     "Please provide a clearer image."):
            with self.subTest(text=text):
                self.assertTrue(bot._looks_like_ai_refusal(text))

    def test_a_real_extraction_is_not_mistaken_for_one(self):
        real = "\n".join(["John Damian", "123 Main St", "Newark NJ 07102", "-", "-",
                          "1HGCM82633A004352", "2019 Honda Accord", "Blue", "-", "-",
                          "call first, unable to park out front"])
        self.assertFalse(bot._looks_like_ai_refusal(real),
                         "the words may appear in a NOTE without meaning refusal")

    def test_empty_is_not_a_refusal(self):
        self.assertFalse(bot._looks_like_ai_refusal(""))


class SettingsGuardTest(unittest.TestCase):

    def _in_settings(self, active):
        conv = mock.MagicMock(spec=bot.ConversationHandler)
        conv._conversations = {("k",): 0 if active else None}
        conv._get_key.return_value = ("k",)
        ctx = mock.MagicMock()
        with mock.patch.object(bot, "_SETTINGS_CONV_HANDLER", conv):
            return bot._in_settings_conversation(mock.MagicMock(), ctx)

    def test_detects_being_inside_settings(self):
        self.assertTrue(self._in_settings(True))

    def test_detects_being_outside_settings(self):
        self.assertFalse(self._in_settings(False))

    def test_degrades_safely_with_no_handler(self):
        with mock.patch.object(bot, "_SETTINGS_CONV_HANDLER", None):
            self.assertFalse(bot._in_settings_conversation(mock.MagicMock(), mock.MagicMock()))

    def test_a_failed_read_in_settings_does_not_fall_through(self):
        """The fall-through exists for forwarded lead images — not for /settings."""
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        self.assertIn("if not forced_col and not _in_settings_conversation(update, context):", src)


class PlatePromptAcceptsATagOnACarTest(unittest.TestCase):

    def test_a_photo_of_a_vehicle_is_no_longer_rejected(self):
        prompt = ai_vision.PLATE_READ_PROMPT
        self.assertNotIn("a photo of a car", prompt,
                         "photographing the tag on the car is the normal case")
        self.assertIn("PHOTO OF A VEHICLE counts", prompt)

    def test_it_still_refuses_documents_and_permanent_plates(self):
        prompt = ai_vision.PLATE_READ_PROMPT
        for must_reject in ("license", "title", "receipt", "ordinary metal plate"):
            self.assertIn(must_reject, prompt, must_reject)

    def test_the_h_and_v_rules_are_intact(self):
        prompt = ai_vision.PLATE_READ_PROMPT
        self.assertIn("H######", prompt)     # resident
        self.assertIn("######V", prompt)     # non-resident


if __name__ == "__main__":
    unittest.main()
