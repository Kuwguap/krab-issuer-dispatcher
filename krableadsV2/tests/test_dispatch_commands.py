"""Spoken/typed commands that send the lead out.

"Finished" used to be filed as the CLIENT'S NAME — the review card ended up with
name="Finished" and the lead was never dispatched.

Run:  venv\\Scripts\\python.exe -m pytest tests/test_dispatch_commands.py -q
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

# The commands the operator dictates to send a lead out.
DISPATCH_COMMANDS = [
    "Dispatch lead",
    "Dispatch",
    "Dispatch client",
    "Send dispatch",
    "Send lead",
    "Finished",
    "Finished dispatch",
    "Send tag",
]

# How the same commands arrive from transcription / casual typing.
VARIANTS = [
    "dispatch lead.", "FINISHED", "finished.", "finished dispatch.",
    "send the lead", "send it out now", "dispatch the client",
    "finishing up", "send tags", "done", "submit", "send out",
]

# These must keep their own meaning — a greedy submit matcher would eat them.
NOT_DISPATCH = [
    "choose driver Kita",
    "dispatcher HighKage",
    "name Finished Goods",       # a real name that contains the word
    "price 150",
    "driver note gate code 4455",
    "color blue",
]


class DispatchCommandTest(unittest.TestCase):

    def test_every_requested_command_dispatches(self):
        failed = [c for c in DISPATCH_COMMANDS
                  if bot._classify_review_command(c)[0] != "SUBMIT"]
        self.assertEqual([], failed)

    def test_spoken_and_typed_variants_dispatch(self):
        failed = [c for c in VARIANTS if bot._classify_review_command(c)[0] != "SUBMIT"]
        self.assertEqual([], failed)

    def test_other_commands_keep_their_meaning(self):
        wrong = [c for c in NOT_DISPATCH
                 if bot._classify_review_command(c)[0] == "SUBMIT"]
        self.assertEqual([], wrong)

    def test_field_edits_still_win_over_submit(self):
        self.assertEqual(bot._classify_review_command("name Finished Goods")[0], "FIELD_EDITS")
        self.assertEqual(bot._classify_review_command("price 150")[0], "FIELD_EDITS")


class NeverFiledAsAValueTest(unittest.TestCase):
    """A dispatch word must never be written into a field if submit is skipped."""

    def test_commands_are_recognised_as_commands(self):
        not_guarded = [c for c in DISPATCH_COMMANDS if not bot._COMMAND_LIKE_RE.search(c)]
        self.assertEqual([], not_guarded, "these could be smart-placed as a field value")

    def test_finished_is_not_placed_as_a_name(self):
        state = {}
        with mock.patch.object(bot.Config, "is_ai_vision_configured",
                               classmethod(lambda cls: False)):
            placed = [] if bot._COMMAND_LIKE_RE.search("Finished") else asyncio.run(
                bot._smart_place_single_value(state, "Finished"))
        self.assertEqual([], placed)
        self.assertIsNone(state.get("name"))


class ReachesTheDispatcherTest(unittest.TestCase):
    """The command must actually run the submit path, not just classify."""

    def _run(self, text):
        update = SimpleNamespace(
            message=SimpleNamespace(text=text, chat_id=1, delete=mock.AsyncMock(),
                                    reply_text=mock.AsyncMock()),
            effective_message=SimpleNamespace(text=text, chat_id=1),
            effective_chat=SimpleNamespace(id=1, type="private"),
            effective_user=SimpleNamespace(id=7, username="tester"),
        )
        ctx = SimpleNamespace(user_data={}, bot=mock.AsyncMock(),
                              application=SimpleNamespace(handlers={}))
        submit = mock.AsyncMock(return_value=bot.STATE_SELECT_GROUP)
        with mock.patch.object(bot, "_continue_phase1_after_ai_review", submit), \
                mock.patch.object(bot, "_cleanup_voice_echo", mock.AsyncMock()):
            asyncio.run(bot._interpret_review_command(update, ctx, 7, {}, text))
        return submit

    def test_each_command_calls_the_submit_path(self):
        for cmd in DISPATCH_COMMANDS:
            with self.subTest(cmd=cmd):
                self._run(cmd).assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
