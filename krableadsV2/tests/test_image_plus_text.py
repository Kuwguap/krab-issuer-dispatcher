"""An image sent WITH text: both must be read.

A picture and its caption are one message, but the caption was only handed to the
model as loose context — so "name John Damian price 150" written under a photo of the
vehicle contributed nothing, and those fields stayed empty.

Run:  venv\\Scripts\\python.exe -m pytest tests/test_image_plus_text.py -q
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

# What vision reads off the picture: the VEHICLE only.
VEHICLE_ONLY = "\n".join(
    ["-", "-", "-", "-", "-", "4S4BSAAC9J3259647", "2019 Honda Accord", "Blue", "-", "-", "-"])


def _send_photo(caption, image_reply=VEHICLE_ONLY, start=None):
    """Push a photo + caption through the real review-upload handler."""
    msg = SimpleNamespace(
        text=None, caption=caption, chat_id=1,
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
    fake_db.get_user_state.return_value = {"state": "phase1",
                                           "data": dict(start or {"vin": "-", "car": "-"})}
    saved = {}
    fake_db.set_user_state.side_effect = lambda u, s, d: saved.update(d or {})
    with mock.patch.object(bot, "db", fake_db), \
            mock.patch.object(bot.ai_vision, "extract_structured_from_media_parts",
                              mock.MagicMock(return_value=image_reply)), \
            mock.patch.object(bot, "_send_vanishing", mock.AsyncMock()), \
            mock.patch.object(bot, "_reanchor_review_card", mock.AsyncMock()), \
            mock.patch.object(bot, "_autoclean_user_msg", mock.AsyncMock()), \
            mock.patch.object(bot, "_add_extra_attachment", mock.MagicMock(return_value=None)), \
            mock.patch.object(bot, "_run_vin_check_for_review",
                              mock.AsyncMock(return_value=bot.STATE_AI_REVIEW)):
        asyncio.run(bot.handle_phase1_adjust_input(update, ctx))
    return saved


class ImagePlusTextTest(unittest.TestCase):

    def test_both_sources_land(self):
        saved = _send_photo("Name John Damian price 150 phone 732-555-1212")
        # from the picture
        self.assertEqual(saved.get("car"), "2019 Honda Accord")
        self.assertEqual(saved.get("vin"), "4S4BSAAC9J3259647")
        # from the words sent with it
        self.assertEqual(saved.get("name"), "John Damian")
        self.assertEqual(saved.get("pending_price"), "$150")
        self.assertTrue(saved.get("pending_phone_number"))

    def test_unlabeled_caption_still_gives_up_its_details(self):
        saved = _send_photo("call him on 732-555-1212, quoted $250, john@x.com")
        self.assertTrue(saved.get("pending_phone_number"))
        self.assertEqual(saved.get("pending_price"), "$250")
        self.assertEqual(saved.get("email"), "john@x.com")
        self.assertEqual(saved.get("car"), "2019 Honda Accord", "the image must survive")

    def test_typed_value_wins_over_the_picture(self):
        """The sender labeled it by hand, so it beats the vision read."""
        saved = _send_photo(
            "color white",
            image_reply="\n".join(["-"] * 7 + ["Blue"] + ["-"] * 3))
        self.assertEqual(saved.get("color"), "White")

    def test_unlabeled_caption_does_not_overwrite_a_filled_field(self):
        saved = _send_photo("quoted $250",
                            start={"vin": "-", "car": "-", "pending_price": "$400"})
        self.assertEqual(saved.get("pending_price"), "$400")

    def test_image_without_a_caption_is_unchanged(self):
        saved = _send_photo(None)
        self.assertEqual(saved.get("car"), "2019 Honda Accord")
        self.assertEqual(saved.get("vin"), "4S4BSAAC9J3259647")


class CaptionHelperTest(unittest.TestCase):

    def test_labeled_values_are_applied(self):
        d = {}
        labels = bot._apply_caption_to_lead(d, "name John Damian price 150")
        self.assertEqual(d.get("name"), "John Damian")
        self.assertEqual(d.get("pending_price"), "$150")
        self.assertTrue(labels)

    def test_empty_caption_is_a_no_op(self):
        d = {"name": "Keep Me"}
        self.assertEqual(bot._apply_caption_to_lead(d, ""), [])
        self.assertEqual(d.get("name"), "Keep Me")

    def test_prose_caption_only_fills_empty_fields(self):
        d = {"pending_price": "$400"}
        bot._apply_caption_to_lead(d, "he said $250")
        self.assertEqual(d.get("pending_price"), "$400")


if __name__ == "__main__":
    unittest.main()
