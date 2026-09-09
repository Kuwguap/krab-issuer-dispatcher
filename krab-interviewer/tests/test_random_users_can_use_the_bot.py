r"""Why random users could not use @krabinterviewerbot, and what stops it recurring.

Asked for: "for @krab-interviewer random users still cant use the bot".

Three separate things put a non-supervisor into a dead conversation:

* Being on the shared `drivers` roster was treated as having been hired HERE.
  Verified against the live database: 82 drivers, 62 of them with no interview
  row in this bot at all. Every one of those people tapped Start, was handed
  the full new-hire onboarding for a job they were never hired for, and was
  then dropped out of the conversation.
* The apply page's own deep link (?start=web_apply) answered once and ended
  the conversation, so an applicant who followed the site's funnel got one
  message and a bot that ignored every word after it.
* Nothing caught a message no handler claimed, so both dead ends were silent
  from both sides.

Run:  python -m pytest tests/test_random_users_can_use_the_bot.py -q
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


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class BeingOnTheRosterIsNotBeingHiredTest(unittest.TestCase):
    """The bug that made this "still" broken — it arrived with the 09-05 commit."""

    def test_a_driver_with_no_interview_here_is_not_hired_here(self):
        db = mock.MagicMock()
        db.get_hired_interview_for_telegram_id.return_value = None
        db.get_latest_interview_for_telegram_id.return_value = None
        db.get_driver_by_telegram_id.return_value = {
            "driver_name": "Kita", "phone_number": "732-555-0000"}
        with mock.patch.object(B, "db", db):
            self.assertIsNone(B._resolve_hired_driver_interview("111222333"))

    def test_it_never_invents_a_hired_record(self):
        import inspect
        src = inspect.getsource(B._resolve_hired_driver_interview)
        self.assertNotIn('"status": "hired"', src)
        self.assertNotIn("get_driver_by_telegram_id", src)

    def test_somebody_actually_hired_here_still_gets_their_onboarding(self):
        db = mock.MagicMock()
        row = {"id": "i-1", "status": "hired", "full_name": "Real Hire"}
        db.get_hired_interview_for_telegram_id.return_value = row
        with mock.patch.object(B, "db", db):
            self.assertEqual(row, B._resolve_hired_driver_interview("444"))

    def test_a_hired_row_found_the_long_way_round_still_counts(self):
        db = mock.MagicMock()
        db.get_hired_interview_for_telegram_id.return_value = None
        db.get_latest_interview_for_telegram_id.return_value = {
            "id": "i-2", "status": "hired"}
        with mock.patch.object(B, "db", db):
            self.assertEqual("i-2", B._resolve_hired_driver_interview("555")["id"])

    def test_a_pending_application_is_not_a_hire(self):
        db = mock.MagicMock()
        db.get_hired_interview_for_telegram_id.return_value = None
        db.get_latest_interview_for_telegram_id.return_value = {
            "id": "i-3", "status": "pending"}
        with mock.patch.object(B, "db", db):
            self.assertIsNone(B._resolve_hired_driver_interview("666"))


class TheWebLinkDoesNotEndTheConversationTest(unittest.TestCase):
    """tristatetags.com/interview/apply hands out ?start=web_apply."""

    def _start(self, args, hired=None):
        msg = mock.MagicMock()
        msg.reply_text = mock.AsyncMock()
        update = mock.MagicMock()
        update.effective_message = msg
        update.effective_user = types.SimpleNamespace(id=777, username="applicant")
        ctx = mock.MagicMock()
        ctx.args = args
        ctx.user_data = {}
        db = mock.MagicMock()
        with mock.patch.object(B, "db", db), \
             mock.patch.object(B, "_user_is_global_supervisor", return_value=False), \
             mock.patch.object(B, "_resolve_hired_driver_interview", return_value=hired), \
             mock.patch.object(B, "_deliver_hired_driver_onboarding",
                               new=mock.AsyncMock()) as deliver, \
             mock.patch.object(B, "_begin_questionnaire",
                               new=mock.AsyncMock(return_value=B.STATE_INTERVIEW_INPUT)) as begin:
            state = run(B.cmd_start(update, ctx))
        return state, msg, begin, deliver

    def test_the_web_link_leads_into_the_questionnaire(self):
        state, msg, begin, _ = self._start(["web_apply"])
        self.assertEqual(B.STATE_INTERVIEW_INPUT, state)
        begin.assert_awaited_once()
        self.assertTrue(msg.reply_text.await_count >= 1)

    def test_it_no_longer_stops_dead(self):
        state, _, _, _ = self._start(["web_apply"])
        self.assertNotEqual(B.ConversationHandler.END, state)

    def test_a_plain_start_still_opens_the_questionnaire(self):
        state, _, begin, _ = self._start([])
        self.assertEqual(B.STATE_INTERVIEW_INPUT, state)
        begin.assert_awaited_once()

    def test_somebody_already_hired_gets_onboarding_and_is_not_asked_to_apply(self):
        state, _, begin, deliver = self._start(["web_apply"], hired={"id": "i-1"})
        self.assertEqual(B.ConversationHandler.END, state)
        deliver.assert_awaited_once()
        begin.assert_not_awaited()


class NothingIsSilentlyDroppedTest(unittest.TestCase):
    """The amplifier: a message no handler claimed used to vanish."""

    def _hint(self, claimed_by_somebody):
        msg = mock.MagicMock()
        msg.reply_text = mock.AsyncMock()
        update = mock.MagicMock()
        update.effective_message = msg
        update.effective_user = types.SimpleNamespace(id=777)
        handler = mock.MagicMock()
        handler.check_update.return_value = claimed_by_somebody
        with mock.patch.object(B, "_REAL_HANDLERS", [handler]):
            try:
                run(B.hint_no_handler(update, mock.MagicMock()))
                stopped = False
            except B.ApplicationHandlerStop:
                stopped = True
        return msg, stopped

    def test_a_message_nobody_wants_gets_an_answer(self):
        msg, stopped = self._hint(None)
        msg.reply_text.assert_awaited_once()
        self.assertIn("/start", msg.reply_text.await_args[0][0])
        self.assertTrue(stopped)

    def test_a_message_the_conversation_wants_is_left_alone(self):
        """PTB hands an update to EVERY group. A net that answered anyway would
        reply "I didn't catch that" to every answer somebody gave."""
        msg, stopped = self._hint(object())
        msg.reply_text.assert_not_awaited()
        self.assertFalse(stopped)

    def test_the_net_runs_before_the_real_handlers(self):
        """Asking "would anybody take this?" only has a true answer before they
        have had it — afterwards the conversation may already have moved on."""
        import inspect
        src = inspect.getsource(B.main)
        self.assertIn("group=-1", src)
        self.assertIn("_REAL_HANDLERS[:] = list(application.handlers.get(0, []))", src)
        self.assertLess(src.index("_REAL_HANDLERS[:]"), src.index("group=-1"))

    def test_an_error_answers_the_person_rather_than_nothing(self):
        msg = mock.MagicMock()
        msg.reply_text = mock.AsyncMock()
        update = types.SimpleNamespace(effective_message=msg)
        ctx = mock.MagicMock()
        ctx.error = RuntimeError("boom")
        run(B.on_error(update, ctx))
        msg.reply_text.assert_awaited_once()

    def test_an_error_with_nobody_to_answer_does_not_raise(self):
        ctx = mock.MagicMock()
        ctx.error = RuntimeError("boom")
        run(B.on_error(object(), ctx))          # must not raise


class DriversCanHireTest(unittest.TestCase):
    """"drivers cant hire allow this".

    They always could press the button -- _user_can_hire returns True for anyone
    unless KRAB_HIRE_REQUIRES_TEAM is set, and Hire is on every review card.
    What they could not do was REACH a card: /interviews, /applications and
    /open all returned silently for anyone who was not a supervisor. An open
    permission behind a locked door.
    """

    def _cmd(self, fn, supervisor=False, driver=False, rows=None, args=None):
        msg = mock.MagicMock()
        msg.reply_text = mock.AsyncMock()
        update = mock.MagicMock()
        update.effective_message = msg
        update.effective_user = types.SimpleNamespace(id=777)
        ctx = mock.MagicMock()
        ctx.args = args or []
        db = mock.MagicMock()
        db.list_interviews.return_value = rows if rows is not None else [
            {"id": "i-1", "first_name": "Ada", "status": "pending"}]
        db.list_interviews_by_status.return_value = db.list_interviews.return_value
        db.get_driver_by_telegram_id.return_value = {"id": "d-1"} if driver else None
        with mock.patch.object(B, "db", db), \
             mock.patch.object(B, "_user_is_global_supervisor", return_value=supervisor), \
             mock.patch.object(B, "_hiring_requires_team", return_value=False), \
             mock.patch.object(B, "_interview_list_keyboard", return_value=None):
            run(fn(update, ctx))
        return msg

    def _said(self, msg):
        return " ".join(str(c[0][0]) for c in msg.reply_text.await_args_list)

    def test_a_driver_can_open_the_applications_list(self):
        msg = self._cmd(B.cmd_interviews, driver=True)
        self.assertIn("Driver applications", self._said(msg))

    def test_a_driver_can_open_a_hired_one(self):
        msg = self._cmd(B.cmd_open, driver=True)
        self.assertIn("Hired drivers", self._said(msg))

    def test_a_supervisor_still_can(self):
        msg = self._cmd(B.cmd_interviews, supervisor=True)
        self.assertIn("Driver applications", self._said(msg))

    def test_somebody_who_is_neither_is_told_why(self):
        """Not silence. A refusal nobody can see is why "the bot ignores me"."""
        msg = self._cmd(B.cmd_interviews)
        said = self._said(msg)
        self.assertIn("supervisors and drivers", said.lower())
        self.assertNotIn("Driver applications", said)

    def test_the_gate_still_closes_both_the_door_and_the_button(self):
        """KRAB_HIRE_REQUIRES_TEAM=1 must not leave the list open to everyone."""
        db = mock.MagicMock()
        db.get_driver_by_telegram_id.return_value = {"id": "d-1"}
        with mock.patch.object(B, "db", db), \
             mock.patch.object(B, "_user_is_global_supervisor", return_value=False), \
             mock.patch.object(B, "_hiring_requires_team", return_value=True):
            self.assertFalse(B._user_can_open_applications(777))

    def test_deleting_a_driver_is_still_supervisors_only(self):
        """Hiring is reversible. Purging somebody from Issuer, Dispatch and the
        interviews at once is not."""
        msg = self._cmd(B.cmd_delete, driver=True)
        self.assertIn("Supervisors only", self._said(msg))

    def test_the_roster_is_still_supervisors_only(self):
        msg = self._cmd(B.cmd_drivers, driver=True)
        self.assertIn("Supervisors only", self._said(msg))

    def test_hiring_itself_was_never_the_thing_that_was_shut(self):
        with mock.patch.object(B, "_hiring_requires_team", return_value=False):
            self.assertTrue(run(B._user_can_hire(mock.MagicMock(), 777)))


class ASupervisorCommandDoesNotEatAnApplicationTest(unittest.TestCase):
    """/announce and friends are conversation ENTRY POINTS with allow_reentry.

    Returning END for a non-supervisor dropped whatever they were in the middle
    of -- a driver halfway through the questionnaire who typed a command /help
    advertises to them lost the lot, and was told nothing.
    """

    def _entry(self, fn):
        msg = mock.MagicMock()
        msg.reply_text = mock.AsyncMock()
        update = mock.MagicMock()
        update.effective_message = msg
        update.effective_user = types.SimpleNamespace(id=777)
        with mock.patch.object(B, "_user_is_global_supervisor", return_value=False), \
             mock.patch.object(B, "db", mock.MagicMock()):
            state = run(fn(update, mock.MagicMock()))
        return state, msg

    def test_announce_leaves_the_conversation_where_it_was(self):
        state, msg = self._entry(B.cmd_announce)
        self.assertIsNone(state)          # None = keep the current state
        self.assertNotEqual(B.ConversationHandler.END, state)
        msg.reply_text.assert_awaited_once()

    def test_announce_schedule_does_too(self):
        state, msg = self._entry(B.cmd_announce_schedule)
        self.assertIsNone(state)
        msg.reply_text.assert_awaited_once()

    def test_setemail_does_too(self):
        state, msg = self._entry(B.cmd_setemail)
        self.assertIsNone(state)
        msg.reply_text.assert_awaited_once()

    def test_the_command_list_says_who_each_one_is_for(self):
        msg = mock.MagicMock()
        msg.reply_text = mock.AsyncMock()
        update = mock.MagicMock()
        update.effective_message = msg
        run(B.cmd_help(update, mock.MagicMock()))
        text = msg.reply_text.await_args[0][0]
        self.assertIn("supervisors and drivers", text)
        self.assertIn("<i>(supervisors)</i>", text)


class TheWiringHoldsTest(unittest.TestCase):

    def test_the_net_and_the_error_handler_are_registered(self):
        import inspect
        src = inspect.getsource(B.main)
        self.assertIn("hint_no_handler", src)
        self.assertIn("add_error_handler(on_error)", src)
        self.assertIn("filters.ChatType.PRIVATE", src)


if __name__ == "__main__":
    unittest.main()
