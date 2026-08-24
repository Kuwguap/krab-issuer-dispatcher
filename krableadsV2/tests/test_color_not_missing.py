r"""A colour that is on the card is never called missing.

Reported: "I entered color white it says white but it says You missed out the
vehicle color."

Anything outside a short hand-written word list was handed to an AI asking "was a
colour really given?", and that answer is a coin flip. Twelve of the DMV codes the
bot itself prints on the tag were outside that list — WHI, for white, among them.

Two more faults in the same flow: it worked off a SNAPSHOT of the card taken when
the question was asked, so anything edited in the meantime was invisible (and was
overwritten when the snapshot got saved back); and the answer was stored raw, so
replying "color white" stored the words "color white".

Run:  venv\Scripts\python.exe -m pytest tests/test_color_not_missing.py -q
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
from utils import ai_vision, tag_pdf  # noqa: E402

CARD = {"name": "John Damian", "vin": "1HGCM82633A004352",
        "car": "2019 Honda Accord", "insurance_company": "GEICO", "extra_info": "-"}


def _missing(color):
    card = dict(CARD, color=color)
    blob = "\n".join(str(card.get(k) or "") for k in ("name", "vin", "car", "color"))
    return ai_vision.detect_missing_fields(card, blob)


class WhiteIsAColourTest(unittest.TestCase):

    def test_the_reported_case(self):
        self.assertNotIn("color", _missing("White"))

    def test_however_it_is_written(self):
        for said in ("White", "white", "WHITE", "Pearl White", "off white"):
            with self.subTest(said=said):
                self.assertNotIn("color", _missing(said), said)

    def test_the_code_the_bot_itself_prints(self):
        """WHI is what goes on the tag for white — it was not on the trusted list."""
        for code in ("WHI", "whi", "WHT", "wht"):
            with self.subTest(code=code):
                self.assertNotIn("color", _missing(code), code)


class NothingWeOfferIsSecondGuessedTest(unittest.TestCase):
    """The AI re-check is a coin flip, so our own vocabulary must never reach it."""

    def test_every_colour_on_the_picker_is_trusted(self):
        untrusted = [c for c in bot._PH1_COLORS if not ai_vision._color_is_trusted(c)]
        self.assertEqual([], untrusted, f"picker colours sent to the AI: {untrusted}")

    def test_every_dmv_code_the_bot_writes_is_trusted(self):
        codes = sorted(set(tag_pdf._COLOR_MAP.values()))
        untrusted = [c for c in codes if not ai_vision._color_is_trusted(c)]
        self.assertEqual([], untrusted, f"tag codes sent to the AI: {untrusted}")

    def test_the_words_people_actually_type_are_trusted(self):
        for said in ("black", "silver", "gray", "grey", "beige", "navy", "maroon",
                     "burgundy", "champagne", "teal", "copper", "pink", "amethyst"):
            with self.subTest(said=said):
                self.assertTrue(ai_vision._color_is_trusted(said), said)


class AnEmptyColourIsStillAskedForTest(unittest.TestCase):
    """The check has to keep working — this is not "never ask"."""

    def test_blank_and_dash_are_missing(self):
        for said in ("", "-", "   "):
            with self.subTest(said=said):
                self.assertIn("color", _missing(said))

    def test_a_placeholder_is_missing(self):
        self.assertIn("color", _missing("n/a"))


class TheAnswerIsParsedTest(unittest.TestCase):
    """Answering "color white" must store White, not the words."""

    def _answer(self, text, card, missing=("color",)):
        msg = mock.MagicMock()
        msg.text, msg.chat_id = text, 1
        msg.reply_text = mock.AsyncMock(return_value=mock.MagicMock(message_id=9))
        msg.delete = mock.AsyncMock()
        update = mock.MagicMock()
        update.message = update.effective_message = msg
        update.effective_user = mock.MagicMock(id=7)
        update.effective_chat = mock.MagicMock(id=1)
        ctx = mock.MagicMock()
        ctx.user_data = {"missing_fields": list(missing), "missing_field_state_data": {}}
        db, saved = mock.MagicMock(), {}
        db.get_user_state.return_value = {"state": "phase1", "data": dict(card)}
        db.set_user_state.side_effect = lambda u, s, d: saved.update(d or {})
        with mock.patch.object(bot, "db", db), \
                mock.patch.object(bot, "_autoclean_user_msg", mock.AsyncMock()), \
                mock.patch.object(bot, "_clear_missing_prompts", mock.AsyncMock()), \
                mock.patch.object(bot, "_track_missing_prompt", mock.MagicMock()), \
                mock.patch.object(bot, "_ensure_phone_price_before_files",
                                  mock.AsyncMock(return_value=99)), \
                mock.patch.object(bot.Config, "is_ai_vision_configured",
                                  classmethod(lambda cls: False)):
            asyncio.run(bot.handle_missing_field(update, ctx))
        asked = [c[0][0] for c in msg.reply_text.await_args_list]
        return saved, asked

    def test_the_label_is_not_stored_as_the_value(self):
        saved, _ = self._answer("color white", dict(CARD, color="-"))
        self.assertEqual("White", saved.get("color"))

    def test_a_bare_colour_works_too(self):
        saved, _ = self._answer("white", dict(CARD, color="-"))
        self.assertEqual("White", saved.get("color"))

    def test_it_does_not_ask_again(self):
        _, asked = self._answer("color white", dict(CARD, color="-"))
        self.assertEqual([], [a for a in asked if "color" in a.lower()], asked)


class ItReadsTheLiveCardTest(unittest.TestCase):
    """The card stays editable while the question is on screen."""

    def test_a_colour_set_in_the_meantime_is_seen(self):
        self.assertTrue(bot._field_already_filled(dict(CARD, color="White"), "color"))

    def test_an_unset_colour_is_still_missing(self):
        self.assertFalse(bot._field_already_filled(dict(CARD, color="-"), "color"))

    def test_other_fields_too(self):
        self.assertTrue(bot._field_already_filled({"vin": "1HGCM82633A004352"}, "vin"))
        self.assertFalse(bot._field_already_filled({"vin": "-"}, "vin"))
        self.assertTrue(bot._field_already_filled({"extra_info": "tomorrow"}, "delivery_date"))

    def test_the_snapshot_is_no_longer_the_source_of_truth(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        self.assertIn("live = db.get_user_state(user_id)", src)

    def test_every_ask_site_checks_the_card_first(self):
        """The three detect_missing_fields sites, plus the re-prompt filter."""
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        self.assertEqual(4, src.count("_field_already_filled(state_data, f)"))


if __name__ == "__main__":
    unittest.main()
