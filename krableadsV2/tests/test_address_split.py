"""Splitting one typed/spoken address into the street line and the city/ST/ZIP line.

The review card keeps them in SEPARATE fields, but people type an address as one
string ("123 Main St, Newark NJ 07102"). Before this, the whole string landed in the
street field and city/ST/ZIP stayed "-".

Run:  venv\\Scripts\\python.exe -m pytest tests/test_address_split.py -q
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

# Only stub the DB class if nothing has imported bot yet — another test module may
# already have bound its own fake to bot.db, and clobbering it breaks that module.
if "bot" not in sys.modules:
    udb.Database = mock.MagicMock()
import bot  # noqa: E402


# (typed address, expected street, expected city/ST/ZIP)
SPLITS = [
    # comma-separated (the common written form)
    ("123 Main St, Newark NJ 07102", "123 Main St", "Newark NJ 07102"),
    ("88 Ocean Ave Apt 3B, Fort Lee, NJ 07024", "88 Ocean Ave Apt 3B", "Fort Lee, NJ 07024"),
    ("45 W 34th St, New York, NY 10001", "45 W 34th St", "New York, NY 10001"),
    # no commas at all (how a dictated address arrives)
    ("543 Garden Place Keyport NJ 07735", "543 Garden Place", "Keyport NJ 07735"),
    ("77 Sunset Blvd Los Angeles CA 90028", "77 Sunset Blvd", "Los Angeles CA 90028"),
    ("350 5th Ave Suite 200 New York NY 10118", "350 5th Ave Suite 200", "New York NY 10118"),
    # spelled-out state
    ("15 Kennedy Blvd Apt 2 North Bergen New Jersey 07047",
     "15 Kennedy Blvd Apt 2", "North Bergen New Jersey 07047"),
    # multi-word cities, incl. ones starting with a compass word
    ("9 East Orange Ave East Orange NJ 07018", "9 East Orange Ave", "East Orange NJ 07018"),
    ("22 Bergen Turnpike West New York NJ 07093", "22 Bergen Turnpike", "West New York NJ 07093"),
    ("12 Oak Rd Jersey City NJ", "12 Oak Rd", "Jersey City NJ"),
    # a compass that belongs to the STREET, not the city
    ("1600 Pennsylvania Ave NW Washington DC 20500",
     "1600 Pennsylvania Ave NW", "Washington DC 20500"),
    ("500 Route 46 West Totowa NJ 07512", "500 Route 46 West", "Totowa NJ 07512"),
    # street with no suffix word
    ("123 Broadway New York NY 10001", "123 Broadway", "New York NY 10001"),
    # nothing to split
    ("200 Broadway", "200 Broadway", ""),
    ("Newark NJ 07102", "", "Newark NJ 07102"),
    ("", "", ""),
]


class SplitAddressTest(unittest.TestCase):
    def test_split_street_and_csz(self):
        failures = []
        for src, want_street, want_csz in SPLITS:
            got = bot._split_street_and_csz(src)
            if got != (want_street, want_csz):
                failures.append(f"{src!r}: got {got}, want {(want_street, want_csz)}")
        self.assertEqual([], failures, "\n".join(failures))

    def test_never_invents_or_loses_words(self):
        """Every word of the input must survive the split, and none may be added."""
        for src, _, _ in SPLITS:
            street, csz = bot._split_street_and_csz(src)
            rejoined = " ".join((street + " " + csz).split()).replace(",", "")
            original = " ".join(src.split()).replace(",", "")
            self.assertEqual(original.lower(), rejoined.strip().lower(), src)


class LabeledAddressEditTest(unittest.TestCase):
    """A labeled edit must fill BOTH fields and report both as updated."""

    def test_reg_address_fills_both_fields(self):
        d = {}
        updated = bot._apply_inline_review_text(d, "address 123 Main St, Newark NJ 07102")
        self.assertEqual(d.get("address"), "123 Main St")
        self.assertEqual(d.get("city_state_zip"), "Newark NJ 07102")
        self.assertIn("reg address", updated)
        self.assertIn("reg city/ST/ZIP", updated)

    def test_delivery_address_fills_both_fields(self):
        d = {}
        updated = bot._apply_inline_review_text(d, "delivery address 12 Oak Rd, Jersey City NJ 07305")
        self.assertEqual(d.get("delivery_address"), "12 Oak Rd")
        self.assertEqual(d.get("delivery_city_state_zip"), "Jersey City NJ 07305")
        self.assertIn("delivery address", updated)
        self.assertIn("delivery city/ST/ZIP", updated)

    def test_city_only_value_goes_to_csz_not_street(self):
        d = {}
        bot._apply_inline_review_text(d, "address Newark NJ 07102")
        self.assertEqual(d.get("city_state_zip"), "Newark NJ 07102")
        self.assertNotEqual(d.get("address"), "Newark NJ 07102")

    def test_street_only_value_leaves_csz_alone(self):
        d = {}
        bot._apply_inline_review_text(d, "address 200 Broadway")
        self.assertEqual(d.get("address"), "200 Broadway")
        self.assertIn(str(d.get("city_state_zip") or "-"), ("-", ""))

    def test_multi_field_line_still_splits_the_address(self):
        d = {}
        updated = bot._apply_inline_review_text(
            d, "price 200 address 123 Main St, Newark NJ 07102")
        self.assertEqual(d.get("pending_price"), "$200")
        self.assertEqual(d.get("address"), "123 Main St")
        self.assertEqual(d.get("city_state_zip"), "Newark NJ 07102")
        self.assertIn("price", updated)


class SmartPlacedAddressTest(unittest.TestCase):
    """An UNLABELED address ('123 Main St, Newark NJ 07102') splits too."""

    def test_bare_address_value_splits(self):
        import asyncio
        d = {}
        with mock.patch.object(bot.Config, "is_ai_vision_configured", classmethod(lambda cls: False)):
            updated = asyncio.run(
                bot._smart_place_single_value(d, "123 Main St, Newark NJ 07102"))
        self.assertEqual(d.get("address"), "123 Main St")
        self.assertEqual(d.get("city_state_zip"), "Newark NJ 07102")
        self.assertTrue(updated)


class AiSplitFallbackTest(unittest.TestCase):
    """The AI fallback only runs when needed, and never invents a city or ZIP."""

    def test_ai_split_rejects_invented_numbers(self):
        with mock.patch.object(
            bot.ai_vision, "_call_openai_text",
            mock.MagicMock(return_value='{"street":"123 Main St","city_state_zip":"Newark NJ 99999"}'),
        ):
            self.assertIsNone(bot.ai_vision.split_address("123 Main St Newark NJ"))

    def test_ai_split_accepts_a_faithful_answer(self):
        with mock.patch.object(
            bot.ai_vision, "_call_openai_text",
            mock.MagicMock(return_value='{"street":"123 Main St","city_state_zip":"Newark NJ 07102"}'),
        ):
            res = bot.ai_vision.split_address("123 Main St Newark NJ 07102")
        self.assertEqual(res, {"street": "123 Main St", "city_state_zip": "Newark NJ 07102"})

    def test_ai_not_called_when_already_split(self):
        import asyncio
        called = mock.MagicMock(return_value=None)
        d = {"address": "123 Main St", "city_state_zip": "Newark NJ 07102"}
        with mock.patch.object(bot.Config, "is_ai_vision_configured", classmethod(lambda cls: True)), \
                mock.patch.object(bot.ai_vision, "split_address", called):
            asyncio.run(bot._ai_split_addresses_if_needed(d))
        called.assert_not_called()

    def test_ai_rescues_what_the_splitter_could_not(self):
        import asyncio
        d = {"address": "Flat 4 12 Kings Road Chelsea London", "city_state_zip": "-"}
        with mock.patch.object(bot.Config, "is_ai_vision_configured", classmethod(lambda cls: True)), \
                mock.patch.object(
                    bot.ai_vision, "split_address",
                    mock.MagicMock(return_value={"street": "Flat 4 12 Kings Road",
                                                 "city_state_zip": "Chelsea London"})):
            changed = asyncio.run(bot._ai_split_addresses_if_needed(d))
        self.assertEqual(d["city_state_zip"], "Chelsea London")
        self.assertEqual(d["address"], "Flat 4 12 Kings Road")
        self.assertTrue(changed)


if __name__ == "__main__":
    unittest.main()
