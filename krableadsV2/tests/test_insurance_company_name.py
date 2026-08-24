r"""The insurance company's name is not the client's name.

Reported: saying "insurance company name <carrier>" filed the carrier as the
client's first/last name. "name" is a label in its own right, so the phrase split
at it — the insurance label was left with an empty value and the carrier went to
the person's name field.

A company name and a person's name are different things. Both must be read, and
neither may end up in the other's field.

Run:  venv\Scripts\python.exe -m pytest tests/test_insurance_company_name.py -q
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


class TheReportedPhrasingTest(unittest.TestCase):

    def test_insurance_company_name_reaches_the_insurance(self):
        self.assertEqual([("ins", "GEICO")],
                         bot._parse_multi_field_line("insurance company name Geico"))

    def test_it_does_not_touch_the_client_name(self):
        card = {"name": "John Damian", "first_name": "John", "last_name": "Damian"}
        bot._apply_inline_review_text(card, "insurance company name Geico")
        self.assertEqual("GEICO", card.get("insurance_company"))
        self.assertEqual("John Damian", card.get("name"), "the client was overwritten")

    def test_every_way_of_asking_for_it(self):
        for line in ("insurance company name Geico",
                     "insurance company name is Geico",
                     "insurance company's name Geico",
                     "insurance companys name Geico",
                     "insurance name Geico",
                     "carrier name Geico",
                     "name of the insurance company Geico",
                     "name of insurance company Geico",
                     "name of the insurance Geico",
                     "name of insurance Geico",
                     "insurance provider Geico",
                     "provider Geico",
                     "company name Geico",
                     "policy name Geico"):
            with self.subTest(line=line):
                self.assertEqual([("ins", "GEICO")], bot._parse_multi_field_line(line), line)

    def test_shouted_and_mixed_case(self):
        for line in ("INSURANCE COMPANY NAME GEICO", "Insurance Company Name Geico"):
            with self.subTest(line=line):
                self.assertEqual([("ins", "GEICO")], bot._parse_multi_field_line(line))

    def test_a_carrier_it_does_not_know_still_gets_there(self):
        self.assertEqual([("ins", "Ocean Harbor")],
                         bot._parse_multi_field_line("insurance company name Ocean Harbor Insurance"))

    def test_the_number_still_rides_along(self):
        self.assertEqual([("ins", "GEICO"), ("pol", "8829301")],
                         bot._parse_multi_field_line("insurance company name geico 8829301"))


class TheClientNameStillWorksTest(unittest.TestCase):
    """The whole point is telling the two apart, not favouring one."""

    def test_a_person_is_read_as_a_person(self):
        for line, want in (("name John Damian", ("name", "John Damian")),
                           ("first name John", ("fn", "John")),
                           ("last name Damian", ("ln", "Damian")),
                           ("client name Maria Gonzalez", ("name", "Maria Gonzalez")),
                           ("full name Robert Rodriguez", ("name", "Robert Rodriguez"))):
            with self.subTest(line=line):
                self.assertEqual([want], bot._parse_multi_field_line(line))

    def test_both_names_in_one_line(self):
        self.assertEqual([("name", "John Damian"), ("ins", "GEICO")],
                         bot._parse_multi_field_line("name John Damian insurance geico"))

    def test_a_person_who_shares_a_word_with_a_carrier_is_still_a_person(self):
        """Only carriers we recognise outright move; everyone else keeps the label."""
        for line, want in (("name Erie Thompson", "name"),
                           ("name Mercury Jones", "name"),
                           ("name Shelter Williams", "name")):
            with self.subTest(line=line):
                self.assertEqual(want, bot._parse_multi_field_line(line)[0][0])


class ACarrierIsNeverAPersonTest(unittest.TestCase):
    """Said under a name label, an unmistakable carrier still goes to insurance."""

    def test_a_bare_name_label_carrying_a_carrier(self):
        for line in ("name Geico", "client name State Farm", "first name Allstate",
                     "last name Progressive"):
            with self.subTest(line=line):
                self.assertEqual("ins", bot._parse_multi_field_line(line)[0][0], line)

    def test_the_helper_directly(self):
        self.assertEqual("ins", bot._carrier_is_never_a_person("name", "Geico"))
        self.assertEqual("ins", bot._carrier_is_never_a_person("fn", "State Farm"))
        self.assertEqual("name", bot._carrier_is_never_a_person("name", "John Damian"))

    def test_it_leaves_other_fields_alone(self):
        for ek in ("col", "car", "addr", "price", "phone", "ins"):
            with self.subTest(ek=ek):
                self.assertEqual(ek, bot._carrier_is_never_a_person(ek, "Geico"))


class LeftoverLabelWordsTest(unittest.TestCase):

    def test_filler_only_is_not_a_name(self):
        """"name of the insurance company X" used to leave "of the" as the client."""
        for junk in ("of the", "the", "of", "is"):
            with self.subTest(junk=junk):
                self.assertEqual("", bot._clean_inline_value("name", junk))

    def test_a_real_name_is_untouched(self):
        self.assertEqual("John Damian", bot._clean_inline_value("name", "John Damian"))

    def test_a_one_word_name_survives(self):
        self.assertEqual("Damian", bot._clean_inline_value("name", "Damian"))


if __name__ == "__main__":
    unittest.main()
