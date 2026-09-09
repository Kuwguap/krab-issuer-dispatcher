r"""An application reaches a supervisor, and carries what a hire needs.

Asked for, after the diagnosis: applications never reach a human and are mostly
unhireable. Measured against the live database before any of this was written:

* 81 applications over three months, 57 still pending, and NOBODY was ever told
  one arrived — the bot posts a card into the sender's own chat and returns. The
  one notifier in the repo is called from the web route only, and even there it
  sits in the else-branch of auto-hire, which is on by default.
* 43 of 81 rows have no telegram_id, and a hire refuses without one. The bot
  asks the applicant for it — a number Telegram never shows them — while
  holding it in update.effective_user.id.

The trap this file exists to hold shut: telegram_id must NEVER default to the
sender. One chat filed 13 applications for eight different people, and
is_supervisor_created cannot tell that apart from somebody applying for
themselves — it is False for both. A wrong id is worse than a missing one: it
mints a driver record pointing at the wrong person's Telegram account.

Run:  python -m pytest tests/test_applications_reach_a_human.py -q
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


class ATelegramIdIsNeverGuessedTest(unittest.TestCase):

    def test_a_pasted_blob_is_not_an_id(self):
        """Written verbatim before, and int(tid) then raises at delivery time —
        the driver record exists with no way to reach them."""
        self.assertEqual("8713796087", B._clean_telegram_id("Chat Id: 8713796087"))
        self.assertEqual("8713796087", B._clean_telegram_id("  8713796087 "))
        self.assertEqual("", B._clean_telegram_id("I don't know it"))
        self.assertEqual("", B._clean_telegram_id(""))
        self.assertEqual("", B._clean_telegram_id(None))

    def test_a_legacy_nine_digit_id_is_still_an_id(self):
        """The live table holds 945529353. A tighter window would reject people
        who are already drivers."""
        self.assertEqual("945529353", B._clean_telegram_id("945529353"))

    def test_it_resolves_the_applicants_username_not_the_senders_id(self):
        db = mock.MagicMock()
        with mock.patch.object(B, "db", db), \
             mock.patch("utils.telegram_resolve.resolve_telegram_id_for_username",
                        return_value=("5865279405", "directory", "")):
            tid, how = B._resolve_applicant_telegram_id(
                {"telegram_username": "@kingbacano"})
        self.assertEqual("5865279405", tid)
        self.assertEqual("directory", how)

    def test_a_given_id_wins_and_is_still_cleaned(self):
        with mock.patch.object(B, "db", mock.MagicMock()):
            tid, how = B._resolve_applicant_telegram_id(
                {"telegram_id": "ID: 8887186065", "telegram_username": "@someone"})
        self.assertEqual("8887186065", tid)
        self.assertEqual("given", how)

    def test_nothing_to_resolve_means_nothing_is_invented(self):
        db = mock.MagicMock()
        with mock.patch.object(B, "db", db), \
             mock.patch("utils.telegram_resolve.resolve_telegram_id_for_username",
                        return_value=(None, "", "not found")):
            tid, how = B._resolve_applicant_telegram_id({"telegram_username": "@nobody"})
        self.assertEqual("", tid)

    def test_the_senders_id_is_never_used_as_a_fallback(self):
        """The whole trap. A recruiter's thirteen rows would all point at the
        recruiter."""
        import inspect
        src = inspect.getsource(B._resolve_applicant_telegram_id)
        self.assertNotIn("user.id", src)
        self.assertNotIn("effective_user", src)


class TheCardSaysWhatIsMissingTest(unittest.TestCase):

    def test_it_names_the_fields_a_hire_would_refuse_for(self):
        self.assertEqual(["full name", "Telegram ID", "email"],
                         B._hire_blockers({}))
        self.assertEqual(["Telegram ID"], B._hire_blockers(
            {"full_name": "Ada", "email": "a@b.com"}))
        self.assertEqual([], B._hire_blockers(
            {"full_name": "Ada", "email": "a@b.com", "telegram_id": "8887186065"}))

    def test_a_garbage_id_still_counts_as_missing(self):
        self.assertIn("Telegram ID", B._hire_blockers(
            {"full_name": "Ada", "email": "a@b.com", "telegram_id": "dunno"}))

    def test_the_card_prints_it(self):
        text = B._format_interview_understanding(
            {"id": "i-1", "full_name": "Ada", "status": "pending"})
        self.assertIn("Cannot hire yet", text)
        self.assertIn("Telegram ID", text)

    def test_a_hired_card_does_not_nag(self):
        text = B._format_interview_understanding(
            {"id": "i-1", "full_name": "Ada", "status": "hired"})
        self.assertNotIn("Cannot hire yet", text)

    def test_the_button_appears_only_while_the_id_is_missing(self):
        kb = B._review_keyboard("11111111-2222-3333-4444-555555555555",
                                {"telegram_id": ""})
        data = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertTrue(any(d.startswith("int_mine_") for d in data), data)

        kb = B._review_keyboard("11111111-2222-3333-4444-555555555555",
                                {"telegram_id": "8887186065"})
        data = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertFalse(any(d.startswith("int_mine_") for d in data), data)

    def test_a_card_with_no_interview_still_builds(self):
        """Four call sites; none may crash for want of the new argument."""
        kb = B._review_keyboard("11111111-2222-3333-4444-555555555555")
        self.assertTrue(kb.inline_keyboard)

    def test_the_new_callback_is_routed(self):
        sid = B._short_uuid("11111111-2222-3333-4444-555555555555")
        self.assertEqual("11111111-2222-3333-4444-555555555555",
                         B._interview_id_from_callback("int_mine_" + sid))


class SomebodyIsToldAnApplicationArrivedTest(unittest.TestCase):

    def _notify(self, interview, chat_ids=(111, 222), fail=()):
        ctx = mock.MagicMock()
        sent = []

        async def send(chat_id, text, **kw):
            if chat_id in fail:
                raise RuntimeError("blocked")
            sent.append((chat_id, text))

        ctx.bot.send_message = mock.AsyncMock(side_effect=send)
        with mock.patch.object(B, "_global_supervisory_chat_ids",
                               return_value=list(chat_ids)):
            told = run(B._notify_supervisors_new_interview(ctx, interview, "@elle"))
        return told, sent

    def test_every_supervisor_is_told_once(self):
        told, sent = self._notify({"id": "i-1", "full_name": "Ada"},
                                  chat_ids=(111, 222, 111))
        self.assertEqual(2, told)
        self.assertEqual([111, 222], [c for c, _ in sent])

    def test_the_notice_carries_what_a_supervisor_needs_to_act(self):
        _, sent = self._notify({
            "id": "abc-123", "full_name": "Ada Lovelace",
            "telegram_username": "@ada", "phone_number": "732-555-0000"})
        text = sent[0][1]
        for needle in ("Ada Lovelace", "@ada", "732-555-0000", "@elle",
                       "/open abc-123"):
            self.assertIn(needle, text, needle)

    def test_it_says_whether_a_hire_would_refuse(self):
        _, sent = self._notify({"id": "i-1", "full_name": "Ada"})
        self.assertIn("Cannot hire yet", sent[0][1])
        self.assertIn("Telegram ID", sent[0][1])
        _, sent = self._notify({"id": "i-1", "full_name": "Ada",
                                "email": "a@b.com", "telegram_id": "8887186065"})
        self.assertIn("Ready to hire", sent[0][1])

    def test_one_unreachable_supervisor_does_not_cost_the_others(self):
        told, sent = self._notify({"id": "i-1", "full_name": "Ada"}, fail=(111,))
        self.assertEqual(1, told)
        self.assertEqual([222], [c for c, _ in sent])

    def test_no_supervisor_configured_is_logged_not_swallowed(self):
        with self.assertLogs("bot", level="ERROR") as log:
            told, _ = self._notify({"id": "i-1"}, chat_ids=())
        self.assertEqual(0, told)
        self.assertIn("nobody was told", " ".join(log.output))

    def test_a_row_with_no_id_is_not_announced(self):
        told, sent = self._notify({"full_name": "Ada"})
        self.assertEqual(0, told)
        self.assertEqual([], sent)

    def test_it_sends_through_the_application_not_blocking_http(self):
        """api/notify.py posts with requests, fifteen seconds per recipient,
        inside the loop every other applicant is waiting in."""
        import inspect
        src = inspect.getsource(B._notify_supervisors_new_interview)
        self.assertIn("await context.bot.send_message", src)
        self.assertNotIn("requests.post(", src)   # the CALL, not the note saying why


class TheCreatePathWiresItAllTogetherTest(unittest.TestCase):

    def test_the_id_is_resolved_before_the_insert_not_passed_as_a_keyword(self):
        """create_interview spreads the parsed fields LAST, so a keyword would
        be silently overwritten by whatever the model returned."""
        import inspect
        src = inspect.getsource(B._process_interview_input)
        self.assertIn("_resolve_applicant_telegram_id(fields)", src)
        self.assertIn("fields = dict(fields, telegram_id=resolved)", src)
        i = src.index("_resolve_applicant_telegram_id(fields)")
        j = src.index("db.create_interview(")
        self.assertLess(i, j, "resolution must happen before the insert")

    def test_an_unusable_id_is_dropped_rather_than_stored(self):
        import inspect
        src = inspect.getsource(B._process_interview_input)
        self.assertIn('fields = dict(fields, telegram_id="")', src)

    def test_the_notice_goes_out_after_the_card_is_recorded(self):
        """It points at a card, so the card has to exist first."""
        import inspect
        src = inspect.getsource(B._process_interview_input)
        self.assertIn("_notify_supervisors_new_interview(", src)
        self.assertLess(src.index("understanding_message_id\": card.message_id"),
                        src.index("_notify_supervisors_new_interview("))

    def test_the_questionnaire_no_longer_demands_an_unknowable_number(self):
        self.assertIn("optional", B.INTERVIEW_QUESTIONNAIRE_PROMPT.split(
            "13. \U0001f4ac Telegram ID")[1][:80])


if __name__ == "__main__":
    unittest.main()
