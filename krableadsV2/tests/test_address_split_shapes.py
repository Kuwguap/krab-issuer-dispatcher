"""Address shapes operators actually type, all landing on the same answer.

Reported from the field: 3125 Park Ave Apt 11D in the Bronx, typed four ways.
Three of the four split wrongly — the whole address stayed on the street line,
or the city was orphaned onto it and only "NY,10451" reached the city field.
"""
import unittest

import bot
from utils import insurance_card as ic


STREET = "3125 park ave apt 11D"


class TheFourReportedShapesTest(unittest.TestCase):
    """Every one of these is the same address and must split the same way."""

    SHAPES = [
        "3125 park ave apt 11D Bronx New York,10451",     # comma, no space, spelled state
        "3125 park ave apt 11D Bronx, NY,10451",          # comma between city and state
        "3125 park ave apt 11D state: NY city: Bronx zip:10451",   # labelled fields
        "3125 park ave apt 11D Bronx New York",           # no ZIP at all
        "3125 park ave apt 11D Bronx NY 10451",           # plain, no commas
        "3125 park ave apt 11D, Bronx, NY 10451",         # canonical
    ]

    def test_the_street_never_absorbs_the_city(self):
        for raw in self.SHAPES:
            with self.subTest(raw=raw):
                street, csz = bot._split_street_and_csz(raw)
                self.assertEqual(street.lower(), STREET.lower())
                self.assertIn("bronx", csz.lower())

    def test_city_and_state_resolve_identically(self):
        for raw in self.SHAPES:
            with self.subTest(raw=raw):
                _, csz = bot._split_street_and_csz(raw)
                parts = ic.parse_city_state_zip(csz)
                self.assertEqual(parts["city"], "Bronx")
                self.assertEqual(parts["state"], "NY")

    def test_the_zip_survives_wherever_one_was_given(self):
        for raw in self.SHAPES:
            with self.subTest(raw=raw):
                _, csz = bot._split_street_and_csz(raw)
                parts = ic.parse_city_state_zip(csz)
                expected = "10451" if "10451" in raw else ""
                self.assertEqual(parts["zip"], expected)


class CityStateZipParsingTest(unittest.TestCase):
    def test_a_state_is_found_without_a_comma_before_it(self):
        # The old regex demanded one, so 'Bronx NY 10451' lost its state and the
        # city became 'Bronx NY' — on a printed insurance card.
        self.assertEqual(
            ic.parse_city_state_zip("Bronx NY 10451"),
            {"city": "Bronx", "state": "NY", "zip": "10451"},
        )

    def test_a_spelled_out_state_is_understood(self):
        for s in ("Bronx New York 10451", "Bronx New York, 10451", "Bronx, New York 10451"):
            with self.subTest(s=s):
                got = ic.parse_city_state_zip(s)
                self.assertEqual(got["city"], "Bronx")
                self.assertEqual(got["state"], "NY")

    def test_two_word_states_do_not_eat_the_city(self):
        got = ic.parse_city_state_zip("Fort Lee New Jersey 07024")
        self.assertEqual(got, {"city": "Fort Lee", "state": "NJ", "zip": "07024"})

    def test_no_zip_is_not_an_error(self):
        self.assertEqual(
            ic.parse_city_state_zip("Bronx New York"),
            {"city": "Bronx", "state": "NY", "zip": ""},
        )

    def test_labelled_fields(self):
        for s in ("state: NY city: Bronx zip:10451",
                  "city: Bronx state: NY zip: 10451",
                  "CITY: Bronx STATE: New York ZIP: 10451"):
            with self.subTest(s=s):
                self.assertEqual(
                    ic.parse_city_state_zip(s),
                    {"city": "Bronx", "state": "NY", "zip": "10451"},
                )

    def test_city_only_keeps_the_city(self):
        got = ic.parse_city_state_zip("Bronx")
        self.assertEqual(got["city"], "Bronx")
        self.assertEqual(got["state"], "")

    def test_empty_is_empty(self):
        self.assertEqual(ic.parse_city_state_zip(""), {"city": "", "state": "", "zip": ""})


class ExistingBehaviourStillHoldsTest(unittest.TestCase):
    """The apartment must stay with the street — the case the comma rule exists for."""

    def test_apartment_stays_on_the_street_line(self):
        street, csz = bot._split_street_and_csz("88 Ocean Ave Apt 3B, Fort Lee, NJ 07024")
        self.assertEqual(street, "88 Ocean Ave Apt 3B")
        self.assertEqual(ic.parse_city_state_zip(csz)["city"], "Fort Lee")

    def test_a_street_directional_is_not_a_city(self):
        street, csz = bot._split_street_and_csz("1600 Pennsylvania Ave NW, Washington DC 20500")
        self.assertEqual(street, "1600 Pennsylvania Ave NW")
        self.assertIn("washington", csz.lower())

    def test_a_street_with_no_city_at_all(self):
        street, csz = bot._split_street_and_csz("123 Main St")
        self.assertEqual(street, "123 Main St")
        self.assertEqual(csz, "")

    def test_a_bare_city_state_zip(self):
        street, csz = bot._split_street_and_csz("Newark NJ 07102")
        self.assertEqual(street, "")
        self.assertEqual(csz, "Newark NJ 07102")


if __name__ == "__main__":
    unittest.main()
