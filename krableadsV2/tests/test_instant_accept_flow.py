r"""Accept first, pay after — and say who the lead is by.

Asked for:
  - "✅ Accepted by Higkage's team / 👤Lead by: king krab krab krab /
     📋Reference ID: LAB4CDVZ"
  - "for the instant tag it shows accept decline and pay $100 to get tag; it
     shouldn't be. Just accept and decline, and when they accept the driver gets
     a link ... in chat and the same link as an inline button"
  - "whoever types password is also sent to supervisors: instant tag released to
     {driver name} by @username"
  - "fix the error ❌ Error accepting lead. Please try again."

The error and the buttons were the same bug: _dispatch_instant_tag_lead never
wrote a lead_assignments row, and db.accept_lead_assignment can only UPDATE one
that already exists — so every Accept failed and edit_text took the offer away.

Run:  venv\Scripts\python.exe -m pytest tests/test_instant_accept_flow.py -q
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
DRIVER = {"id": "d1", "driver_name": "Susan", "driver_telegram_id": "700001"}


def _lead(**over):
    lead = {"id": LEAD_ID, "reference_id": "LAB4CDVZ", "group_id": GROUP_ID,
            "instant_tag": True, "price": "$250", "driver_amount": "$200",
            "telegram_name": "👑King🦀 Krab", "user_id": 1184788227}
    lead.update(over)
    return lead


class TheOfferIsAcceptOrDeclineOnlyTest(unittest.IsolatedAsyncioTestCase):

    async def _dispatch(self, drivers=(DRIVER,)):
        ctx = mock.MagicMock()
        ctx.bot.send_message = mock.AsyncMock()
        fake_db = mock.MagicMock()
        link = mock.AsyncMock(return_value=("https://pay.test/x", None))
        with mock.patch.object(bot, "db", fake_db), \
                mock.patch.object(bot, "request_instant_pdf_link", link), \
                mock.patch.object(bot, "_skip_dispatch_allowed",
                                  lambda lead, uid: (False, "not_supervisor")):
            await bot._dispatch_instant_tag_lead(
                ctx, _lead(), list(drivers), notify_chat_id=None, user_data={})
        kwargs = ctx.bot.send_message.call_args.kwargs
        buttons = [b for row in kwargs["reply_markup"].inline_keyboard for b in row]
        return kwargs["text"], buttons, fake_db, link

    async def test_only_accept_and_decline(self):
        _text, buttons, _db, _link = await self._dispatch()
        self.assertEqual(["✅ Accept", "❌ Decline"], [b.text for b in buttons])

    async def test_there_is_no_pay_button_on_the_offer(self):
        _text, buttons, _db, _link = await self._dispatch()
        self.assertTrue(all(b.url is None for b in buttons),
                        "a driver could pay for a job they never claimed")

    async def test_no_checkout_is_created_before_anybody_accepts(self):
        _text, _b, _db, link = await self._dispatch()
        link.assert_not_awaited()

    async def test_the_offer_is_recorded_so_accept_can_work(self):
        _text, _b, fake_db, _link = await self._dispatch()
        fake_db.create_lead_assignment.assert_called_once_with(
            LEAD_ID, "d1", GROUP_ID)

    async def test_the_money_lines_survive(self):
        text, _b, _db, _link = await self._dispatch()
        for needle in ("CASH DELIVERY ALERT", "Cash collection:", "$250",
                       "Required prepay:", "$200", "Driver keeps:", "$50"):
            self.assertIn(needle, text, needle)

    async def test_it_says_accept_gets_you_the_link(self):
        text, _b, _db, _link = await self._dispatch()
        self.assertIn("ACCEPT → GET YOUR PAYMENT LINK", text)
        self.assertNotIn("PAY $200 →", text)

    async def test_a_driver_who_cannot_be_reached_is_not_recorded_as_offered(self):
        ctx = mock.MagicMock()
        ctx.bot.send_message = mock.AsyncMock(side_effect=Exception("blocked"))
        fake_db = mock.MagicMock()
        with mock.patch.object(bot, "db", fake_db), \
                mock.patch.object(bot, "_skip_dispatch_allowed",
                                  lambda lead, uid: (False, "x")):
            await bot._dispatch_instant_tag_lead(
                ctx, _lead(), [DRIVER], notify_chat_id=None, user_data={})
        # The row is written before the send — harmless, and it is what lets a
        # retry work — but the driver must not be reported as offered.
        self.assertTrue(fake_db.create_lead_assignment.called)


class AcceptingHandsOverTheLinkTest(unittest.IsolatedAsyncioTestCase):

    async def _accept(self, url=("https://pay.test/abc", None)):
        ctx = mock.MagicMock()
        ctx.bot.send_message = mock.AsyncMock()
        with mock.patch.object(bot, "db", mock.MagicMock()), \
                mock.patch.object(bot, "request_instant_pdf_link",
                                  mock.AsyncMock(return_value=url)), \
                mock.patch.object(bot, "_tell_supervisors", mock.AsyncMock()):
            await bot._instant_tag_link_after_accept(ctx, _lead(), DRIVER)
        return ctx.bot.send_message

    async def test_the_link_is_in_the_text_and_on_a_button(self):
        send = await self._accept()
        kwargs = send.call_args.kwargs
        self.assertIn("https://pay.test/abc", kwargs["text"],
                      "a button alone cannot be copied or forwarded")
        button = kwargs["reply_markup"].inline_keyboard[0][0]
        self.assertEqual("https://pay.test/abc", button.url)
        self.assertIn("$200", button.text)

    async def test_it_goes_to_the_driver(self):
        send = await self._accept()
        self.assertEqual(700001, send.call_args.kwargs["chat_id"])

    async def test_a_broken_checkout_tells_the_driver_and_the_office(self):
        ctx = mock.MagicMock()
        ctx.bot.send_message = mock.AsyncMock()
        told = mock.AsyncMock()
        with mock.patch.object(bot, "db", mock.MagicMock()), \
                mock.patch.object(bot, "request_instant_pdf_link",
                                  mock.AsyncMock(return_value=(None, "stripe key missing"))), \
                mock.patch.object(bot, "_tell_supervisors", told):
            await bot._instant_tag_link_after_accept(ctx, _lead(), DRIVER)
        said = ctx.bot.send_message.call_args.kwargs["text"]
        self.assertIn("You have the job", said)
        told.assert_awaited()
        self.assertIn("STRIPE_SECRET_KEY", told.call_args.args[1])

    async def test_an_already_paid_tag_says_so_instead_of_erroring(self):
        ctx = mock.MagicMock()
        ctx.bot.send_message = mock.AsyncMock()
        with mock.patch.object(bot, "db", mock.MagicMock()), \
                mock.patch.object(bot, "request_instant_pdf_link",
                                  mock.AsyncMock(return_value=(None, "already paid"))), \
                mock.patch.object(bot, "_tell_supervisors", mock.AsyncMock()):
            await bot._instant_tag_link_after_accept(ctx, _lead(), DRIVER)
        self.assertIn("already paid",
                      ctx.bot.send_message.call_args.kwargs["text"].lower())

    async def test_a_driver_with_no_chat_id_is_not_a_crash(self):
        ctx = mock.MagicMock()
        ctx.bot.send_message = mock.AsyncMock()
        with mock.patch.object(bot, "db", mock.MagicMock()):
            await bot._instant_tag_link_after_accept(
                ctx, _lead(), {"id": "d9", "driver_name": "Ghost"})
        ctx.bot.send_message.assert_not_awaited()


class TheAcceptErrorIsFixedTest(unittest.TestCase):
    """"❌ Error accepting lead. Please try again." with the offer wiped."""

    def _body(self):
        b = SRC.split("async def handle_accept_lead", 1)[1]
        return b.split("\nasync def ", 1)[0]

    def test_an_instant_offer_with_no_row_heals_itself(self):
        body = self._body()
        self.assertIn('if not accepted_row and lead.get("instant_tag"):', body)
        self.assertIn("db.create_lead_assignment", body)

    def test_it_only_heals_when_nobody_has_accepted_yet(self):
        """Otherwise the second driver to tap would steal a taken lead."""
        self.assertIn("if not db.get_lead_assignment_status(lead_id):", self._body())

    def test_the_generic_error_is_still_there_for_real_failures(self):
        self.assertIn("Error accepting lead", self._body())

    def test_an_instant_accept_does_not_leak_the_details(self):
        """The address and the client's phone are what the payment buys."""
        body = self._body()
        self.assertIn('if lead.get("instant_tag"):', body)
        self.assertIn("_instant_tag_link_after_accept", body)
        i_instant = body.index("_instant_tag_link_after_accept")
        i_details = body.index("_start_tracking_gate_or_send_details")
        self.assertLess(i_instant, i_details)
        self.assertIn("    else:\n        # Location gate", body)

    def test_the_renewal_still_gets_scheduled(self):
        """An early return here would have skipped it."""
        body = self._body()
        self.assertIn("db.schedule_renewal", body)
        self.assertLess(body.index("_instant_tag_link_after_accept"),
                        body.index("db.schedule_renewal"))

    def test_accepting_does_not_release_the_tag(self):
        """The tag is the thing being SOLD. Making Accept work at all made this
        block reachable for instant leads for the first time, and it would have
        posted the PDF to the group before a cent was paid."""
        body = self._body()
        i_guard = body.index('if lead.get("instant_tag"):\n            # On an Instant Tag')
        i_send = body.index("_send_all_tag_pdfs")
        self.assertLess(i_guard, i_send, "the tag block is not guarded")
        self.assertIn("elif offered_to_a_team:", body)


