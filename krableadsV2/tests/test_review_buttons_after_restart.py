r"""A review card outlives the process that drew it — its buttons must still work.

Reported: "edit button not working and all inline buttons on review". Every deploy
restarts the bot, and a restart drops BOTH things the card depends on:

  * the in-memory conversation, so the review callbacks — registered only inside
    STATE_AI_REVIEW — were never consulted (entry points are all an idle
    conversation looks at, and none of them matched ph1_edit / ph1_accept); and
  * context.user_data, so the handlers that DID run could not find the message id
    they edit the card by, and returned without a word.

Both faults are silent, which is why it looked like the buttons were simply dead.

This drives the REAL handler graph built by bot.main(), with a card sitting in
Supabase and a brand-new Application that has never seen the user — exactly the
state a redeploy leaves behind.

Run:  venv\Scripts\python.exe -m pytest tests/test_review_buttons_after_restart.py -q
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
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11")
os.environ.pop("OPENAI_API_KEY", None)

# Reuse the end-to-end harness (real Application, faked transport and Supabase).
from test_real_routing_e2e import (  # noqa: E402
    CHAT_ID, USER_ID, FAKE_DB, TRANSPORT, _build_application, _REVIEW_DATA,
)

import bot  # noqa: E402
import telegram  # noqa: E402
from telegram import Update  # noqa: E402

CARD_MID = 4242


def _card_keyboard():
    """The review card's own keyboard, as the tap arrives carrying it."""
    return bot._build_review_keyboard_with_selections(dict(_REVIEW_DATA)).to_dict()


def _tap(app, data, mid=CARD_MID, keyboard=None):
    return Update.de_json({
        "update_id": mid + 1,
        "callback_query": {
            "id": str(mid),
            "chat_instance": "ci",
            "from": {"id": USER_ID, "is_bot": False, "first_name": "Tester", "username": "tester"},
            "data": data,
            "message": {
                "message_id": mid,
                "date": 1700000000,
                "chat": {"id": CHAT_ID, "type": "private"},
                "from": {"id": 1, "is_bot": True, "first_name": "TestBot", "username": "testbot"},
                "text": "Review card",
                "reply_markup": keyboard if keyboard is not None else _card_keyboard(),
            },
        },
    }, app.bot)


def _after_restart(data, keyboard=None):
    """Push one tap through a freshly built Application that has never seen this
    user — no conversation, no user_data — with the lead still in Supabase."""
    async def run():
        app = _build_application()
        with mock.patch.object(telegram.Bot, "_do_post", TRANSPORT.do_post):
            await app.initialize()
            FAKE_DB.states.clear()
            FAKE_DB.set_user_state(USER_ID, "phase1", dict(_REVIEW_DATA))
            TRANSPORT.reset()
            await app.process_update(_tap(app, data, keyboard=keyboard))
            ud = app.user_data.get(USER_ID, {})
            await app.shutdown()
        return list(TRANSPORT.calls), dict(ud)
    return asyncio.run(run())


class EveryReviewButtonAnswersAfterARestartTest(unittest.TestCase):

    def test_the_edit_button_opens_the_picker(self):
        calls, _ = _after_restart("ph1_edit")
        edits = [d for e, d in calls if e == "editMessageText"]
        self.assertTrue(edits, f"Edit did nothing at all: {[e for e, _ in calls]}")
        self.assertIn("Tap a field to change it", edits[0].get("text", ""))

    def test_it_edits_the_very_card_that_was_tapped(self):
        calls, ud = _after_restart("ph1_edit")
        edited = [d for e, d in calls if e == "editMessageText"]
        self.assertEqual(CARD_MID, int(edited[0].get("message_id")))
        self.assertEqual(CARD_MID, ud.get("review_message_id"))

    def test_no_button_on_the_card_goes_unanswered(self):
        """Every one of them, not just the one that got reported."""
        for data in ("ph1_edit", "ph1_vin_check", "ph1_add_image", "ph1_ins_toggle",
                     "ph1_pick_group", "ph1_pick_driver", "ph1_pick_source",
                     "ph1edit_price", "ph1edit_col", "ph1edit_ins", "ph1_back"):
            with self.subTest(data=data):
                calls, _ = _after_restart(data)
                answered = [e for e, _ in calls
                            if e in ("editMessageText", "sendMessage", "editMessageReplyMarkup")]
                self.assertTrue(answered, f"{data} produced no reply of any kind")

    def test_submitting_still_works(self):
        calls, _ = _after_restart("ph1_accept")
        self.assertTrue([e for e, _ in calls if e in ("sendMessage", "editMessageText")],
                        "Submit went silent")

    def test_the_dmv_answer_is_still_answerable(self):
        for data in ("vin_use", "vin_keep"):
            with self.subTest(data=data):
                calls, _ = _after_restart(data, keyboard=None)
                self.assertTrue(calls, f"{data} produced nothing")


