r"""Sending to ONE chosen driver goes to that driver, or says why it could not.

Reported: "when sending to specific driver it doesnt work and the driver gets no
message". "Nobody was picked" and "the drivers you picked all dropped out" were the
same empty list, and an empty list means "use the whole pool" — so a single pick
that no longer resolved (stale id, no Telegram chat id, suspended, switched off)
quietly became a broadcast, and when the pool was empty too the driver simply got
nothing and nobody was told.

Run:  venv\Scripts\python.exe -m pytest tests/test_specific_driver_dispatch.py -q
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

KITA = {"id": "d1", "driver_name": "Kita", "is_active": True, "driver_telegram_id": "111"}
MARCO = {"id": "d2", "driver_name": "Marco", "is_active": True, "driver_telegram_id": "222"}
SARA = {"id": "d3", "driver_name": "Sara", "is_active": True, "driver_telegram_id": None}
OFF = {"id": "d4", "driver_name": "Tony", "is_active": False, "driver_telegram_id": "444"}
ALL = [KITA, MARCO, SARA, OFF]


def _resolve(selected, suspended=(), drivers=ALL, is_all_groups=True):
    with mock.patch.object(bot, "_get_all_drivers_cached", lambda: list(drivers)), \
            mock.patch.object(bot, "_get_suspended_driver_ids", lambda: set(suspended)):
        return bot._dispatch_drivers_with_reasons(
            {"selected_driver_ids": list(selected)}, is_all_groups=is_all_groups)


class OnePickGoesToOneDriverTest(unittest.TestCase):

    def test_the_chosen_driver_is_the_only_one(self):
        ids, dropped = _resolve(["d1"])
        self.assertEqual(["d1"], ids)
        self.assertEqual([], dropped)

    def test_two_chosen_drivers_are_the_only_two(self):
        ids, _ = _resolve(["d1", "d2"])
        self.assertEqual(["d1", "d2"], sorted(ids))


class APickNeverWidensToEveryoneTest(unittest.TestCase):
    """The reported bug: one pick became a broadcast."""

    def test_a_stale_id_does_not_become_the_whole_roster(self):
        ids, dropped = _resolve(["gone"])
        self.assertEqual([], ids, "an unresolvable pick must not dispatch to anyone")
        self.assertTrue(dropped)

    def test_a_driver_with_no_chat_id_does_not_become_the_whole_roster(self):
        ids, dropped = _resolve(["d3"])
        self.assertEqual([], ids)
        self.assertEqual("Sara", dropped[0][0])

    def test_a_suspended_pick_does_not_become_the_whole_roster(self):
        ids, dropped = _resolve(["d1"], suspended=("d1",))
        self.assertEqual([], ids)
        self.assertIn("suspended", dropped[0][1])

    def test_a_switched_off_pick_does_not_become_the_whole_roster(self):
        ids, dropped = _resolve(["d4"])
        self.assertEqual([], ids)
        self.assertIn("switched off", dropped[0][1])

    def test_one_good_pick_survives_a_bad_one(self):
        ids, dropped = _resolve(["d1", "d3"])
        self.assertEqual(["d1"], ids)
        self.assertEqual(1, len(dropped))


class EveryDropSaysWhyTest(unittest.TestCase):
    """A silent non-delivery is the thing that made this impossible to diagnose."""

    def test_the_reasons_are_specific(self):
        for selected, want in ((["gone"], "no longer on the roster"),
                               (["d3"], "no Telegram chat id"),
                               (["d4"], "switched off")):
            with self.subTest(selected=selected):
                _, dropped = _resolve(selected)
                self.assertIn(want, dropped[0][1])

    def test_the_note_names_the_driver(self):
        _, dropped = _resolve(["d3"])
        note = bot._dropped_drivers_note(dropped)
        self.assertIn("Sara", note)
        self.assertIn("chat id", note)

    def test_no_drops_no_note(self):
        self.assertEqual("", bot._dropped_drivers_note([]))


class NoPickStillMeansThePoolTest(unittest.TestCase):
    """Picking nobody must keep working — that is the everyday case."""

    def test_an_empty_selection_uses_every_reachable_driver(self):
        ids, dropped = _resolve([])
        self.assertEqual(["d1", "d2"], sorted(ids))
        self.assertEqual([], dropped, "a thinning pool is routine, not worth reporting")

    def test_the_pool_still_skips_the_unreachable(self):
        ids, _ = _resolve([])
        self.assertNotIn("d3", ids)   # no chat id
        self.assertNotIn("d4", ids)   # switched off

    def test_the_old_helper_still_returns_plain_ids(self):
        with mock.patch.object(bot, "_get_all_drivers_cached", lambda: ALL), \
                mock.patch.object(bot, "_get_suspended_driver_ids", lambda: set()):
            self.assertEqual(["d1"], bot._resolve_dispatch_driver_ids(
                {"selected_driver_ids": ["d1"]}, is_all_groups=True))


class TheFallbacksAreGuardedTest(unittest.TestCase):
    """Both places that used to widen a failed pick."""

    def _src(self):
        return (ROOT / "bot.py").read_text(encoding="utf-8")

    def test_finalize_reports_instead_of_broadcasting(self):
        self.assertIn("could not be sent to the driver you picked", self._src())

    def test_group_accept_only_falls_back_when_nothing_was_picked(self):
        self.assertIn("if not selected_drivers and not raw_ids:", self._src())


if __name__ == "__main__":
    unittest.main()
