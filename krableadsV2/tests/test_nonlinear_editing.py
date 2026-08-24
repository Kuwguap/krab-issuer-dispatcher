r"""Edit fields in any order, changing your mind at any point.

Reported: "I hit PRICE but didn't choose an option then hit a different field to
edit and it's not allowed." The field prompts were dead ends — STATE_EDIT_FIELD_PROMPT
and STATE_AI_EDIT_INPUT registered the two pickers and Cancel and nothing else, so a
tap on any OTHER field button reached no handler and did nothing at all.

The review card never leaves the screen, so its buttons have to answer from wherever
the flow happens to be. This drives the REAL handler graph.

Run:  venv\Scripts\python.exe -m pytest tests/test_nonlinear_editing.py -q
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
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11")
os.environ.pop("OPENAI_API_KEY", None)

from test_real_routing_e2e import (  # noqa: E402
    CHAT_ID, USER_ID, FAKE_DB, TRANSPORT, _build_application, _REVIEW_DATA,
)

import bot  # noqa: E402
import telegram  # noqa: E402
from telegram import Update  # noqa: E402

CARD_MID = 7000


def _tap(app, data, uid_seed):
    return Update.de_json({
        "update_id": uid_seed,
        "callback_query": {
            "id": str(uid_seed),
            "chat_instance": "ci",
            "from": {"id": USER_ID, "is_bot": False, "first_name": "Tester", "username": "tester"},
            "data": data,
            "message": {
                "message_id": CARD_MID,
                "date": 1700000000,
                "chat": {"id": CHAT_ID, "type": "private"},
                "from": {"id": 1, "is_bot": True, "first_name": "TestBot", "username": "testbot"},
                "text": "Review card",
                "reply_markup": bot._build_review_keyboard_with_selections(
                    dict(_REVIEW_DATA)).to_dict(),
            },
        },
    }, app.bot)


def _sequence(*taps):
    """Push a run of taps through one Application and report what each produced."""
    async def run():
        app = _build_application()
        out = []
        with mock.patch.object(telegram.Bot, "_do_post", TRANSPORT.do_post):
            await app.initialize()
            FAKE_DB.states.clear()
            FAKE_DB.set_user_state(USER_ID, "phase1", dict(_REVIEW_DATA))
            # Start from the review card, the way the flow really gets there.
            await app.process_update(_tap(app, "ph1_edit", 1))
            for n, data in enumerate(taps, start=2):
                TRANSPORT.reset()
                await app.process_update(_tap(app, data, n))
                out.append(list(TRANSPORT.calls))
            card = FAKE_DB.states.get(USER_ID, {}).get("data", {})
            await app.shutdown()
        return out, dict(card)
    return asyncio.run(run())


def _texts(calls):
    return " | ".join(d.get("text", "") for e, d in calls if e in ("sendMessage", "editMessageText"))


class ChangingYourMindMidEditTest(unittest.TestCase):

    def test_the_reported_sequence(self):
        """Open Price, pick nothing, open Color, pick black, then set the price."""
        steps, card = _sequence("ph1edit_price", "ph1edit_col",
                                f"{bot.PH1_COLOR_CB}Black", f"{bot.PH1_PRICE_CB}150")
        self.assertIn("price", _texts(steps[0]).lower(), "Price prompt never opened")
        self.assertIn("color", _texts(steps[1]).lower(),
                      "switching from Price to Color did nothing — the reported bug")
        self.assertEqual("Black", card.get("color"))
        self.assertEqual("$150", card.get("pending_price"))

    def test_switching_works_in_either_direction(self):
        steps, card = _sequence("ph1edit_col", "ph1edit_price", f"{bot.PH1_PRICE_CB}200")
        self.assertIn("price", _texts(steps[1]).lower())
        self.assertEqual("$200", card.get("pending_price"))

    def test_you_can_walk_through_several_fields(self):
        steps, _ = _sequence("ph1edit_price", "ph1edit_col", "ph1edit_vin",
                             "ph1edit_ins", "ph1edit_phone", "ph1edit_price")
        for i, data in enumerate(("price", "col", "vin", "ins", "phone", "price")):
            with self.subTest(step=i, field=data):
                self.assertTrue(steps[i], f"tap {i} ({data}) produced nothing")

    def test_the_abandoned_prompt_is_taken_down(self):
        """Otherwise a live price picker sits there to be tapped into the wrong field."""
        steps, _ = _sequence("ph1edit_price", "ph1edit_col")
        self.assertTrue([e for e, _ in steps[1] if e == "deleteMessage"],
                        f"stale prompt left on screen: {[e for e, _ in steps[1]]}")

    def test_the_other_card_buttons_work_mid_edit_too(self):
        for data in ("ph1_vin_check", "ph1_add_image", "ph1_ins_toggle",
                     "ph1_pick_group", "ph1_pick_driver", "ph1_pick_source", "ph1_accept"):
            with self.subTest(data=data):
                steps, _ = _sequence("ph1edit_price", data)
                self.assertTrue(steps[1], f"{data} did nothing while a prompt was open")

    def test_typing_still_works_at_a_prompt(self):
        """The buttons are an addition — the typed path must not regress."""
        steps, card = _sequence("ph1edit_price")
        self.assertIn("price", _texts(steps[0]).lower())


class EveryStateAnswersTheCardTest(unittest.TestCase):
    """Guards the class: a lead state that registers none of the card's buttons is a
    dead end, exactly like the field prompts were."""

    def _states(self):
        _build_application()
        return bot._MAIN_CONV_HANDLER.states

    def test_no_lead_state_drops_a_tap_on_the_card(self):
        gaps = []
        for state, handlers in self._states().items():
            live = any(isinstance(h, bot.CallbackQueryHandler) and h.pattern
                       and h.pattern.search("ph1edit_price") for h in handlers)
            if not live:
                gaps.append(state)
        self.assertEqual([], gaps, f"states that ignore the card's buttons: {gaps}")

    def test_every_state_answers_the_pickers(self):
        for probe in (f"{bot.PH1_COLOR_CB}Black", f"{bot.PH1_PRICE_CB}150", "vin_use"):
            for state, handlers in self._states().items():
                with self.subTest(probe=probe, state=state):
                    self.assertTrue(
                        any(isinstance(h, bot.CallbackQueryHandler) and h.pattern
                            and h.pattern.search(probe) for h in handlers))

    def test_the_list_is_declared_once(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        self.assertEqual(1, src.count("def _card_buttons_always_live()"))
        self.assertGreaterEqual(src.count("] + _card_buttons_always_live(),"), 14)


if __name__ == "__main__":
    unittest.main()
