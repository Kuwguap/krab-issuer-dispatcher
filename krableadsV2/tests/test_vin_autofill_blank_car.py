"""An empty Car field is not a conflict — the VIN decode just fills it.

The review card renders a missing vehicle as "-". Treating that as something to
"keep" meant the bot asked "use the DMV decode, or keep '-'?", and any operator
who ignored the prompt dispatched a lead with an empty Car even though the VIN
had decoded fine. Reported from the field with VIN JTLKT324364094480, which
NHTSA resolves to a 2006 Toyota Scion xB.
"""
import unittest
from unittest.mock import patch

import bot


DECODED = {
    "year": "2006",
    "make": "TOYOTA",
    "model": "Scion xB",
    "car_line": "2006 TOYOTA Scion xB",
}
REAL_VIN = "JTLKT324364094480"


class CarIsBlankTest(unittest.TestCase):
    def test_the_placeholders_the_card_actually_renders(self):
        for blank in ("", "-", "--", "—", "n/a", "N/A", "none", "Unknown", " - "):
            with self.subTest(blank=blank):
                self.assertTrue(bot._car_is_blank(blank))

    def test_a_real_car_is_not_blank(self):
        for car in ("2006 Toyota Scion xB", "Honda", "F-150"):
            with self.subTest(car=car):
                self.assertFalse(bot._car_is_blank(car))


class BlankCarIsFilledNotQueriedTest(unittest.TestCase):
    def _check(self, state):
        with patch.object(bot.Config, "is_vin_lookup_configured", return_value=True), \
             patch.object(bot.vin_lookup, "vin_lookup", return_value=DECODED):
            return bot._vin_check_after_phase1(state)

    def test_a_dash_car_is_filled_in_with_no_prompt(self):
        state = {"vin": REAL_VIN, "car": "-"}
        alert, conflict = self._check(state)
        self.assertIsNone(conflict, "an empty Car field must not raise a conflict")
        self.assertIsNone(alert)
        self.assertEqual(state["car"], "2006 TOYOTA Scion xB")

    def test_a_missing_car_key_is_filled_in_too(self):
        state = {"vin": REAL_VIN}
        _, conflict = self._check(state)
        self.assertIsNone(conflict)
        self.assertEqual(state["car"], "2006 TOYOTA Scion xB")

    def test_a_real_disagreement_still_asks(self):
        state = {"vin": REAL_VIN, "car": "1999 Honda Civic"}
        _, conflict = self._check(state)
        self.assertIsNotNone(conflict, "a genuine mismatch must still be confirmed")
        self.assertEqual(conflict[0], "2006 TOYOTA Scion xB")
        self.assertEqual(conflict[1], "1999 Honda Civic")
        # And it must NOT have silently overwritten what the operator typed.
        self.assertEqual(state["car"], "1999 Honda Civic")

    def test_a_matching_car_is_left_alone(self):
        state = {"vin": REAL_VIN, "car": "2006 TOYOTA Scion xB"}
        _, conflict = self._check(state)
        self.assertIsNone(conflict)
        self.assertEqual(state["car"], "2006 TOYOTA Scion xB")

    def test_a_short_vin_still_warns_and_fills_nothing(self):
        state = {"vin": "TOOSHORT", "car": "-"}
        alert, conflict = self._check(state)
        self.assertIsNone(conflict)
        self.assertIn("17", alert or "")
        self.assertEqual(state["car"], "-")


class DecodeFailureLeavesTheCardAloneTest(unittest.TestCase):
    def test_no_result_does_not_wipe_the_car(self):
        state = {"vin": REAL_VIN, "car": "-"}
        with patch.object(bot.Config, "is_vin_lookup_configured", return_value=True), \
             patch.object(bot.vin_lookup, "vin_lookup", return_value=None):
            alert, conflict = bot._vin_check_after_phase1(state)
        self.assertIsNone(conflict)
        self.assertIn("no result", (alert or "").lower())
        self.assertEqual(state["car"], "-")


if __name__ == "__main__":
    unittest.main()
