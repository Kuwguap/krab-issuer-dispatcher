r"""🤖 Instant Tag — the driver's offer message.

Asked for: the payment DM should read like a job ticket — reference, the
client's CITY/STATE/ZIP (never the street before payment), the date/time, and
"Payment in cash" — with the full address and client phone arriving only after
the card clears (which the paid delivery already does via the full lead send).

Run:  venv\Scripts\python.exe -m pytest tests/test_instant_tag_offer.py -q
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


class TheCityLineNeverLeaksTheStreetTest(unittest.TestCase):

    def test_a_full_address_becomes_city_state_zip(self):
        lead = {"delivery_details": "3 Raritan River River Rd,, Califon,, NJ 07830"}
        self.assertEqual("Califon, NJ 07830", bot._instant_tag_city_line(lead))

    def test_the_street_is_not_in_the_line(self):
        lead = {"delivery_details": "19 Pennwood Dr, Ewing NJ"}
        line = bot._instant_tag_city_line(lead)
        self.assertNotIn("Pennwood", line)
        self.assertIn("Ewing", line)

    def test_multiline_addresses_use_the_tail(self):
        lead = {"delivery_details": "Apt 4B\n55 Main Street\nNewark, NJ 07102"}
        self.assertEqual("Newark, NJ 07102", bot._instant_tag_city_line(lead))

    def test_no_address_is_no_line_not_a_crash(self):
        self.assertEqual("", bot._instant_tag_city_line({"delivery_details": ""}))
        self.assertEqual("", bot._instant_tag_city_line({}))

    def test_a_pure_number_tail_stays_hidden(self):
        """Nothing but street-shaped pieces — better silent than leaky."""
        self.assertEqual("", bot._instant_tag_city_line({"delivery_details": "12 Oak Ave 4"}))


class TheWhenLineTest(unittest.TestCase):

    def test_created_at_renders_in_ny_time(self):
        lead = {"created_at": "2026-08-27T18:42:00+00:00"}   # 2:42 PM in NY (EDT)
        self.assertEqual("Aug 27, 2:42 PM", bot._instant_tag_when_line(lead))

    def test_garbage_still_produces_a_time(self):
        line = bot._instant_tag_when_line({"created_at": "not-a-date"})
        self.assertRegex(line, r"^[A-Z][a-z]{2} \d{1,2}, \d{1,2}:\d{2} [AP]M$")


class TheOfferMessageReadsLikeTheTicketTest(unittest.TestCase):
    """The copy itself, sliced from the dispatch function like test_instant_tag
    does — a reworded offer that silently drops a line is how the driver stops
    being told the terms."""

    def _offer(self):
        body = SRC.split("async def _dispatch_instant_tag_lead", 1)[1]
        return body.split("\nasync def ", 1)[0]

    def test_the_lines_are_all_there_in_order(self):
        offer = self._offer()
        needles = ["Instant Tag 🏷️", "Reference:", "_instant_tag_city_line",
                   "_instant_tag_when_line", "Payment in cash",
                   "sends itself here", "cash-in-hand from the client"]
        positions = [offer.index(n) for n in needles]
        self.assertEqual(positions, sorted(positions), needles)

    def test_full_details_are_promised_after_payment_not_before(self):
        offer = self._offer()
        self.assertIn("after payment", offer)
        # The offer never prints the raw address or phone fields itself.
        self.assertNotIn('lead.get("phone_number")', offer)
        self.assertNotIn("encrypted_link", offer)

    def test_the_paid_delivery_is_still_the_full_send(self):
        """After the card clears, the driver gets the whole lead — the same
        full send as an accepted lead, phone link and all."""
        sweep = SRC.split("async def deliver_paid_instant_pdfs", 1)[1]
        sweep = sweep.split("\nasync def ", 1)[0]
        self.assertIn("_deliver_skip_dispatch", sweep)


if __name__ == "__main__":
    unittest.main()
