r"""Saying "change the driver Susan" changes the driver — from any screen.

Reported: "When I say change the driver Susan, it does not change it to driver
Susan. Basically the parsing and understanding text."

On the review card it always worked. With a field prompt open it did not — and it
did not merely fail, it wrote the sentence into whichever field was waiting:

    colour palette open   ->  color = "Change The Driver Susan"
    VIN prompt open       ->  vin   = "change the driver Susan"
    2nd Tag VIN prompt    ->  that car's vin, the same
    driver-note prompt    ->  the note became the sentence

The colour and the VIN both print on the tag, so this was a wrong document, not
just a lost instruction.

``_place_text_at_field_prompt`` already redirects a LABELLED field edit — its own
docstring says "a prompt is where you are, not a cage", and "color black" typed at
the Price prompt changes the colour. Selections were simply never included.

Run:  venv\Scripts\python.exe -m pytest tests/test_select_driver_anywhere.py -q
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

CARD_MID = 7400
DRIVERS = [
    {"id": "d1", "driver_name": "Kita", "is_active": True, "telegram_id": "42"},
    {"id": "d2", "driver_name": "Susan", "is_active": True, "telegram_id": "43"},
]


def _tap(app, data, n):
    return Update.de_json({"update_id": n, "callback_query": {
        "id": str(n), "chat_instance": "ci",
        "from": {"id": USER_ID, "is_bot": False, "first_name": "T", "username": "t"},
        "data": data,
        "message": {"message_id": CARD_MID, "date": 1700000000,
                    "chat": {"id": CHAT_ID, "type": "private"},
                    "from": {"id": 1, "is_bot": True, "first_name": "B", "username": "b"},
                    "text": "Review card",
                    "reply_markup": bot._build_review_keyboard_with_selections(
                        dict(_REVIEW_DATA)).to_dict()}}}, app.bot)


def _typed(app, text, n):
    return Update.de_json({"update_id": n, "message": {
        "message_id": 9000 + n, "date": 1700000000,
        "chat": {"id": CHAT_ID, "type": "private"},
        "from": {"id": USER_ID, "is_bot": False, "first_name": "T", "username": "t"},
        "text": text}}, app.bot)


def _say(phrase, *prelude):
    """Tap through `prelude`, then TYPE `phrase`. Returns the card afterwards."""
    async def run():
        app = _build_application()
        with mock.patch.object(telegram.Bot, "_do_post", TRANSPORT.do_post), \
             mock.patch.object(bot, "_get_all_drivers_cached", return_value=DRIVERS), \
             mock.patch.object(bot, "_get_suspended_driver_ids", return_value=set()):
            await app.initialize()
            FAKE_DB.states.clear()
            FAKE_DB.set_user_state(USER_ID, "phase1", dict(_REVIEW_DATA))
            n = 1
            for step in prelude:
                n += 1
                await app.process_update(_tap(app, step, n))
            TRANSPORT.reset()
            await app.process_update(_typed(app, phrase, n + 1))
            card = FAKE_DB.states.get(USER_ID, {}).get("data", {})
            await app.shutdown()
        return dict(card)
    return asyncio.run(run())


# Every screen the operator can be looking at when they say it.
SCREENS = {
    "the review card": (),
    "the edit picker": ("ph1_edit",),
    "the VIN prompt": ("ph1_edit", "ph1edit_vin"),
    "the car prompt": ("ph1_edit", "ph1edit_car"),
    "the colour palette": ("ph1_edit", "ph1edit_col"),
    "the price picker": ("ph1_edit", "ph1edit_price"),
    "the driver-note prompt": ("ph1_edit", "ph1edit_driver"),
    "the issuer-note prompt": ("ph1_edit", "ph1edit_issuer"),
    "the insurance prompt": ("ph1_edit", "ph1edit_ins"),
    "the 2nd Tag's VIN prompt": ("ph1_add_car", "ph1edit_v2vin"),
}


class TheDriverChangesFromEveryScreenTest(unittest.TestCase):

    def test_change_the_driver_susan(self):
        for where, prelude in SCREENS.items():
            with self.subTest(screen=where):
                card = _say("change the driver Susan", *prelude)
                self.assertEqual(card.get("selected_driver_names"), "Susan",
                                 f"saying it at {where} did not change the driver")

    def test_the_sentence_is_never_written_into_a_field(self):
        """The real damage: the colour and the VIN both print on the tag."""
        for where, prelude in SCREENS.items():
            with self.subTest(screen=where):
                card = _say("change the driver Susan", *prelude)
                blob = " ".join(str(v) for v in card.values())
                for v in bot._extra_vehicles(card):
                    blob += " " + " ".join(str(x) for x in v.values())
                self.assertNotIn("Driver Susan", blob)
                self.assertNotIn("driver Susan", blob)

    def test_the_phrasings_people_actually_use(self):
        for phrase in ("change the driver Susan", "change driver Susan",
                       "driver Susan", "change the driver to Susan",
                       "switch driver to Susan", "set driver Susan",
                       "use driver Susan", "assign driver Susan",
                       "DRIVER SUSAN", "change the driver susan"):
            with self.subTest(phrase=phrase):
                card = _say(phrase, "ph1_edit", "ph1edit_vin")
                self.assertEqual(card.get("selected_driver_names"), "Susan")

    def test_the_dispatcher_and_source_redirect_too(self):
        card = _say("change the dispatcher HighKage", "ph1_edit", "ph1edit_vin")
        self.assertNotEqual(card.get("vin"), "change the dispatcher HighKage")
        card = _say("source Instagram", "ph1_edit", "ph1edit_vin")
        self.assertNotEqual(card.get("vin"), "source Instagram")


class RealFieldValuesStillLandTest(unittest.TestCase):
    """The other half. A prompt that redirects everything is no better than one
    that redirects nothing."""

    CASES = [
        ("a real VIN", ("ph1_edit", "ph1edit_vin"),
         "1N4AL3AP0HC166043", "vin", "1N4AL3AP0HC166043"),
        ("a colour", ("ph1_edit", "ph1edit_col"), "grey", "color", "Grey"),
        ("a price", ("ph1_edit", "ph1edit_price"), "150", "pending_price", "$150"),
        ("a car", ("ph1_edit", "ph1edit_car"),
         "2017 Nissan Altima", "car", "2017 Nissan Altima"),
        ("an insurer", ("ph1_edit", "ph1edit_ins"),
         "Progressive", "insurance_company", "Progressive"),
    ]

    def test_them(self):
        for label, prelude, text, key, want in self.CASES:
            with self.subTest(value=label):
                self.assertEqual(_say(text, *prelude).get(key), want)

    def test_a_note_containing_a_loose_vin_verb(self):
        """_VIN_KEEP_RE finds a bare "same" or "keep" ANYWHERE in a line, which is
        why only the three selections are redirected and the VIN verbs are not."""
        card = _say("Same Day Delivery", "ph1_edit", "ph1edit_issuer")
        self.assertEqual(card.get("special_request_issuers"), "Same Day Delivery")
        card = _say("keep the gate code handy", "ph1_edit", "ph1edit_driver")
        self.assertEqual(card.get("special_request_drivers"), "keep the gate code handy")

    def test_driver_note_is_still_a_note_not_a_selection(self):
        card = _say("driver note call ahead", "ph1_edit", "ph1edit_vin")
        self.assertEqual(card.get("special_request_drivers"), "call ahead")
        self.assertNotEqual(card.get("selected_driver_names"), "call ahead")

    def test_a_company_name_at_the_name_prompt_stays_a_name(self):
        """"Dispatch Solutions LLC" and "Team Rubicon" are real registrants, and
        the group regex matches anything opening with dispatch/team/group/crew."""
        for name in ("Dispatch Solutions LLC", "Team Rubicon", "Group One Auto"):
            with self.subTest(name=name):
                card = _say(name, "ph1_edit", "ph1edit_fn")
                self.assertIn(name.split()[0], card.get("name") or "")

    def test_a_driver_selection_still_wins_at_the_name_prompt(self):
        """It needs the literal word "driver", which no person is called."""
        card = _say("change the driver Susan", "ph1_edit", "ph1edit_fn")
        self.assertEqual(card.get("selected_driver_names"), "Susan")

    def test_a_cross_field_edit_still_works(self):
        """The behaviour this fix is modelled on: the prompt is where you are, not
        what you meant."""
        self.assertEqual(_say("color black", "ph1_edit", "ph1edit_price").get("color"),
                         "Black")


if __name__ == "__main__":
    unittest.main()
