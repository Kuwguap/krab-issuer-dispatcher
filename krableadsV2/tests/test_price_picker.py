r"""Edit -> Price offers tap-a-price, and still takes typing and voice.

Asked for: "under edit button under price click or type or say prices use in line
button / 90 / 100 / 120 / 150 / 200 / 250".

Run:  venv\Scripts\python.exe -m pytest tests/test_price_picker.py -q
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

ASKED_FOR = ["90", "100", "120", "150", "200", "250"]


class PriceKeyboardTest(unittest.TestCase):

    def _buttons(self):
        return [b for row in bot._price_picker_keyboard().inline_keyboard for b in row]

    def test_exactly_the_six_prices_asked_for(self):
        prices = [b.text for b in self._buttons() if b.callback_data.startswith(bot.PH1_PRICE_CB)]
        self.assertEqual([f"${p}" for p in ASKED_FOR], prices)

    def test_each_button_carries_its_own_amount(self):
        for b in self._buttons():
            if b.callback_data.startswith(bot.PH1_PRICE_CB):
                self.assertEqual(b.text, "$" + b.callback_data.replace(bot.PH1_PRICE_CB, "", 1))

    def test_there_is_a_way_out(self):
        self.assertIn("edit_cancel", [b.callback_data for b in self._buttons()])

    def test_the_prompt_names_all_three_routes(self):
        for word in ("type", "speak", "tap"):
            self.assertIn(word, bot._PH1_PRICE_PROMPT.lower(), word)


class TappingAPriceTest(unittest.TestCase):

    def _tap(self, amount="150", card=None):
        query = SimpleNamespace(
            data=f"{bot.PH1_PRICE_CB}{amount}",
            from_user=SimpleNamespace(id=7),
            answer=mock.AsyncMock(),
            message=SimpleNamespace(chat_id=1, message_id=3, delete=mock.AsyncMock()))
        update = SimpleNamespace(callback_query=query)
        ctx = SimpleNamespace(user_data={"phase1_pending_edit_key": "price",
                                         "edit_prompt_msg_id": 3},
                              bot=mock.AsyncMock())
        fake_db = mock.MagicMock()
        state_data = dict(card if card is not None else {"name": "John Damian"})
        fake_db.get_user_state.return_value = {"state": "phase1", "data": state_data}
        saved, vanished = {}, []
        fake_db.set_user_state.side_effect = lambda u, s, d: saved.update(d or {})
        with mock.patch.object(bot, "db", fake_db), \
                mock.patch.object(bot, "_show_edit_picker", mock.AsyncMock()) as picker, \
                mock.patch.object(bot, "_send_vanishing",
                                  mock.AsyncMock(side_effect=lambda c, ch, t, **k: vanished.append(t))):
            state = asyncio.run(bot.handle_phase1_price_pick(update, ctx))
        return state, saved, vanished, picker, query

    def test_the_price_lands_on_the_card(self):
        _, saved, _, _, _ = self._tap("150")
        self.assertEqual("$150", saved.get("pending_price"))

    def test_the_dollar_sign_is_added(self):
        """Without it the sanitizer drops the value and Phase 2 asks again."""
        _, saved, _, _, _ = self._tap("90")
        self.assertTrue(bot._is_valid_pending_price(saved.get("pending_price")))

    def test_every_offered_price_survives_the_sanitizer(self):
        for amount in ASKED_FOR:
            with self.subTest(amount=amount):
                _, saved, _, _, _ = self._tap(amount)
                self.assertEqual(f"${amount}", saved.get("pending_price"))

    def test_it_goes_back_to_the_edit_picker(self):
        state, _, _, picker, _ = self._tap()
        picker.assert_awaited()
        self.assertEqual(bot.STATE_AI_REVIEW, state)

    def test_the_picker_message_is_cleared_away(self):
        _, _, _, _, query = self._tap()
        query.message.delete.assert_awaited()

    def test_it_confirms_what_it_set(self):
        _, _, vanished, _, _ = self._tap("200")
        self.assertTrue(any("$200" in v for v in vanished), vanished)

    def test_a_tap_on_an_empty_card_does_not_crash(self):
        """The bug class that ate single edits: a missing key blowing up the handler."""
        _, saved, _, _, _ = self._tap("120", card={})
        self.assertEqual("$120", saved.get("pending_price"))


class TypedAndSpokenStillWorkTest(unittest.TestCase):
    """The buttons are an addition — the old routes must not regress."""

    def test_a_bare_number_typed_at_the_prompt_becomes_a_price(self):
        self.assertEqual("$175", bot._clean_inline_value("price", "175"))

    def test_a_spoken_amount_is_understood(self):
        for spoken in ("$150", "150 dollars", "price is 150", "one fifty".replace("one fifty", "150")):
            with self.subTest(spoken=spoken):
                self.assertEqual("$150", bot._clean_inline_value("price", spoken))

    def test_prose_with_no_number_is_not_a_price(self):
        self.assertEqual("", bot._clean_inline_value("price", "whatever you think"))


class WiringTest(unittest.TestCase):
    """Registered everywhere the palette is — a picker in an unregistered state is dead."""

    def _source(self):
        return (ROOT / "bot.py").read_text(encoding="utf-8")

    def test_registered_as_often_as_the_colour_palette(self):
        src = self._source()
        self.assertEqual(src.count("CallbackQueryHandler(handle_phase1_color_pick"),
                         src.count("CallbackQueryHandler(handle_phase1_price_pick"))

    def test_both_edit_prompts_offer_the_picker(self):
        self.assertEqual(2, self._source().count("_PH1_PRICE_PROMPT, reply_markup=_price_picker_keyboard()"))


if __name__ == "__main__":
    unittest.main()
