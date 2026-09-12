r"""The new-application alert is something you can act on.

The alert told a supervisor an application had arrived and that it could not be
hired yet -- then left them to copy a uuid into /open. Everything you might do
about it lived somewhere else.

Now it carries buttons: fix the Telegram ID, upload the licence photo, set the
licence number, view the whole application, edit any field. It also says when
the driver's licence is missing, which nothing anywhere used to mention.

Three things here are load-bearing and easy to undo by accident:

  * A missing licence WARNS. It must never join _hire_blockers -- the office
    asked to be told, not to be stopped, and anyone hireable today must stay
    hireable.

  * The prompt-then-capture buttons are ENTRY POINTS of the conversation.
    Registered only top-level, PTB discards the state they return, so the
    prompt appears and nothing is ever waiting for the answer.

  * The button's id beats context.user_data. user_data is per-USER: a
    supervisor who has ever typed an application carries a stale
    active_interview_id, and reading it first put one applicant's licence on
    another's row.

Run:  py -3.13 -m pytest tests/test_the_alert_is_actionable.py -q
"""
import asyncio
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:test")
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy-key")

import bot as B  # noqa: E402

IID = "11111111-2222-3333-4444-555555555555"
SID = B._short_uuid(IID)
SRC = (ROOT / "bot.py").read_text(encoding="utf-8")


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def _data(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


class AMissingLicenceWarnsButNeverBlocksTest(unittest.TestCase):
    """The owner's decision, in executable form."""

    def test_it_reports_the_number_and_the_photo_separately(self):
        self.assertEqual(["licence number", "licence photo"], B._license_gaps({}))
        self.assertEqual(["licence number"], B._license_gaps(
            {"drivers_license_file_url": "https://x/y.jpg"}))
        self.assertEqual(["licence photo"], B._license_gaps(
            {"drivers_license_id": "D1234"}))
        self.assertEqual([], B._license_gaps(
            {"drivers_license_id": "D1234", "drivers_license_file_url": "https://x/y.jpg"}))

    def test_a_licence_never_reaches_the_hire_blockers(self):
        """The whole point. Someone hireable today must stay hireable."""
        ready = {"full_name": "Ada", "email": "a@b.com", "telegram_id": "8887186065"}
        self.assertEqual([], B._hire_blockers(ready))
        self.assertTrue(B._license_gaps(ready), "fixture should have no licence")

    def test_the_two_warnings_are_worded_apart(self):
        """A person scanning the card must not read a licence gap as a refusal,
        and neither must a test."""
        line = B._license_warning_line({})
        self.assertNotIn("Cannot hire", line)
        self.assertIn("warning only", line)

    def test_the_card_says_it(self):
        text = B._format_interview_understanding(
            {"id": IID, "full_name": "Ada", "status": "pending"})
        self.assertIn("licence number", text)
        self.assertIn("warning only", text)

    def test_a_hired_card_still_does_not_nag(self):
        text = B._format_interview_understanding(
            {"id": IID, "full_name": "Ada", "status": "hired"})
        self.assertNotIn("Cannot hire", text)
        self.assertNotIn("warning only", text)

    def test_the_alert_says_it_on_its_own_line(self):
        text = B._format_new_application_alert(
            {"id": IID, "full_name": "Ada"}, "@elle")
        self.assertIn("Cannot hire yet", text)
        self.assertIn("warning only", text)
        blockers = [ln for ln in text.splitlines() if "Cannot hire yet" in ln]
        self.assertEqual(1, len(blockers))
        self.assertNotIn("licence", blockers[0])


class TheAlertCarriesTheButtonsTest(unittest.TestCase):

    def test_it_offers_both_ways_to_supply_a_missing_id(self):
        d = _data(B._alert_keyboard(IID, {"telegram_id": ""}))
        self.assertIn("int_mine_" + SID, d)
        self.assertIn("int_ef_telegram_id_" + SID, d)

    def test_the_id_buttons_go_once_the_id_is_there(self):
        d = _data(B._alert_keyboard(IID, {"telegram_id": "8887186065"}))
        self.assertFalse([x for x in d if x.startswith("int_mine_")], d)
        self.assertNotIn("int_ef_telegram_id_" + SID, d)

    def test_it_always_offers_the_licence_view_and_edit(self):
        d = _data(B._alert_keyboard(IID, {"telegram_id": "8887186065"}))
        for expected in ("int_lic_", "int_ef_drivers_license_id_",
                         "int_open_", "int_edit_", "int_sched_", "int_hire_"):
            with self.subTest(prefix=expected):
                self.assertTrue([x for x in d if x.startswith(expected)], d)

    def test_the_labels_say_whether_something_is_already_there(self):
        kb = B._alert_keyboard(IID, {"drivers_license_file_url": "https://x/y.jpg",
                                     "drivers_license_id": "D1"})
        labels = [b.text for row in kb.inline_keyboard for b in row]
        self.assertTrue(any("Replace licence photo" in t for t in labels), labels)
        kb = B._alert_keyboard(IID, {})
        labels = [b.text for row in kb.inline_keyboard for b in row]
        self.assertTrue(any("Upload licence photo" in t for t in labels), labels)

    def test_every_button_resolves_back_to_this_application(self):
        """A prefix missing from _interview_id_from_callback is a button that
        looks fine and does nothing."""
        for d in _data(B._alert_keyboard(IID, {})):
            with self.subTest(callback=d):
                self.assertEqual(IID, B._interview_id_from_callback(d))

    def test_every_callback_fits_telegrams_64_bytes(self):
        """Past 64 Telegram refuses the WHOLE keyboard, so the alert would
        arrive with no buttons at all."""
        for d in _data(B._alert_keyboard(IID, {})):
            with self.subTest(callback=d):
                self.assertLessEqual(len(d.encode("utf-8")), 64)

    def test_a_keyboard_is_attached_to_the_alert_that_goes_out(self):
        ctx = mock.MagicMock()
        seen = []

        async def send(chat_id, text, **kw):
            seen.append(kw.get("reply_markup"))

        ctx.bot.send_message = mock.AsyncMock(side_effect=send)
        with mock.patch.object(B, "_global_supervisory_chat_ids", return_value=[111]):
            run(B._notify_supervisors_new_interview(ctx, {"id": IID, "full_name": "Ada"}, "@elle"))
        self.assertEqual(1, len(seen))
        self.assertIsNotNone(seen[0], "the alert went out with no buttons")
        self.assertIn("int_lic_" + SID, _data(seen[0]))

    def test_an_unusable_id_costs_the_buttons_not_the_alert(self):
        """Older fixtures and hand-made rows carry ids that are not uuids. The
        notice is the point; the buttons are a convenience."""
        ctx = mock.MagicMock()
        seen = []

        async def send(chat_id, text, **kw):
            seen.append((text, kw.get("reply_markup")))

        ctx.bot.send_message = mock.AsyncMock(side_effect=send)
        with mock.patch.object(B, "_global_supervisory_chat_ids", return_value=[111]):
            told = run(B._notify_supervisors_new_interview(
                ctx, {"id": "not-a-uuid", "full_name": "Ada"}, "@elle"))
        self.assertEqual(1, told)
        self.assertIn("New driver application", seen[0][0])
        self.assertIsNone(seen[0][1])


class TheButtonsWorkFromASupervisorsChatTest(unittest.TestCase):
    """Registered only top-level, PTB throws away the state these return and the
    prompt leads nowhere."""

    def test_the_prompting_callbacks_are_an_entry_point(self):
        self.assertIn("CallbackQueryHandler(handle_interview_callbacks,\n"
                      "                                 pattern=_INTERVIEW_ENTRY_CALLBACK_PATTERN)",
                      SRC)
        entry = SRC.split("entry_points=[", 1)[1].split("],", 1)[0]
        self.assertIn("_INTERVIEW_ENTRY_CALLBACK_PATTERN", entry)

    def test_only_prompt_then_capture_callbacks_enter_the_conversation(self):
        """^int_ would trap anyone merely LOOKING at an application in the
        questionnaire, where their next message becomes a new application."""
        pat = re.compile(B._INTERVIEW_ENTRY_CALLBACK_PATTERN)
        for good in ("int_lic_", "int_ef_telegram_id_", "int_ef_drivers_license_id_",
                     "int_sched_"):
            with self.subTest(good=good):
                self.assertTrue(pat.match(good + SID))
        for bad in ("int_open_", "int_edit_", "int_hire_", "int_mine_", "int_eback_"):
            with self.subTest(bad=bad):
                self.assertIsNone(pat.match(bad + SID))


class TheButtonBeatsTheStaleSessionTest(unittest.TestCase):
    """context.user_data is per-USER. Reading it first put one applicant's
    licence and edits onto whichever application that supervisor last touched."""

    def test_the_callback_id_is_read_first(self):
        i_cb = SRC.index("_interview_id_from_callback(data) or context.user_data")
        self.assertGreater(i_cb, 0)
        self.assertNotIn('context.user_data.get("active_interview_id") or _interview_id_from_callback',
                         SRC)

    def test_the_edit_keyboard_lands_on_the_message_that_was_tapped(self):
        self.assertNotIn('message_id=context.user_data.get("understanding_message_id")', SRC)


class TheCardIsFoundFromTheApplicationTest(unittest.TestCase):

    def _refresh(self, interview, user_data):
        ctx = mock.MagicMock()
        ctx.user_data = dict(user_data)
        calls = []

        async def edit(**kw):
            calls.append(kw)

        ctx.bot.edit_message_text = mock.AsyncMock(side_effect=edit)
        run(B._refresh_understanding_card(ctx, interview))
        return calls

    def test_the_row_is_enough_with_no_session_at_all(self):
        calls = self._refresh(
            {"id": IID, "status": "pending", "full_name": "Ada",
             "understanding_chat_id": 55, "understanding_message_id": 66}, {})
        self.assertEqual(1, len(calls), "a supervisor's refresh did nothing")
        self.assertEqual(55, calls[0]["chat_id"])
        self.assertEqual(66, calls[0]["message_id"])

    def test_a_stale_session_card_does_not_win(self):
        """The coordinates in user_data belong to whatever that person looked at
        last -- which is somebody else's application."""
        calls = self._refresh(
            {"id": IID, "status": "pending", "full_name": "Ada",
             "understanding_chat_id": 55, "understanding_message_id": 66},
            {"understanding_chat_id": 999, "understanding_message_id": 888})
        self.assertEqual(55, calls[0]["chat_id"])
        self.assertEqual(66, calls[0]["message_id"])

    def test_the_applicants_own_flow_still_works_without_the_columns(self):
        calls = self._refresh(
            {"id": IID, "status": "pending", "full_name": "Ada"},
            {"understanding_chat_id": 999, "understanding_message_id": 888})
        self.assertEqual(999, calls[0]["chat_id"])


class TheAlertStopsSayingWhatIsNoLongerTrueTest(unittest.TestCase):
    """Buttons that work while the message above them still reads "still needs:
    Telegram ID" are worse than no buttons: the supervisor cannot tell whether
    the tap did anything."""

    ALERT = ("\U0001f195 New driver application\n\n"
             "\U0001f464 Kazeem\n\U0001f4ac @brian\n\U0001f4f1 9734941210\n"
             "\U0001f64b Filed by @brian_brixx\n\n"
             "\u26a0\ufe0f Cannot hire yet \u2014 still needs: Telegram ID")

    def test_filed_by_is_recovered_from_the_message(self):
        """It is not a column -- the message is the only place it survives."""
        self.assertEqual("@brian_brixx", B._filed_by_from_alert_text(self.ALERT))
        self.assertEqual("\u2014", B._filed_by_from_alert_text("nothing useful"))

    def _ctx(self):
        ctx = mock.MagicMock()
        ctx.user_data = {}
        return ctx

    def _query(self, chat_id, message_id, text=ALERT):
        q = mock.MagicMock()
        q.message.chat_id = chat_id
        q.message.message_id = message_id
        q.message.text = text
        return q

    def test_an_alert_tap_is_remembered(self):
        ctx = self._ctx()
        B._remember_acting_alert(ctx, self._query(900, 7),
                                 {"id": IID, "understanding_chat_id": 55,
                                  "understanding_message_id": 66})
        self.assertEqual({"iid": IID, "chat_id": 900, "message_id": 7,
                          "filed_by": "@brian_brixx"}, ctx.user_data["acting_alert"])

    def test_a_tap_on_the_card_itself_is_not_remembered(self):
        """That message is the card. Rewriting it with the alert's text would
        replace a card with a notice."""
        ctx = self._ctx()
        B._remember_acting_alert(ctx, self._query(55, 66),
                                 {"id": IID, "understanding_chat_id": 55,
                                  "understanding_message_id": 66})
        self.assertNotIn("acting_alert", ctx.user_data)

    def test_the_alert_is_rewritten_with_the_new_state(self):
        ctx = self._ctx()
        ctx.user_data["acting_alert"] = {"iid": IID, "chat_id": 900,
                                         "message_id": 7, "filed_by": "@brian_brixx"}
        edits = []

        async def edit(**kw):
            edits.append(kw)

        ctx.bot.edit_message_text = mock.AsyncMock(side_effect=edit)
        run(B._refresh_interview_surfaces(ctx, {
            "id": IID, "status": "pending", "full_name": "Kazeem",
            "email": "k@x.com", "telegram_id": "8887186065",
            "drivers_license_id": "D1", "drivers_license_file_url": "https://x/y.jpg",
        }))
        alert = [e for e in edits if e.get("message_id") == 7]
        self.assertEqual(1, len(alert), edits)
        self.assertIn("Ready to hire", alert[0]["text"])
        self.assertNotIn("Cannot hire yet", alert[0]["text"])
        self.assertNotIn("warning only", alert[0]["text"])

    def test_a_stash_from_another_application_is_ignored(self):
        """A supervisor works several alerts in a row; the wrong message must
        not be rewritten with this application's details."""
        ctx = self._ctx()
        ctx.user_data["acting_alert"] = {"iid": "99999999-9999-4999-8999-999999999999",
                                         "chat_id": 900, "message_id": 7,
                                         "filed_by": "@someone"}
        edits = []

        async def edit(**kw):
            edits.append(kw)

        ctx.bot.edit_message_text = mock.AsyncMock(side_effect=edit)
        run(B._refresh_interview_surfaces(ctx, {"id": IID, "status": "pending",
                                                "full_name": "Kazeem"}))
        self.assertEqual([], [e for e in edits if e.get("message_id") == 7])

    def test_a_deleted_alert_does_not_cost_the_action(self):
        ctx = self._ctx()
        ctx.user_data["acting_alert"] = {"iid": IID, "chat_id": 900,
                                         "message_id": 7, "filed_by": "@b"}
        ctx.bot.edit_message_text = mock.AsyncMock(side_effect=RuntimeError("gone"))
        run(B._refresh_interview_surfaces(ctx, {"id": IID, "status": "pending",
                                                "full_name": "Kazeem"}))

    def test_the_action_handlers_refresh_both_surfaces(self):
        """Six call sites had been refreshing only the applicant's card."""
        self.assertEqual(6, SRC.count("await _refresh_interview_surfaces(context, interview)"))


if __name__ == "__main__":
    unittest.main()
