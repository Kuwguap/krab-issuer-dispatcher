r"""The Instant Tag offer must quote the amount the office actually set.

Reported: lead 3IP1D92T was priced $250 (driver_amount $200 in the database),
and the driver was offered "Pay $100". $100 was a hardcoded fallback, reached
because `db.update_lead` writes the ROW while the dispatch reads the dict it
already holds -- which never had driver_amount added to it. The same None also
went to Stripe as `amount_cents`, where it means "use the dashboard's flat
price", so the charge was wrong too, not just the label.

Run:  venv\Scripts\python.exe -m pytest tests/test_instant_amount.py -q
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

SRC = (ROOT / "bot.py").read_text(encoding="utf-8")


class TheAmountComesFromTheLeadTest(unittest.TestCase):

    def test_the_reported_lead_now_quotes_two_hundred(self):
        """price $250, driver_amount $200 -- exactly the row in production."""
        lead = {"reference_id": "3IP1D92T", "price": "$250",
                "driver_amount": "$200", "instant_tag": True}
        self.assertEqual(20000, bot._driver_amount_cents(lead))

    def test_a_lead_whose_amount_has_not_been_read_back_still_bills_right(self):
        """The dict handed to the dispatch often predates the column write."""
        self.assertEqual(20000, bot._driver_amount_cents({"price": "$250"}))

    def test_the_stored_amount_wins_over_the_derived_one(self):
        """An operator who edited the Amount by hand keeps it."""
        self.assertEqual(12500, bot._driver_amount_cents(
            {"price": "$250", "driver_amount": "$125"}))

    def test_prices_at_or_below_the_discount_are_not_a_charge(self):
        for price in ("$50", "$40", "$0"):
            self.assertIsNone(bot._driver_amount_cents({"price": price}), price)

    def test_junk_never_becomes_an_amount(self):
        for lead in ({}, {"price": "free"}, {"price": ""}, {"driver_amount": "-"}):
            self.assertIsNone(bot._driver_amount_cents(lead), str(lead))

    def test_the_discount_is_the_one_constant(self):
        self.assertEqual(
            (250 - bot.INSTANT_AMOUNT_DISCOUNT_USD) * 100,
            bot._driver_amount_cents({"price": "$250"}))


class NoInventedPriceTest(unittest.TestCase):

    def test_the_offer_no_longer_hardcodes_a_hundred_dollars(self):
        body = SRC.split("async def _dispatch_instant_tag_lead", 1)[1]
        body = body.split("\nasync def ", 1)[0]
        self.assertNotIn('else "$100"', body)
        self.assertIn('"the agreed amount"', body)

    def test_the_persisted_amount_is_mirrored_onto_the_lead(self):
        body = SRC.split("async def _on_lead_created", 1)[1]
        body = body.split("\nasync def ", 1)[0]
        self.assertIn('lead["driver_amount"] = _amt', body)
        self.assertIn('lead["instant_tag"] = True', body)

    def test_the_office_is_told_when_stripe_is_the_problem(self):
        """"No payment link" read as a key problem, and the key was fine."""
        body = SRC.split("async def _dispatch_instant_tag_lead", 1)[1]
        body = body.split("\nasync def ", 1)[0]
        self.assertIn("STRIPE_SECRET_KEY is not set on krab-issuer-admin", body)
        self.assertIn('elif "stripe" in low:', body)


if __name__ == "__main__":
    unittest.main()