class ASettledInstantTagCannotBeAcceptedAgainTest(unittest.IsolatedAsyncioTestCase):
    """Without this a second driver takes over instant_pdf_driver_id and is told
    the tag is "on its way to you" — for a job already sent to somebody else."""

    async def _accept(self, **lead_over):
        lead = {"id": LEAD_ID, "reference_id": "REF7", "group_id": GROUP_ID,
                "instant_tag": True,
                "vehicle_details": "Client\n-\n-\n-\n-\n-\n-\n-\n-\n-\n-"}
        lead.update(lead_over)
        q = mock.MagicMock()
        q.data = f"accept_lead_{LEAD_ID}"
        q.answer = mock.AsyncMock()
        q.from_user = mock.MagicMock(id=999, username="susan", full_name="Susan")
        q.message.chat_id = 999
        q.message.edit_text = mock.AsyncMock()
        q.message.reply_text = mock.AsyncMock()
        upd = mock.MagicMock(callback_query=q)
        db = mock.MagicMock()
        db.get_lead_by_id.return_value = lead
        db.accept_lead_assignment.return_value = {"id": "asg1"}
        db.get_group_lead_offers.return_value = []
        db.get_driver_pending_receipts.return_value = []
        db.get_active_renewal_for_lead.return_value = None
        db.apply_paper_on_lead_accept.return_value = None
        link = mock.AsyncMock()
        with mock.patch.object(bot, "db", db), \
                mock.patch.object(bot, "_driver_row_for_telegram_user",
                                  lambda uid: {"id": "d1", "driver_name": "Susan",
                                               "driver_telegram_id": "999"}), \
                mock.patch.object(bot, "_instant_tag_link_after_accept", link), \
                mock.patch.object(bot, "_send_all_tag_pdfs", mock.AsyncMock()), \
                mock.patch.object(bot, "_notify_initiator_lead_accepted_summary",
                                  mock.AsyncMock()), \
                mock.patch.object(bot, "_should_defer_supervisory_until_source",
                                  lambda l: True):
            await bot.handle_accept_lead(upd, mock.MagicMock())
        said = " ".join(str(c.args[0]) for c in q.message.edit_text.call_args_list
                        if c.args)
        return db, link, said

    async def test_an_already_paid_tag_is_refused(self):
        db, link, said = await self._accept(instant_pdf_paid_at="2026-08-30T10:00:00Z")
        db.accept_lead_assignment.assert_not_called()
        self.assertEqual(0, link.await_count)
        self.assertIn("settled", said.lower())

    async def test_an_already_released_tag_is_refused(self):
        db, link, _ = await self._accept(
            instant_pdf_delivered_at="2026-08-30T10:00:00Z")
        db.accept_lead_assignment.assert_not_called()
        self.assertEqual(0, link.await_count)

    async def test_an_unsettled_tag_is_still_acceptable(self):
        db, link, _ = await self._accept()
        db.accept_lead_assignment.assert_called_once()
        self.assertEqual(1, link.await_count)


