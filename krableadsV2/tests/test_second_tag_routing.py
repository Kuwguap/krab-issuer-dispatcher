r"""➕ Add Car and every 2nd-Tag button, through the REAL handler graph.

Pattern-matching tests prove a callback COULD reach a handler. These prove it
does: each tap is pushed through a real Application and has to produce a real
reply. That distinction is exactly where this codebase keeps losing buttons —
``PH1_EDIT_MENU_CB_PATTERN`` was ``ph1edit_[a-z]+``, which cannot match a digit,
so in STATE_AI_EDIT_MENU (where that handler runs FIRST) every per-car button
would have been silently dead.

Run:  venv\Scripts\python.exe -m pytest tests/test_second_tag_routing.py -q
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

CARD_MID = 7100

CAR2 = {
    "name": "CHARLES G JONES",
    "address": "11530 Mango terrace drive apt.102",
    "city_state_zip": "Seffner Florida 33584",
    "vin": "4T1BF3EK6AU051219",
    "car": "2010 Toyota Camry",
    "color": "Grey",
    "insurance_company": "Progressive",
    "insurance_policy_number": "982658176",
}


def _tap(app, data, uid_seed):
    return Update.de_json({
        "update_id": uid_seed,
        "callback_query": {
            "id": str(uid_seed),
            "chat_instance": "ci",
            "from": {"id": USER_ID, "is_bot": False, "first_name": "Tester",
                     "username": "tester"},
            "data": data,
            "message": {
                "message_id": CARD_MID,
                "date": 1700000000,
                "chat": {"id": CHAT_ID, "type": "private"},
                "from": {"id": 1, "is_bot": True, "first_name": "TestBot",
                         "username": "testbot"},
                "text": "Review card",
                "reply_markup": bot._build_review_keyboard_with_selections(
                    dict(_REVIEW_DATA)).to_dict(),
            },
        },
    }, app.bot)


def _type(app, text, uid_seed):
    return Update.de_json({
        "update_id": uid_seed,
        "message": {
            "message_id": 8000 + uid_seed,
            "date": 1700000000,
            "chat": {"id": CHAT_ID, "type": "private"},
            "from": {"id": USER_ID, "is_bot": False, "first_name": "Tester",
                     "username": "tester"},
            "text": text,
        },
    }, app.bot)


def _sequence(*steps, start_card=None):
    """Push taps ("cb:data") and typing ("txt:words") through one Application.

    Returns (per-step transport calls, the card as the DB finally holds it).
    """
    async def run():
        app = _build_application()
        out = []
        with mock.patch.object(telegram.Bot, "_do_post", TRANSPORT.do_post):
            await app.initialize()
            FAKE_DB.states.clear()
            FAKE_DB.set_user_state(USER_ID, "phase1",
                                   dict(start_card or _REVIEW_DATA))
            await app.process_update(_tap(app, "ph1_edit", 1))
            for n, step in enumerate(steps, start=2):
                TRANSPORT.reset()
                kind, _, payload = step.partition(":")
                upd = _tap(app, payload, n) if kind == "cb" else _type(app, payload, n)
                await app.process_update(upd)
                out.append(list(TRANSPORT.calls))
            card = FAKE_DB.states.get(USER_ID, {}).get("data", {})
            await app.shutdown()
        return out, dict(card)
    return asyncio.run(run())


def _texts(calls):
    return " | ".join(d.get("text", "") for e, d in calls
                      if e in ("sendMessage", "editMessageText"))


class AddCarActuallyAddsACarTest(unittest.TestCase):

    def test_the_button_answers(self):
        steps, card = _sequence(f"cb:{bot.PH1_ADD_CAR_CB}")
        self.assertTrue(steps[0], "➕ Add Car produced nothing at all")
        self.assertIn("2nd Tag", _texts(steps[0]))
        self.assertEqual(len(bot._extra_vehicles(card)), 1)

    def test_tapping_it_twice_does_not_stack_up_blank_cars(self):
        """An untouched blank car should be reopened, not duplicated."""
        _steps, card = _sequence(f"cb:{bot.PH1_ADD_CAR_CB}", f"cb:{bot.PH1_ADD_CAR_CB}")
        self.assertEqual(len(bot._extra_vehicles(card)), 1)

    def test_a_second_car_can_be_added_once_the_first_is_filled(self):
        _steps, card = _sequence(
            f"cb:{bot.PH1_ADD_CAR_CB}", "cb:ph1edit_v2vin", "txt:4T1BF3EK6AU051219",
            f"cb:{bot.PH1_ADD_CAR_CB}")
        self.assertEqual(len(bot._extra_vehicles(card)), 2)


class EveryPerCarButtonAnswersTest(unittest.TestCase):
    """One tap per field. Any that produces nothing is a dead button."""

    def test_all_of_them(self):
        bases = list(bot.VEHICLE_EDIT_KEYS)
        steps, _card = _sequence(
            f"cb:{bot.PH1_ADD_CAR_CB}",
            *[f"cb:ph1edit_{bot._vehicle_edit_key(2, b)}" for b in bases])
        for i, base in enumerate(bases, start=1):
            with self.subTest(field=base, callback=f"ph1edit_v2{base}"):
                self.assertTrue(steps[i], f"ph1edit_v2{base} reached no handler")

    def test_each_prompt_says_which_car_it_is_for(self):
        steps, _ = _sequence(f"cb:{bot.PH1_ADD_CAR_CB}", "cb:ph1edit_v2vin")
        self.assertIn("2nd Tag", _texts(steps[1]))


class TypingAtAPerCarPromptLandsOnThatCarTest(unittest.TestCase):

    def test_a_vin(self):
        _steps, card = _sequence(
            f"cb:{bot.PH1_ADD_CAR_CB}", "cb:ph1edit_v2vin", "txt:4T1BF3EK6AU051219")
        self.assertEqual(bot._extra_vehicles(card)[0]["vin"], "4T1BF3EK6AU051219")
        self.assertNotEqual(card.get("vin"), "4T1BF3EK6AU051219",
                            "car 1's VIN was overwritten")

    def test_an_insurer(self):
        _steps, card = _sequence(
            f"cb:{bot.PH1_ADD_CAR_CB}", "cb:ph1edit_v2ins", "txt:Progressive")
        self.assertEqual(bot._extra_vehicles(card)[0]["insurance_company"], "Progressive")

    def test_a_whole_address_line_fills_both_rows(self):
        _steps, card = _sequence(
            f"cb:{bot.PH1_ADD_CAR_CB}", "cb:ph1edit_v2addr",
            "txt:11530 Mango terrace drive apt.102 Seffner Florida 33584")
        v = bot._extra_vehicles(card)[0]
        self.assertEqual(v["address"], "11530 Mango terrace drive apt.102")
        self.assertEqual(v["city_state_zip"], "Seffner Florida 33584")

    def test_a_colour_tapped_from_the_palette_paints_the_right_car(self):
        """This hardcoded car 1, so picking a colour for the 2nd Tag repainted the
        first car."""
        _steps, card = _sequence(
            f"cb:{bot.PH1_ADD_CAR_CB}", "cb:ph1edit_v2col",
            f"cb:{bot.PH1_COLOR_CB}Black")
        self.assertEqual(bot._extra_vehicles(card)[0]["color"], "Black")
        self.assertNotEqual(card.get("color"), "Black", "car 1 was repainted")

    def test_car_ones_own_colour_picker_still_works(self):
        _steps, card = _sequence("cb:ph1edit_col", f"cb:{bot.PH1_COLOR_CB}Black")
        self.assertEqual(card.get("color"), "Black")


class OpeningAndRemovingACarTest(unittest.TestCase):

    CARD = dict(_REVIEW_DATA, extra_vehicles=[dict(CAR2)])

    def test_the_shortcut_opens_that_cars_fields(self):
        steps, _ = _sequence(f"cb:{bot.PH1_CAR_MENU_CB}2", start_card=self.CARD)
        self.assertTrue(steps[0], "the 2nd Tag shortcut produced nothing")
        self.assertIn("2nd Tag", _texts(steps[0]))

    def test_remove_takes_the_car_off_the_lead(self):
        steps, card = _sequence(f"cb:{bot.PH1_CAR_REMOVE_CB}2", start_card=self.CARD)
        self.assertTrue(steps[0], "🗑 Remove produced nothing")
        self.assertEqual(bot._extra_vehicles(card), [])
        self.assertIn("Removed", _texts(steps[0]))

    def test_a_stale_shortcut_says_so_instead_of_doing_nothing(self):
        steps, _ = _sequence(f"cb:{bot.PH1_CAR_REMOVE_CB}2",
                             f"cb:{bot.PH1_CAR_MENU_CB}2", start_card=self.CARD)
        self.assertTrue(steps[1])
        self.assertIn("no longer", _texts(steps[1]))

    def test_a_nonsense_car_number_does_not_crash(self):
        steps, _ = _sequence(f"cb:{bot.PH1_CAR_MENU_CB}99", start_card=self.CARD)
        self.assertTrue(steps[0])


class SubmitRefusesAnIncompleteCarThroughTheRealGraphTest(unittest.TestCase):

    def test_a_car_with_no_vin_blocks_submit_and_says_which(self):
        steps, _ = _sequence(f"cb:{bot.PH1_ADD_CAR_CB}", "cb:ph1edit_v2fn",
                             "txt:CHARLES", f"cb:{bot.PH1_REVIEW_ACCEPT}")
        said = _texts(steps[-1])
        self.assertIn("2nd Tag", said)
        self.assertIn("VIN", said)

    def test_an_untouched_blank_car_does_not_block_submit(self):
        """Tapping ➕ Add Car and changing your mind must not wedge the lead."""
        steps, card = _sequence(f"cb:{bot.PH1_ADD_CAR_CB}",
                                f"cb:{bot.PH1_REVIEW_ACCEPT}")
        self.assertNotIn("2nd Tag:", _texts(steps[-1]))
        self.assertEqual(bot._extra_vehicles(card), [])


class ACarSurvivesEveryOtherScreenTest(unittest.TestCase):
    """The card never leaves the screen, so its buttons must answer from anywhere."""

    def test_walking_between_car_one_and_car_two_fields(self):
        steps, card = _sequence(
            f"cb:{bot.PH1_ADD_CAR_CB}",
            "cb:ph1edit_v2vin", "cb:ph1edit_vin", "cb:ph1edit_v2ins",
            "cb:ph1edit_price", "cb:ph1edit_v2col", f"cb:{bot.PH1_CAR_MENU_CB}2",
            "cb:ph1_back")
        for i, label in enumerate(["add", "v2vin", "vin", "v2ins", "price",
                                   "v2col", "open car 2", "back"]):
            with self.subTest(step=i, label=label):
                self.assertTrue(steps[i], f"{label} produced nothing")
        self.assertEqual(len(bot._extra_vehicles(card)), 1)


CHARLES_PASTE = """Client Charles
Phone 845-423-9476

