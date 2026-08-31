r"""Two drivers working one lead, and a quota that could not see the work.

Reported: "driver 1 accepted a lead but driver 2 got it as well — and driver 2
has hit the owed quota of 5 so they shouldn't even get any request".

Measured against the live database first: 4957 assignment rows, and ZERO leads
with more than one accepted assignment. The database was never confused. Two
separate holes produced the symptom.

Run:  venv\Scripts\python.exe -m pytest tests/test_one_driver_one_lead.py -q
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

LEAD_ID = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
GROUP_ID = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
WINNER_CHAT = 700001
LOSER_CHAT = 700002
LOSER2_CHAT = 700003


class TheLosingDriversOfferIsClosedTest(unittest.IsolatedAsyncioTestCase):
    """It used to stay live: the reference, the delivery city and an Accept
    button that still looked available. Two drivers worked the same job."""

    def _ctx(self, remembered):
        ctx = mock.MagicMock()
        ctx.application.bot_data = {bot._DISPATCH_MSGS_KEY: {LEAD_ID: remembered}}
        ctx.bot.edit_message_text = mock.AsyncMock()
        return ctx

    async def test_every_other_driver_is_told_it_is_taken(self):
        ctx = self._ctx([(WINNER_CHAT, 10), (LOSER_CHAT, 11), (LOSER2_CHAT, 12)])
        closed = await bot._revoke_other_driver_offers(ctx, LEAD_ID, WINNER_CHAT)
        self.assertEqual(2, closed)
        chats = {c.kwargs["chat_id"] for c in ctx.bot.edit_message_text.call_args_list}
        self.assertEqual({LOSER_CHAT, LOSER2_CHAT}, chats)
        said = str(ctx.bot.edit_message_text.call_args.kwargs["text"])
        self.assertIn("Taken by another driver", said)

    async def test_the_accept_button_is_removed_not_just_the_text(self):
        ctx = self._ctx([(LOSER_CHAT, 11)])
        await bot._revoke_other_driver_offers(ctx, LEAD_ID, WINNER_CHAT)
        kb = ctx.bot.edit_message_text.call_args.kwargs["reply_markup"]
        self.assertFalse(kb.inline_keyboard, "the offer is still tappable")

    async def test_the_winner_keeps_their_own_confirmation(self):
        ctx = self._ctx([(WINNER_CHAT, 10)])
        self.assertEqual(0, await bot._revoke_other_driver_offers(ctx, LEAD_ID, WINNER_CHAT))
        ctx.bot.edit_message_text.assert_not_awaited()

    async def test_a_paid_offer_stops_offering_to_take_money(self):
        """The payer's own copy kept a live Pay button after they had paid —
        an invitation to pay twice for the same tag."""
        ctx = self._ctx([(WINNER_CHAT, 10), (LOSER_CHAT, 11)])
        closed = await bot._revoke_other_driver_offers(
            ctx, LEAD_ID, WINNER_CHAT, winner_text="✅ Paid — this one is yours.")
        self.assertEqual(2, closed)
        said = {c.kwargs["chat_id"]: c.kwargs["text"]
                for c in ctx.bot.edit_message_text.call_args_list}
        self.assertIn("Paid", said[WINNER_CHAT])
        self.assertIn("Taken by another driver", said[LOSER_CHAT])
        for c in ctx.bot.edit_message_text.call_args_list:
            self.assertFalse(c.kwargs["reply_markup"].inline_keyboard)

    def test_the_paid_delivery_closes_it(self):
        body = SRC.split("async def _deliver_skip_dispatch", 1)[1]
        body = body.split("\nasync def ", 1)[0]
        self.assertIn("winner_text=", body)
        self.assertIn("Paid — this one is yours", body)

    async def test_ids_compare_across_str_and_int(self):
        ctx = self._ctx([(str(WINNER_CHAT), 10), (LOSER_CHAT, 11)])
        self.assertEqual(1, await bot._revoke_other_driver_offers(ctx, LEAD_ID, WINNER_CHAT))

    async def test_a_message_too_old_to_edit_is_not_a_crash(self):
        ctx = self._ctx([(LOSER_CHAT, 11), (LOSER2_CHAT, 12)])
        ctx.bot.edit_message_text = mock.AsyncMock(
            side_effect=[Exception("message can't be edited"), None])
        self.assertEqual(1, await bot._revoke_other_driver_offers(ctx, LEAD_ID, WINNER_CHAT))

    async def test_nothing_remembered_is_not_a_crash(self):
        """bot_data is RAM: a restart between the offer and the accept loses it."""
        ctx = mock.MagicMock()
        ctx.application.bot_data = {}
        ctx.bot.edit_message_text = mock.AsyncMock()
        self.assertEqual(0, await bot._revoke_other_driver_offers(ctx, LEAD_ID, WINNER_CHAT))

    async def test_a_duplicate_record_is_only_closed_once(self):
        ctx = self._ctx([(LOSER_CHAT, 11), (LOSER_CHAT, 11)])
        self.assertEqual(1, await bot._revoke_other_driver_offers(ctx, LEAD_ID, WINNER_CHAT))

    def test_accepting_actually_calls_it(self):
        body = SRC.split("async def handle_accept_lead", 1)[1]
        body = body.split("\nasync def ", 1)[0]
        self.assertIn("_revoke_other_driver_offers(context, lead_id, query.message.chat_id)",
                      body)

    def test_it_cannot_cost_the_driver_their_acceptance(self):
        body = SRC.split("async def handle_accept_lead", 1)[1]
        body = body.split("\nasync def ", 1)[0]
        i = body.index("_revoke_other_driver_offers")
        self.assertIn("try:", body[max(0, i - 200):i])


class ADeliveryIsBookedAgainstTheDriverTest(unittest.TestCase):
    """lead_assignments is the only ledger the receipt debt and the suspension
    counter read. Skip Dispatch and paid Instant Tags wrote no row, so those
    jobs were invisible to both — five such leads are sitting in the live
    database right now, delivered, no receipt, no row, counting toward nobody."""

    def _book(self, existing=None, accepted=True, driver_id="d1"):
        db = mock.MagicMock()
        db.get_lead_assignment_status.return_value = existing
        db.accept_lead_assignment.return_value = {"id": "asg1"} if accepted else None
        lead = {"id": LEAD_ID, "reference_id": "REF1", "group_id": GROUP_ID}
        with mock.patch.object(bot, "db", db):
            out = bot._book_delivery_against_driver(
                lead, {"id": driver_id, "driver_name": "Susan"})
        return out, db

    def test_it_creates_and_accepts_the_row(self):
        (ok, held), db = self._book()
        self.assertTrue(ok)
        self.assertIsNone(held)
        db.create_lead_assignment.assert_called_once_with(LEAD_ID, "d1", GROUP_ID)
        db.accept_lead_assignment.assert_called_once_with(LEAD_ID, "d1")

    def test_a_lead_somebody_else_holds_is_refused_and_names_them(self):
        (ok, held), db = self._book(existing={
            "id": "other", "driver_id": "d9", "driver": {"driver_name": "Marcus"}})
        self.assertFalse(ok)
        self.assertEqual("Marcus", held)
        db.create_lead_assignment.assert_not_called()

    def test_re_releasing_to_the_SAME_driver_is_fine(self):
        (ok, held), db = self._book(existing={"id": "a", "driver_id": "d1"})
        self.assertTrue(ok, "a retry to the same driver must not be refused")
        self.assertIsNone(held)
        db.create_lead_assignment.assert_not_called()

    def test_losing_the_race_reports_the_winner(self):
        """Somebody accepted between the check and the write."""
        db = mock.MagicMock()
        db.get_lead_assignment_status.side_effect = [
            None, {"driver_id": "d9", "driver": {"driver_name": "Marcus"}}]
        db.accept_lead_assignment.return_value = None
        with mock.patch.object(bot, "db", db):
            ok, held = bot._book_delivery_against_driver(
                {"id": LEAD_ID, "group_id": GROUP_ID}, {"id": "d1"})
        self.assertFalse(ok)
        self.assertEqual("Marcus", held)

    def test_a_missing_id_is_not_a_crash(self):
        db = mock.MagicMock()
        with mock.patch.object(bot, "db", db):
            self.assertEqual((False, None),
                             bot._book_delivery_against_driver({}, {"id": "d1"}))
            self.assertEqual((False, None),
                             bot._book_delivery_against_driver({"id": LEAD_ID}, {}))
        db.create_lead_assignment.assert_not_called()

    def test_the_release_claims_the_lead_BEFORE_handing_it_over(self):
        """Booking after the send still hands a second driver a full job ticket
        when they accepted a half-second earlier."""
        body = SRC.split("async def _deliver_skip_dispatch", 1)[1]
        body = body.split("\nasync def ", 1)[0]
        i_book = body.index("_book_delivery_against_driver")
        i_send = body.index("await _send_full_group_lead_to_chat(")
        self.assertLess(i_book, i_send, "it hands the job over before claiming it")

    def test_a_lead_already_held_stops_the_release_dead(self):
        body = SRC.split("async def _deliver_skip_dispatch", 1)[1]
        body = body.split("\nasync def ", 1)[0]
        self.assertIn("if not booked and held_by:", body)
        i_guard = body.index("if not booked and held_by:")
        i_send = body.index("await _send_full_group_lead_to_chat(")
        self.assertLess(i_guard, i_send)
        self.assertIn("Already taken", body)

    def test_the_release_also_closes_the_other_offers(self):
        body = SRC.split("async def _deliver_skip_dispatch", 1)[1]
        body = body.split("\nasync def ", 1)[0]
        self.assertIn("_revoke_other_driver_offers", body)


class EveryOfferIsRevocableTest(unittest.TestCase):
    """A revoke can only reach offers whose message ids were recorded. Two
    senders never recorded theirs, so their drivers kept live Accept buttons
    for jobs that were already gone — including every Instant Tag offer."""

    def _body(self, fn):
        return SRC.split(f"async def {fn}", 1)[1].split("\nasync def ", 1)[0]

    def test_the_instant_tag_offer_is_remembered(self):
        self.assertIn("_remember_dispatch_message",
                      self._body("_dispatch_instant_tag_lead"))

    def test_the_group_fan_out_offer_is_remembered(self):
        self.assertIn("_remember_dispatch_message",
                      self._body("_send_driver_requests_for_group"))

    def test_the_ordinary_dispatch_still_is(self):
        self.assertGreaterEqual(SRC.count("_remember_dispatch_message("), 5)


if __name__ == "__main__":
    unittest.main()
