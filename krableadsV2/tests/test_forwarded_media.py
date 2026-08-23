"""A forwarded image (with or without a caption) must be parsed into a lead.

With no lead running, an image reached nothing: every photo handler lived inside an
already-active conversation state, and there was no photo ENTRY point. For supervisors
it was worse — the temp-tag reader answered "I couldn't read a tag number" and
swallowed it, so a forwarded screenshot was never read for lead data.

Run:  venv\\Scripts\\python.exe -m pytest tests/test_forwarded_media.py -q
"""
import asyncio
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
TAG = {"plate": "H256693", "number": "256693", "kind": "resident"}


def _photo_update(app, i, caption=None, forwarded=True):
    msg = {
        "message_id": i, "date": int(time.time()),
        "chat": {"id": CHAT, "type": "private"},
        "from": {"id": USER, "is_bot": False, "first_name": "S"},
        "photo": [{"file_id": "f1", "file_unique_id": "u1", "width": 800, "height": 600}],
    }
    if caption:
        msg["caption"] = caption
    if forwarded:
        msg["forward_origin"] = {"type": "user", "date": int(time.time()),
                                 "sender_user": {"id": 999, "is_bot": False,
                                                 "first_name": "Client"}}
    return Update.de_json({"update_id": i, "message": msg}, app.bot)


def _send(*, supervisor, caption=None, forwarded=True, tag_result=None):
    """Send one photo to an idle bot through the real handler graph."""
    async def run():
        app = e2e._build_application()
        tg_file = mock.AsyncMock()
        tg_file.download_to_memory = mock.AsyncMock(side_effect=lambda out: out.write(b"jpg"))
        tg_file.file_path = "photo.jpg"
        with mock.patch.object(telegram.Bot, "_do_post", e2e.TRANSPORT.do_post), \
                mock.patch.object(telegram.Bot, "get_file",
                                  mock.AsyncMock(return_value=tg_file)), \
                mock.patch.object(bot, "_user_is_global_supervisor",
                                  mock.MagicMock(return_value=supervisor)), \
                mock.patch.object(bot, "_download_update_image_bytes",
                                  mock.AsyncMock(return_value=(b"x", "image/jpeg"))), \
                mock.patch.object(bot.ai_vision, "extract_plate_number_from_image",
                                  mock.MagicMock(return_value=tag_result)), \
                mock.patch.object(bot.Config, "is_ai_vision_configured",
                                  classmethod(lambda cls: True)):
            await app.initialize()
            try:
                e2e.FAKE_DB.states.clear()
                e2e.TRANSPORT.reset()
                await app.process_update(_photo_update(app, 1, caption, forwarded))
                return {
                    "lead": e2e.FAKE_DB.get_user_state(USER),
                    "batch": app.user_data[USER].get("phase1_vision_batch") or [],
                    "sent": e2e.TRANSPORT.sent_texts(),
                }
            finally:
                await app.shutdown()
    return asyncio.run(run())


class ForwardedImageStartsALeadTest(unittest.TestCase):

    def test_regular_user_forwarded_image_with_caption(self):
        r = _send(supervisor=False, caption="Name John Damian price 150")
        self.assertTrue(r["lead"], "the image must start a lead")
        self.assertEqual(len(r["batch"]), 1, "the image must be queued for extraction")
        self.assertTrue(r["batch"][0].get("caption"),
                        "the caption is context for the extraction — text + image is one thing")

    def test_supervisor_forwarded_image_is_no_longer_swallowed(self):
        r = _send(supervisor=True, caption="Name John Damian price 150")
        self.assertTrue(r["lead"])
        self.assertEqual(len(r["batch"]), 1)
        self.assertFalse(any("couldn't read a tag number" in s for s in r["sent"]),
                         "a lead image must not be rejected as a bad tag")

    def test_image_with_no_caption_still_starts_a_lead(self):
        r = _send(supervisor=False)
        self.assertTrue(r["lead"])
        self.assertEqual(len(r["batch"]), 1)

    def test_a_photo_sent_directly_behaves_the_same_as_a_forward(self):
        r = _send(supervisor=False, forwarded=False)
        self.assertTrue(r["lead"])
        self.assertEqual(len(r["batch"]), 1)


class TagReadingStillWinsTest(unittest.TestCase):
    """A real temp tag must still update the plate counter, not become a lead."""

    def test_supervisor_tag_photo_is_read_as_a_tag(self):
        with mock.patch.object(bot, "_router_stage_confirm", mock.AsyncMock()):
            r = _send(supervisor=True, tag_result=TAG)
        self.assertFalse(r["lead"], "a temp tag must not start a lead")
        self.assertEqual(len(r["batch"]), 0)


class EntryPointRegisteredTest(unittest.TestCase):

    def test_media_entry_point_exists(self):
        """Guards the gap: every other photo handler needs an ACTIVE conversation."""
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        self.assertIn("handle_idle_media_start", src)
        # It must sit in the entry_points block — i.e. before the states dict, which
        # is where every other photo handler lives (and those need an ACTIVE
        # conversation, which is exactly why an idle image reached nothing).
        entry_at = src.index("entry_points=[")
        states_at = src.index("states={", entry_at)
        self.assertLess(entry_at, src.index("handle_idle_media_start", entry_at))
        self.assertLess(src.index("handle_idle_media_start", entry_at), states_at,
                        "an image with no lead running must have an entry point")


if __name__ == "__main__":
    unittest.main()
