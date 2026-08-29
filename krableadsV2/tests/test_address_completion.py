"""Filling in the address parts nobody typed — without overwriting anyone.

The geocoder is stubbed everywhere: these assert OUR rules (what gets filled,
what is left alone), not OpenStreetMap's uptime.
"""
import unittest
from unittest.mock import patch

from utils import address_complete as ac


BRONX = {"city": "Bronx", "state": "NY", "zip": "10451"}


class StripUnitTest(unittest.TestCase):
    def test_unit_designators_are_removed(self):
        for raw, want in [
            ("3125 Park Ave Apt 11D", "3125 Park Ave"),
            ("3125 Park Ave, Apt 11D", "3125 Park Ave"),
            ("88 Ocean Ave Suite 200", "88 Ocean Ave"),
            ("5 Main St #4", "5 Main St"),
            ("5 Main St Fl 3", "5 Main St"),
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(ac.strip_unit(raw), want)

    def test_a_plain_street_is_untouched(self):
        self.assertEqual(ac.strip_unit("3125 Park Ave"), "3125 Park Ave")

    def test_it_is_idempotent(self):
        once = ac.strip_unit("3125 Park Ave Apt 11D")
        self.assertEqual(ac.strip_unit(once), once)


class OnlyEverAddsTest(unittest.TestCase):
    def _complete(self, street, csz, found=BRONX):
        with patch.object(ac, "lookup", return_value=dict(found)):
            return ac.complete_city_state_zip(street, csz)

    def test_a_missing_zip_is_filled(self):
        out, changed = self._complete("3125 park ave apt 11D", "Bronx New York")
        self.assertTrue(changed)
        self.assertIn("10451", out)
        self.assertIn("Bronx", out)

    def test_a_bare_zip_gains_city_and_state(self):
        out, changed = self._complete("3125 park ave apt 11D", "10451")
        self.assertTrue(changed)
        self.assertEqual(out, "Bronx NY 10451")

    def test_a_complete_address_is_left_alone_and_costs_no_lookup(self):
        with patch.object(ac, "lookup", side_effect=AssertionError("must not geocode")):
            out, changed = ac.complete_city_state_zip("88 Ocean Ave", "Fort Lee NJ 07024")
        self.assertFalse(changed)
        self.assertEqual(out, "Fort Lee NJ 07024")

    def test_the_operator_is_never_overruled(self):
        # Geocoder insists on Bronx; the operator typed Yonkers. Operator wins,
        # and only the absent ZIP is taken.
        out, changed = self._complete("1 Main St", "Yonkers NY")
        self.assertTrue(changed)
        self.assertIn("Yonkers", out)
        self.assertNotIn("Bronx", out)
        self.assertIn("10451", out)

    def test_a_zip_already_in_the_street_is_used_before_the_network(self):
        with patch.object(ac, "lookup", side_effect=AssertionError("must not geocode")):
            out, changed = ac.complete_city_state_zip("3125 Park Ave 10451", "Bronx NY")
        self.assertEqual(out, "Bronx NY 10451")
        self.assertTrue(changed)

    def test_nothing_found_changes_nothing(self):
        out, changed = self._complete("123 Nowhere Rd", "Zzzz", found={})
        self.assertFalse(changed)
        self.assertEqual(out, "Zzzz")

    def test_a_lookup_failure_is_not_an_error(self):
        with patch.object(ac, "lookup", side_effect=OSError("network down")):
            # complete_city_state_zip must not raise — lookup swallows, but be sure.
            try:
                out, changed = ac.complete_city_state_zip("1 Main St", "Bronx")
            except OSError:
                self.fail("a geocoder outage must not break lead entry")
        self.assertFalse(changed)


class NycBoroughTest(unittest.TestCase):
    def test_a_borough_beats_new_york(self):
        hit = {"address": {"city": "New York", "suburb": "The Bronx",
                           "state": "New York", "postcode": "10451"}}
        self.assertEqual(ac._addr_of(hit), BRONX)

    def test_an_ordinary_city_is_taken_as_is(self):
        hit = {"address": {"city": "Fort Lee", "state": "New Jersey", "postcode": "07024"}}
        self.assertEqual(
            ac._addr_of(hit), {"city": "Fort Lee", "state": "NJ", "zip": "07024"}
        )


class StateAbbrTest(unittest.TestCase):
    def test_names_and_abbreviations_both_resolve(self):
        for raw, want in [("NY", "NY"), ("ny", "NY"), ("New York", "NY"),
                          ("new jersey", "NJ"), ("Bronx", ""), ("", "")]:
            with self.subTest(raw=raw):
                self.assertEqual(ac.state_abbr(raw), want)


if __name__ == "__main__":
    unittest.main()
