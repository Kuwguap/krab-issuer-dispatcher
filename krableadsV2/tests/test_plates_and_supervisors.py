r"""Plate screen without control numbers, and supervisors managed from /settings.

Asked for:
  * "remove the control numbers from the /settings plate number response and make
    it completely random 10 digits for both resident and non resident";
  * "users can just upload a picture of recent plate and read the image add
    +10,000 to the range and update plate number";
  * "allow me to add admins supervisors and view who is add or remove supervisors
    who can access settings anytime".

Run:  venv\Scripts\python.exe -m pytest tests/test_plates_and_supervisors.py -q
"""
import asyncio
import os
import re
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

# bot's import replaced utils.database.Database with a MagicMock, so load a private
# copy of the module to get at the REAL class this file needs to exercise.
import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location(
    "_real_database_for_test", ROOT / "utils" / "database.py")
_real_database = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_real_database)
Database = _real_database.Database

PLATE_ROW = {"nj_plate_prefix": "H", "non_nj_plate_suffix": "V",
             "nj_plate_next_number": 209238, "non_nj_plate_next_number": 477040,
             "nj_car_next_number": 5, "non_nj_car_next_number": 6}


def _plate_screen():
    db = mock.MagicMock()
    db.get_plate_settings.return_value = dict(PLATE_ROW)
    with mock.patch.object(bot, "db", db):
        return asyncio.run(bot._settings_view_plates())


class ThePlateScreenDropsControlNumbersTest(unittest.TestCase):

    def test_the_control_lines_are_gone(self):
        text, _ = _plate_screen()
        self.assertNotIn("control next", text.lower())

    def test_the_control_buttons_are_gone(self):
        _, kb = _plate_screen()
        labels = [b.text for row in kb.inline_keyboard for b in row]
        self.assertEqual([], [l for l in labels if "control" in l.lower()], labels)

    def test_the_two_plate_counters_are_still_there(self):
        text, kb = _plate_screen()
        self.assertIn("209238", text)
        self.assertIn("477040", text)
        labels = [b.text for row in kb.inline_keyboard for b in row]
        self.assertIn("Set Resident plate #", labels)
        self.assertIn("Set Non-Res plate #", labels)

    def test_it_says_control_numbers_are_random(self):
        text, _ = _plate_screen()
        self.assertIn("random", text.lower())

    def test_it_explains_the_photo_shortcut(self):
        text, _ = _plate_screen()
        self.assertIn("photo", text.lower())
        self.assertIn("10,000", text)

    def test_nothing_offers_a_control_counter_any_more(self):
        self.assertNotIn("nj_car_next_number", bot._PLATE_SET_LABELS)
        self.assertNotIn("non_nj_car_next_number", bot._PLATE_SET_LABELS)


class TheControlNumberIsRandomTest(unittest.TestCase):

    def _db(self, rpc_data=None, boom=False):
        d = Database.__new__(Database)
        d.client = mock.MagicMock()
        if boom:
            d.client.rpc.side_effect = Exception("no rpc")
        else:
            d.client.rpc.return_value.execute.return_value = mock.MagicMock(data=rpc_data)
        return d

    def test_ten_digits_never_starting_with_zero(self):
        d = self._db({"plate": "H209238", "car": "1"})
        for _ in range(20):
            control = d.allocate_temp_plate(True)["control_number"]
            self.assertRegex(control, r"^[1-9]\d{9}$", control)

    def test_a_fresh_one_every_time(self):
        d = self._db({"plate": "H209238", "car": "1"})
        seen = {d.allocate_temp_plate(True)["control_number"] for _ in range(25)}
        self.assertGreater(len(seen), 20, "these should essentially never collide")

    def test_the_database_counter_is_ignored(self):
        """The whole point — no counter to keep in step any more."""
        d = self._db({"plate": "H209238", "car": "0000000042"})
        for _ in range(10):
            self.assertNotEqual("0000000042", d.allocate_temp_plate(True)["control_number"])

    def test_the_plate_itself_still_comes_from_the_database(self):
        d = self._db({"plate": "H209238", "car": "1"})
        self.assertEqual("H209238", d.allocate_temp_plate(True)["plate"])

    def test_both_kinds_get_one(self):
        d = self._db(boom=True)
        for is_nj in (True, False):
            with self.subTest(is_nj=is_nj):
                got = d.allocate_temp_plate(is_nj)
                self.assertRegex(got["control_number"], r"^[1-9]\d{9}$")
                self.assertRegex(got["plate"], r"^H\d{6}$" if is_nj else r"^\d{6}V$")


