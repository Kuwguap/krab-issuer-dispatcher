r"""The client's tag can still be released after the address turns up.

Measured against the live database before this was written: eleven leads asked
for a tag email, none was ever approved, and so in the product's whole life ZERO
client tag emails have gone out. The release button existed in exactly one
place — attached to the tag PDF at the moment it goes to the team — and only for
a lead that ALREADY had both the toggle and the client's address. Supply the
address afterwards and the tag has already been posted with no button on it,
and there is no way to release it from anywhere.

The button also sits on the lead-sent confirmation, by the owner's decision: it
was raised that at that moment no driver has accepted, no tag exists, and a tap
makes the sweep BUILD one — allocating a plate for a lead that may still be
reassigned or withdrawn — and the answer was to put it there anyway. So what is
held still here is what makes that survivable rather than an argument against it:

* it never shares a row with "🚫 Skip Dispatch", which withdraws the lead from
  the team and every driver — those two must not be a mis-tap apart;
* an early tap SAYS it is making the tag now, rather than minting one silently;
* and tapping remains the only thing that sends. Relaxing WHO is offered the
  choice must not relax who makes it.

Run:  venv\Scripts\python.exe -m pytest tests/test_tag_release_reachable.py -q
"""
import asyncio
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy-key")

import bot as B  # noqa: E402

LEAD = "11111111-2222-3333-4444-555555555555"


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class WhoIsOfferedTheReleaseTest(unittest.TestCase):

    def test_an_address_is_enough_toggle_or_no_toggle(self):
        """Thirty live leads carry a client address with the toggle off. The
        button offers a choice; it does not take one."""
        self.assertTrue(B._lead_awaiting_tag_email(
            {"email": "a@b.com", "wants_tag_email": False}))
        self.assertTrue(B._lead_awaiting_tag_email(
            {"email": "a@b.com", "wants_tag_email": True}))

    def test_without_an_address_there_is_nothing_to_offer(self):
        self.assertFalse(B._lead_awaiting_tag_email({"wants_tag_email": True}))
        self.assertFalse(B._lead_awaiting_tag_email({"email": "   "}))

    def test_it_is_not_offered_twice(self):
        self.assertFalse(B._lead_awaiting_tag_email(
            {"email": "a@b.com", "tag_emailed_at": "2026-09-09T10:00:00-04:00"}))
        self.assertFalse(B._lead_awaiting_tag_email(
            {"email": "a@b.com", "tag_email_approved_at": "2026-09-09T10:00:00-04:00"}))

    def test_nothing_at_all(self):
        self.assertFalse(B._lead_awaiting_tag_email(None))
        self.assertFalse(B._lead_awaiting_tag_email({}))


class SettingTheAddressOffersItTest(unittest.TestCase):
    """The case that produced eleven stuck leads: the address arrives after the
    tag has already gone out."""

    def _set_email(self, fresh):
        msg = mock.MagicMock()
        msg.reply_text = mock.AsyncMock()
        msg.chat_id = -100123
        u = mock.MagicMock()
        u.effective_message = msg
        u.effective_user = types.SimpleNamespace(id=777, username="issuer")
        ctx = mock.MagicMock()
        ctx.args = ["LAB4CDVZ", "client@example.com"]
        db = mock.MagicMock()
        lead = {"id": LEAD, "reference_id": "LAB4CDVZ", "group_id": None}
        db.get_lead_by_reference_id.return_value = lead
        db.get_lead_by_id.return_value = fresh
        db.update_lead.return_value = True
        with mock.patch.object(B, "db", db), \
             mock.patch.object(B, "_user_is_global_supervisor", return_value=True), \
             mock.patch.object(B, "_maybe_ride_insurance_with_tag", new=mock.AsyncMock()):
            run(B.cmd_set_client_email(u, ctx))
        return msg

    def test_the_release_button_comes_with_the_confirmation(self):
        msg = self._set_email({"id": LEAD, "reference_id": "LAB4CDVZ",
                               "email": "client@example.com"})
        kb = msg.reply_text.await_args[1].get("reply_markup")
        self.assertIsNotNone(kb, "no button offered after the address was set")
        data = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertEqual([B.TAG_EMAIL_CB + LEAD], data)
        said = " ".join(str(c[0][0]) for c in msg.reply_text.await_args_list)
        self.assertIn("can go to the client", said)

    def test_a_lead_already_emailed_is_not_offered_it_again(self):
        msg = self._set_email({
            "id": LEAD, "reference_id": "LAB4CDVZ", "email": "client@example.com",
            "tag_emailed_at": "2026-09-09T10:00:00-04:00"})
        self.assertIsNone(msg.reply_text.await_args[1].get("reply_markup"))

    def test_the_insurance_branch_still_wins_when_a_card_is_held(self):
        """Setting the address on an insurance lead issues the held card, which
        is the older behaviour and must not be shadowed."""
        msg = self._set_email({
            "id": LEAD, "reference_id": "LAB4CDVZ", "email": "client@example.com",
            "wants_insurance": True})
        said = " ".join(str(c[0][0]) for c in msg.reply_text.await_args_list)
        self.assertIn("insurance card", said.lower())


class WhereTheButtonLivesTest(unittest.TestCase):

    def test_it_is_on_the_lead_sent_confirmation_by_decision(self):
        """The owner asked for it there and, told what it costs, asked again.

        What it costs: that message is posted before any driver has accepted,
        so the sweep BUILDS the tag when this is tapped, and building one
        allocates a plate for a lead that may still be reassigned or withdrawn.
        Two things make that survivable, and both are pinned below — it sits on
        its own row away from Skip Dispatch, and the handler says out loud when
        it is minting a tag early rather than doing it silently.
        """
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        i = src.index("def _after_send_keyboard(")
        block = src[i:src.index("\ndef ", i + 10)]
        self.assertIn("TAG_EMAIL_CB", block)
        self.assertIn("Skip Dispatch", block)

    def test_it_never_shares_a_row_with_skip_dispatch(self):
        """Skip Dispatch withdraws the lead from the team and every driver."""
        for row in B._after_send_keyboard(LEAD).inline_keyboard:
            texts = " ".join(b.text for b in row)
            self.assertFalse("Skip Dispatch" in texts and "Email tag" in texts, texts)

    def test_an_early_tap_says_it_is_making_the_tag_now(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        i = src.index("async def handle_tag_email_to_client(")
        self.assertIn("has not gone to the team yet", src[i:i + 4000])

    def test_the_tag_pdf_is_still_where_the_button_normally_rides(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        self.assertIn("_tag_email_keyboard(str(lead.get(\"id\")))\n"
                      "                              if _lead_awaiting_tag_email(lead) else None)",
                      src)

    def test_pressing_it_is_still_the_only_thing_that_sends(self):
        """Relaxing who is OFFERED the choice must not relax who makes it: the
        sweep sends on tag_email_approved_at, which only the tap writes."""
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        i = src.index("async def handle_tag_email_to_client(")
        block = src[i:i + 4000]
        self.assertIn('"tag_email_approved_at": stamp', block)
        j = src.index("async def send_approved_tag_emails(")
        self.assertIn("get_tag_emails_awaiting_send", src[j:j + 900])


if __name__ == "__main__":
    unittest.main()
