r"""A dictated client name arrives with its own spelling recited after it.

Seen in production, on a supervisory card:

    Client name: Bianca Laidlaw, B-I-A-N-C-A-L-A-I-D-L-A-W. The

One utterance: the name, the caller spelling it out loud, and whatever the
transcript ran into next. Keep the name, drop the recital.

Run:  venv\Scripts\python.exe -m pytest tests/test_spoken_name.py -q
"""
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy-key")

import bot  # noqa: E402


class TheRecitalIsDroppedTest(unittest.TestCase):

    def test_the_exact_case_from_production(self):
        self.assertEqual(
            "Bianca Laidlaw",
            bot._clean_spoken_name(
                "Bianca Laidlaw, B-I-A-N-C-A-L-A-I-D-L-A-W. The"))

    def test_spelling_separated_by_spaces_or_dots(self):
        for raw in ("Bianca Laidlaw B I A N C A L A I D L A W",
                    "Bianca Laidlaw B.I.A.N.C.A",
                    "Bianca Laidlaw, b-i-a-n-c-a"):
            self.assertEqual("Bianca Laidlaw", bot._clean_spoken_name(raw), raw)

    def test_trailing_transcript_scraps_go_too(self):
        for raw in ("Maria Gonzalez. and", "Maria Gonzalez, uh",
                    "Maria Gonzalez the", "Maria Gonzalez. That"):
            self.assertEqual("Maria Gonzalez", bot._clean_spoken_name(raw), raw)


class RealNamesSurviveTest(unittest.TestCase):
    """The cleaner is only allowed to remove what is certainly a recital."""

    def test_initials_are_not_a_spell_out(self):
        for raw in ("J. R. R. Tolkien", "J R Smith", "A. B. Carter"):
            self.assertEqual(raw, bot._clean_spoken_name(raw), raw)

    def test_ordinary_names_are_untouched(self):
        for raw in ("Bianca Laidlaw", "Anne-Marie Dupont", "O'Connor, Sean",
                    "Jean-Luc de la Cruz", "Li Wei"):
            self.assertEqual(raw, bot._clean_spoken_name(raw), raw)

    def test_it_never_empties_a_name(self):
        """A name that really is just spelled letters is kept as-is — losing
        the client is far worse than an ugly card."""
        self.assertEqual("B-I-A-N-C-A", bot._clean_spoken_name("B-I-A-N-C-A"))

    def test_blank_stays_blank(self):
        self.assertEqual("", bot._clean_spoken_name(""))
        self.assertEqual("", bot._clean_spoken_name("   "))
        self.assertEqual("", bot._clean_spoken_name(None))


class ItIsAppliedWhereItMattersTest(unittest.TestCase):

    SRC = (ROOT / "bot.py").read_text(encoding="utf-8")

    def test_the_parser_cleans_the_name_it_stores(self):
        self.assertIn("name = _clean_spoken_name(field(0))", self.SRC)

    def test_display_cleans_it_too_for_leads_already_stored(self):
        body = self.SRC.split("def _client_display_name_from_lead", 1)[1]
        body = body.split("\ndef ", 1)[0]
        self.assertIn("_clean_spoken_name", body)

    def test_a_parsed_block_comes_out_clean(self):
        block = "\n".join([
            "Bianca Laidlaw, B-I-A-N-C-A-L-A-I-D-L-A-W. The",
            "19 Pennwood Dr", "Ewing, NJ 08638", "19 Pennwood Dr",
            "Ewing, NJ 08638", "1HGCM82633A004352", "2017 M Benz",
            "Black", "Geico", "POL1", "call first",
        ])
        parsed = bot.parse_phase1_structured(block)
        self.assertEqual("Bianca Laidlaw", parsed["name"])
        self.assertTrue(parsed["vehicle_details"].startswith("Bianca Laidlaw\n"))
        self.assertNotIn("B-I-A-N-C-A", parsed["vehicle_details"])


if __name__ == "__main__":
    unittest.main()
