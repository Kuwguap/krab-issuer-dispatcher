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

    def _amount_buttons(self):
        """The amounts only — the toll toggle rides the same prefix."""
        return [b for b in self._buttons()
                if b.callback_data.startswith(bot.PH1_PRICE_CB)
                and b.callback_data != bot.PH1_PRICE_CB + bot.PH1_PRICE_TOLL]

    def test_exactly_the_six_prices_asked_for(self):
        self.assertEqual([f"${p}" for p in ASKED_FOR],
                         [b.text for b in self._amount_buttons()])

    def test_each_button_carries_its_own_amount(self):
        for b in self._amount_buttons():
            self.assertEqual(b.text, "$" + b.callback_data.replace(bot.PH1_PRICE_CB, "", 1))

    def test_there_is_a_way_out(self):
        self.assertIn("edit_cancel", [b.callback_data for b in self._buttons()])

    def test_the_prompt_names_all_three_routes(self):
        for word in ("type", "speak", "tap"):
            self.assertIn(word, bot._PH1_PRICE_PROMPT.lower(), word)


class TappingAPriceTest(unittest.TestCase):

    def _tap(self, amount="150", card=None, user_data=None):
        query = SimpleNamespace(
            data=f"{bot.PH1_PRICE_CB}{amount}",
            edit_message_reply_markup=mock.AsyncMock(),
            from_user=SimpleNamespace(id=7),
            answer=mock.AsyncMock(),
            message=SimpleNamespace(chat_id=1, message_id=3, delete=mock.AsyncMock()))
        update = SimpleNamespace(callback_query=query)
        ud = {"phase1_pending_edit_key": "price", "edit_prompt_msg_id": 3}
        ud.update(user_data or {})
        ctx = SimpleNamespace(user_data=ud, bot=mock.AsyncMock())
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
        return state, saved, vanished, picker, query, ctx

    def test_the_price_lands_on_the_card(self):
        _, saved, _, _, _, _ = self._tap("150")
        self.assertEqual("$150", saved.get("pending_price"))

    def test_the_dollar_sign_is_added(self):
        """Without it the sanitizer drops the value and Phase 2 asks again."""
        _, saved, _, _, _, _ = self._tap("90")
        self.assertTrue(bot._is_valid_pending_price(saved.get("pending_price")))

    def test_every_offered_price_survives_the_sanitizer(self):
        for amount in ASKED_FOR:
            with self.subTest(amount=amount):
                _, saved, _, _, _, _ = self._tap(amount)
                self.assertEqual(f"${amount}", saved.get("pending_price"))

    def test_it_goes_back_to_the_edit_picker(self):
        state, _, _, picker, _, _ = self._tap()
        picker.assert_awaited()
        self.assertEqual(bot.STATE_AI_REVIEW, state)

    def test_the_picker_message_is_cleared_away(self):
        _, _, _, _, query, _ = self._tap()
        query.message.delete.assert_awaited()

    def test_it_confirms_what_it_set(self):
        _, _, vanished, _, _, _ = self._tap("200")
        self.assertTrue(any("$200" in v for v in vanished), vanished)

    def test_a_tap_on_an_empty_card_does_not_crash(self):
        """The bug class that ate single edits: a missing key blowing up the handler."""
        _, saved, _, _, _, _ = self._tap("120", card={})
        self.assertEqual("$120", saved.get("pending_price"))


