r"""A phone said mid-sentence must not swallow the rest of the sentence.

Reported, verbatim off the card:

    Phone: 551-301-3737. The colors is black.
    Color: -

from "Price $150 phone number 551-301-3737. The colors is black." Three faults:

  * "colors" is not "color" — the alias regex demanded a word boundary straight
    after the label, so a plural label matched nothing and no new field started;
  * the phone cleaner only CHECKED for ten digits and returned the string it was
    given, so everything after the number was stored as part of the number; and
  * a value ending a sentence kept the full stop ("black.").

Run:  venv\Scripts\python.exe -m pytest tests/test_phone_sentence_tail.py -q
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

REPORTED = "Price $150 phone number 551-301-3737. The colors is black."


class TheReportedLineTest(unittest.TestCase):

    def test_all_three_fields_land(self):
        self.assertEqual(
            [("price", "$150"), ("phone", "551-301-3737"), ("col", "black")],
            bot._parse_multi_field_line(REPORTED))

    def test_the_phone_is_only_the_phone(self):
        pairs = dict(bot._parse_multi_field_line(REPORTED))
        self.assertNotIn("black", pairs["phone"])
        self.assertNotIn("The", pairs["phone"])

    def test_the_colour_is_no_longer_lost(self):
        self.assertEqual("black", dict(bot._parse_multi_field_line(REPORTED))["col"])

    def test_it_reaches_the_card(self):
        card = {}
        bot._apply_inline_review_text(card, REPORTED)
        self.assertEqual("551-301-3737", card.get("pending_phone_number"))
        self.assertEqual("$150", card.get("pending_price"))
        self.assertEqual("Black", str(card.get("color")).title())


class ThePhoneIsExtractedTest(unittest.TestCase):
    """Taking the number OUT is what makes this robust to any wording."""

    def test_a_number_mid_sentence(self):
        self.assertEqual("551-301-3737",
                         bot._clean_inline_value("phone", "551-301-3737. The colors is black."))

    def test_a_trailing_full_stop_goes(self):
        self.assertEqual("551-301-3737", bot._clean_inline_value("phone", "551-301-3737."))

    def test_the_usual_shapes_survive_intact(self):
        for said in ("(551) 301-3737", "551.301.3737", "551 301 3737", "5513013737",
                     "+1 551-301-3737"):
            with self.subTest(said=said):
                got = bot._clean_inline_value("phone", said)
                self.assertEqual(10, len(__import__("re").sub(r"\D", "", got).lstrip("1")), got)

    def test_words_with_no_number_are_still_rejected(self):
        for said in ("no phone here", "call me later", "phone is dead"):
            with self.subTest(said=said):
                self.assertEqual("", bot._clean_inline_value("phone", said))

    def test_a_short_number_is_still_rejected(self):
        self.assertEqual("", bot._clean_inline_value("phone", "301-3737"))


class PluralLabelsTest(unittest.TestCase):
    """"the colors is black" is how it gets said and dictated."""

    def test_a_plural_label_is_the_same_label(self):
        for line, want in [("colors black", ("col", "black")),
                           ("prices 150", ("price", "$150")),
                           ("phone numbers 551-301-3737", ("phone", "551-301-3737")),
                           ("addresses 123 Main St", ("addr", "123 Main St"))]:
            with self.subTest(line=line):
                self.assertIn(want, bot._parse_multi_field_line(line) or [])

    def test_a_possessive_label_is_the_same_label(self):
        self.assertEqual([("name", "John Damian")],
                         bot._parse_multi_field_line("client's name John Damian"))

    def test_the_singular_still_works(self):
        self.assertEqual([("col", "black")], bot._parse_multi_field_line("color black"))


class TrailingPunctuationTest(unittest.TestCase):

    def test_a_sentence_final_value_drops_its_full_stop(self):
        self.assertEqual("black", bot._clean_inline_value("col", "black."))
        self.assertEqual("John Damian", bot._clean_inline_value("name", "John Damian."))

    def test_a_value_that_is_only_punctuation_is_kept_as_typed(self):
        """Minus still clears the field."""
        self.assertEqual("-", bot._clean_inline_value("col", "-"))

    def test_an_email_keeps_its_dots(self):
        self.assertEqual("rrod782@gmail.com",
                         bot._clean_inline_value("email", "rrod782@gmail.com"))

    def test_a_price_keeps_its_decimals(self):
        self.assertEqual("$150.50", bot._clean_inline_value("price", "150.50"))


class OtherSentencesTest(unittest.TestCase):
    """The same shape, said other ways."""

    def test_a_car_after_the_phone(self):
        pairs = dict(bot._parse_multi_field_line(
            "phone number +1 551-301-3737. Car is a 2019 Honda Accord."))
        self.assertEqual("+1 551-301-3737", pairs["phone"])
        self.assertIn("Honda Accord", pairs["car"])

    def test_three_sentences_in_a_row(self):
        pairs = dict(bot._parse_multi_field_line(
            "name John Damian. Phone 551.301.3737. Price $200."))
        self.assertEqual("John Damian", pairs["name"])
        self.assertEqual("551.301.3737", pairs["phone"])
        self.assertEqual("$200", pairs["price"])


if __name__ == "__main__":
    unittest.main()
