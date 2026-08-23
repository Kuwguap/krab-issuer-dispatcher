"""No completeness gate before the review card, and reassign right after sending.

Two changes:
  * "I couldn't find enough info … Please include at least name and delivery
    address/city" rejected the message AND discarded everything already extracted, so
    a lead with one gap could not reach the card where that gap is one edit away.
  * Reassigning a driver or dispatcher straight after sending needed a timeout or
    hunting for an older message; both are now on the confirmation.

Run:  venv\\Scripts\\python.exe -m pytest tests/test_submit_and_reassign.py -q
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

SRC = (ROOT / "bot.py").read_text(encoding="utf-8")


class NoCompletenessGateTest(unittest.TestCase):
    """A partial lead must reach the card instead of being thrown away."""

    def test_the_rejection_messages_are_gone(self):
        for phrase in ("find enough info", "Please include at least name",
                       "pass validation"):
            self.assertNotIn(phrase, SRC, phrase)

    def test_the_validator_is_no_longer_gating_either_path(self):
        self.assertNotIn("valid, validation_errors", SRC,
                         "neither the typed nor the photo path may block on it")

    def test_a_partial_extraction_still_parses_what_it_can(self):
        """Nothing about extraction changed — only the gate that discarded it."""
        # A missing name arrives as "-", holding its line so the rest stay aligned.
        block = "\n".join(["-", "123 Main St", "Newark NJ 07102", "-", "-",
                           "1HGCM82633A004352", "2019 Honda Accord", "Blue", "-", "-", "-"])
        parsed = bot.parse_phase1_structured(block)
        self.assertEqual(parsed.get("vin"), "1HGCM82633A004352")
        self.assertEqual(parsed.get("address"), "123 Main St")


class AfterSendKeyboardTest(unittest.TestCase):

    def _buttons(self, lead_id="lead-123"):
        kb = bot._after_send_keyboard(lead_id)
        return [b for row in kb.inline_keyboard for b in row]

    def test_offers_driver_and_dispatcher_reassign(self):
        labels = " | ".join(b.text for b in self._buttons())
        self.assertIn("Reassign driver", labels)
        self.assertIn("Reassign dispatcher", labels)

    def test_another_tag_is_still_offered(self):
        self.assertIn("Another tag", " | ".join(b.text for b in self._buttons()))

    def test_callbacks_carry_the_lead_id_each_handler_expects(self):
        datas = [b.callback_data for b in self._buttons("lead-123")]
        self.assertIn("resend_driver_lead-123", datas)
        self.assertIn("reassign_group_lead-123", datas)
        self.assertIn("another_tag_lead-123", datas)

    def test_callback_data_fits_telegrams_limit(self):
        long_id = "a" * 36  # a UUID
        for b in self._buttons(long_id):
            self.assertLessEqual(len(b.callback_data.encode()), 64, b.callback_data)

    def test_both_reassign_routes_are_entry_points(self):
        """Submit ends the conversation, so these must work from outside it."""
        entry = SRC.split("entry_points=[", 1)[1].split("states={", 1)[0]
        self.assertIn("resend_driver_", entry)
        self.assertIn("reassign_group_", entry)

    def test_both_success_messages_use_the_same_keyboard(self):
        self.assertGreaterEqual(SRC.count("_after_send_keyboard("), 3,
                                "definition plus both send paths")


if __name__ == "__main__":
    unittest.main()
