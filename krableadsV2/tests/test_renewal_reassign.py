r"""Renewals move only when somebody reassigns them — never on a timer, and
never to every driver on the board.

Asked for: "when driver or person who created lead misses or time passes it
goes out to everyone. change that. only go out when its reassigned. if person
who created tag reassign to different driver or driver themselves reassign to
different driver the person who created lead sees message that driver decided
not to take lead choose a new driver."

Run:  venv\Scripts\python.exe -m pytest tests/test_renewal_reassign.py -q
"""
import asyncio
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

RENEWAL = "11111111-2222-3333-4444-555555555555"
DRIVER = "99999999-8888-7777-6666-555555555555"
CREATOR_CHAT = 4242


def _fn(name, src=None):
    body = (src or SRC).split(f"def {name}", 1)[1]
    return body.split("\nasync def ", 1)[0].split("\ndef ", 1)[0]


class NothingFansOutAnyMoreTest(unittest.TestCase):
    """The all-drivers broadcast is gone, and so is the timer that fired it."""

    def test_the_all_drivers_escalation_is_gone(self):
        self.assertNotIn("_escalate_renewal_driver_all", SRC)

    def test_no_timed_escalation_job_is_scheduled(self):
        self.assertNotIn("renewal_driver_esc_", SRC)
        self.assertNotIn("_driver_esc_job", SRC)

    def test_the_due_pass_does_not_broadcast(self):
        """A renewal the driver has simply not answered stays theirs."""
        body = _fn("check_renewals")
        self.assertNotIn("ALL drivers", body)
        self.assertIn("_renewal_hand_back_to_creator", body)

    def test_the_promises_in_the_texts_match_the_behaviour(self):
        body = _fn("check_renewals")
        self.assertNotIn("opens to all drivers", body.lower())
        self.assertNotIn("first accept wins", body.lower())

    def test_the_creator_can_move_it_themselves(self):
        """They decide where it goes, so they are told directly and get the
        same picker — not just a notice in a team chat they may not sit in."""
        body = _fn("check_renewals")
        self.assertIn("_renewal_creator_chat", body)
        self.assertIn("rpk_", body)


class AReassignGoesBackToTheCreatorTest(unittest.IsolatedAsyncioTestCase):

    def _renewal(self, status="sent"):
        return {"id": RENEWAL, "driver_status": status,
                "lead": {"reference_id": "REF1", "user_id": CREATOR_CHAT}}

    async def test_the_creator_is_told_and_offered_a_picker(self):
        db = mock.MagicMock()
        db.claim_renewal_driver_escalation.return_value = True
        db.get_renewal_by_id.return_value = self._renewal()
        ctx = mock.MagicMock()
        ctx.bot.send_message = mock.AsyncMock()
        with mock.patch.object(bot, "db", db):
            await bot._renewal_hand_back_to_creator(ctx, RENEWAL, declined_by="Kita")
        ctx.bot.send_message.assert_awaited_once()
        kw = ctx.bot.send_message.await_args.kwargs
        self.assertEqual(CREATOR_CHAT, kw["chat_id"])
        self.assertIn("Kita", kw["text"])
        self.assertIn("not taking", kw["text"])
        self.assertIn("choose a new driver", kw["text"].lower())
        button = kw["reply_markup"].inline_keyboard[0][0]
        self.assertTrue(button.callback_data.startswith("rpk_"))

    async def test_it_never_fires_twice_for_one_refusal(self):
        """The atomic claim is what stops a double hand-back."""
        db = mock.MagicMock()
        db.claim_renewal_driver_escalation.return_value = False
        ctx = mock.MagicMock()
        ctx.bot.send_message = mock.AsyncMock()
        with mock.patch.object(bot, "db", db):
            await bot._renewal_hand_back_to_creator(ctx, RENEWAL)
        ctx.bot.send_message.assert_not_awaited()

    async def test_a_lead_with_no_creator_chat_is_survivable(self):
        db = mock.MagicMock()
        db.claim_renewal_driver_escalation.return_value = True
        db.get_renewal_by_id.return_value = {
            "id": RENEWAL, "lead": {"reference_id": "REF1"}}     # no user_id
        ctx = mock.MagicMock()
        ctx.bot.send_message = mock.AsyncMock()
        with mock.patch.object(bot, "db", db):
            await bot._renewal_hand_back_to_creator(ctx, RENEWAL)
        ctx.bot.send_message.assert_not_awaited()

    def test_the_driver_reassign_button_hands_back(self):
        body = _fn("handle_renewal_driver_reassign")
        self.assertIn("_renewal_hand_back_to_creator", body)
        self.assertNotIn("ALL drivers", body)


