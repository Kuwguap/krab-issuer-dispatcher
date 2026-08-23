"""The DMV VIN check asks one short question with Yes / No.

It used to be a three-line preamble ("Pulling up 17 Digit Vin in DMV portal", "Success!
Your Vehicle pulls up in the Motor Vehicle system!", "Choose which to use:") over three
stacked buttons.

Run:  venv\\Scripts\\python.exe -m pytest tests/test_vin_choice_prompt.py -q
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

DMV_CAR = "2017 MERCEDES-BENZ E-Class"
STATED_CAR = "2017 Mercedes E350"


class PromptWordingTest(unittest.TestCase):

    def test_asks_the_short_question(self):
        body = bot._vin_conflict_body(STATED_CAR, DMV_CAR)
        self.assertIn("Would you like to use DMV system?", body)

    def test_the_old_preamble_is_gone(self):
        body = bot._vin_conflict_body(STATED_CAR, DMV_CAR)
        for old in ("Pulling up", "Motor Vehicle system", "Choose which to use",
                    "VIN result in DMV", "Success"):
            self.assertNotIn(old, body, old)

    def test_the_decoded_vehicle_is_still_shown(self):
        """Yes/No would be a blind choice without it."""
        self.assertIn(DMV_CAR, bot._vin_conflict_body(STATED_CAR, DMV_CAR))

    def test_message_is_two_lines(self):
        self.assertEqual(bot._vin_conflict_body(STATED_CAR, DMV_CAR).count("\n"), 1)


class ButtonsTest(unittest.TestCase):

    def _buttons(self):
        kb = bot._vin_choice_keyboard(DMV_CAR, STATED_CAR)
        return [b for row in kb.inline_keyboard for b in row]

    def test_exactly_two_buttons_on_one_row(self):
        kb = bot._vin_choice_keyboard(DMV_CAR, STATED_CAR)
        self.assertEqual(len(kb.inline_keyboard), 1)
        self.assertEqual(len(kb.inline_keyboard[0]), 2)

    def test_yes_uses_the_dmv_lookup(self):
        yes = next(b for b in self._buttons() if "Yes" in b.text)
        self.assertEqual(yes.callback_data, "vin_use")

    def test_no_keeps_the_same_vin(self):
        no = next(b for b in self._buttons() if "No" in b.text)
        self.assertEqual(no.callback_data, "vin_keep")

    def test_retype_button_is_gone(self):
        self.assertNotIn("vin_retype", [b.callback_data for b in self._buttons()])

    def test_retype_is_still_reachable_without_a_button(self):
        """Saying "retype vin" must still work — only the button was removed."""
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        self.assertIn("vin_use|vin_keep|vin_retype", src,
                      "the retype callback must stay registered")
        self.assertEqual(bot._classify_review_command("retype vin")[0], "VIN_RETYPE")


if __name__ == "__main__":
    unittest.main()
