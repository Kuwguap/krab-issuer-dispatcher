"""Sending a bundle: several images AND several lines of text.

Two failures this covers:
  * text arriving while photos were queued CLEARED the queue — the images were
    thrown away and only the text was read;
  * a multi-line paste was all-or-nothing, so one unreadable line ("Email now")
    discarded every readable one in the same message ("Color white").

Run:  venv\\Scripts\\python.exe -m pytest tests/test_bulk_send.py -q
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


def _review_text(text, start=None):
    """Send one text message to an open review card; return (saved, toasts)."""
    msg = SimpleNamespace(text=text, caption=None, chat_id=1, photo=None, document=None,
                          delete=mock.AsyncMock(), reply_text=mock.AsyncMock())
    update = SimpleNamespace(
        message=msg, effective_message=msg,
        effective_chat=SimpleNamespace(id=1, type="private"),
        effective_user=SimpleNamespace(id=7, username="tester"))
    ctx = SimpleNamespace(user_data={"review_message_id": 5, "review_chat_id": 1},
                          bot=mock.AsyncMock(), application=SimpleNamespace(handlers={}))
    fake_db = mock.MagicMock()
    fake_db.get_user_state.return_value = {
        "state": "phase1", "data": dict(start or {"vin": "-", "car": "-"})}
    saved, toasts = {}, []
    fake_db.set_user_state.side_effect = lambda u, s, d: saved.update(d or {})
    with mock.patch.object(bot, "db", fake_db), \
            mock.patch.object(bot.Config, "is_ai_vision_configured",
                              classmethod(lambda cls: False)), \
            mock.patch.object(bot, "_update_review_message_text", mock.AsyncMock()), \
            mock.patch.object(bot, "_cleanup_voice_echo", mock.AsyncMock()), \
            mock.patch.object(bot, "_autoclean_user_msg", mock.AsyncMock()), \
            mock.patch.object(bot, "_send_vanishing",
                              mock.AsyncMock(side_effect=lambda c, ch, t, **k: toasts.append(t))):
        asyncio.run(bot.handle_phase1_review_message(update, ctx))
    return saved, (toasts[0] if toasts else "")


class BulkTextTest(unittest.TestCase):
    """One line the parser cannot read must not discard the ones it can."""

    def test_the_bundle_that_was_reported(self):
        saved, toast = _review_text("rrod782@gmail.com\n\nEmail now\nColor white")
        self.assertEqual(saved.get("email"), "rrod782@gmail.com")
        self.assertEqual(saved.get("color"), "White")
        self.assertIn("Not understood", toast, "the stray line should be reported")

    def test_a_stray_line_is_not_filed_as_the_name(self):
        saved, _ = _review_text("rrod782@gmail.com\n\nEmail now\nColor white")
        self.assertIsNone(saved.get("name"))

    def test_a_stray_line_never_overwrites_a_good_value(self):
        saved, _ = _review_text(
            "name Johnathan Perez\nrrod782@gmail.com\nColor white\n"
            "price 200\n551-374-0027\nsome random note")
        self.assertEqual(saved.get("name"), "Johnathan Perez")
        self.assertEqual(saved.get("email"), "rrod782@gmail.com")
        self.assertEqual(saved.get("color"), "White")
        self.assertEqual(saved.get("pending_price"), "$200")
        self.assertTrue(saved.get("pending_phone_number"))

    def test_all_labeled_lines_still_work(self):
        saved, toast = _review_text("name John Damian\nprice 150\ncolor blue")
        self.assertEqual(saved.get("name"), "John Damian")
        self.assertEqual(saved.get("pending_price"), "$150")
        self.assertEqual(saved.get("color"), "Blue")
        self.assertNotIn("Not understood", toast)

    def test_bulk_helper_separates_labeled_from_leftovers(self):
        state = {}
        labels, leftovers = bot._apply_bulk_review_text(
            state, "color white\nsomething odd\nprice 150")
        self.assertIn("color", labels)
        self.assertIn("price", labels)
        self.assertEqual(leftovers, ["something odd"])


class LeftoverPlacementIsConservativeTest(unittest.TestCase):
    """Leftovers place only on a strong signal — never the loose name guess."""

    def _place(self, line, state=None):
        st = dict(state or {})
        with mock.patch.object(bot.Config, "is_ai_vision_configured",
                               classmethod(lambda cls: False)):
            placed = asyncio.run(bot._place_bulk_leftover(st, line))
        return placed, st

    def test_structured_values_place(self):
        for line, key in [("rrod782@gmail.com", "email"),
                          ("551-374-0027", "pending_phone_number"),
                          ("$250", "pending_price")]:
            with self.subTest(line=line):
                placed, st = self._place(line)
                self.assertTrue(placed, line)
                self.assertTrue(st.get(key), line)

    def test_prose_does_not_place(self):
        for line in ("Email now", "some random note", "Sorry for the late response"):
            with self.subTest(line=line):
                placed, st = self._place(line)
                self.assertEqual(placed, [], line)

    def test_a_filled_field_is_not_replaced(self):
        placed, st = self._place("other@x.com", {"email": "first@x.com"})
        self.assertEqual(placed, [])
        self.assertEqual(st.get("email"), "first@x.com")


class TextDoesNotDiscardQueuedPhotosTest(unittest.TestCase):

    def test_queued_photos_survive_and_are_read_with_the_text(self):
        ctx = SimpleNamespace(
            user_data={"phase1_vision_batch": [
                {"kind": "image", "bytes": b"a", "mime": "image/jpeg"}] * 3,
                "phase1_vision_reply_chat_id": 1},
            bot=mock.AsyncMock(), application=SimpleNamespace(handlers={}))
        msg = SimpleNamespace(text="Color white", chat_id=1, reply_text=mock.AsyncMock())
        update = SimpleNamespace(
            message=msg, effective_message=msg,
            effective_chat=SimpleNamespace(id=1, type="private"),
            effective_user=SimpleNamespace(id=7, username="tester"))
        seen = {}

        async def fake_extract(context, user_id):
            seen["batch"] = len(context.user_data.get("phase1_vision_batch") or [])
            seen["notes"] = list(context.user_data.get("phase1_typed_notes") or [])
            return bot.STATE_AI_REVIEW

        with mock.patch.object(bot, "_execute_phase1_vision_batch_extraction", fake_extract), \
                mock.patch.object(bot, "db", mock.MagicMock()):
            state = asyncio.run(bot.handle_phase1(update, ctx))
        self.assertEqual(seen.get("batch"), 3, "the queued images must not be discarded")
        self.assertEqual(seen.get("notes"), ["Color white"], "the text must ride along")
        self.assertEqual(state, bot.STATE_AI_REVIEW)


if __name__ == "__main__":
    unittest.main()
