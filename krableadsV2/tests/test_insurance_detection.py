r"""Insurance said any of the ways it gets said, and the carrier recognised.

Asked for: read "POLICY NAME / INSURANCE POLICY / POLICY / INSURANCE COMPANY /
COMPANY POLICY", and detect Geico, Allstate, State Farm and the rest.

The same words get used for BOTH halves of the insurance, so the value decides:
a carrier name is the company, digits are the policy number, and "geico 8829301"
is both.

Run:  venv\Scripts\python.exe -m pytest tests/test_insurance_detection.py -q
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
from utils import ai_vision  # noqa: E402


class TheWordsPeopleUseTest(unittest.TestCase):
    """Every phrasing in the request reaches an insurance field."""

    ASKED_FOR = ["policy name", "insurance policy", "policy",
                 "insurance company", "company policy"]

    def test_all_five_are_known_labels(self):
        for phrase in self.ASKED_FOR:
            with self.subTest(phrase=phrase):
                self.assertIn(bot._INLINE_EDIT_ALIASES.get(phrase), ("ins", "pol"), phrase)

    def test_each_one_carries_a_carrier_to_the_company(self):
        for phrase in self.ASKED_FOR:
            with self.subTest(phrase=phrase):
                self.assertEqual([("ins", "GEICO")],
                                 bot._parse_multi_field_line(f"{phrase} geico"))

    def test_each_one_carries_a_number_to_the_policy(self):
        for phrase in self.ASKED_FOR:
            with self.subTest(phrase=phrase):
                self.assertEqual([("pol", "8829301")],
                                 bot._parse_multi_field_line(f"{phrase} 8829301"))

    def test_shouting_it_works_too(self):
        self.assertEqual([("ins", "State Farm")],
                         bot._parse_multi_field_line("COMPANY POLICY STATE FARM"))

    def test_the_other_ways_it_gets_said(self):
        for phrase in ("carrier", "insurer", "insurance carrier", "insurance name",
                       "insurance co", "policy company"):
            with self.subTest(phrase=phrase):
                self.assertEqual([("ins", "Allstate")],
                                 bot._parse_multi_field_line(f"{phrase} allstate"))


class CarrierNamesTest(unittest.TestCase):

    def test_the_ones_named_in_the_request(self):
        self.assertEqual("GEICO", bot._insurer_name("geico"))
        self.assertEqual("Allstate", bot._insurer_name("allstate"))
        self.assertEqual("State Farm", bot._insurer_name("state farm"))

    def test_the_big_carriers(self):
        for said, canon in [
            ("progressive", "Progressive"), ("usaa", "USAA"),
            ("liberty mutual", "Liberty Mutual"), ("travelers", "Travelers"),
            ("nationwide", "Nationwide"), ("farmers", "Farmers"),
            ("american family", "American Family"), ("safeco", "Safeco"),
            ("esurance", "Esurance"), ("kemper", "Kemper"), ("amica", "Amica"),
            ("chubb", "Chubb"), ("metlife", "MetLife"), ("national general", "National General"),
            ("dairyland", "Dairyland"), ("bristol west", "Bristol West"),
        ]:
            with self.subTest(said=said):
                self.assertEqual(canon, bot._insurer_name(said))

    def test_the_new_jersey_carriers(self):
        """This bot issues NJ temp tags, so these turn up constantly."""
        for said, canon in [("njm", "NJM"), ("new jersey manufacturers", "NJM"),
                            ("plymouth rock", "Plymouth Rock"), ("palisades", "Palisades"),
                            ("high point", "High Point"), ("cure auto", "CURE"),
                            ("rutgers casualty", "Rutgers Casualty")]:
            with self.subTest(said=said):
                self.assertEqual(canon, bot._insurer_name(said))

    def test_it_is_stored_spelled_the_same_way_every_time(self):
        for said in ("geico", "GEICO", "Geico", "gieco", "geiko",
                     "Geico Insurance", "geico insurance company"):
            with self.subTest(said=said):
                self.assertEqual("GEICO", bot._insurer_name(said))
        self.assertEqual("State Farm", bot._insurer_name("statefarm"))
        self.assertEqual("Allstate", bot._insurer_name("All State"))

    def test_a_carrier_it_has_never_heard_of_still_counts(self):
        """'all insurance companies' is longer than any list."""
        self.assertEqual("Ocean Harbor", bot._insurer_name("Ocean Harbor Insurance"))
        self.assertEqual("Kingsway", bot._insurer_name("Kingsway Assurance"))

    def test_things_that_are_not_carriers(self):
        for said in ("John Damian", "Honda Accord", "white", "123 Main St", ""):
            with self.subTest(said=said):
                self.assertEqual("", bot._insurer_name(said))


class ACarrierOnItsOwnTest(unittest.TestCase):
    """Typing just "Geico" used to become the CLIENT'S NAME."""

    def test_a_bare_carrier_goes_to_the_insurance(self):
        for said in ("Geico", "State Farm", "Allstate", "Progressive", "NJM",
                     "USAA", "Liberty Mutual", "Travelers", "Nationwide"):
            with self.subTest(said=said):
                self.assertEqual("ins", bot._structured_value_ek(said))

    def test_a_bare_carrier_is_not_the_client_name(self):
        card = {}
        bot._apply_ek_value(card, bot._structured_value_ek("Geico"), "Geico")
        self.assertEqual("GEICO", card.get("insurance_company"))
        self.assertNotEqual("Geico", card.get("name"))

    def test_a_person_is_still_a_person(self):
        self.assertIsNone(bot._structured_value_ek("John Damian"))


