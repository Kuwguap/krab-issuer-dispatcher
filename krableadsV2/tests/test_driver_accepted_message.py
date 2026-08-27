r"""The driver's LEAD ACCEPTED DM — the job ticket first, and bold.

Asked for: phone, delivery address, name, price, payment method and time in one
BOLD block at the top; notes renamed "Extra Notes"; the payment rails with
"CLIENT MUST PAY⚡️DIRECT TO US"; the reference at the bottom — and the SAME
message for every way a driver gets a lead: normal accept, paid Instant Tag,
and Skip Dispatch.

Run:  venv\Scripts\python.exe -m pytest tests/test_driver_accepted_message.py -q
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

import bot  # noqa: E402

SRC = (ROOT / "bot.py").read_text(encoding="utf-8")

LEAD = {
    "reference_id": "79TVVYGI",
    "phone_number": "+12128148158",
    "encrypted_link": "https://link.example/secret",
    "delivery_details": "19 Pennwood Dr\nEwing, NJ 08638-4725",
    "vehicle_details": "Johnathan Perez\n19 Pennwood Dr\nEwing, NJ 08638\n"
                       "VIN123\n2017 M Benz\nBlack\nGeico\nPOL1\ncall first",
    "price": "$250",
    "extra_info": "call before arriving",
}


def _msg(driverblock=False):
    with mock.patch.object(bot, "_driverblock_enabled", return_value=driverblock):
        return bot._build_driver_lead_accepted_message_html(dict(LEAD))


class TheTicketIsBoldAndInOrderTest(unittest.TestCase):

    def test_the_bold_block_carries_the_ticket_in_the_asked_order(self):
        m = _msg()
        bold = m.split("</b>", 1)[0]
        needles = ["✅ LEAD ACCEPTED — 🕊LET'S FLY 💸", "📞Phone +12128148158",
                   "📍 Delivery Address", "19 Pennwood Dr", "Ewing, NJ 08638-4725",
                   "👤 Name: Johnathan Perez", "💰 Price: $250",
                   "💵Payment Method:", "⏱️Time:"]
        positions = [bold.index(n) for n in needles]
        self.assertEqual(positions, sorted(positions), needles)

    def test_the_message_opens_with_the_bold_block(self):
        self.assertTrue(_msg().startswith("<b>✅ LEAD ACCEPTED"))

    def test_what_follows_the_ticket(self):
        m = _msg()
        after = m.split("</b>", 1)[1]
        for n in ("📝Extra Notes: call before arriving",
                  "🚨Clients E-Payments to dealership directly🚨",
                  "🏦CLIENT MUST PAY⚡️DIRECT TO US🏦",
                  "🆔 Reference ID: <code>79TVVYGI</code>"):
            self.assertIn(n, after, n)
        # The reference closes the message, after the payment rails.
        self.assertGreater(after.index("Reference ID"), after.index("CLIENT MUST PAY"))

    def test_the_old_lines_are_gone(self):
        m = _msg()
        for gone in ("Extra info:", "Call Client Now Confirm",
                     "ask client to pay", "Important Message",
                     "Upload Payment Receipt", "DO NOT HAND TAG"):
            self.assertNotIn(gone, m, gone)

    def test_the_ticket_defaults_read_cash_and_asap(self):
        m = _msg()
        self.assertIn(f"💵Payment Method: {bot.Config.DRIVER_PAYMENT_METHOD}", m)
        self.assertIn(f"⏱️Time: {bot.Config.DRIVER_DELIVERY_TIME}", m)


class ThePhoneRespectsDriverblockTest(unittest.TestCase):

    def test_redaction_off_shows_the_number_and_no_password_line(self):
        m = _msg(driverblock=False)
        self.assertIn("📞Phone +12128148158", m)
        self.assertNotIn("callclient", m)

    def test_redaction_on_shows_the_link_and_how_to_open_it(self):
        m = _msg(driverblock=True)
        bold = m.split("</b>", 1)[0]
        self.assertIn("open link", bold)
        self.assertIn("callclient", bold)
        self.assertNotIn("+12128148158", m)


class EveryDispatchPathSendsTheSameTicketTest(unittest.TestCase):
    """Normal accept, paid Instant Tag, Skip Dispatch — one driver format."""

    def test_the_normal_accept_dm_uses_the_builder(self):
        body = SRC.split("async def _send_driver_lead_details", 1)[1]
        body = body.split("\nasync def ", 1)[0]
        self.assertIn("_build_driver_lead_accepted_message_html", body)

    def test_skip_dispatch_sends_the_driver_format(self):
        body = SRC.split("async def _deliver_skip_dispatch", 1)[1]
        body = body.split("\nasync def ", 1)[0]
        self.assertIn("driver_dm=True", body)

    def test_paid_instant_tags_ride_the_same_delivery(self):
        body = SRC.split("async def deliver_paid_instant_pdfs", 1)[1]
        body = body.split("\nasync def ", 1)[0]
        self.assertIn("_deliver_skip_dispatch", body)

    def test_the_group_card_is_not_hijacked(self):
        body = SRC.split("async def _send_full_group_lead_to_chat", 1)[1]
        body = body.split("\nasync def ", 1)[0]
        self.assertIn("if driver_dm:", body)
        self.assertIn("_format_group_lead_message_html", body)


if __name__ == "__main__":
    unittest.main()
