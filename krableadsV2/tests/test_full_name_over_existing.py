r"""Giving both halves of the name at once replaces the name that is already there.

Reported: "after sending first name and last name, john doe over a previously
entered name it didn't work". Naming both labels and then saying the whole name is
ONE edit, but it parsed as two: the first label got no value of its own and the
last one swallowed everything, so a card reading "Maria Gonzalez" became

    Maria , john doe

Run:  venv\Scripts\python.exe -m pytest tests/test_full_name_over_existing.py -q
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


def _card():
    """A card that already has a name on it — the reported starting point."""
    return {"name": "Maria Gonzalez", "first_name": "Maria", "last_name": "Gonzalez"}


def _apply(line, card=None):
    card = _card() if card is None else card
    changed = bot._apply_inline_review_text(card, line)
    return card, changed


class TheReportedLineTest(unittest.TestCase):

    def test_it_replaces_the_name_that_was_there(self):
        card, changed = _apply("first name and last name, john doe")
        self.assertEqual("john doe", card.get("name"))
        self.assertTrue(changed, "the edit reported no change at all")

    def test_nothing_of_the_old_name_survives(self):
        card, _ = _apply("first name and last name, john doe")
        self.assertNotIn("Maria", card.get("name"))
        self.assertNotIn("Gonzalez", card.get("name"))

    def test_the_stray_comma_does_not_become_part_of_the_name(self):
        card, _ = _apply("first name and last name, john doe")
        self.assertNotIn(",", card.get("name"))

    def test_it_splits_back_into_first_and_last(self):
        card, _ = _apply("first name and last name, john doe")
        self.assertEqual(("john", "doe"), bot._display_name_parts(card))


class EveryWayOfSayingBothHalvesTest(unittest.TestCase):

    def test_the_phrasings(self):
        for line in ("first name and last name, john doe",
                     "first name and last name john doe",
                     "first name last name john doe",
                     "first and last name john doe",
                     "first and last john doe",
                     "first last name john doe",
                     "full name john doe",
                     "client name john doe",
                     "name john doe"):
            with self.subTest(line=line):
                card, _ = _apply(line)
                self.assertEqual("john doe", card.get("name"), line)

    def test_it_works_on_an_empty_card_too(self):
        card, _ = _apply("first name and last name john doe", card={})
        self.assertEqual("john doe", card.get("name"))

    def test_a_three_word_name(self):
        card, _ = _apply("first name and last name john van doe")
        self.assertEqual("john van doe", card.get("name"))


class OneHalfAtATimeStillWorksTest(unittest.TestCase):
    """Naming both must not break naming one."""

    def test_the_first_name_alone_keeps_the_last(self):
        card, _ = _apply("first name john")
        self.assertEqual("john Gonzalez", card.get("name"))

    def test_the_last_name_alone_keeps_the_first(self):
        card, _ = _apply("last name doe")
        self.assertEqual("Maria doe", card.get("name"))

    def test_both_given_with_their_own_values_stay_separate(self):
        """"first name john last name doe" has a value after EACH label."""
        self.assertEqual([("fn", "john"), ("ln", "doe")],
                         bot._parse_multi_field_line("first name john last name doe"))
        card, _ = _apply("first name john last name doe")
        self.assertEqual("john doe", card.get("name"))


class TheMergeIsNarrowTest(unittest.TestCase):
    """Only name labels that genuinely run together collapse."""

    def test_other_fields_are_not_merged(self):
        self.assertEqual([("price", "$150"), ("col", "black")],
                         bot._parse_multi_field_line("price 150 color black"))

    def test_a_name_label_next_to_a_non_name_label_is_untouched(self):
        self.assertEqual([("name", "john doe"), ("ins", "GEICO")],
                         bot._parse_multi_field_line("name john doe insurance geico"))

    def test_a_value_between_the_labels_stops_the_merge(self):
        pairs = bot._parse_multi_field_line("first name john last name doe")
        self.assertEqual(2, len(pairs), pairs)

    def test_a_leading_comma_is_stripped_for_any_field(self):
        self.assertEqual("black", bot._clean_inline_value("col", ", black"))
        self.assertEqual("John Damian", bot._clean_inline_value("name", ": John Damian"))


if __name__ == "__main__":
    unittest.main()