class APhotoJumpsTheCounterTest(unittest.TestCase):
    """"upload a picture of recent plate … add +10,000 to the range"."""

    def test_the_reported_tag(self):
        self.assertEqual(523013, bot._plate_after_image(513013))

    def test_the_jump_is_ten_thousand(self):
        self.assertEqual(10_000, bot.PLATE_IMAGE_JUMP)
        for n in (1, 209238, 477040, 800000):
            with self.subTest(n=n):
                self.assertEqual(n + 10_000, bot._plate_after_image(n))

    def test_it_stays_six_digits(self):
        """995000 + 10000 would be seven digits and print a plate that cannot exist."""
        got = bot._plate_after_image(995000)
        self.assertLess(got, 1_000_000)
        self.assertEqual(5000, got)

    def test_both_read_paths_use_it(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        self.assertEqual(2, src.count("jumped = _plate_after_image(number)"))

    def test_the_confirmation_says_what_it_did(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        self.assertEqual(2, src.count("that tag plus {PLATE_IMAGE_JUMP:,}"))

    def test_a_typed_number_is_taken_literally(self):
        """Typing a number means that number — only a PHOTO jumps."""
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        body = src.split('if st.get("kind") == "plate":', 1)[1].split("if st.get(", 1)[0]
        self.assertIn("int(digits)", body)
        self.assertNotIn("_plate_after_image", body)


class SupervisorsCanBeManagedTest(unittest.TestCase):

    def setUp(self):
        self.store = {}
        self.db = mock.MagicMock()
        self.db.get_setting.side_effect = lambda k: self.store.get(k)
        self.db.set_setting.side_effect = (
            lambda k, v: (self.store.__setitem__(k, v), True)[1])
        bot._extra_sup_cache["at"] = 0.0

    def _ctx(self, env=(111,)):
        return mock.patch.object(bot, "db", self.db), \
            mock.patch.object(bot, "_global_supervisory_chat_ids", lambda: list(env))

    def test_someone_added_can_open_settings(self):
        a, b = self._ctx()
        with a, b:
            self.assertFalse(bot._user_is_global_supervisor(999))
            bot._add_extra_supervisor("999", "Marco")
            self.assertTrue(bot._user_is_global_supervisor(999))

    def test_removing_them_takes_it_away(self):
        a, b = self._ctx()
        with a, b:
            bot._add_extra_supervisor("999", "Marco")
            bot._remove_extra_supervisor("999")
            self.assertFalse(bot._user_is_global_supervisor(999))

    def test_the_env_ones_still_work(self):
        a, b = self._ctx()
        with a, b:
            self.assertTrue(bot._user_is_global_supervisor(111))

    def test_the_env_ones_cannot_be_removed(self):
        """Never lock everyone out of the door you are standing in."""
        a, b = self._ctx()
        with a, b:
            bot._remove_extra_supervisor("111")
            self.assertTrue(bot._user_is_global_supervisor(111))

    def test_adding_the_same_person_twice_keeps_one_row(self):
        a, b = self._ctx()
        with a, b:
            bot._add_extra_supervisor("999", "Marco")
            bot._add_extra_supervisor("999", "Marco Rossi")
            rows = bot._extra_supervisors(force=True)
            self.assertEqual(1, len(rows))
            self.assertEqual("Marco Rossi", rows[0]["label"])

    def test_a_broken_stored_value_does_not_lock_anyone_out(self):
        self.store[bot.EXTRA_SUPERVISORS_KEY] = "{not json"
        a, b = self._ctx()
        with a, b:
            self.assertEqual([], bot._extra_supervisors(force=True))
            self.assertTrue(bot._user_is_global_supervisor(111))


class TheSupervisorScreenTest(unittest.TestCase):

    def _screen(self, extra=(), env=(111,)):
        db = mock.MagicMock()
        import json
        db.get_setting.side_effect = lambda k: json.dumps(list(extra))
        with mock.patch.object(bot, "db", db), \
                mock.patch.object(bot, "_global_supervisory_chat_ids", lambda: list(env)):
            return asyncio.run(bot._settings_view_supervisors())

    def test_it_lists_everyone(self):
        text, _ = self._screen(extra=[{"id": "999", "label": "Marco"}])
        self.assertIn("111", text)
        self.assertIn("999", text)
        self.assertIn("Marco", text)

    def test_the_env_ones_are_marked_fixed(self):
        text, _ = self._screen()
        self.assertIn("cannot be removed here", text)

    def test_only_the_added_ones_get_a_remove_button(self):
        _, kb = self._screen(extra=[{"id": "999", "label": "Marco"}])
        labels = [b.text for row in kb.inline_keyboard for b in row]
        self.assertIn("Remove Marco", labels)
        self.assertEqual([], [l for l in labels if "111" in l])

    def test_there_is_a_way_to_add(self):
        _, kb = self._screen()
        data = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertIn("tset_supadd", data)

    def test_it_says_where_to_get_an_id(self):
        text, _ = self._screen()
        self.assertIn("/whoami", text)

    def test_an_empty_roster_says_so(self):
        text, _ = self._screen(extra=[], env=[])
        self.assertIn("Nobody configured", text)


class ItIsReachableTest(unittest.TestCase):

    def test_the_menu_has_a_button(self):
        data = [b.callback_data for row in bot._settings_main_kb().inline_keyboard for b in row]
        self.assertIn("tset_sups", data)

    def test_the_view_is_registered(self):
        self.assertIn("tset_sups", bot._SETTINGS_VIEWS)

    def test_saying_it_opens_it(self):
        for said in ("supervisors", "admins", "administrators", "add an admin"):
            with self.subTest(said=said):
                self.assertEqual("tset_sups", bot._settings_nav_target(said), said)

    def test_the_hint_mentions_it(self):
        self.assertIn("supervisors", bot._SETTINGS_HINT.lower())

    def test_saying_plates_still_opens_plates(self):
        self.assertEqual("tset_plates", bot._settings_nav_target("plate numbers"))


if __name__ == "__main__":
    unittest.main()