class TheCreatorPicksTheNextDriverTest(unittest.IsolatedAsyncioTestCase):

    def _query(self, data):
        q = mock.MagicMock()
        q.data = data
        q.answer = mock.AsyncMock()
        q.message.reply_text = mock.AsyncMock()
        q.message.edit_text = mock.AsyncMock()
        upd = mock.MagicMock()
        upd.callback_query = q
        return upd, q

    async def test_the_picker_lists_only_live_drivers(self):
        db = mock.MagicMock()
        db.get_renewal_by_id.return_value = {
            "id": RENEWAL, "driver_status": "escalated",
            "lead": {"reference_id": "REF1", "user_id": CREATOR_CHAT}}
        drivers = [
            {"id": DRIVER, "driver_name": "Kita", "is_active": True},
            {"id": "d2", "driver_name": "Gone", "is_active": False},
            {"id": "d3", "driver_name": "Benched", "is_active": True},
        ]
        upd, q = self._query("rpk_" + bot._short_uuid(RENEWAL))
        with mock.patch.object(bot, "db", db), \
                mock.patch.object(bot, "_get_all_drivers_cached", return_value=drivers), \
                mock.patch.object(bot, "_get_suspended_driver_ids", return_value={"d3"}):
            await bot.handle_renewal_pick_open(upd, mock.MagicMock())
        kb = q.message.reply_text.await_args.kwargs["reply_markup"]
        names = [b.text for row in kb.inline_keyboard for b in row]
        self.assertEqual(["Kita"], names)          # inactive and suspended excluded

    async def test_picking_sends_the_offer_and_reopens_the_ladder(self):
        """driver_status must return to 'sent', or the NEXT driver's Reassign
        would be swallowed by the escalation claim and nobody would be told."""
        db = mock.MagicMock()
        db.get_renewal_by_id.return_value = {
            "id": RENEWAL, "driver_status": "escalated",
            "lead": {"reference_id": "REF1", "user_id": CREATOR_CHAT}}
        driver = {"id": DRIVER, "driver_name": "Kita", "is_active": True}
        upd, q = self._query("rpd_" + bot._short_uuid(RENEWAL) + bot._short_uuid(DRIVER))
        send = mock.AsyncMock(return_value=True)
        with mock.patch.object(bot, "db", db), \
                mock.patch.object(bot, "_get_all_drivers_cached", return_value=[driver]), \
                mock.patch.object(bot, "_send_renewal_to_driver", send):
            await bot.handle_renewal_pick_driver(upd, mock.MagicMock())
        send.assert_awaited_once()
        self.assertEqual(driver, send.await_args.args[2])
        wrote = [c.args[1] for c in db.update_renewal.call_args_list]
        self.assertEqual("sent", wrote[0]["driver_status"])

    async def test_an_unreachable_pick_leaves_it_handed_back(self):
        db = mock.MagicMock()
        db.get_renewal_by_id.return_value = {
            "id": RENEWAL, "driver_status": "escalated",
            "lead": {"reference_id": "REF1", "user_id": CREATOR_CHAT}}
        driver = {"id": DRIVER, "driver_name": "Kita", "is_active": True}
        upd, q = self._query("rpd_" + bot._short_uuid(RENEWAL) + bot._short_uuid(DRIVER))
        with mock.patch.object(bot, "db", db), \
                mock.patch.object(bot, "_get_all_drivers_cached", return_value=[driver]), \
                mock.patch.object(bot, "_send_renewal_to_driver",
                                  mock.AsyncMock(return_value=False)):
            await bot.handle_renewal_pick_driver(upd, mock.MagicMock())
        last = db.update_renewal.call_args_list[-1].args[1]
        self.assertEqual("escalated", last["driver_status"])

    async def test_an_already_accepted_renewal_is_not_reoffered(self):
        db = mock.MagicMock()
        db.get_renewal_by_id.return_value = {"id": RENEWAL, "driver_status": "accepted",
                                             "lead": {"reference_id": "REF1"}}
        upd, q = self._query("rpd_" + bot._short_uuid(RENEWAL) + bot._short_uuid(DRIVER))
        send = mock.AsyncMock()
        with mock.patch.object(bot, "db", db), \
                mock.patch.object(bot, "_send_renewal_to_driver", send):
            await bot.handle_renewal_pick_driver(upd, mock.MagicMock())
        send.assert_not_awaited()

    def test_both_callbacks_are_registered(self):
        self.assertIn('pattern="^rpk_"', SRC)
        self.assertIn('pattern="^rpd_"', SRC)

    def test_the_callback_payloads_fit_telegrams_limit(self):
        cb = "rpd_" + bot._short_uuid(RENEWAL) + bot._short_uuid(DRIVER)
        self.assertLessEqual(len(cb.encode()), 64)
        self.assertEqual((bot._short_uuid(RENEWAL), bot._short_uuid(DRIVER)),
                         bot._parse_paired_short_uuids(cb, "rpd_"))


if __name__ == "__main__":
    unittest.main()
