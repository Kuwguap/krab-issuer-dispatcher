r"""A button that brings the password prompt back to the bottom of the chat.

Asked for: "for instant tags for the client adder, messages can come in and
disrupt, so add an inline button so when user is ready to type password they can
scroll anytime and click inline button and message sends to bottom of chat ready
for password".

The summary that arms a release is buried within minutes by whatever else the
chat is doing, and the 15-minute window closes while it is out of sight.

Run:  venv\Scripts\python.exe -m pytest tests/test_password_arm_button.py -q
"""
import os
import sys
import time
import unittest
import uuid
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
D1 = "cccccccc-3333-4333-8333-cccccccccccc"
D2 = "dddddddd-4444-4444-8444-dddddddddddd"
SUP = 900500
STRANGER = 900502


def _lead(**over):
    lead = {"id": LEAD_ID, "reference_id": "LAB4CDVZ", "group_id": GROUP_ID,
            "instant_tag": True, "user_id": SUP}
    lead.update(over)
    return lead


def _sup(*ids):
    allowed = {str(i) for i in ids}
    return mock.patch.object(bot, "_user_is_global_supervisor",
                             lambda uid: str(uid) in allowed)


class TheButtonIsOnTheSummaryTest(unittest.IsolatedAsyncioTestCase):

    async def _dispatch(self, drivers=2, allowed=True):
        ctx = mock.MagicMock()
        ctx.bot.send_message = mock.AsyncMock()
        rows = [{"id": D1, "driver_name": "Susan", "driver_telegram_id": "700001"},
                {"id": D2, "driver_name": "Marcus", "driver_telegram_id": "700002"}][:drivers]
        with mock.patch.object(bot, "db", mock.MagicMock()), \
                mock.patch.object(bot, "_skip_dispatch_allowed",
                                  lambda lead, uid: (allowed, "ok")), \
                mock.patch.object(bot, "_driver_amount_cents", lambda l: 20000):
            await bot._dispatch_instant_tag_lead(
                ctx, _lead(price="$250"), rows,
                notify_chat_id=SUP, user_data={}, by_user_id=SUP)
        # The issuer summary is the one addressed to the issuer's chat.
        for c in ctx.bot.send_message.call_args_list:
            if c.kwargs.get("chat_id") == SUP:
                return c.kwargs
        return {}

    async def test_the_summary_has_a_release_button(self):
        kw = await self._dispatch()
        kb = kw.get("reply_markup")
        self.assertIsNotNone(kb, "no button to come back to")
        labels = [b.text for row in kb.inline_keyboard for b in row]
        self.assertEqual(["🔑 Release with password"], labels)

    async def test_the_button_names_this_lead(self):
        kw = await self._dispatch()
        data = kw["reply_markup"].inline_keyboard[0][0].callback_data
        self.assertTrue(data.startswith(bot.SKIP_DISPATCH_ARM_CB))
        self.assertEqual(LEAD_ID, bot._long_uuid(data[len(bot.SKIP_DISPATCH_ARM_CB):]))

    async def test_it_fits_in_a_callback(self):
        kw = await self._dispatch()
        data = kw["reply_markup"].inline_keyboard[0][0].callback_data
        self.assertLessEqual(len(data.encode("utf-8")), 64)

    async def test_no_button_for_somebody_who_may_not_release(self):
        kw = await self._dispatch(allowed=False)
        self.assertIsNone(kw.get("reply_markup"))
        self.assertNotIn("password", str(kw.get("text") or "").lower())

    async def test_the_text_still_says_you_can_just_reply(self):
        kw = await self._dispatch()
        self.assertIn("reply here with the password", kw["text"])