class TheCardIsFoundFromTheTapTest(unittest.TestCase):
    """_adopt_review_message on its own — the memory the restart wiped."""

    def _ctx(self, **user_data):
        return SimpleNamespace(user_data=dict(user_data))

    def _query(self, callbacks, mid=CARD_MID):
        rows = [[SimpleNamespace(callback_data=c) for c in callbacks]]
        return SimpleNamespace(message=SimpleNamespace(
            message_id=mid, chat_id=CHAT_ID,
            reply_markup=SimpleNamespace(inline_keyboard=rows)))

    def test_a_tapped_review_card_is_adopted(self):
        ctx = self._ctx()
        bot._adopt_review_message(ctx, self._query(["ph1_edit", "ph1_accept"]))
        self.assertEqual(CARD_MID, ctx.user_data.get("review_message_id"))
        self.assertEqual(CHAT_ID, ctx.user_data.get("review_chat_id"))

    def test_the_edit_picker_counts_as_the_card(self):
        """It IS the card — _show_edit_picker rewrites the same message."""
        ctx = self._ctx()
        bot._adopt_review_message(ctx, self._query(["ph1edit_price", "ph1edit_col"]))
        self.assertEqual(CARD_MID, ctx.user_data.get("review_message_id"))

    def test_a_card_already_known_is_left_alone(self):
        ctx = self._ctx(review_message_id=77, review_chat_id=CHAT_ID)
        bot._adopt_review_message(ctx, self._query(["ph1_edit"]))
        self.assertEqual(77, ctx.user_data.get("review_message_id"))

    def test_some_other_message_is_not_mistaken_for_the_card(self):
        ctx = self._ctx()
        bot._adopt_review_message(ctx, self._query(["ph1prc_150", "edit_cancel"]))
        self.assertIsNone(ctx.user_data.get("review_message_id"))

    def test_a_message_with_no_buttons_is_ignored(self):
        ctx = self._ctx()
        q = SimpleNamespace(message=SimpleNamespace(
            message_id=1, chat_id=CHAT_ID, reply_markup=None))
        bot._adopt_review_message(ctx, q)
        self.assertIsNone(ctx.user_data.get("review_message_id"))


class NoCardLeftBehindTest(unittest.TestCase):
    """Guards the whole class: a button offered inside the conversation but not as an
    entry point is dead the moment the process restarts."""

    def _entry_patterns(self):
        app = _build_application()
        conv = bot._MAIN_CONV_HANDLER
        return [h.pattern.pattern for h in conv.entry_points
                if isinstance(h, bot.CallbackQueryHandler) and h.pattern]

    def test_the_review_buttons_are_entry_points(self):
        self.assertIn(bot.PH1_REVIEW_CB_PATTERN, self._entry_patterns())

    def test_the_dmv_answer_is_an_entry_point(self):
        self.assertIn(bot.PH1_VIN_CHOICE_CB_PATTERN, self._entry_patterns())

    def test_the_pickers_are_entry_points(self):
        pats = self._entry_patterns()
        self.assertIn(f"^{bot.PH1_COLOR_CB}", pats)
        self.assertIn(f"^{bot.PH1_PRICE_CB}", pats)

    def test_the_pattern_is_declared_once(self):
        """Two copies drift apart; the state and the entry point must share one."""
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        self.assertEqual(1, src.count("PH1_REVIEW_CB_PATTERN = ("))
        self.assertGreaterEqual(src.count("pattern=PH1_REVIEW_CB_PATTERN"), 2)


if __name__ == "__main__":
    unittest.main()
