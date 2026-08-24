r"""The driver roster is a list you can tap, in /settings and /drivers alike.

Asked for: "the driver list in /settings and /drivers should utilize better list
like using the inline buttons especially for full info … and in both allow adding
removing suspending and unsuspending drivers".

A Telegram button label is one short line, so it carries the name and the status;
the phone, email and chat id live on that driver's own screen, one tap away, next
to every action.

Run:  venv\Scripts\python.exe -m pytest tests/test_driver_buttons.py -q
"""
import asyncio
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

FULL = {"id": "d1", "driver_name": "Kita", "is_active": True,
        "phone_number": "551-301-3737", "email": "kita@x.com",
        "driver_telegram_id": "123456789"}
EMPTY = {"id": "d2", "driver_name": "Marco", "is_active": True}
OFF = {"id": "d3", "driver_name": "Sara", "is_active": False, "email": "sara@x.com"}
SUSP = {"id": "d4", "driver_name": "Tony", "is_active": True,
        "phone_number": "201-555-0000", "driver_telegram_id": "999"}
ALL = [FULL, EMPTY, OFF, SUSP]


def _screen(drivers=ALL, suspended=("d4",)):
    with mock.patch.object(bot, "_get_all_drivers_cached", lambda: list(drivers)), \
            mock.patch.object(bot, "_get_suspended_driver_ids", lambda: set(suspended)):
        return asyncio.run(bot._settings_view_drivers())


def _labels(kb):
    return [b.text for row in kb.inline_keyboard for b in row]


def _data(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def _drivers_cmd(uid=777, drivers=ALL, suspended=()):
    msg = mock.MagicMock()
    msg.reply_text = mock.AsyncMock()
    update = mock.MagicMock()
    update.effective_message = msg
    update.effective_user = mock.MagicMock(id=uid)
    with mock.patch.object(bot, "_get_all_drivers_cached", lambda: list(drivers)), \
            mock.patch.object(bot, "_get_suspended_driver_ids", lambda: set(suspended)), \
            mock.patch.object(bot, "_user_is_global_supervisor", lambda u: u == 777):
        asyncio.run(bot.cmd_drivers(update, mock.MagicMock()))
    return msg.reply_text.await_args_list


class TheListIsButtonsTest(unittest.TestCase):

    def test_every_driver_gets_a_row(self):
        _, kb = _screen()
        rows = [d for d in _data(kb) if d.startswith("tset_drv:")]
        self.assertEqual(4, len(rows))

    def test_the_label_carries_name_and_status(self):
        _, kb = _screen()
        labels = _labels(kb)
        self.assertIn("✅ Kita", labels)
        self.assertIn("⛔ Sara — off", labels)
        self.assertIn("🚫 Tony — suspended", labels)

    def test_the_screen_says_where_the_details_are(self):
        text, _ = _screen()
        for word in ("phone", "email", "chat id"):
            self.assertIn(word, text.lower(), word)

    def test_adding_is_offered(self):
        _, kb = _screen()
        self.assertIn("tset_dadd", _data(kb))

    def test_an_empty_roster_says_so(self):
        text, _ = _screen(drivers=[])
        self.assertIn("No drivers yet", text)


class ADriversOwnScreenTest(unittest.TestCase):

    def test_it_shows_all_three_details(self):
        text, _ = bot._driver_detail(FULL, set())
        for want in ("551-301-3737", "kita@x.com", "123456789"):
            self.assertIn(want, text, want)

    def test_blanks_are_visible_not_omitted(self):
        text, _ = bot._driver_detail(EMPTY, set())
        self.assertEqual(3, text.count("—") - text.count("means nothing"), text)

    def test_every_action_is_there(self):
        _, kb = bot._driver_detail(FULL, set())
        data = _data(kb)
        self.assertTrue(any(d.startswith("tset_dsusp:") for d in data), data)
        self.assertTrue(any(d.startswith("tset_dtog:") for d in data), data)
        self.assertIn("tset_drivers", data)

    def test_a_suspended_driver_is_offered_a_lift(self):
        text, kb = bot._driver_detail(SUSP, {"d4"})
        self.assertIn("suspended", text)
        self.assertTrue(any(d.startswith("tset_dlift:") for d in _data(kb)))
        self.assertFalse(any(d.startswith("tset_dsusp:") for d in _data(kb)))

    def test_a_disabled_driver_is_offered_enable(self):
        _, kb = bot._driver_detail(OFF, set())
        self.assertIn("🔌 Enable", _labels(kb))

    def test_an_active_driver_is_offered_disable(self):
        _, kb = bot._driver_detail(FULL, set())
        self.assertIn("⛔ Disable", _labels(kb))


class TheCommandIsTheSameListTest(unittest.TestCase):

    def test_it_posts_buttons_not_a_wall_of_text(self):
        calls = _drivers_cmd()
        kb = calls[0].kwargs.get("reply_markup")
        self.assertIsNotNone(kb, "the roster should be tappable")
        self.assertTrue(any(d.startswith("tset_drv:") for d in _data(kb)))

    def test_every_driver_is_reachable(self):
        calls = _drivers_cmd()
        rows = [d for c in calls for d in _data(c.kwargs["reply_markup"])
                if d.startswith("tset_drv:")]
        self.assertEqual(4, len(rows))

    def test_it_can_add_too(self):
        calls = _drivers_cmd()
        data = [d for c in calls for d in _data(c.kwargs["reply_markup"])]
        self.assertIn("tset_dadd", data)

    def test_still_supervisors_only(self):
        self.assertEqual([], list(_drivers_cmd(uid=42)))

    def test_an_empty_roster_says_so(self):
        calls = _drivers_cmd(drivers=[])
        self.assertIn("No drivers yet", calls[0][0][0])


class ALongRosterIsSplitTest(unittest.TestCase):
    BIG = [{"id": f"d{i}", "driver_name": f"Driver {i}", "is_active": True}
           for i in range(1, 96)]

    def test_the_command_lists_every_last_one(self):
        calls = _drivers_cmd(drivers=self.BIG)
        rows = [d for c in calls for d in _data(c.kwargs["reply_markup"])
                if d.startswith("tset_drv:")]
        self.assertEqual(95, len(rows))

    def test_no_keyboard_is_unreadably_long(self):
        for kb in bot._driver_list_keyboard(self.BIG, set()):
            self.assertLessEqual(len(kb.inline_keyboard), bot._DRIVER_ROWS_PER_MSG + 2)

    def test_the_settings_screen_says_what_it_could_not_fit(self):
        text, _ = _screen(drivers=self.BIG, suspended=())
        self.assertIn("/drivers", text)


class TheTapsReachAHandlerTest(unittest.TestCase):
    """/drivers posts these buttons OUTSIDE /settings — a lesson already learnt once."""

    def test_the_settings_buttons_are_entry_points(self):
        """Every one of them — a stale screen outlives the conversation."""
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        entry = src.split("settings_conv = ConversationHandler(", 1)[1].split("states={", 1)[0]
        self.assertIn('CallbackQueryHandler(handle_settings_cb, pattern=r"^tset_")', entry)

    def test_every_callback_the_screens_emit_matches_that_pattern(self):
        import re
        pattern = re.compile(r"^tset_")
        _, kb = _screen()
        emitted = set(_data(kb))
        for d in ALL:
            emitted.update(_data(bot._driver_detail(d, {"d4"})[1]))
        unreachable = [d for d in emitted if not pattern.match(d)]
        self.assertEqual([], unreachable, f"these taps reach nothing: {unreachable}")


if __name__ == "__main__":
    unittest.main()
