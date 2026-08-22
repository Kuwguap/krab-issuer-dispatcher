"""/cancel and /restart are the same action, by command or by voice.

Before this they diverged: /restart opened a fresh review card, while /cancel dropped
to idle with a "Nothing to cancel" style reply — and /cancel did nothing at all when
no lead flow was running.

Run:  venv\\Scripts\\python.exe -m pytest tests/test_cancel_restart.py -q
"""
import asyncio
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy-key")

import utils.database as udb  # noqa: E402

if "bot" not in sys.modules:
    udb.Database = mock.MagicMock()
import bot  # noqa: E402

# Every word that should wipe and hand back a fresh card.
CANCEL_WORDS = ["cancel", "stop", "never mind", "nvm", "abort", "quit", "discard",
                "forget it", "scrap it"]
RESTART_WORDS = ["restart", "start over", "start again", "reset", "redo", "do over"]


def _update(text):
    msg = SimpleNamespace(text=text, chat_id=1, photo=None, document=None,
                          delete=mock.AsyncMock(), reply_text=mock.AsyncMock())
    return SimpleNamespace(
        message=msg, effective_message=msg,
        effective_chat=SimpleNamespace(id=1, type="private"),
        effective_user=SimpleNamespace(id=7, username="tester"),
    )


def _ctx():
    return SimpleNamespace(user_data={}, bot=mock.AsyncMock(),
                           application=SimpleNamespace(handlers={}))


class SameActionTest(unittest.TestCase):

    def test_both_words_are_recognised(self):
        for w in CANCEL_WORDS:
            self.assertIsNotNone(bot._cancel_restart_kind(w), w)
        for w in RESTART_WORDS:
            self.assertEqual(bot._cancel_restart_kind(w), "restart", w)

    def test_both_kinds_open_a_fresh_card(self):
        """The action must not depend on which word was used."""
        for kind in ("cancel", "restart"):
            with self.subTest(kind=kind):
                fresh = mock.AsyncMock(return_value=bot.STATE_AI_REVIEW)
                fake_db = mock.MagicMock()
                with mock.patch.object(bot, "_begin_lead_flow_with_review", fresh), \
                        mock.patch.object(bot, "_clear_phase1_vision_upload_state",
                                          mock.AsyncMock()), \
                        mock.patch.object(bot, "db", fake_db):
                    state = asyncio.run(
                        bot._do_cancel_or_restart(_update(kind), _ctx(), kind))
                fresh.assert_awaited_once()
                fake_db.clear_user_state.assert_called_once_with(7)
                self.assertEqual(state, bot.STATE_AI_REVIEW)

    def test_cancel_no_longer_ends_the_conversation(self):
        """It used to return END, leaving the issuer with nothing on screen."""
        fake_db = mock.MagicMock()
        with mock.patch.object(bot, "_begin_lead_flow_with_review",
                               mock.AsyncMock(return_value=bot.STATE_AI_REVIEW)), \
                mock.patch.object(bot, "_clear_phase1_vision_upload_state", mock.AsyncMock()), \
                mock.patch.object(bot, "db", fake_db):
            state = asyncio.run(bot._do_cancel_or_restart(_update("cancel"), _ctx(), "cancel"))
        self.assertNotEqual(state, bot.ConversationHandler.END)

    def test_lead_cancel_command_routes_to_the_same_place(self):
        restart = mock.AsyncMock(return_value=bot.STATE_AI_REVIEW)
        with mock.patch.object(bot, "_do_cancel_or_restart", restart):
            asyncio.run(bot.cancel_from_lead_conversation(_update("/cancel"), _ctx()))
        restart.assert_awaited_once()

    def test_restart_command_routes_to_the_same_place(self):
        restart = mock.AsyncMock(return_value=bot.STATE_AI_REVIEW)
        with mock.patch.object(bot, "_do_cancel_or_restart", restart):
            asyncio.run(bot.restart_command(_update("/restart"), _ctx()))
        restart.assert_awaited_once()


class OtherFlowsKeepTheirOwnCancelTest(unittest.TestCase):
    """/cancel inside receipts, settings, follow-ups or appeals must NOT start a lead."""

    def test_each_flow_has_its_own_cancel_handler(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        for handler in ("cancel_from_receipt_conversation",
                        "settings_cancel", "cmd_followup_cancel"):
            self.assertIn(f'CommandHandler("cancel", {handler})', src, handler)

    def test_settings_cancel_does_not_start_a_lead(self):
        start = mock.AsyncMock()
        with mock.patch.object(bot, "_begin_lead_flow_with_review", start):
            asyncio.run(bot.settings_cancel(_update("/cancel"), _ctx()))
        start.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