class NamesSharedWithCarsAndTownsTest(unittest.TestCase):
    """Mercury and Plymouth are car makes; Erie and Westfield are towns. All three
    are also real carriers — so they count only once insurance is in play."""

    def test_a_car_is_not_read_as_a_carrier(self):
        for said in ("Mercury Sable", "Plymouth Voyager", "2019 Mercury Grand Marquis"):
            with self.subTest(said=said):
                self.assertNotEqual("ins", bot._structured_value_ek(said))

    def test_an_everyday_word_is_not_read_as_a_carrier(self):
        for said in ("Erie", "Westfield", "root", "shelter", "the general"):
            with self.subTest(said=said):
                self.assertEqual("", bot._insurer_name(said))

    def test_but_they_are_carriers_once_you_say_so(self):
        for line, canon in [("insurance mercury", "Mercury"),
                            ("policy name erie", "Erie"),
                            ("company policy westfield", "Westfield"),
                            ("insurance root", "Root"),
                            ("carrier the general", "The General")]:
            with self.subTest(line=line):
                self.assertEqual([("ins", canon)], bot._parse_multi_field_line(line))

    def test_or_once_the_value_says_so(self):
        self.assertEqual("Root", bot._insurer_name("Root Insurance"))
        self.assertEqual("Mercury", bot._insurer_name("Mercury Insurance"))


class CarrierAndNumberTogetherTest(unittest.TestCase):
    """People say both in one breath."""

    def test_both_halves_land(self):
        self.assertEqual([("ins", "GEICO"), ("pol", "8829301")],
                         bot._parse_multi_field_line("policy geico 8829301"))

    def test_it_works_from_the_company_side_too(self):
        self.assertEqual([("ins", "Progressive"), ("pol", "92831884")],
                         bot._parse_multi_field_line("insurance company progressive 92831884"))

    def test_a_hyphenated_policy_number_survives(self):
        self.assertEqual([("ins", "State Farm"), ("pol", "447-291-88")],
                         bot._parse_multi_field_line("policy name state farm 447-291-88"))

    def test_the_card_gets_both(self):
        card = {}
        changed = bot._apply_ek_value(card, "ins", "geico 8829301")
        self.assertEqual("GEICO", card.get("insurance_company"))
        self.assertEqual("8829301", card.get("insurance_policy_number"))
        self.assertEqual(2, len(changed), changed)

    def test_the_carrier_name_is_never_mistaken_for_the_number(self):
        card = {}
        bot._apply_ek_value(card, "ins", "21st Century")
        self.assertEqual("21st Century", card.get("insurance_company"))
        self.assertIn(str(card.get("insurance_policy_number") or "-"), ("-", ""))


class NothingElseMovedTest(unittest.TestCase):
    """The insurance words must not start eating other fields."""

    def test_clearing_the_field_still_clears_it(self):
        self.assertEqual([("ins", "-")], bot._parse_multi_field_line("insurance -"))

    def test_a_plain_policy_number_is_still_a_policy_number(self):
        self.assertEqual([("pol", "8829301")], bot._parse_multi_field_line("policy number 8829301"))

    def test_other_labels_are_untouched(self):
        self.assertEqual([("price", "$150")], bot._parse_multi_field_line("price 150"))
        self.assertEqual([("col", "white")], bot._parse_multi_field_line("color white"))

    def test_a_mixed_line_still_splits(self):
        self.assertEqual([("price", "$200"), ("ins", "GEICO")],
                         bot._parse_multi_field_line("price 200 insurance geico"))


class TheAIKnowsTooTest(unittest.TestCase):
    """The deterministic parser handles labelled lines; the AI gets everything else."""

    def test_the_prompt_names_the_carriers(self):
        for carrier in ("Geico", "State Farm", "Allstate", "Progressive"):
            self.assertIn(carrier, ai_vision.FIELD_VALUE_PROMPT, carrier)

    def test_the_prompt_says_a_carrier_is_not_a_name(self):
        self.assertIn("never the client name", ai_vision.FIELD_VALUE_PROMPT)

    def test_the_ai_may_answer_with_any_of_the_phrasings(self):
        for key in ("carrier", "insurer", "policy_name", "company_policy"):
            self.assertEqual("ins", ai_vision._FIELD_VALUE_TO_EK.get(key), key)
        for key in ("insurance_policy", "policy_number", "binder"):
            self.assertEqual("pol", ai_vision._FIELD_VALUE_TO_EK.get(key), key)


if __name__ == "__main__":
    unittest.main()