class TheBoardDoesNotWalkAnUnpaidTagAlongTest(unittest.IsolatedAsyncioTestCase):
    """Accept had to start writing accepted_at for the button to work, which put
    unpaid instant tags into the timed sweep for the first time — reporting
    "Tag printed" and "Driver on the way" for a job nobody has paid for."""

    async def _sweep(self, unpaid):
        db = mock.MagicMock()
        db.get_unpaid_instant_lead_ids.return_value = set(unpaid)
        db.get_recently_accepted_leads.return_value = [
            {"lead_id": LEAD_ID, "accepted_at": "2020-01-01T00:00:00+00:00"}]
        db.get_recently_group_accepted_leads.return_value = []
        db.get_recently_paid_instant_leads.return_value = []
        with mock.patch.object(bot, "db", db):
            await bot.advance_timed_statuses(mock.MagicMock())
        return [c.args[1] for c in db.advance_delivery_status.call_args_list
                if len(c.args) > 1]

    async def test_an_unpaid_instant_tag_is_left_alone(self):
        self.assertEqual([], await self._sweep({LEAD_ID}))

    async def test_everything_else_still_advances(self):
        self.assertTrue(await self._sweep(set()))


class AcceptingNeverReleasesThePaidTagTest(unittest.IsolatedAsyncioTestCase):
    """Driven through the real handler, because a source check cannot prove the
    document did not go out."""

    async def _accept(self, *, instant):
        lead = {
            "id": LEAD_ID, "reference_id": "REF7", "group_id": GROUP_ID,
            "instant_tag": instant, "price": "$250", "driver_amount": "$200",
            "vehicle_details": "Client\n-\n-\n-\n-\n-\n-\n-\n-\n-\n-",
        }
        q = mock.MagicMock()
        q.data = f"accept_lead_{LEAD_ID}"
        q.answer = mock.AsyncMock()
        q.from_user = mock.MagicMock(id=999, username="susan", full_name="Susan")
        q.message.chat_id = 999
        q.message.edit_text = mock.AsyncMock()
        q.message.reply_text = mock.AsyncMock()
        upd = mock.MagicMock(callback_query=q)
        db = mock.MagicMock()
        db.get_lead_by_id.return_value = lead
        db.accept_lead_assignment.return_value = {"id": "asg1"}
        db.get_group_lead_offers.return_value = []
        db.apply_paper_on_lead_accept.return_value = None
        db.get_driver_pending_receipts.return_value = []
        db.get_active_renewal_for_lead.return_value = None
        db.get_group_by_id.return_value = {
            "id": GROUP_ID, "group_name": "HighKage", "group_telegram_id": "-100123"}
        tags = mock.AsyncMock()
        with mock.patch.object(bot, "db", db), \
                mock.patch.object(bot, "_driver_row_for_telegram_user",
                                  lambda uid: {"id": "d1", "driver_name": "Susan",
                                               "driver_telegram_id": "999"}), \
                mock.patch.object(bot, "_send_all_tag_pdfs", tags), \
                mock.patch.object(bot, "_instant_tag_link_after_accept", mock.AsyncMock()), \
                mock.patch.object(bot, "_start_tracking_gate_or_send_details", mock.AsyncMock()), \
                mock.patch.object(bot, "_notify_initiator_lead_accepted_summary", mock.AsyncMock()), \
                mock.patch.object(bot, "_send_supervisory_new_lead_notices_from_lead",
                                  mock.AsyncMock()), \
                mock.patch.object(bot, "_should_defer_supervisory_until_source",
                                  lambda l: True):
            await bot.handle_accept_lead(upd, mock.MagicMock())
        return tags, db

    async def test_an_instant_tag_is_not_sent_on_accept(self):
        tags, db = await self._accept(instant=True)
        self.assertEqual(0, tags.await_count,
                         "the paid tag went out before anybody paid for it")
        statuses = [c.args[1] for c in db.advance_delivery_status.call_args_list
                    if len(c.args) > 1]
        self.assertNotIn("tag_issued", statuses)

    async def test_an_ordinary_lead_still_gets_its_tag(self):
        tags, _ = await self._accept(instant=False)
        self.assertEqual(1, tags.await_count,
                         "the guard swallowed a normal lead's tag")


