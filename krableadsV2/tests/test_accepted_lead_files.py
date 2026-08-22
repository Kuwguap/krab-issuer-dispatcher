"""Files sent for parsing must follow the lead to whoever accepts it.

Before this, the parsed images/PDFs were forwarded ONLY to a group that accepted the
offer, and were then wiped from the lead row — so the driver who accepted (the person
actually doing the delivery, who needs the title/registration shots) got nothing.

Run:  venv\\Scripts\\python.exe -m pytest tests/test_accepted_lead_files.py -q
"""
import asyncio
import base64
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

DRIVER_CHAT = 4242
PHOTO_B64 = base64.b64encode(b"fake-jpeg-bytes").decode("ascii")


def _lead_with_files(files=None):
    return {
        "id": "lead-1",
        "reference_id": "REF123",
        "phase1_attached_files": files if files is not None else [
            {"type": "photo", "mime": "image/jpeg", "filename": "title.jpg",
             "data_b64": PHOTO_B64, "caption": "title"},
        ],
    }


def _ctx(user_data=None):
    return SimpleNamespace(
        bot=mock.AsyncMock(),
        user_data=user_data if user_data is not None else {},
        application=SimpleNamespace(handlers={}),
    )


class ForwardToAcceptingDriverTest(unittest.TestCase):

    def test_driver_gets_the_files(self):
        ctx = _ctx()
        fwd = mock.AsyncMock()
        with mock.patch.object(bot, "_forward_phase1_attached_files_to_targets", fwd):
            asyncio.run(bot._forward_accepted_lead_files(ctx, _lead_with_files(), DRIVER_CHAT))
        fwd.assert_awaited_once()
        self.assertEqual(fwd.await_args.args[2], DRIVER_CHAT)
        self.assertEqual(len(fwd.await_args.args[1]), 1)

    def test_files_are_refetched_when_the_dict_is_trimmed(self):
        """Callers often pass a slim lead dict; the row still has the files."""
        ctx = _ctx()
        fwd = mock.AsyncMock()
        fake_db = mock.MagicMock()
        fake_db.get_lead_by_id.return_value = _lead_with_files()
        with mock.patch.object(bot, "_forward_phase1_attached_files_to_targets", fwd), \
                mock.patch.object(bot, "db", fake_db):
            asyncio.run(bot._forward_accepted_lead_files(
                ctx, {"id": "lead-1", "reference_id": "REF123"}, DRIVER_CHAT))
        fwd.assert_awaited_once()
        fake_db.get_lead_by_id.assert_called_once_with("lead-1")

    def test_not_sent_twice_to_the_same_driver(self):
        ctx = _ctx()
        fwd = mock.AsyncMock()
        with mock.patch.object(bot, "_forward_phase1_attached_files_to_targets", fwd):
            asyncio.run(bot._forward_accepted_lead_files(ctx, _lead_with_files(), DRIVER_CHAT))
            asyncio.run(bot._forward_accepted_lead_files(ctx, _lead_with_files(), DRIVER_CHAT))
        self.assertEqual(fwd.await_count, 1, "re-sent details must not duplicate the files")

    def test_no_files_is_a_no_op(self):
        ctx = _ctx()
        fwd = mock.AsyncMock()
        fake_db = mock.MagicMock()
        fake_db.get_lead_by_id.return_value = {"id": "lead-1"}
        with mock.patch.object(bot, "_forward_phase1_attached_files_to_targets", fwd), \
                mock.patch.object(bot, "db", fake_db):
            asyncio.run(bot._forward_accepted_lead_files(ctx, _lead_with_files([]), DRIVER_CHAT))
        fwd.assert_not_awaited()

    def test_a_storage_error_never_breaks_the_accept(self):
        ctx = _ctx()
        fake_db = mock.MagicMock()
        fake_db.get_lead_by_id.side_effect = RuntimeError("supabase down")
        with mock.patch.object(bot, "db", fake_db):
            asyncio.run(bot._forward_accepted_lead_files(
                ctx, {"id": "lead-1"}, DRIVER_CHAT))      # must not raise

    def test_details_message_triggers_the_forward(self):
        """The hook lives in the one place accepted-lead details reach a driver."""
        ctx = _ctx()
        fwd = mock.AsyncMock()
        with mock.patch.object(bot, "_forward_accepted_lead_files", fwd), \
                mock.patch.object(bot, "_build_driver_lead_accepted_message_html",
                                  mock.MagicMock(return_value="details")), \
                mock.patch.object(bot, "_driver_keyboard_after_accept",
                                  mock.MagicMock(return_value=None)):
            asyncio.run(bot._send_driver_lead_details(ctx, _lead_with_files(), DRIVER_CHAT))
        fwd.assert_awaited_once()
        self.assertEqual(fwd.await_args.args[2], DRIVER_CHAT)


class FilesSurviveGroupAcceptTest(unittest.TestCase):
    """The group accept must no longer wipe the files off the lead row."""

    def test_group_accept_does_not_clear_attached_files(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        self.assertNotIn(
            'db.update_lead(lead_id, {"phase1_attached_files": []})', src,
            "clearing the files leaves the accepting driver with no paperwork",
        )


class ColorPhotoIsAttachedTest(unittest.TestCase):
    """A picture sent to read the colour is still a file sent for parsing."""

    def test_color_photo_is_added_as_an_attachment(self):
        ctx = _ctx()
        added = bot._add_extra_attachment(
            ctx, "photo", "image/jpeg", "color.jpg", b"bytes", "🎨 Colour reference")
        self.assertIsNone(added)                       # no cap hit
        extras = ctx.user_data.get("phase1_extra_attachments")
        self.assertEqual(len(extras), 1)
        self.assertEqual(extras[0]["type"], "photo")


if __name__ == "__main__":
    unittest.main()
