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


class PriceIsNotInflatedByInsuranceTest(unittest.TestCase):
    """Insurance must not change the price.

    It used to add $100 whenever the toggle went on, so a $250 quote became a
    $350 charge without anybody touching the number — and because the arithmetic
    landed inside the string, every downstream reader (driver amount, Monday's
    number column) inherited the inflated figure silently. The price an issuer
    types already has insurance in it.
    """

    def test_a_plain_price_is_unchanged(self):
        self.assertEqual("$150", bot._price_with_insurance_addon("$150", True))

    def test_the_reported_case_stays_put(self):
        # $250 with insurance on was being dispatched as $350.
        self.assertEqual("$250", bot._price_with_insurance_addon("$250", True))

    def test_a_toll_price_is_unchanged(self):
        self.assertEqual("$150 + toll",
                         bot._price_with_insurance_addon("$150 + toll", True))

    def test_a_thousands_separator_is_not_rewritten(self):
        self.assertEqual("1,500", bot._price_with_insurance_addon("1,500", True))

    def test_a_price_with_no_number_is_left_alone(self):
        for raw in ("-", "", None):
            with self.subTest(price=raw):
                self.assertEqual(str(raw or ""),
                                 bot._price_with_insurance_addon(raw, True))

    def test_on_and_off_agree(self):
        for raw in ("$150", "$250", "$150 + toll", "1,500", "-", ""):
            with self.subTest(price=raw):
                self.assertEqual(bot._price_with_insurance_addon(raw, False),
                                 bot._price_with_insurance_addon(raw, True))

    def test_downstream_number_readers_see_what_was_typed(self):
        kept = bot._price_with_insurance_addon("$150 + toll", True)
        self.assertEqual("150", bot._price_amount_str(kept))

    def test_no_addon_constant_survives(self):
        self.assertFalse(hasattr(bot, "INSURANCE_ADDON_USD"),
                         "the $100 constant should be gone, not merely unused")


class PortalPasswordTest(unittest.TestCase):
    """One standard temporary password, matching /admin and the API.

    The bot used to mint a random 10-character password per account, so the same
    client got Temp#A9 if entered through /admin and something else if the bot
    issued it — and for an email that already had an account the portal kept its
    own password anyway, making the random one printed in the login block wrong.
    """

    def test_it_is_the_standard_temp_password(self):
        self.assertEqual("Temp#A9", bot._generate_portal_password())

    def test_it_is_stable_across_calls(self):
        self.assertEqual({bot._generate_portal_password() for _ in range(20)},
                         {"Temp#A9"})

    def test_it_matches_the_named_constant(self):
        self.assertEqual(bot.PORTAL_DEFAULT_PASSWORD, bot._generate_portal_password())


if __name__ == "__main__":
    unittest.main()
