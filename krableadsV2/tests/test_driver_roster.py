r"""Every driver, with every contact detail, whether it is filled in or not.

Asked for: "under /settings the drivers side doesnt show all driver phone number
email and chat id it should show all and also show all drivers whether they have
the info or not and after add it to /drivers for supervisors only".

The screen printed a phone or an email only when it had one, never showed the chat
id at all, and stopped at 25 drivers — so a half-filled record looked complete and
the rest of the roster was simply absent.

Run:  venv\Scripts\python.exe -m pytest tests/test_driver_roster.py -q
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
HALF = {"id": "d3", "driver_name": "Sara", "is_active": False, "email": "sara@x.com"}
SUSP = {"id": "d4", "driver_name": "Tony", "is_active": True,
        "phone_number": "201-555-0000", "driver_telegram_id": "999"}
ALL = [FULL, EMPTY, HALF, SUSP]


def _settings_screen(drivers=ALL, suspended=("d4",)):
    with mock.patch.object(bot, "_get_all_drivers_cached", lambda: list(drivers)), \
            mock.patch.object(bot, "_get_suspended_driver_ids", lambda: set(suspended)):
        return asyncio.run(bot._settings_view_drivers())


def _drivers_command(uid=777, drivers=ALL, suspended=()):
    msg = mock.MagicMock()
    msg.reply_text = mock.AsyncMock()
    update = mock.MagicMock()
    update.effective_message = msg
    update.effective_user = mock.MagicMock(id=uid)
    with mock.patch.object(bot, "_get_all_drivers_cached", lambda: list(drivers)), \
            mock.patch.object(bot, "_get_suspended_driver_ids", lambda: set(suspended)), \
            mock.patch.object(bot, "_user_is_global_supervisor", lambda u: u == 777):
        asyncio.run(bot.cmd_drivers(update, mock.MagicMock()))
    return [c[0][0] for c in msg.reply_text.await_args_list]


class AllThreeDetailsAlwaysTest(unittest.TestCase):

    def test_a_full_record_shows_all_three(self):
        lines = "\n".join(bot._driver_contact_lines(FULL))
        for want in ("551-301-3737", "kita@x.com", "123456789"):
            self.assertIn(want, lines, want)

    def test_the_chat_id_is_shown_at_all(self):
        """It was stored and never displayed anywhere."""
        self.assertIn("123456789", "\n".join(bot._driver_contact_lines(FULL)))

    def test_an_empty_record_still_shows_three_lines(self):
        lines = bot._driver_contact_lines(EMPTY)
        self.assertEqual(3, len(lines))
        self.assertEqual(3, sum(1 for l in lines if "—" in l))

    def test_a_half_filled_record_shows_what_is_missing(self):
        """The point: a blank must be visible, not omitted."""
        lines = "\n".join(bot._driver_contact_lines(HALF))
        self.assertIn("sara@x.com", lines)
        self.assertEqual(2, lines.count("—"), lines)

    def test_a_dash_on_file_counts_as_empty(self):
        lines = bot._driver_contact_lines({"phone_number": "-", "email": "  "})
        self.assertEqual(3, sum(1 for l in lines if "—" in l))


class TheSettingsScreenTest(unittest.TestCase):

    def test_every_driver_is_listed(self):
        text, _ = _settings_screen()
        for d in ALL:
            with self.subTest(driver=d["driver_name"]):
                self.assertIn(d["driver_name"], text)

    def test_the_count_is_stated(self):
        text, _ = _settings_screen()
        self.assertIn("4 on file", text)

    def test_disabled_and_suspended_are_marked(self):
        text, _ = _settings_screen()
        self.assertIn("disabled", text)
        self.assertIn("suspended", text)

    def test_each_driver_still_has_its_toggle(self):
        _, kb = _settings_screen()
        labels = [b.text for row in kb.inline_keyboard for b in row]
        self.assertIn("Disable Kita", labels)
        self.assertIn("Enable Sara", labels)

    def test_an_empty_roster_says_so(self):
        text, _ = _settings_screen(drivers=[])
        self.assertIn("No drivers yet", text)


class ALongRosterIsNotSilentlyCutTest(unittest.TestCase):
    """One screen is one Telegram message, so what does not fit must be declared."""

    BIG = [{"id": f"d{i}", "driver_name": f"Driver Number {i}", "is_active": True,
            "phone_number": f"201-555-{i:04d}", "email": f"driver{i}@example.com",
            "driver_telegram_id": str(100000000 + i)} for i in range(1, 61)]

    def test_the_screen_fits_inside_telegrams_limit(self):
        text, _ = _settings_screen(drivers=self.BIG, suspended=())
        self.assertLess(len(text), 4096, "Telegram would reject this message")

    def test_it_says_how_many_it_could_not_show(self):
        text, _ = _settings_screen(drivers=self.BIG, suspended=())
        self.assertIn("more — see /drivers", text)

    def test_it_says_the_buttons_are_capped(self):
        text, kb = _settings_screen(drivers=self.BIG, suspended=())
        self.assertIn("first 30 of 60", text)
        self.assertLessEqual(len([b for r in kb.inline_keyboard for b in r]), 32)

    def test_drivers_command_lists_every_last_one(self):
        msgs = _drivers_command(drivers=self.BIG)
        self.assertGreater(len(msgs), 1, "60 drivers cannot fit in one message")
        self.assertEqual(60, sum(m.count("Driver Number") for m in msgs))

    def test_no_message_exceeds_the_limit(self):
        for m in _drivers_command(drivers=self.BIG):
            self.assertLess(len(m), 4096)

    def test_a_driver_is_never_split_across_messages(self):
        """Each block is name + three details, kept together."""
        for m in _drivers_command(drivers=self.BIG):
            self.assertEqual(m.count("Driver Number"), m.count("📞"), m[:80])


class TheCommandIsSupervisorsOnlyTest(unittest.TestCase):

    def test_a_supervisor_gets_the_roster(self):
        msgs = _drivers_command(uid=777)
        self.assertTrue(msgs)
        self.assertIn("Kita", msgs[0])

    def test_anyone_else_gets_nothing(self):
        self.assertEqual([], _drivers_command(uid=42))

    def test_it_is_registered_under_the_name_asked_for(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        self.assertIn('CommandHandler(["drivers", "driverlist", "roster"], cmd_drivers)', src)

    def test_it_checks_the_supervisor_gate(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        body = src.split("async def cmd_drivers", 1)[1].split("async def ", 1)[0]
        self.assertIn("_user_is_global_supervisor", body)


class BothViewsShareOneRendererTest(unittest.TestCase):
    """So the screen and the command can never disagree about a driver."""

    def test_the_blocks_helper_is_used_by_both(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        self.assertEqual(1, src.count("def _driver_roster_blocks("))
        self.assertEqual(2, src.count("_driver_roster_blocks(drivers, suspended)"))

    def test_a_block_is_a_name_plus_three_details(self):
        blocks = bot._driver_roster_blocks([FULL])
        self.assertEqual(4, len(blocks[0]))

    def test_no_drivers_gives_no_blocks(self):
        self.assertEqual([], bot._driver_roster_blocks([]))


if __name__ == "__main__":
    unittest.main()
