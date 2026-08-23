"""A picture sent at ANY step of the lead flow must be read, never dropped.

Reported: after ignoring the DMV Yes/No question, a photo of a phone number did
nothing. Eight states accepted typed text but registered no photo handler, so PTB
dropped the image with no reply — the same button-only hole that once ate typed edits.

Run:  venv\\Scripts\\python.exe -m pytest tests/test_media_in_any_state.py -q
"""
import asyncio
import os
import re
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

# What vision returns for a photo showing only a phone number.
PHONE_ONLY = "\n".join(["-"] * 11 + ["Phone: 551-374-0027"])


def _send_photo(db_state="phase1", ai_reply=PHONE_ONLY):
    msg = SimpleNamespace(
        text=None, caption=None, chat_id=1,
        photo=[SimpleNamespace(file_id="f1")], document=None,
        delete=mock.AsyncMock(),
        reply_text=mock.AsyncMock(return_value=SimpleNamespace(message_id=9, chat_id=1)))
    update = SimpleNamespace(
        message=msg, effective_message=msg,
        effective_chat=SimpleNamespace(id=1, type="private"),
        effective_user=SimpleNamespace(id=7, username="tester"))
    tg_file = mock.AsyncMock()
    tg_file.download_to_memory = mock.AsyncMock()
    ctx = SimpleNamespace(user_data={"review_message_id": 5, "review_chat_id": 1},
                          bot=mock.AsyncMock(), application=SimpleNamespace(handlers={}))
    ctx.bot.get_file = mock.AsyncMock(return_value=tg_file)
    fake_db = mock.MagicMock()
    fake_db.get_user_state.return_value = {"state": db_state,
                                           "data": {"vin": "-", "car": "-"}}
    saved, vanished = {}, []
    fake_db.set_user_state.side_effect = lambda u, s, d: saved.update(d or {})
    with mock.patch.object(bot, "db", fake_db), \
            mock.patch.object(bot.ai_vision, "extract_structured_from_media_parts",
                              mock.MagicMock(return_value=ai_reply)), \
            mock.patch.object(bot, "_send_vanishing",
                              mock.AsyncMock(side_effect=lambda c, ch, t, **k: vanished.append(t))), \
            mock.patch.object(bot, "_reanchor_review_card", mock.AsyncMock()), \
            mock.patch.object(bot, "_autoclean_user_msg", mock.AsyncMock()), \
            mock.patch.object(bot, "_add_extra_attachment", mock.MagicMock(return_value=None)), \
            mock.patch.object(bot, "_run_vin_check_for_review",
                              mock.AsyncMock(return_value=bot.STATE_AI_REVIEW)):
        state = asyncio.run(bot.handle_media_in_any_state(update, ctx))
    return state, saved, vanished


class PhotoIsReadInAnyStateTest(unittest.TestCase):

    def test_a_phone_photo_updates_the_card(self):
        _, saved, _ = _send_photo()
        self.assertEqual(saved.get("pending_phone_number"), "551-374-0027")

    def test_the_question_or_picker_above_stays_open(self):
        """Returning None leaves the conversation where it was, so the DMV Yes/No
        (or a picker) is still answerable after the image is read."""
        state, _, _ = _send_photo()
        self.assertIsNone(state)

    def test_it_also_works_while_the_notes_step_holds_the_row(self):
        _, saved, _ = _send_photo(db_state="special_request_drivers")
        self.assertEqual(saved.get("pending_phone_number"), "551-374-0027")

    def test_after_dispatch_it_says_so_rather_than_going_quiet(self):
        _, saved, vanished = _send_photo(db_state="select_driver")
        self.assertIsNone(saved.get("pending_phone_number"))
        self.assertTrue(vanished, "silence is what made this look broken")

    def test_a_message_with_no_media_is_ignored(self):
        update = SimpleNamespace(
            effective_message=SimpleNamespace(text="hello", photo=None, document=None),
            effective_user=SimpleNamespace(id=7))
        self.assertIsNone(asyncio.run(bot.handle_media_in_any_state(update, SimpleNamespace())))


class NoStateMayDropImagesTest(unittest.TestCase):
    """Guards the whole class of bug, not just the state that was reported."""

    def _state_table(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        block = src.split("        states={", 1)[1].split("        fallbacks=[", 1)[0]
        table, cur = {}, None
        for line in block.splitlines():
            m = re.match(r"\s{12}(STATE_[A-Z0-9_]+):", line)
            if m:
                cur = m.group(1)
                table.setdefault(cur, {"text": False, "photo": False})
            if cur:
                if "filters.TEXT" in line:
                    table[cur]["text"] = True
                if "filters.PHOTO" in line:
                    table[cur]["photo"] = True
        return table

    def test_every_state_that_takes_text_also_takes_photos(self):
        gaps = [s for s, v in self._state_table().items() if v["text"] and not v["photo"]]
        self.assertEqual([], gaps,
                         "a state that accepts typing must not silently drop an image")

    def test_the_reported_state_is_covered(self):
        self.assertTrue(self._state_table()["STATE_VIN_CHOICE"]["photo"])


if __name__ == "__main__":
    unittest.main()
