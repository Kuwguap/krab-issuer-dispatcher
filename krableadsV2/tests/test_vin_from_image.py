"""A picture of a VIN sent to the review card.

Sending an image that reads "VIN:<vin>" did nothing: the 11-line parser only reads its
own VIN line, and a VIN of the wrong length was blanked to "-" while the toast still
claimed "Read: VIN". The DMV decode never ran either.

Run:  venv\\Scripts\\python.exe -m pytest tests/test_vin_from_image.py -q
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
from utils import ai_vision  # noqa: E402

GOOD_VIN = "4S4BSAAC9J3259647"      # 17 characters
BAD_VIN = "4S4BSAACC9J3259647"      # 18 — a doubled character, as mis-read


def ai_block(vin):
    """What the extractor returns for an image that is only a VIN: line 6 filled."""
    lines = ["-"] * 11
    lines[5] = vin
    return "\n".join(lines)


def _drive(ai_reply, start_vin="-"):
    """Push an image through the real review-upload handler; report what landed."""
    msg = SimpleNamespace(
        text=None, caption=None, chat_id=1,
        photo=[SimpleNamespace(file_id="f1")], document=None,
        delete=mock.AsyncMock(),
        reply_text=mock.AsyncMock(return_value=SimpleNamespace(message_id=9, chat_id=1)),
    )
    update = SimpleNamespace(
        message=msg, effective_message=msg,
        effective_chat=SimpleNamespace(id=1, type="private"),
        effective_user=SimpleNamespace(id=7, username="tester"),
    )
    tg_file = mock.AsyncMock()
    tg_file.download_to_memory = mock.AsyncMock()
    ctx = SimpleNamespace(user_data={"review_message_id": 5, "review_chat_id": 1},
                          bot=mock.AsyncMock(), application=SimpleNamespace(handlers={}))
    ctx.bot.get_file = mock.AsyncMock(return_value=tg_file)

    fake_db = mock.MagicMock()
    fake_db.get_user_state.return_value = {"state": "phase1",
                                           "data": {"vin": start_vin, "car": "-"}}
    saved = {}
    fake_db.set_user_state.side_effect = lambda u, s, d: saved.update(d or {})
    vin_check = mock.AsyncMock(return_value=bot.STATE_AI_REVIEW)

    with mock.patch.object(bot, "db", fake_db), \
            mock.patch.object(bot.ai_vision, "extract_structured_from_media_parts",
                              mock.MagicMock(return_value=ai_reply)), \
            mock.patch.object(bot, "_send_vanishing", mock.AsyncMock()), \
            mock.patch.object(bot, "_reanchor_review_card", mock.AsyncMock()), \
            mock.patch.object(bot, "_autoclean_user_msg", mock.AsyncMock()), \
            mock.patch.object(bot, "_add_extra_attachment", mock.MagicMock(return_value=None)), \
            mock.patch.object(bot, "_run_vin_check_for_review", vin_check):
        asyncio.run(bot.handle_phase1_adjust_input(update, ctx))

    warnings = [c.args[0] for c in msg.reply_text.await_args_list
                if c.args and "VIN" in str(c.args[0])]
    return {"vin": saved.get("vin"), "checks": vin_check.await_count, "warnings": warnings}


class VinFromImageTest(unittest.TestCase):

    def test_valid_vin_lands_and_runs_the_dmv_check(self):
        r = _drive(ai_block(GOOD_VIN))
        self.assertEqual(r["vin"], GOOD_VIN)
        self.assertEqual(r["checks"], 1, "the DMV decode must run automatically")
        self.assertEqual(r["warnings"], [])

    def test_vin_written_in_prose_is_still_found(self):
        """The extractor does not always use the VIN line."""
        r = _drive(f"Scan text. VIN: {GOOD_VIN} . nothing else")
        self.assertEqual(r["vin"], GOOD_VIN)
        self.assertEqual(r["checks"], 1)

    def test_wrong_length_vin_is_reported_not_silently_blanked(self):
        r = _drive(ai_block(BAD_VIN))
        self.assertEqual(r["vin"], "-", "an 18-character VIN must not be saved")
        self.assertEqual(r["checks"], 0)
        self.assertTrue(r["warnings"], "the issuer must be told it was rejected")
        said = r["warnings"][0]
        self.assertIn(BAD_VIN, said)
        self.assertIn("18 characters", said)

    def test_same_vin_resent_does_not_re_run_the_check(self):
        r = _drive(ai_block(GOOD_VIN), start_vin=GOOD_VIN)
        self.assertEqual(r["checks"], 0)

    def test_image_without_a_vin_is_quiet(self):
        r = _drive(ai_block("-"))
        self.assertEqual(r["checks"], 0)
        self.assertEqual(r["warnings"], [])


class VinScanHelpersTest(unittest.TestCase):

    def test_strict_scan_finds_a_vin_anywhere_in_the_reply(self):
        for text in (f"VIN:{GOOD_VIN}", ai_block(GOOD_VIN),
                     f"the number is {GOOD_VIN} on the sticker"):
            self.assertEqual(ai_vision.vin_from_text(text), GOOD_VIN, text[:30])

    def test_near_miss_only_fires_for_a_wrong_length(self):
        self.assertEqual(ai_vision.vin_near_miss_from_text(f"VIN:{BAD_VIN}"), BAD_VIN)
        self.assertIsNone(ai_vision.vin_near_miss_from_text(f"VIN:{GOOD_VIN}"))

    def test_ai_vin_line_reads_the_parsers_own_line(self):
        """Used for the warning when the block carries no "VIN:" label."""
        self.assertEqual(bot._ai_vin_line(ai_block(BAD_VIN)), BAD_VIN)
        self.assertEqual(bot._ai_vin_line(ai_block("-")), "")

    def test_auto_check_is_a_no_op_for_an_unusable_vin(self):
        for vin in ("-", "", BAD_VIN):
            with self.subTest(vin=vin):
                called = mock.AsyncMock()
                with mock.patch.object(bot, "_handle_phase1_vin_check_button", called):
                    state = asyncio.run(bot._run_vin_check_for_review(
                        None, SimpleNamespace(user_data={}), 7, {"vin": vin}))
                called.assert_not_awaited()
                self.assertEqual(state, bot.STATE_AI_REVIEW)


if __name__ == "__main__":
    unittest.main()