class TappingItReopensThePromptTest(unittest.IsolatedAsyncioTestCase):

    async def _tap(self, *, lead=None, presser=SUP, offered=(D1, D2), sups=(SUP,)):
        q = mock.MagicMock()
        q.data = bot.SKIP_DISPATCH_ARM_CB + bot._short_uuid(LEAD_ID)
        q.answer = mock.AsyncMock()
        q.from_user = mock.MagicMock(id=presser, username="kingkrab")
        q.message.chat_id = presser
        q.message.reply_text = mock.AsyncMock()
        upd = mock.MagicMock(callback_query=q)
        ctx = mock.MagicMock()
        ctx.user_data = {}
        ctx.bot.send_message = mock.AsyncMock()
        db = mock.MagicMock()
        db.get_lead_by_id.return_value = lead if lead is not None else _lead()
        db.get_lead_offered_driver_ids.return_value = list(offered)
        with _sup(*sups), \
                mock.patch.object(bot, "db", db), \
                mock.patch.object(bot, "_safe_answer_callback_query", mock.AsyncMock()):
            await bot.handle_password_arm_request(upd, ctx)
        sent = ctx.bot.send_message.call_args
        refused = " ".join(str(c.args[0]) for c in q.message.reply_text.call_args_list
                           if c.args)
        return ctx.user_data.get(bot.SKIP_DISPATCH_PENDING_KEY), sent, refused

    async def test_it_arms_the_release_again(self):
        armed, sent, _ = await self._tap()
        self.assertIsNotNone(armed)
        self.assertEqual(LEAD_ID, armed["lead_id"])
        self.assertEqual([D1, D2], armed["driver_ids"])
        self.assertEqual(SUP, armed["by"])
        self.assertLess(time.time() - armed["at"], 5, "the window was not refreshed")

    async def test_the_prompt_is_a_new_message_at_the_bottom(self):
        _armed, sent, _ = await self._tap()
        self.assertIsNotNone(sent, "nothing was posted to the bottom of the chat")
        self.assertEqual(SUP, sent.kwargs["chat_id"])
        self.assertIn("LAB4CDVZ", sent.kwargs["text"])
        self.assertIn("Password", sent.kwargs["text"])

    async def test_the_keyboard_opens_ready_to_type(self):
        _armed, sent, _ = await self._tap()
        self.assertIsInstance(sent.kwargs.get("reply_markup"), bot.ForceReply)

    async def test_one_driver_is_armed_directly(self):
        armed, _sent, _ = await self._tap(offered=(D1,))
        self.assertEqual(D1, armed["driver_id"])

    async def test_several_drivers_wait_for_a_pick(self):
        armed, sent, _ = await self._tap(offered=(D1, D2))
        self.assertEqual("", armed["driver_id"], "it guessed a driver")
        self.assertIn("pick which driver", sent.kwargs["text"])

    async def test_a_stranger_gets_nothing(self):
        armed, sent, refused = await self._tap(presser=STRANGER)
        self.assertIsNone(armed)
        self.assertIsNone(sent)
        self.assertIn("supervisor", refused.lower())

    async def test_a_settled_tag_says_so(self):
        for col in ("instant_pdf_paid_at", "instant_pdf_delivered_at"):
            armed, sent, refused = await self._tap(
                lead=_lead(**{col: "2026-08-30T10:00:00Z"}))
            self.assertIsNone(armed, col)
            self.assertIn("settled", refused.lower(), col)

    async def test_a_tag_offered_to_nobody_is_a_dead_end_it_explains(self):
        armed, sent, refused = await self._tap(offered=())
        self.assertIsNone(armed)
        self.assertIn("Skip Dispatch", refused)

    async def test_a_missing_lead_does_not_crash(self):
        armed, sent, refused = await self._tap(lead=False)
        self.assertIsNone(armed)
        self.assertIn("gone", refused)


class ItSurvivesARedeployTest(unittest.TestCase):
    """The armed state is RAM-only. The whole point of this button is being
    tappable later, so it must be reachable when the conversation is gone."""

    def test_it_is_an_entry_point_and_a_fallback(self):
        self.assertEqual(
            2, SRC.count("CallbackQueryHandler(handle_password_arm_request,"))

    def test_the_drivers_come_from_the_database_not_from_ram(self):
        body = SRC.split("async def handle_password_arm_request", 1)[1]
        body = body.split("\nasync def ", 1)[0]
        self.assertIn("db.get_lead_offered_driver_ids", body)
        self.assertNotIn("SKIP_DISPATCH_PENDING_KEY)", body.split("context.user_data[")[0])

    def test_a_lead_id_that_is_not_a_uuid_is_survived(self):
        body = SRC.split("async def handle_password_arm_request", 1)[1]
        body = body.split("\nasync def ", 1)[0]
        self.assertIn("except Exception:", body)


class TheDatabaseHelperTest(unittest.TestCase):

    @staticmethod
    def _real_database_class():
        """A private, unclobbered copy of the module.

        Several dispatch suites do `udb.Database = MagicMock()` and never put it
        back, so importing it normally here yields a mock whose every method
        answers with another mock — and these tests would pass on nothing.
        """
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_udb_private_for_arm_tests", ROOT / "utils" / "database.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.Database

    def _rows(self, rows):
        db = mock.MagicMock()
        db._check_tables_exist.return_value = True
        db.client.table.return_value.select.return_value.eq.return_value \
            .limit.return_value.execute.return_value = mock.MagicMock(data=rows)
        return self._real_database_class().get_lead_offered_driver_ids(db, LEAD_ID)

    def test_the_accepted_driver_wins(self):
        self.assertEqual([D2], self._rows([
            {"driver_id": D1, "status": "declined"},
            {"driver_id": D2, "status": "accepted"}]))

    def test_otherwise_everyone_offered(self):
        self.assertEqual([D1, D2], self._rows([
            {"driver_id": D1, "status": "pending"},
            {"driver_id": D2, "status": "pending"}]))

    def test_duplicates_collapse(self):
        self.assertEqual([D1], self._rows([
            {"driver_id": D1, "status": "pending"},
            {"driver_id": D1, "status": "pending"}]))

    def test_nothing_offered_is_an_empty_list(self):
        self.assertEqual([], self._rows([]))


if __name__ == "__main__":
    unittest.main()
