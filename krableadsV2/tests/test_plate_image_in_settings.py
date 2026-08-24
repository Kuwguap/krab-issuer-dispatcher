"""A photo of a temp tag sent inside /settings must update the plate counter.

It did nothing: the tag reader stands down whenever ANY conversation is active so a
lead's title/licence photo is not misread as a tag — and /settings counted, even
though it has no image handling of its own and is exactly where a plate photo is the
intended input.

Run:  venv\\Scripts\\python.exe -m pytest tests/test_plate_image_in_settings.py -q
"""
import asyncio
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

sys.argv = ["pytest"]
import test_real_routing_e2e as e2e  # noqa: E402  (shared real-Application harness)
import bot  # noqa: E402
import telegram  # noqa: E402
from telegram import Update  # noqa: E402

CHAT = e2e.CHAT_ID
USER = e2e.USER_ID
# The tag in the photo: New Jersey resident (H-prefix) temporary plate.
PLATE = {"plate": "H256693", "number": "256693", "kind": "resident"}


def _photo(app, i):
    return Update.de_json({"update_id": i, "message": {
        "message_id": i, "date": int(time.time()),
        "chat": {"id": CHAT, "type": "private"},
        "from": {"id": USER, "is_bot": False, "first_name": "S"},
        "photo": [{"file_id": "f1", "file_unique_id": "u1", "width": 100, "height": 100}],
    }}, app.bot)


def _command(app, i, text):
    return Update.de_json({"update_id": i, "message": {
        "message_id": i, "date": int(time.time()),
        "chat": {"id": CHAT, "type": "private"},
        "from": {"id": USER, "is_bot": False, "first_name": "S"},
        "text": text,
        "entities": [{"type": "bot_command", "offset": 0, "length": len(text)}],
    }}, app.bot)


def _send_plate_photo(*, inside_settings, plate=PLATE):
    """Drive the real handler graph; return what the confirm step was staged with."""
    async def run():
        app = e2e._build_application()
        staged = {}
        with mock.patch.object(telegram.Bot, "_do_post", e2e.TRANSPORT.do_post), \
                mock.patch.object(bot, "_user_is_global_supervisor",
                                  mock.MagicMock(return_value=True)), \
                mock.patch.object(bot, "_download_update_image_bytes",
                                  mock.AsyncMock(return_value=(b"jpegbytes", "image/jpeg"))), \
                mock.patch.object(bot.ai_vision, "extract_plate_number_from_image",
                                  mock.MagicMock(return_value=plate)), \
                mock.patch.object(bot, "_router_stage_confirm",
                                  mock.AsyncMock(side_effect=lambda m, c, a: staged.update(a))):
            await app.initialize()
            try:
                e2e.FAKE_DB.states.clear()
                if inside_settings:
                    await app.process_update(_command(app, 1, "/settings"))
                e2e.TRANSPORT.reset()
                await app.process_update(_photo(app, 2))
                return staged
            finally:
                await app.shutdown()
    return asyncio.run(run())


class PlateImageInSettingsTest(unittest.TestCase):

    def test_photo_in_settings_stages_the_counter_update(self):
        staged = _send_plate_photo(inside_settings=True)
        self.assertTrue(staged, "a plate photo in /settings must be read, not swallowed")
        self.assertEqual(staged.get("kind"), "set_plate")
        self.assertEqual(staged.get("col"), "nj_plate_next_number")   # H = resident
        self.assertEqual(staged.get("value"), 266693)   # 256693 + PLATE_IMAGE_JUMP

    def test_photo_when_idle_still_works(self):
        staged = _send_plate_photo(inside_settings=False)
        self.assertEqual(staged.get("value"), 266693)   # 256693 + PLATE_IMAGE_JUMP

    def test_non_resident_tag_targets_the_other_counter(self):
        staged = _send_plate_photo(
            inside_settings=True,
            plate={"plate": "100000V", "number": "100000", "kind": "nonresident"})
        self.assertEqual(staged.get("col"), "non_nj_plate_next_number")
        self.assertEqual(staged.get("value"), 110000)   # 100000 + PLATE_IMAGE_JUMP

    def test_nothing_is_written_before_confirming(self):
        """The staging step only prepares the change."""
        staged = _send_plate_photo(inside_settings=True)
        self.assertIn("prompt", staged, "the supervisor is shown what was read first")


class GuardStillProtectsTheLeadFlowTest(unittest.TestCase):
    """A title/licence photo sent mid-lead must NOT be read as a tag."""

    def test_lead_conversation_is_still_deferred_to(self):
        ctx = mock.MagicMock()
        conv = mock.MagicMock(spec=bot.ConversationHandler)
        conv._conversations = {("k",): bot.STATE_AI_REVIEW}
        conv._get_key.return_value = ("k",)
        ctx.application.handlers = {0: [conv]}
        self.assertTrue(bot._user_in_active_conversation(mock.MagicMock(), ctx))

    def test_only_the_ignored_handler_is_skipped(self):
        ctx = mock.MagicMock()
        settings = mock.MagicMock(spec=bot.ConversationHandler)
        settings._conversations = {("k",): 0}
        settings._get_key.return_value = ("k",)
        ctx.application.handlers = {0: [settings]}
        self.assertTrue(bot._user_in_active_conversation(mock.MagicMock(), ctx))
        self.assertFalse(
            bot._user_in_active_conversation(mock.MagicMock(), ctx, ignore=settings))


if __name__ == "__main__":
    unittest.main()
