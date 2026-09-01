"""/settings is open, the operator types /lead and pastes a client.

The paste used to open the DRIVERS screen and vanish. The settings conversation
is registered ahead of the lead flow, and its text listener matched its keyword
rules ANYWHERE in the message -- so the "driver license" line inside an ordinary
pasted lead read as "show me the drivers".
"""
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bot                                                   # noqa: E402


PASTED_LEAD = """John Carter
845-555-0123
12 Mulberry St, Newark NJ 07102
Driver license 883-291-004
1HGCM82633A004352
2019 Honda Accord
Silver
Geico
0407306000
now 1 hour"""


class APastedLeadIsNotANavigationPhraseTest(unittest.TestCase):

    def test_the_pasted_lead_does_not_open_the_drivers_screen(self):
        """The exact report: paste a lead in /settings, land on /drivers."""
        self.assertIsNone(
            bot._settings_nav_target(PASTED_LEAD),
            "a pasted lead still reads as a settings destination")

    def test_the_word_driver_inside_a_lead_is_not_a_request_for_drivers(self):
        self.assertIsNone(bot._settings_nav_target(
            "John Carter\n845-555-0123\nDriver license 883-291-004"))

    def test_a_single_long_line_is_not_navigation_either(self):
        """Not every paste has newlines in it."""
        self.assertIsNone(bot._settings_nav_target(
            "John Carter 845-555-0123 12 Mulberry St Newark NJ 07102 "
            "driver license 883-291-004 2019 Honda Accord"))

    def test_settings_never_even_claims_the_paste(self):
        """The filter, not the callback, is what has to refuse it: a
        ConversationHandler consumes whatever its state matches."""
        self.assertFalse(bot._looks_like_settings_phrase(PASTED_LEAD))


class TheScreensAreStillReachableTest(unittest.TestCase):
    """The guard must not cost the feature it is guarding."""

    PHRASES = [
        ("drivers", "tset_drivers"),
        ("plate numbers", "tset_plates"),
        ("close the drivers list", "tset_drivers"),
        ("go back to plates", "tset_plates"),
        ("show me the dispatchers", "tset_groups"),
        ("supervisors", "tset_sups"),
        ("paper girls", "tset_pg"),
        ("instant tag", "tset_instant"),
        ("recent leads", "tset_recent"),
        ("suspensions", "tset_susp"),
        ("follow-ups", "tset_fu"),
        ("client sources", "tset_srcs"),
    ]

    def test_every_spoken_destination_still_works(self):
        for said, target in self.PHRASES:
            with self.subTest(said=said):
                self.assertEqual(target, bot._settings_nav_target(said))
                self.assertTrue(bot._looks_like_settings_phrase(said))

    def test_a_short_unknown_phrase_still_reaches_the_hint(self):
        """Short gibberish must still be CLAIMED by settings, so the operator
        gets the "say or type what you want" hint instead of silence."""
        self.assertTrue(bot._looks_like_settings_phrase("wibble"))
        self.assertIsNone(bot._settings_nav_target("wibble"))

    def test_back_and_close_still_reach_the_handler(self):
        for t in ("back", "close", "exit", "done"):
            with self.subTest(said=t):
                self.assertTrue(bot._looks_like_settings_phrase(t))


class StartingALeadClosesSettingsTest(unittest.TestCase):

    def test_begin_lead_command_lets_go_of_the_settings_card(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        body = src.split("async def begin_lead_command", 1)[1].split("\nasync def ", 1)[0]
        self.assertIn("_end_settings_conversation(update)", body,
                      "/lead still leaves the settings conversation open behind it")

    def test_the_closer_is_safe_when_nothing_is_registered(self):
        """No handler wired up (a bare import, a test) must not raise."""
        keep = bot._SETTINGS_CONV_HANDLER
        try:
            bot._SETTINGS_CONV_HANDLER = None
            self.assertFalse(bot._end_settings_conversation(object()))
        finally:
            bot._SETTINGS_CONV_HANDLER = keep


class TheMenuStateFiltersOnShapeTest(unittest.TestCase):

    def test_the_text_listener_is_filtered_not_just_the_callback(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        block = src.split("SET_MENU: [", 1)[1].split("],", 1)[0]
        self.assertIn("_SettingsPhraseFilter()", block,
                      "SET_MENU still claims every text message it is offered")


if __name__ == "__main__":
    unittest.main()
