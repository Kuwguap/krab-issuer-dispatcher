"""The $100 add-on lands in the NUMBER, and only where it belongs.

Every numeric consumer of a price (_price_amount_str: the portal premium,
Monday's number column) reads the FIRST number in the string — so the fold must
be arithmetic, not a suffix, and the toll suffix must survive on the outside.

Also here: the portal password is now RANDOM (one fixed password for every
client's account was the alternative), and the optional-column retry helper is
token-bounded (an error naming `portal_password_unchanged` must not evict the
real `portal_password`; one naming `insurance_emailed_at` must not evict
`email` — both happened by substring match).

Run:  venv\\Scripts\\python.exe -m pytest tests/test_insurance_addon_price.py -q
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


class PriceFoldTest(unittest.TestCase):

    def test_plain_price_gains_100(self):
        self.assertEqual("$250", bot._price_with_insurance_addon("$150", True))

    def test_toll_survives_outside_the_maths(self):
        self.assertEqual("$250 + toll",
                         bot._price_with_insurance_addon("$150 + toll", True))

    def test_thousands_separator_is_read_as_one_number(self):
        self.assertEqual("$1600", bot._price_with_insurance_addon("1,500", True))

    def test_a_price_with_no_number_is_left_alone(self):
        for raw in ("-", "", None):
            with self.subTest(price=raw):
                self.assertEqual(str(raw or ""),
                                 bot._price_with_insurance_addon(raw, True))

    def test_toggle_off_changes_nothing(self):
        for raw in ("$150", "$150 + toll", "-", ""):
            with self.subTest(price=raw):
                self.assertEqual(raw, bot._price_with_insurance_addon(raw, False))

    def test_downstream_number_readers_see_the_total(self):
        folded = bot._price_with_insurance_addon("$150 + toll", True)
        self.assertEqual("250", bot._price_amount_str(folded))


class PortalPasswordTest(unittest.TestCase):

    def test_shape(self):
        for _ in range(20):
            pw = bot._generate_portal_password()
            self.assertEqual(10, len(pw))
            self.assertTrue(set(pw) <= set(bot._PORTAL_PW_ALPHABET), pw)
            self.assertTrue(any(c.islower() for c in pw), pw)
            self.assertTrue(any(c.isupper() for c in pw), pw)
            self.assertTrue(any(c.isdigit() for c in pw), pw)
            self.assertTrue(any(c in "#!@" for c in pw), pw)

    def test_two_accounts_do_not_share_one(self):
        self.assertNotEqual(bot._generate_portal_password(),
                            bot._generate_portal_password())


class RetryHelperIsTokenBoundedTest(unittest.TestCase):
    """The error message names ONE column; only that exact column may be popped."""

    def test_a_longer_identifier_does_not_evict_its_prefix(self):
        payload = {"portal_password": "x", "insurance_card_sent_at": "now"}
        exc = Exception("Could not find the 'portal_password_unchanged' column of 'leads'")
        self.assertFalse(udb._retry_lead_write_without_phase1_files(exc, payload))
        self.assertIn("portal_password", payload)
        self.assertIn("insurance_card_sent_at", payload)

    def test_emailed_at_does_not_evict_email(self):
        payload = {"email": "a@b.c"}
        exc = Exception("Could not find the 'insurance_emailed_at' column of 'leads'")
        self.assertFalse(udb._retry_lead_write_without_phase1_files(exc, payload))
        self.assertIn("email", payload)

    def test_the_exact_column_is_still_popped(self):
        payload = {"portal_password": "x", "email": "a@b.c"}
        exc = Exception("Could not find the 'portal_password' column of 'leads'")
        self.assertTrue(udb._retry_lead_write_without_phase1_files(exc, payload))
        self.assertNotIn("portal_password", payload)
        self.assertIn("email", payload)

    def test_pgrst204_without_a_name_still_pops_all_optionals(self):
        payload = {"portal_password": "x", "email": "a@b.c", "price": "$250"}
        exc = Exception("PGRST204: something about leads schema cache")
        self.assertTrue(udb._retry_lead_write_without_phase1_files(exc, payload))
        self.assertNotIn("portal_password", payload)
        self.assertNotIn("email", payload)
        self.assertIn("price", payload)  # not an optional key — never popped


if __name__ == "__main__":
    unittest.main()