class TheAcceptedMessageNamesTheAuthorTest(unittest.TestCase):

    def test_both_group_notices_use_the_new_shape(self):
        self.assertEqual(2, SRC.count("👤Lead by: {lead_by_esc}"))
        self.assertEqual(2, SRC.count("📋Reference ID: `{ref_show}`")
                         + SRC.count("📋Reference ID: `{reference_id}`"))
        self.assertNotIn("Issuer: @{acceptor_esc}", SRC)

    def test_it_is_the_lead_author_not_the_acceptor(self):
        self.assertEqual(
            2, SRC.count("lead_by_esc = _telegram_md1_escape("
                         "_lead_issuer_display_from_lead(lead or {}))"))

    def test_the_author_renders_as_a_name(self):
        self.assertEqual("👑King🦀 Krab",
                         bot._lead_issuer_display_from_lead(_lead()))

    def test_an_older_lead_without_a_name_falls_back_to_the_handle(self):
        self.assertEqual("@kingkrab", bot._lead_issuer_display_from_lead(
            _lead(telegram_name=None, telegram_username="kingkrab")))


class SupervisorsHearAboutEveryReleaseTest(unittest.IsolatedAsyncioTestCase):

    async def _deliver(self, how, released_by=None, driver_ok=True):
        told = []
        with mock.patch.object(bot, "_global_supervisory_chat_ids", lambda: [11, 22]), \
                mock.patch.object(bot, "db", mock.MagicMock()):
            ctx = mock.MagicMock()
            ctx.bot.send_message = mock.AsyncMock(
                side_effect=lambda **k: told.append((k.get("chat_id"), k.get("text"))))
            await bot._tell_supervisors(
                ctx,
                (f"🤖 Instant tag released to <b>Susan</b> by "
                 f"{bot._acting_user_label(released_by)}") if how == "password"
                else "🤖 Instant tag paid by <b>Susan</b>")
        return told

    async def test_the_password_release_names_the_driver_and_the_releaser(self):
        who = mock.MagicMock(username="kingkrab", full_name="King Krab")
        told = await self._deliver("password", who)
        self.assertEqual([11, 22], [c for c, _ in told])
        self.assertIn("Instant tag released to <b>Susan</b> by @kingkrab", told[0][1])

    async def test_someone_without_a_username_is_named_anyway(self):
        who = mock.MagicMock(username="", full_name="King Krab")
        told = await self._deliver("password", who)
        self.assertIn("by King Krab", told[0][1])

    async def test_a_card_payment_is_reported_too(self):
        told = await self._deliver("paid")
        self.assertIn("Instant tag paid by <b>Susan</b>", told[0][1])

    async def test_one_supervisor_failing_does_not_stop_the_rest(self):
        sent = []

        async def _flaky(**k):
            if k.get("chat_id") == 11:
                raise RuntimeError("blocked")
            sent.append(k.get("chat_id"))

        ctx = mock.MagicMock()
        ctx.bot.send_message = _flaky
        with mock.patch.object(bot, "_global_supervisory_chat_ids", lambda: [11, 22]):
            await bot._tell_supervisors(ctx, "x")
        self.assertEqual([22], sent)

    def test_the_delivery_actually_sends_it(self):
        body = SRC.split("async def _deliver_skip_dispatch", 1)[1]
        body = body.split("\nasync def ", 1)[0]
        self.assertIn("Instant tag released to", body)
        self.assertIn("Instant tag paid by", body)
        self.assertIn("await _tell_supervisors(context, note)", body)
        # Only once the tag actually reached the driver.
        self.assertIn("if driver_ok:", body)

    def test_both_password_call_sites_say_who_released_it(self):
        self.assertIn("released_by=update.effective_user", SRC)
        self.assertIn("released_by=query.from_user", SRC)


if __name__ == "__main__":
    unittest.main()
