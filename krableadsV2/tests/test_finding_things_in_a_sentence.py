r"""Two places that demanded a value arrive alone.

REFERENCE IDS. Both the receipt flow and the appeal flow handed the ENTIRE
message to an exact `.eq("reference_id", …)`, so every one of these came back
"Reference ID not found":

    ref ABC12345          Reference: ABC12345      here it is ABC12345
    ABC12345 thanks       its ABC12345             A B C 1 2 3 4 5

The last is not a typo — it is how a phone transcribes someone reading an id
aloud, and this is the flow drivers use standing in the street.

SETTINGS NAVIGATION. "back to drivers" and "close the drivers list" both name
exactly where the operator wants to go, and a one-word prefix threw it away:

    "back to drivers"        -> _SETTINGS_BACK_RE matched  -> main menu
    "close the drivers list" -> _SETTINGS_CLOSE_RE matched -> settings closed

Run:  venv\Scripts\python.exe -m pytest tests/test_finding_things_in_a_sentence.py -q
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
os.environ.pop("OPENAI_API_KEY", None)

import utils.database as udb  # noqa: E402

if "bot" not in sys.modules:
    udb.Database = mock.MagicMock()
import bot  # noqa: E402

ON_FILE = {"ABC12345", "XY9876543"}


def exists(candidate):
    return candidate in ON_FILE


class AReferenceIdInsideASentenceTest(unittest.TestCase):

    AROUND_IT = [
        "ABC12345", "abc12345", " ABC12345 ", "ref ABC12345", "REF ABC12345",
        "Reference: ABC12345", "reference id ABC12345", "here it is ABC12345",
        "ABC12345 thanks", "its ABC12345", "it's ABC12345",
        "reference id is ABC12345 for the nissan", "the ref is ABC12345 ok",
    ]

    def test_it_is_found_however_it_is_wrapped(self):
        for t in self.AROUND_IT:
            with self.subTest(sent=t):
                self.assertEqual(bot.extract_reference_id(t, exists), "ABC12345", t)

    def test_an_id_read_out_loud(self):
        """A phone transcribing "A, B, C, one, two…" spaces every character."""
        for t in ("A B C 1 2 3 4 5", "a b c 1 2 3 4 5", "ref A B C 1 2 3 4 5"):
            with self.subTest(sent=t):
                self.assertEqual(bot.extract_reference_id(t, exists), "ABC12345", t)

    def test_the_older_id_format_still_resolves(self):
        self.assertEqual(bot.extract_reference_id("appeal XY9876543", exists),
                         "XY9876543")

    def test_an_unknown_id_falls_back_to_the_whole_message(self):
        """Exactly today's behaviour, so a lookup can only go from failing to
        working — never the reverse."""
        for t in ("no idea", "12345", "ZZZZZZZZ"):
            with self.subTest(sent=t):
                self.assertEqual(bot.extract_reference_id(t, exists), t.strip().upper())

    def test_the_one_that_actually_exists_wins(self):
        """Two candidates, one real: verify before preferring."""
        self.assertEqual(
            bot.extract_reference_id("was it QQQQQQQQ or ABC12345", exists),
            "ABC12345")

    def test_with_no_verifier_it_still_picks_a_well_formed_id(self):
        self.assertEqual(bot.extract_reference_id("ref ABC12345"), "ABC12345")

    def test_both_flows_use_it(self):
        bot_src = (ROOT / "bot.py").read_text(encoding="utf-8")
        appeal_src = (ROOT / "appeal_flow.py").read_text(encoding="utf-8")
        for name, src in (("bot.py", bot_src), ("appeal_flow.py", appeal_src)):
            with self.subTest(file=name):
                self.assertIn("extract_reference_id(msg.text", src)
                self.assertNotIn("reference_id = msg.text.strip().upper()", src)


class ANamedScreenBeatsALeadingBackTest(unittest.TestCase):

    NAMED = [
        ("back to drivers", "tset_drivers"),
        ("go back to plates", "tset_plates"),
        ("back to the sources", "tset_srcs"),
        ("close the drivers list", "tset_drivers"),
        ("drivers", "tset_drivers"),
        ("supervisors", "tset_sups"),
    ]

    def test_the_destination_is_still_recognised(self):
        for phrase, target in self.NAMED:
            with self.subTest(said=phrase):
                self.assertEqual(bot._settings_nav_target(phrase), target)

    def test_the_handler_asks_for_the_destination_first(self):
        """A bare "back" still goes back — there is no destination in it to
        prefer. The order is what this pins."""
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        body = src.split("async def handle_settings_text", 1)[1].split("\nasync def ", 1)[0]
        nav = body.index("_settings_nav_target(text)")
        back = body.index("_SETTINGS_BACK_RE.match(text)")
        close = body.index("_SETTINGS_CLOSE_RE.match(text)")
        self.assertLess(nav, back, "a named screen must beat 'back'")
        self.assertLess(nav, close, "a named screen must beat 'close'")

    def test_a_bare_escape_word_has_no_destination(self):
        for t in ("back", "close", "exit", "done"):
            with self.subTest(said=t):
                self.assertIsNone(bot._settings_nav_target(t), t)


if __name__ == "__main__":
    unittest.main()
