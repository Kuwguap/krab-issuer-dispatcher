r"""Editable follow-up recipients, the team chat, the leaderboard, bare "start",
and the field prompt that used to swallow "price 150".

Asked for:
  * "follow ups are very rigid allow easy editting … EDIT EMAIL / EDIT PHONE /
    EDIT CHATIDS", defaults being the configured email/phone and every supervisor;
  * "send follow up messages to DISPATCH TEAM main dispatch chat … with an edit,
    stop, close, or post phone button can also postpone followup reminder to next
    week", renewals too;
  * "/stats /leaderboard — Driver name - # of submitted leads. Name only";
  * "start and /start should do the same thing";
  * "during a test to submit a lead i typed price 150 and the bot didnt get that".

Run:  venv\Scripts\python.exe -m pytest tests/test_followups_and_leaderboard.py -q
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

import utils.database as udb  # noqa: E402

if "bot" not in sys.modules:
    udb.Database = mock.MagicMock()
import bot  # noqa: E402

GROUPS = [{"id": "g1", "group_name": "HighKage", "is_active": True,
           "group_telegram_id": "-1001234"}]


class EditableRecipientsTest(unittest.TestCase):

    def setUp(self):
        self.store = {}
        self.db = mock.MagicMock()
        self.db.get_setting.side_effect = lambda k: self.store.get(k)
        self.db.set_setting.side_effect = (
            lambda k, v: (self.store.__setitem__(k, v), True)[1])
        self.db.get_all_groups.return_value = list(GROUPS)

    def _ctx(self, sups=(111, 222)):
        return mock.patch.object(bot, "db", self.db), \
            mock.patch.object(bot, "_global_supervisory_chat_ids", lambda: list(sups))

    def test_the_defaults_are_what_was_configured(self):
        a, b = self._ctx()
        with a, b:
            self.assertEqual(bot.Config.FOLLOWUP_EMAIL_COPY, bot.followup_email())
            self.assertEqual(bot.Config.FOLLOWUP_PHONE, bot.followup_phone())

    def test_by_default_every_supervisor_gets_the_reminder(self):
        a, b = self._ctx()
        with a, b:
            self.assertEqual([111, 222], bot.followup_chat_ids())

    def test_the_email_can_be_changed(self):
        a, b = self._ctx()
        with a, b:
            self.store[bot.FU_EMAIL_KEY] = "ops@example.com"
            self.assertEqual("ops@example.com", bot.followup_email())

    def test_the_phone_can_be_changed(self):
        a, b = self._ctx()
        with a, b:
            self.store[bot.FU_PHONE_KEY] = "201-555-0000"
            self.assertEqual("201-555-0000", bot.followup_phone())

    def test_the_chat_ids_can_be_replaced(self):
        a, b = self._ctx()
        with a, b:
            self.store[bot.FU_CHATIDS_KEY] = "333 444"
            self.assertEqual([333, 444], bot.followup_chat_ids())

    def test_clearing_them_restores_the_default(self):
        a, b = self._ctx()
        with a, b:
            self.store[bot.FU_CHATIDS_KEY] = ""
            self.assertEqual([111, 222], bot.followup_chat_ids())

    def test_the_ids_survive_any_separator(self):
        a, b = self._ctx()
        with a, b:
            for raw in ("333,444", "333; 444", "333\n444", " 333 , 444 "):
                with self.subTest(raw=raw):
                    self.store[bot.FU_CHATIDS_KEY] = raw
                    self.assertEqual([333, 444], bot.followup_chat_ids())

    def test_a_repeated_id_is_only_messaged_once(self):
        a, b = self._ctx()
        with a, b:
            self.store[bot.FU_CHATIDS_KEY] = "333 333 444"
            self.assertEqual([333, 444], bot.followup_chat_ids())

    def test_rubbish_in_the_list_is_skipped_not_fatal(self):
        a, b = self._ctx()
        with a, b:
            self.store[bot.FU_CHATIDS_KEY] = "333 not-an-id 444"
            self.assertEqual([333, 444], bot.followup_chat_ids())

    def test_the_team_chat_defaults_to_the_first_active_dispatcher(self):
        a, b = self._ctx()
        with a, b:
            self.assertEqual(-1001234, bot.followup_team_chat_id())

    def test_the_team_chat_can_be_set(self):
        a, b = self._ctx()
        with a, b:
            self.store[bot.FU_TEAM_CHAT_KEY] = "-1009999"
            self.assertEqual(-1009999, bot.followup_team_chat_id())

    def test_a_database_that_is_down_falls_back_rather_than_raising(self):
        self.db.get_setting.side_effect = Exception("down")
        a, b = self._ctx()
        with a, b:
            self.assertEqual(bot.Config.FOLLOWUP_PHONE, bot.followup_phone())
            self.assertEqual([111, 222], bot.followup_chat_ids())


class TheSettingsScreenTest(unittest.TestCase):

    def _screen(self, store=None):
        db = mock.MagicMock()
        db.get_setting.side_effect = lambda k: (store or {}).get(k)
        db.get_all_groups.return_value = list(GROUPS)
        with mock.patch.object(bot, "db", db), \
                mock.patch.object(bot, "_global_supervisory_chat_ids", lambda: [111]):
            return asyncio.run(bot._settings_view_followups())

    def test_it_shows_every_recipient(self):
        text, _ = self._screen()
        self.assertIn(bot.Config.FOLLOWUP_EMAIL_COPY, text)
        self.assertIn("111", text)
        self.assertIn("-1001234", text)

    def test_all_four_edit_buttons_are_there(self):
        _, kb = self._screen()
        data = [b.callback_data for row in kb.inline_keyboard for b in row]
        for want in ("tset_fuemail", "tset_fuphone", "tset_fuids", "tset_futeam"):
            self.assertIn(want, data, want)

    def test_it_says_when_the_ids_are_the_default(self):
        text, _ = self._screen()
        self.assertIn("every supervisor", text)

    def test_it_stops_saying_that_once_they_are_set(self):
        text, _ = self._screen({bot.FU_CHATIDS_KEY: "333"})
        self.assertNotIn("every supervisor", text)
        self.assertIn("333", text)

    def test_it_is_reachable_by_voice(self):
        for said in ("follow-ups", "follow ups", "renewals", "reminders"):
            with self.subTest(said=said):
                self.assertEqual("tset_fu", bot._settings_nav_target(said), said)

    def test_the_menu_has_it(self):
        data = [b.callback_data for row in bot._settings_main_kb().inline_keyboard
                for b in row]
        self.assertIn("tset_fu", data)


class TheTeamChatMessageTest(unittest.TestCase):
    """The whole team sees the reminder and can act on it there."""

    def test_every_action_asked_for_is_on_it(self):
        labels = [b.text for row in bot._followup_team_keyboard("abc").inline_keyboard
                  for b in row]
        joined = " ".join(labels).lower()
        for want in ("close", "stop", "pause", "email", "phone"):
            self.assertIn(want, joined, want)

    def test_pausing_is_a_week(self):
        labels = [b.text for row in bot._followup_team_keyboard("abc").inline_keyboard
                  for b in row]
        self.assertTrue(any("week" in l.lower() for l in labels), labels)

    def test_the_buttons_carry_the_record(self):
        data = [b.callback_data for row in bot._followup_team_keyboard("abc").inline_keyboard
                for b in row]
        self.assertTrue(all(d.endswith("abc") for d in data), data)

    def test_every_button_reaches_a_handler(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        data = [b.callback_data for row in bot._followup_team_keyboard("abc").inline_keyboard
                for b in row]
        for d in data:
            prefix = d[:-len("abc")]
            with self.subTest(prefix=prefix):
                self.assertIn(f'pattern="^{prefix}"', src, prefix)

    def test_renewals_go_there_too(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        job = src.split("async def check_client_followups", 1)[1].split("\n    if application", 1)[0]
        self.assertIn("followup_team_chat_id()", job)
        self.assertIn("Renewal due", job)

    def test_the_team_is_not_messaged_twice(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        job = src.split("async def check_client_followups", 1)[1].split("\n    if application", 1)[0]
        self.assertIn("_norm_chat_id(team_chat) not in sent_to", job)


class TheLeaderboardTest(unittest.TestCase):

    def _run(self, rows):
        msg = mock.MagicMock()
        msg.reply_text = mock.AsyncMock()
        update = mock.MagicMock()
        update.effective_message = msg
        with mock.patch.object(bot.db, "get_lead_counts_by_sender",
                               mock.MagicMock(return_value=rows)):
            asyncio.run(bot.cmd_leaderboard(update, mock.MagicMock()))
        return msg.reply_text.await_args[0][0]

    def test_it_ranks_by_count(self):
        said = self._run([("kita", 12), ("marco", 9), ("sara", 4)])
        self.assertLess(said.index("kita"), said.index("marco"))
        self.assertLess(said.index("marco"), said.index("sara"))

    def test_it_shows_the_count(self):
        said = self._run([("kita", 12)])
        self.assertIn("12", said)

    def test_names_only_no_contact_details(self):
        """"we don't need drivers reaching out to each other"."""
        said = self._run([("kita", 12)])
        for leak in ("@", "551", "+1", "http"):
            self.assertNotIn(leak, said, leak)

    def test_an_empty_board_says_so(self):
        self.assertIn("No leads counted yet", self._run([]))

    def test_both_names_reach_it(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        self.assertIn('CommandHandler(["leaderboard", "stats", "board", "ranking"]', src)

    def test_saying_it_works_too(self):
        for said in ("leaderboard", "stats", "scoreboard"):
            with self.subTest(said=said):
                self.assertEqual("leaderboard", bot._BARE_COMMANDS.get(said), said)


class BareStartTest(unittest.TestCase):

    def test_start_runs_the_command(self):
        self.assertEqual("start", bot._BARE_COMMANDS.get("start"))

    def test_it_no_longer_opens_a_blank_lead(self):
        self.assertFalse(bot._PURE_TRIGGER_RE.match("start"))

    def test_the_other_triggers_still_do(self):
        for said in ("new", "new lead", "lead", "new client", "tag"):
            with self.subTest(said=said):
                self.assertTrue(bot._PURE_TRIGGER_RE.match(said), said)


class APromptWithNoPendingFieldTest(unittest.TestCase):
    """"price 150" typed at a field prompt after a restart was dropped in silence."""

    def test_it_is_treated_as_a_review_edit(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        body = src.split("async def handle_edit_field_text", 1)[1].split("\nasync def ", 1)[0]
        self.assertIn("return await handle_phase1_review_message(update, context)", body)
        self.assertNotIn("return STATE_AI_REVIEW\n\n    state = db.get_user_state", body)

    def test_the_edit_menu_prompt_too(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        body = src.split("async def handle_phase1_edit_input", 1)[1].split("\nasync def ", 1)[0]
        self.assertIn("return await handle_phase1_review_message(update, context)", body)
        self.assertNotIn("Use the buttons above", body)


class TheDriverOfferCarriesTheLinkTest(unittest.TestCase):

    def test_the_offer_has_an_upload_button(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        self.assertIn("🧾 Upload receipt", src)
        self.assertIn("url=receipt_portal_url(lead_id)", src)


if __name__ == "__main__":
    unittest.main()