CHARLES JONES
9 hibiscus Lane Monticello New York 13701
2017 Nissan Altima
VIN: 1N4AL3AP0HC166043
Geico
0407306000
Color grey

CHARLES G JONES
11530 Mango terrace drive apt.102 Seffner Florida 33584
2010 Toyota Camry
VIN: 4T1BF3EK6AU051219
Progressive
982658176
Color grey

Delivery time now 1 hour to 9 hibiscus Lane Monticello New York 13701
Phone 845-423-9476"""


class PastingTheWholeJobTest(unittest.TestCase):
    """The operator's actual workflow: paste it, get two cars. Testing the helper
    in isolation proves it works; this proves it is WIRED."""

    def test_the_paste_lands_two_cars_on_the_card(self):
        steps, card = _sequence("txt:" + CHARLES_PASTE)
        self.assertTrue(steps[0], "the paste produced no reply at all")
        self.assertEqual(len(bot._extra_vehicles(card)), 1,
                         f"got {bot._extra_vehicles(card)}")
        v = bot._extra_vehicles(card)[0]
        self.assertEqual(v["vin"], "4T1BF3EK6AU051219")
        self.assertEqual(v["insurance_company"], "Progressive")
        self.assertEqual(v["insurance_policy_number"], "982658176")
        self.assertEqual(v["city_state_zip"], "Seffner Florida 33584")

    def test_the_operator_is_told_two_cars_were_found(self):
        steps, _ = _sequence("txt:" + CHARLES_PASTE)
        said = " | ".join(d.get("text", "") for e, d in steps[0]
                          if e in ("sendMessage", "editMessageText"))
        self.assertIn("2nd Tag", said)

    def test_car_one_still_gets_its_own_details_from_the_same_paste(self):
        _steps, card = _sequence("txt:" + CHARLES_PASTE)
        self.assertEqual(card.get("vin"), "1N4AL3AP0HC166043")
        self.assertNotEqual(card.get("vin"), "4T1BF3EK6AU051219",
                            "car 1 got car 2's VIN")

    def test_the_card_then_shows_both_cars(self):
        _steps, card = _sequence("txt:" + CHARLES_PASTE)
        shown = bot._format_phase1_field_lines(card)
        self.assertIn("2nd Tag", shown)
        self.assertIn("4T1BF3EK6AU051219", shown)
        self.assertIn("1N4AL3AP0HC166043", shown)

    ONE_CAR_PASTE = """JOHN DOE
