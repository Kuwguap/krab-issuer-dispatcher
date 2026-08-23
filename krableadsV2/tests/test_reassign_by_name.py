"""Reassigning by tapping, typing OR saying the name — and naming who it went to.

While a reassign picker was open, typed text got "tap a button above". And the sent
confirmation said "1 driver(s)", which does not say WHO — the one thing worth checking
before walking away.

Run:  venv\\Scripts\\python.exe -m pytest tests/test_reassign_by_name.py -q
"""
import asyncio
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy-key")

import utils.database as udb  # noqa: E402

if "bot" not in sys.modules:
    udb.Database = mock.MagicMock()
import bot  # noqa: E402

DRIVERS = [{"id": "d1", "driver_name": "Kita", "is_active": True},
           {"id": "d2", "driver_name": "Sam Okafor", "is_active": True}]
GROUPS = [{"id": "g1", "group_name": "HighKage", "is_active": True}]


def _say(text, state="select_driver", suspended=frozenset()):
    """Type/say something while a reassign picker is open."""
    msg = SimpleNamespace(text=text, chat_id=1, reply_text=mock.AsyncMock())
    update = SimpleNamespace(
        message=msg, effective_message=msg,
        effective_chat=SimpleNamespace(id=1, type="private"),
        effective_user=SimpleNamespace(id=7, username="tester"))
    ctx = SimpleNamespace(user_data={}, bot=mock.AsyncMock(),
                          application=SimpleNamespace(handlers={}))
    seen = {}

    async def fake_resend(upd, context, lead_data, callback_data, user_id):
        seen["callback"] = callback_data
        seen["shim_message"] = upd.callback_query.message is msg
        return bot.STATE_SELECT_DRIVER

    async def fake_group(upd, context):
        seen["callback"] = upd.callback_query.data
        return bot.STATE_SELECT_DRIVER

    fake_db = mock.MagicMock()
    fake_db.get_user_state.return_value = {"state": state,
                                           "data": {"lead_id": "L1", "resend": True}}
    fake_db.get_all_groups.return_value = GROUPS
    with mock.patch.object(bot, "db", fake_db), \
            mock.patch.object(bot, "_get_all_drivers_cached",
                              mock.MagicMock(return_value=DRIVERS)), \
            mock.patch.object(bot, "_get_suspended_driver_ids",
                              mock.MagicMock(return_value=set(suspended))), \
            mock.patch.object(bot, "_handle_resend_to_drivers", fake_resend), \
            mock.patch.object(bot, "handle_group_selection", fake_group), \
            mock.patch.object(bot, "_send_vanishing", mock.AsyncMock()):
        asyncio.run(bot.handle_select_state_text(update, ctx))
    said = msg.reply_text.await_args.args[0] if msg.reply_text.await_args else None
    return seen, said


class ReassignByNameTest(unittest.TestCase):

    def test_a_driver_name_selects_that_driver(self):
        for text, want in (("Kita", "select_driver_d1"),
                           ("kita", "select_driver_d1"),
                           ("Sam Okafor", "select_driver_d2"),
                           ("sam", "select_driver_d2")):
            with self.subTest(text=text):
                seen, _ = _say(text)
                self.assertEqual(seen.get("callback"), want)

    def test_all_drivers_is_understood(self):
        seen, _ = _say("all drivers")
        self.assertEqual(seen.get("callback"), "select_driver_all")

    def test_the_shim_gives_the_resend_flow_a_message_to_reply_on(self):
        """It was written for a button tap; a typed answer must not crash it."""
        seen, _ = _say("Kita")
        self.assertTrue(seen.get("shim_message"))

    def test_an_unknown_name_keeps_the_picker_open(self):
        seen, said = _say("Zzzz")
        self.assertIsNone(seen.get("callback"), "nothing may be reassigned on a miss")
        self.assertIn("No driver matched", said)

    def test_a_suspended_driver_is_refused_by_name_too(self):
        seen, said = _say("Kita", suspended={"d1"})
        self.assertIsNone(seen.get("callback"))
        self.assertIn("suspended", said.lower())

    def test_a_dispatcher_name_selects_that_dispatcher(self):
        seen, _ = _say("HighKage", state="select_group")
        self.assertEqual(seen.get("callback"), "select_group_g1")

    def test_an_unknown_dispatcher_keeps_the_picker_open(self):
        seen, said = _say("Nowhere", state="select_group")
        self.assertIsNone(seen.get("callback"))
        self.assertIn("No dispatcher matched", said)


class DriversSentLabelTest(unittest.TestCase):
    """The confirmation must say WHO, not how many."""

    def _label(self, state_data, drivers, all_drivers=None):
        pool = all_drivers if all_drivers is not None else DRIVERS
        with mock.patch.object(bot, "_get_all_drivers_cached",
                               mock.MagicMock(return_value=pool)), \
                mock.patch.object(bot, "_get_suspended_driver_ids",
                                  mock.MagicMock(return_value=set())):
            return bot._drivers_sent_label(state_data, drivers)

    def test_one_driver_is_named(self):
        self.assertEqual(self._label({"selected_driver_names": "Kita"}, DRIVERS[:1]), "Kita")

    def test_a_few_drivers_are_all_named(self):
        """A genuine subset lists the names — here 2 picked out of a pool of 3."""
        pool = DRIVERS + [{"id": "d3", "driver_name": "Ava", "is_active": True}]
        self.assertEqual(
            self._label({"selected_driver_names": "Kita"}, DRIVERS, all_drivers=pool),
            "Kita, Sam Okafor")

    def test_everyone_says_all_drivers_instead_of_a_wall_of_names(self):
        self.assertEqual(self._label({"selected_driver_names": "All Drivers"}, DRIVERS),
                         "All drivers (2)")

    def test_everyone_is_detected_even_without_an_explicit_all_pick(self):
        self.assertEqual(self._label({}, DRIVERS), "All drivers (2)")

    def test_a_long_list_is_trimmed(self):
        many = [{"id": f"d{i}", "driver_name": f"Driver{i}", "is_active": True}
                for i in range(9)]
        label = self._label({"selected_driver_names": "Driver0"}, many[:8], all_drivers=many)
        self.assertIn("+4 more", label)

    def test_no_drivers_is_stated_plainly(self):
        self.assertEqual(self._label({}, []), "no drivers")

    def test_the_confirmation_uses_the_label_not_a_count(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        # Only the CONFIRMATION line matters — "driver(s)" is fine elsewhere, e.g.
        # the picker's "Select which driver(s) to notify".
        self.assertIn("Approval sent to <b>{drivers_label}</b>", src)
        self.assertNotIn("drivers_count", src, "the bare count is no longer built")


if __name__ == "__main__":
    unittest.main()
