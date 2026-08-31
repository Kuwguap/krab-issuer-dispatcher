r"""Email the temp tag to the client — a toggle beside Instant Tag and Insurance.

Asked for: "add toggle just like instant and insurance to email client the temp
tag; if no email detected ask for email if on".

Nothing in this system emailed a tag before, which is also why "Tag emailed" was
the one stop on the /receipts ladder with no automatic trigger. It has one now.

Run:  venv\Scripts\python.exe -m pytest tests/test_tag_email_toggle.py -q
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
from utils import resend_client as rc  # noqa: E402

SRC = (ROOT / "bot.py").read_text(encoding="utf-8")
LEAD_ID = "11111111-2222-3333-4444-555555555555"


def _labels(state):
    kb = bot._build_review_keyboard_with_selections(state)
    return [b.text for row in kb.inline_keyboard for b in row]


def _callbacks(state):
    kb = bot._build_review_keyboard_with_selections(state)
    return [b.callback_data for row in kb.inline_keyboard for b in row]


class TheToggleSitsWithTheOthersTest(unittest.TestCase):

    def test_it_is_on_the_card(self):
        self.assertIn("📧 Email tag to client", _labels({}))
        self.assertIn("ph1_tagmail_toggle", _callbacks({}))

    def test_it_reads_on_when_it_is_on(self):
        labels = _labels({"wants_tag_email": True})
        self.assertIn("📧 Tag email: ON", labels)
        self.assertNotIn("📧 Email tag to client", labels)

    def test_it_stands_beside_the_other_two_switches(self):
        state = {"wants_insurance": True, "instant_tag": True, "wants_tag_email": True}
        labels = " | ".join(_labels(state))
        for needle in ("🛡 Insurance: ON", "🤖 Instant Tag", "📧 Tag email: ON"):
            self.assertIn(needle, labels, needle)


class TurningItOnWithNoEmailAsksForOneTest(unittest.IsolatedAsyncioTestCase):

    async def _toggle(self, state):
        q = mock.MagicMock()
        q.data = "ph1_tagmail_toggle"
        q.answer = mock.AsyncMock()
        q.message.chat_id = 999
        upd = mock.MagicMock()
        upd.callback_query = q
        upd.effective_user = mock.MagicMock(id=7, username="tester")
        upd.effective_chat = mock.MagicMock(id=999, type="private")
        said = []
        # The handler reads the card straight out of the stored user state.
        db = mock.MagicMock()
        db.get_user_state.return_value = {"state": "phase1", "data": state}
        with mock.patch.object(bot, "db", db), \
                mock.patch.object(bot, "_update_review_message_text", mock.AsyncMock()), \
                mock.patch.object(bot, "_send_vanishing",
                                  mock.AsyncMock(side_effect=lambda c, ch, t, **k: said.append(t))):
            await bot.handle_phase1_ai_review_callback(upd, mock.MagicMock())
        return state, said

    async def test_no_email_on_file_prompts_for_one(self):
        # A real card always carries something; an empty dict is "data lost".
        state, said = await self._toggle({"name": "Magnolia Diaz"})
        self.assertTrue(state.get("wants_tag_email"))
        self.assertTrue(said, "the issuer was not asked for an address")
        self.assertIn("email", said[0].lower())

    async def test_an_email_already_on_file_asks_nothing(self):
        state, said = await self._toggle({"email": "a@b.com"})
        self.assertTrue(state.get("wants_tag_email"))
        self.assertEqual([], said)

    async def test_turning_it_off_never_prompts(self):
        state, said = await self._toggle({"wants_tag_email": True})
        self.assertFalse(state.get("wants_tag_email"))
        self.assertEqual([], said)


class TheTagActuallyGetsEmailedTest(unittest.IsolatedAsyncioTestCase):

    def _lead(self, **over):
        lead = {"id": LEAD_ID, "reference_id": "REF1", "email": "client@example.com",
                "wants_tag_email": True,
                "vehicle_details": "Magnolia Diaz\n-\n-\n-\n-\n-\n-\n-\n-\n-\n-"}
        lead.update(over)
        return lead

    async def _send(self, lead, vehicle=1, ok=True, db=None,
                    approvers=("boss@example.com",)):
        db = db or mock.MagicMock()
        sent = {}
        asked = []

        def _fake(**kw):
            sent.update(kw)
            asked.append(kw.get("to_address"))
            return mock.MagicMock(ok=ok, error=None if ok else "mailbox full")

        with mock.patch.object(bot, "db", db), \
                mock.patch.object(bot, "tag_approval_emails", lambda: list(approvers)), \
                mock.patch.object(rc, "send_insurance_card_email", _fake):
            await bot._maybe_email_tag_to_client(lead, vehicle, b"%PDF-1.4", "tag.pdf")
        self._asked = asked
        return sent, db

    async def test_the_supervisors_are_asked_not_the_client(self):
        """The toggle used to mail the client the moment the tag existed."""
        sent, db = await self._send(self._lead())
        self.assertEqual("boss@example.com", sent["to_address"])
        self.assertNotEqual("client@example.com", sent["to_address"])
        self.assertEqual(b"%PDF-1.4", sent["pdf_bytes"], "the tag must be attached")

    async def test_the_ask_names_the_client_and_the_address(self):
        sent, _ = await self._send(self._lead())
        self.assertIn("Magnolia Diaz", sent["subject"])
        self.assertIn("client@example.com", sent["subject"])
        self.assertIn("Magnolia Diaz", sent["body"])
        self.assertIn("client@example.com", sent["body"])

    async def test_the_ask_carries_a_button_to_send(self):
        sent, _ = await self._send(self._lead())
        self.assertIn("html", sent)
        self.assertIn("Send the tag to the client", sent["html"])
        self.assertIn("/tagsend/", sent["html"])
        self.assertIn("/tagsend/", sent["body"], "a plain-text client needs the link too")

    async def test_it_says_the_client_has_NOT_had_it(self):
        sent, _ = await self._send(self._lead())
        self.assertIn("not been sent to the client", sent["body"].lower())

    async def test_the_board_does_not_move_yet(self):
        """Nothing has reached the client — only a question has been asked."""
        _, db = await self._send(self._lead())
        db.advance_delivery_status.assert_not_called()

    async def test_every_supervisor_is_asked(self):
        await self._send(self._lead(), approvers=("a@x.com", "b@x.com", "c@x.com"))
        self.assertEqual(["a@x.com", "b@x.com", "c@x.com"], self._asked)

    async def test_nobody_configured_means_nothing_is_sent(self):
        sent, _ = await self._send(self._lead(), approvers=())
        self.assertEqual({}, sent)

    async def test_asking_twice_about_one_car_does_not_happen(self):
        lead = self._lead(tag_email_asked_at="2026-08-29T10:00:00-04:00")
        sent, db = await self._send(lead)
        self.assertEqual({}, sent, "the supervisors were asked twice")

    async def test_a_second_send_does_not_mail_a_second_copy(self):
        lead = self._lead(tag_emailed_at="2026-08-29T10:00:00-04:00")
        sent, db = await self._send(lead)
        self.assertEqual({}, sent, "the client was emailed twice")
        db.advance_delivery_status.assert_not_called()

    async def test_the_switch_off_means_no_email(self):
        sent, _ = await self._send(self._lead(wants_tag_email=False))
        self.assertEqual({}, sent)

    async def test_no_address_means_no_email_and_no_crash(self):
        sent, _ = await self._send(self._lead(email=""))
        self.assertEqual({}, sent)

    async def test_an_ask_nobody_received_is_not_stamped(self):
        """Otherwise the supervisors are never asked again."""
        _, db = await self._send(self._lead(), ok=False)
        wrote = [c.args[1] for c in db.update_lead.call_args_list if len(c.args) > 1]
        self.assertFalse(any("tag_email_asked_at" in w for w in wrote))
        self.assertFalse(any("tag_emailed_at" in w for w in wrote))

    async def test_a_successful_ask_is_stamped_once(self):
        _, db = await self._send(self._lead())
        wrote = [c.args[1] for c in db.update_lead.call_args_list if len(c.args) > 1]
        self.assertTrue(any("tag_email_asked_at" in w for w in wrote))
        self.assertFalse(any("tag_emailed_at" in w for w in wrote),
                         "the client has not been sent anything yet")

    async def test_a_broken_mailer_never_takes_the_tag_down(self):
        with mock.patch.object(bot, "db", mock.MagicMock()), \
                mock.patch.object(rc, "send_insurance_card_email",
                                  mock.MagicMock(side_effect=RuntimeError("boom"))):
            await bot._maybe_email_tag_to_client(self._lead(), 1, b"%PDF", "t.pdf")


class ItIsWiredIntoTheTagSendTest(unittest.TestCase):

    def test_the_sender_runs_only_for_a_tag_that_went_out(self):
        body = SRC.split("async def _build_and_send_tag_pdf", 1)[1]
        body = body.split("\nasync def ", 1)[0]
        self.assertIn("if sent:\n        await _maybe_email_tag_to_client(", body)

    def test_the_switch_is_persisted_like_the_others(self):
        body = SRC.split("async def _on_lead_created", 1)[1]
        body = body.split("\nasync def ", 1)[0]
        self.assertIn('"wants_tag_email": True', body)

    def test_the_columns_have_a_migration(self):
        sql = (ROOT / "database" / "migration_tag_email.sql").read_text(encoding="utf-8")
        for col in ("wants_tag_email", "tag_emailed_at", "tag_email_error"):
            self.assertIn(col, sql, col)

    def test_the_columns_are_optional_writes(self):
        from utils.database import _OPTIONAL_LEADS_WRITE_KEYS as keys
        for col in ("wants_tag_email", "tag_emailed_at", "tag_email_error"):
            self.assertIn(col, keys, col)


if __name__ == "__main__":
    unittest.main()
