r"""The "price missing" gate offers the same buttons as Edit -> Price.

Asked for: "when asking for price put inline buttons like how it is in price edit,
and accept price without dollar sign".

Run:  venv\Scripts\python.exe -m pytest tests/test_phase2_price_buttons.py -q
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

PHONE = "+15513013737"
CARD = {"name": "John Damian"}


def _ask(card):
    msg = mock.MagicMock()
    msg.reply_text = mock.AsyncMock()
    ctx = mock.MagicMock()
    ctx.user_data = {}
    state = asyncio.run(bot._phase2_ask(msg, ctx, card))
    kb = msg.reply_text.await_args.kwargs.get("reply_markup")
    labels = [b.text for row in kb.inline_keyboard for b in row] if kb else None
    return msg.reply_text.await_args[0][0], labels, ctx.user_data, state


def _phase2(text, card):
    msg = mock.MagicMock()
    msg.text, msg.chat_id = text, 1
    msg.reply_text = mock.AsyncMock()
    msg.delete = mock.AsyncMock()
    update = mock.MagicMock()
    update.message = update.effective_message = msg
    update.effective_user = mock.MagicMock(id=7)
    update.effective_chat = mock.MagicMock(id=1)
    ctx = mock.MagicMock()
    ctx.user_data = {}
    db, saved = mock.MagicMock(), {}
    db.get_user_state.return_value = {"state": "phase1", "data": dict(card)}
    db.set_user_state.side_effect = lambda u, s, d: saved.update(d or {})
    with mock.patch.object(bot, "db", db), \
            mock.patch.object(bot, "_autoclean_user_msg", mock.AsyncMock()), \
            mock.patch.object(bot, "_prompt_issuer_special_request",
                              mock.AsyncMock(return_value="FORWARD")):
        state = asyncio.run(bot.handle_phase2(update, ctx))
    return state, saved


class ThePriceGateOffersTheButtonsTest(unittest.TestCase):

    def test_the_same_six_amounts_as_the_edit_picker(self):
        _, labels, _, _ = _ask(dict(CARD, pending_phone_number=PHONE))
        for amount in ("$90", "$100", "$120", "$150", "$200", "$250"):
            self.assertIn(amount, labels, amount)

    def test_the_toll_toggle_comes_along(self):
        _, labels, _, _ = _ask(dict(CARD, pending_phone_number=PHONE))
        self.assertTrue(any("toll" in l.lower() for l in labels), labels)

    def test_the_prompt_says_a_bare_number_is_fine(self):
        said, _, _, _ = _ask(dict(CARD, pending_phone_number=PHONE))
        self.assertIn("150 or $150", said)

    def test_it_stays_in_phase_two(self):
        _, _, _, state = _ask(dict(CARD, pending_phone_number=PHONE))
        self.assertEqual(bot.STATE_PHASE2, state)

    def test_no_buttons_when_the_phone_is_what_is_missing(self):
        """Tapping a price would not answer the question being asked."""
        _, labels, ud, _ = _ask(dict(CARD, pending_price="$150"))
        self.assertIsNone(labels)
        self.assertNotIn("phase2_awaiting_price", ud)

    def test_no_buttons_when_both_are_missing(self):
        _, labels, _, _ = _ask(dict(CARD))
        self.assertIsNone(labels)


class TappingAPriceCarriesTheDispatchOnTest(unittest.TestCase):
    """Not back to the review card — the lead was mid-send."""

    def _tap(self, amount="150", card=None, awaiting=True):
        query = mock.MagicMock()
        query.data = f"{bot.PH1_PRICE_CB}{amount}"
        query.from_user = mock.MagicMock(id=7)
        query.message = mock.MagicMock(chat_id=1, message_id=3)
        query.message.delete = mock.AsyncMock()
        query.message.reply_text = mock.AsyncMock()
        query.answer = mock.AsyncMock()
        update = mock.MagicMock()
        update.callback_query = query
        ctx = mock.MagicMock()
        ctx.user_data = {"phase2_awaiting_price": True} if awaiting else {}
        db, saved, states = mock.MagicMock(), {}, []
        db.get_user_state.return_value = {"state": "phase1",
                                          "data": dict(card if card is not None else CARD)}

        def _set(u, st, d):
            states.append(st)
            saved.update(d or {})
        db.set_user_state.side_effect = _set
        with mock.patch.object(bot, "db", db), \
                mock.patch.object(bot, "_show_edit_picker", mock.AsyncMock()) as picker, \
                mock.patch.object(bot, "_send_vanishing", mock.AsyncMock()), \
                mock.patch.object(bot, "_prompt_issuer_special_request",
                                  mock.AsyncMock(return_value="FORWARD")) as onward:
            state = asyncio.run(bot.handle_phase1_price_pick(update, ctx))
        return state, saved, states, picker, onward

    def test_it_moves_the_lead_forward(self):
        state, saved, _, picker, onward = self._tap(
            card=dict(CARD, pending_phone_number=PHONE))
        self.assertEqual("$150", saved.get("pending_price"))
        onward.assert_awaited()
        picker.assert_not_awaited()
        self.assertEqual("FORWARD", state)

    def test_the_flag_is_spent(self):
        query_ctx = self._tap(card=dict(CARD, pending_phone_number=PHONE))
        self.assertEqual("FORWARD", query_ctx[0])

    def test_a_still_missing_phone_asks_again_rather_than_sending(self):
        state, saved, _, _, onward = self._tap(card=dict(CARD))
        self.assertEqual("$150", saved.get("pending_price"))
        onward.assert_not_awaited()
        self.assertEqual(bot.STATE_PHASE2, state)

    def test_the_review_card_path_is_untouched(self):
        """Tapped under Edit -> Price, it still returns to the edit picker."""
        state, _, _, picker, onward = self._tap(
            card=dict(CARD, pending_phone_number=PHONE), awaiting=False)
        picker.assert_awaited()
        onward.assert_not_awaited()
        self.assertEqual(bot.STATE_AI_REVIEW, state)


class APriceWithNoDollarSignTest(unittest.TestCase):

    def test_a_bare_number_when_only_the_price_is_missing(self):
        state, saved = _phase2("150", dict(CARD, pending_phone_number=PHONE))
        self.assertEqual("$150", saved.get("pending_price"))
        self.assertEqual("FORWARD", state)

    def test_a_bare_number_when_both_are_missing(self):
        """Too short to be the phone we are also waiting for, so it is the price."""
        state, saved = _phase2("150", dict(CARD))
        self.assertEqual("$150", saved.get("pending_price"))
        self.assertEqual(bot.STATE_PHASE2, state, "the phone is still outstanding")

    def test_a_phone_is_still_read_as_a_phone(self):
        _, saved = _phase2("5513013737", dict(CARD))
        self.assertEqual(PHONE, saved.get("pending_phone_number"))
        self.assertIsNone(saved.get("pending_price"))

    def test_both_in_one_message_still_works(self):
        state, saved = _phase2("5513013737 $150", dict(CARD))
        self.assertEqual("$150", saved.get("pending_price"))
        self.assertEqual(PHONE, saved.get("pending_phone_number"))
        self.assertEqual("FORWARD", state)

    def test_the_dollar_sign_is_still_accepted(self):
        _, saved = _phase2("$150", dict(CARD, pending_phone_number=PHONE))
        self.assertEqual("$150", saved.get("pending_price"))


class AnEmptyCardDoesNotEndTheLeadTest(unittest.TestCase):
    """`{}` is a real card — ending the lead over it is the old silent-drop shape."""

    def test_an_empty_card_is_still_answerable(self):
        state, saved = _phase2("150", {})
        self.assertNotEqual(bot.ConversationHandler.END, state)
        self.assertEqual("$150", saved.get("pending_price"))


if __name__ == "__main__":
    unittest.main()
