"""All Drivers is the DEFAULT for an Instant Tag — offered first, never auto-on.

When supervisors switch the Instant Tag broadcast on, sending to everyone is the
point of the setting: each driver gets their own payment link and the first card
to clear wins. It used to sit underneath every individual driver, so the thing
you wanted was the last thing you saw.

Promoted, not preselected: nothing dispatches until somebody taps it.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "dummy:token")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy-key")

import utils.database as udb  # noqa: E402

if "bot" not in sys.modules:
    udb.Database = mock.MagicMock()
import bot  # noqa: E402


DRIVERS = [
    {"id": "d1", "driver_name": "Ana"},
    {"id": "d2", "driver_name": "Ben"},
]
ALL_CB = "seldrv_all"


def labels(rows):
    return [b.text for row in rows for b in row]


def callbacks(rows):
    return [b.callback_data for row in rows for b in row]


class InstantTagWithBroadcastOnTest(unittest.TestCase):
    def rows(self, instant, broadcast):
        with mock.patch.object(bot, "_instant_all_drivers_enabled", return_value=broadcast):
            return bot._driver_picker_rows(DRIVERS, set(), {"instant_tag": instant})

    def test_all_drivers_is_offered_first(self):
        rows = self.rows(instant=True, broadcast=True)
        self.assertEqual(rows[0][0].callback_data, ALL_CB,
                         "the broadcast is the point of the setting — it goes first")

    def test_it_says_it_is_the_default(self):
        rows = self.rows(instant=True, broadcast=True)
        self.assertIn("default", rows[0][0].text.lower())

    def test_every_driver_is_still_pickable(self):
        rows = self.rows(instant=True, broadcast=True)
        self.assertEqual(
            {c for c in callbacks(rows) if c.startswith("seldrv_") and c != ALL_CB},
            {"seldrv_d1", "seldrv_d2"},
        )

    def test_nothing_is_preselected(self):
        # Promoted is not chosen: every entry is a plain button with a callback,
        # and none of them is marked as already taken.
        rows = self.rows(instant=True, broadcast=True)
        for text in labels(rows):
            self.assertNotIn("✅", text, "a tick would read as already selected")


class InstantTagWithBroadcastOffTest(unittest.TestCase):
    def test_all_drivers_is_not_offered_at_all(self):
        with mock.patch.object(bot, "_instant_all_drivers_enabled", return_value=False):
            rows = bot._driver_picker_rows(DRIVERS, set(), {"instant_tag": True})
        self.assertNotIn(ALL_CB, callbacks(rows),
                         "Instant Tag needs ONE driver unless supervisors allow the broadcast")


class OrdinaryLeadTest(unittest.TestCase):
    def test_all_drivers_stays_at_the_bottom(self):
        with mock.patch.object(bot, "_instant_all_drivers_enabled", return_value=True):
            rows = bot._driver_picker_rows(DRIVERS, set(), {})
        cbs = callbacks(rows)
        self.assertIn(ALL_CB, cbs)
        self.assertGreater(cbs.index(ALL_CB), cbs.index("seldrv_d2"),
                           "one driver is the usual answer on an ordinary lead")

    def test_back_is_always_last(self):
        for instant, broadcast in ((True, True), (True, False), (False, True), (False, False)):
            with self.subTest(instant=instant, broadcast=broadcast):
                with mock.patch.object(bot, "_instant_all_drivers_enabled",
                                       return_value=broadcast):
                    rows = bot._driver_picker_rows(DRIVERS, set(), {"instant_tag": instant})
                self.assertEqual(rows[-1][0].callback_data, "ph1_sel_back")


class SuspendedDriversTest(unittest.TestCase):
    def test_a_suspended_driver_cannot_be_picked(self):
        with mock.patch.object(bot, "_instant_all_drivers_enabled", return_value=True):
            rows = bot._driver_picker_rows(DRIVERS, {"d1"}, {"instant_tag": True})
        self.assertIn("driver_suspended_d1", callbacks(rows))
        self.assertNotIn("seldrv_d1", callbacks(rows))

    def test_all_drivers_disappears_when_everyone_is_suspended(self):
        with mock.patch.object(bot, "_instant_all_drivers_enabled", return_value=True):
            rows = bot._driver_picker_rows(DRIVERS, {"d1", "d2"}, {"instant_tag": True})
        self.assertNotIn(ALL_CB, callbacks(rows))


if __name__ == "__main__":
    unittest.main()