class PlusTollTest(unittest.TestCase):
    r"""Sometimes it's 150, sometimes it's 150 + toll — one toggle covers both."""

    def _toll_button(self, toll):
        for row in bot._price_picker_keyboard(toll).inline_keyboard:
            for b in row:
                if b.callback_data == bot.PH1_PRICE_CB + bot.PH1_PRICE_TOLL:
                    return b
        return None

    def test_the_toggle_is_offered(self):
        self.assertIsNotNone(self._toll_button(False))

    def test_it_says_which_way_it_is_set(self):
        self.assertIn("toll", self._toll_button(False).text.lower())
        self.assertNotEqual(self._toll_button(False).text, self._toll_button(True).text)

    def test_toll_then_price_quotes_both(self):
        """Tap +toll on an empty price, then tap 150."""
        _, saved, _, _, _, _ = TappingAPriceTest()._tap(
            "150", card={}, user_data={"phase1_price_toll": True})
        self.assertEqual("$150 + toll", saved.get("pending_price"))

    def test_price_alone_stays_alone(self):
        _, saved, _, _, _, _ = TappingAPriceTest()._tap("150", card={})
        self.assertEqual("$150", saved.get("pending_price"))

    def test_arming_toll_first_keeps_the_picker_open(self):
        state, saved, _, picker, query, ctx = TappingAPriceTest()._tap(
            bot.PH1_PRICE_TOLL, card={})
        self.assertIsNone(state, "the amount still has to be tapped")
        self.assertTrue(ctx.user_data.get("phase1_price_toll"))
        query.edit_message_reply_markup.assert_awaited()
        query.message.delete.assert_not_awaited()
        picker.assert_not_awaited()

    def test_toll_on_a_price_already_set_finishes_the_edit(self):
        state, saved, vanished, picker, query, _ = TappingAPriceTest()._tap(
            bot.PH1_PRICE_TOLL, card={"pending_price": "$150"})
        self.assertEqual("$150 + toll", saved.get("pending_price"))
        self.assertEqual(bot.STATE_AI_REVIEW, state)
        picker.assert_awaited()
        self.assertTrue(any("toll" in v for v in vanished), vanished)

    def test_tapping_it_again_takes_the_toll_back_off(self):
        _, saved, _, _, _, _ = TappingAPriceTest()._tap(
            bot.PH1_PRICE_TOLL, card={"pending_price": "$150 + toll"})
        self.assertEqual("$150", saved.get("pending_price"))

    def test_changing_the_amount_keeps_the_toll(self):
        _, saved, _, _, _, _ = TappingAPriceTest()._tap(
            "200", card={"pending_price": "$150 + toll"})
        self.assertEqual("$200 + toll", saved.get("pending_price"))

    def test_a_toll_price_is_still_a_valid_price(self):
        """Otherwise the sanitizer drops it and Phase 2 asks for the price again."""
        self.assertTrue(bot._is_valid_pending_price("$150 + toll"))
        card = {"pending_price": "$150 + toll"}
        bot._sanitize_phase1_pending_phone_price(card)
        self.assertEqual("$150 + toll", card.get("pending_price"))

    def test_typed_and_spoken_tolls_are_understood(self):
        for said in ("150 + toll", "150 plus toll", "$150 plus tolls",
                     "price 150 and toll", "150+toll"):
            with self.subTest(said=said):
                self.assertEqual("$150 + toll", bot._clean_inline_value("price", said))

    def test_a_price_read_off_a_screenshot_keeps_its_toll(self):
        self.assertEqual("$150 + toll", bot._normalize_ai_price("150 + toll"))
        self.assertEqual("$150", bot._normalize_ai_price("150"))

    def test_the_toggle_opens_matching_whatever_the_card_says(self):
        ctx = SimpleNamespace(user_data={})
        on = bot._fresh_price_picker(ctx, {"pending_price": "$150 + toll"})
        off = bot._fresh_price_picker(ctx, {"pending_price": "$150"})
        self.assertNotEqual(on.inline_keyboard[2][0].text, off.inline_keyboard[2][0].text)

    def test_opening_the_prompt_clears_a_stale_armed_toll(self):
        ctx = SimpleNamespace(user_data={"phase1_price_toll": True})
        bot._fresh_price_picker(ctx, {})
        self.assertNotIn("phase1_price_toll", ctx.user_data)


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
        """Edit -> Price is reachable from the review card and from the edit menu."""
        self.assertEqual(2, self._source().count("reply_markup=_fresh_price_picker("))


if __name__ == "__main__":
    unittest.main()