5 Oak Street Newark New Jersey 07102
2019 Honda Accord
VIN: 1HGCM82633A004352
Geico
123456789"""

    def test_an_ordinary_one_car_paste_adds_no_car(self):
        steps, card = _sequence("txt:" + self.ONE_CAR_PASTE)
        self.assertTrue(steps[0])
        self.assertEqual(bot._extra_vehicles(card), [])


REAL_PASTE = """Client Charles
Phone 845-423-9476

CHARLES JONES
9 hibiscus Lane Monticello New York 13701
Vin numbers:
CHARLES JONES
2017 Nissan Altima
VIN: 1N4AL3AP0HC166043
Geico
0407306000
Color grey

CHARLES G JONES
11530 Mango terrace drive apt.102
 Seffner Florida 33584
2010 Toyota Camry
VIN: 4T1BF3EK6AU051219
Progressive
982658176
Color grey


Delivery time now 1 hour to 9 hibiscus Lane Monticello New York 13701
Phone 845-423-9476"""


class TheRealMessageFromABlankCardTest(unittest.TestCase):
    r"""Reported: "Doesn't read the 2nd car address empty".

    Starts from an EMPTY card, the way a real lead starts — not the pre-filled
    _REVIEW_DATA the other tests use — so nothing can be inherited and every
    field has to come out of the message itself.
    """

    def setUp(self):
        self.steps, self.card = _sequence("txt:" + REAL_PASTE, start_card={})

    def test_the_second_cars_address_is_on_the_card(self):
        v = bot._extra_vehicles(self.card)[0]
        self.assertEqual(v["address"], "11530 Mango terrace drive apt.102")
        self.assertEqual(v["city_state_zip"], "Seffner Florida 33584")

    def test_car_one_is_the_registrant_not_the_header_line(self):
        self.assertEqual(self.card.get("name"), "CHARLES JONES")

    def test_car_one_keeps_its_own_vin_and_policy(self):
        self.assertEqual(self.card.get("vin"), "1N4AL3AP0HC166043")
        self.assertEqual(self.card.get("insurance_policy_number"), "0407306000")

    def test_the_transaction_fields_are_still_single_and_correct(self):
        self.assertEqual(self.card.get("pending_phone_number"), "845-423-9476")
        self.assertIn("hibiscus", (self.card.get("delivery_address") or "")
                      + (self.card.get("extra_info") or ""))

    def test_the_card_shows_both_cars_in_full(self):
        shown = bot._format_phase1_field_lines(self.card)
        # The card splits the name into First/Last, as it always has for car 1.
        for want in ("2nd Tag", "First name: CHARLES", "Last name: JONES",
                     "Last name: G JONES",
                     "1N4AL3AP0HC166043", "4T1BF3EK6AU051219",
                     "9 hibiscus Lane", "11530 Mango terrace drive apt.102",
                     "Monticello New York 13701", "Seffner Florida 33584",
                     "GEICO", "Progressive", "0407306000", "982658176"):
            with self.subTest(expected=want):
                self.assertIn(want, shown)

    def test_the_operator_is_told_what_happened(self):
        self.assertIn("2nd Tag", _texts(self.steps[0]))

    def test_it_submits_without_being_blocked(self):
        self.assertEqual(bot._extra_vehicles_submit_block(self.card), "")


if __name__ == "__main__":
    unittest.main()
