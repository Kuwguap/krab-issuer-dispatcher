"""END-TO-END routing test: the REAL Application, the REAL handler stack, real PTB routing.

Unlike the sibling unit tests (which call handler functions directly), this builds the
production handler graph by running bot.main() with polling stubbed out, then pushes real
telegram.Update objects through Application.process_update(). It therefore proves the
thing that actually broke in production: WHICH handler a typed single-line edit reaches,
in every conversation state the review card can be parked in.

Telegram I/O is intercepted at Bot._do_post (no network); Supabase is a dict-backed fake.

Run:  venv\\Scripts\\python.exe -m pytest tests/test_real_routing_e2e.py -q
"""
import os
import sys
import asyncio
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy-key")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11")
os.environ.pop("OPENAI_API_KEY", None)          # force deterministic (no-AI) paths

import utils.database as udb  # noqa: E402

CHAT_ID = 555000111
USER_ID = 555000111


class FakeDB:
    """Dict-backed stand-in for the Supabase wrapper (states + lookup tables)."""

    def __init__(self):
        self.states = {}
        self.set_calls = []

    # --- states ---------------------------------------------------------
    def get_user_state(self, user_id):
        row = self.states.get(int(user_id))
        return {"state": row["state"], "data": dict(row["data"])} if row else None

    def set_user_state(self, user_id, state, data=None):
        self.states[int(user_id)] = {"state": state, "data": dict(data or {})}
        self.set_calls.append((state, dict(data or {})))
        return True

    def clear_user_state(self, user_id):
        self.states.pop(int(user_id), None)
        return True

    # --- lookup tables used while rendering the review card -------------
    def get_all_groups(self):
        return [{"id": "g1", "group_name": "HighKage", "is_active": True, "chat_id": "-100123"}]

    def get_all_drivers(self):
        return [{"id": "d1", "driver_name": "Kita", "is_active": True, "telegram_id": "42"}]

    def get_contact_info_sources(self):
        return [{"id": "s1", "label": "Facebook", "is_active": True}]

    def get_suspended_drivers(self):
        return []

    def get_driver_penalties(self):
        return []

    def __getattr__(self, name):        # any other db call -> benign empty result
        def _noop(*a, **k):
            return [] if name.startswith("get_") else None
        return _noop


FAKE_DB = FakeDB()
udb.Database = mock.MagicMock(return_value=FAKE_DB)

import bot  # noqa: E402
import telegram  # noqa: E402
from telegram import Update  # noqa: E402

# bot.db is built at import time, so whichever test module imports bot FIRST decides
# it. Bind our fake explicitly so this file works regardless of collection order.
bot.db = FAKE_DB


# -- fake Telegram transport: record every outgoing API call, answer plausibly --
class Transport:
    def __init__(self):
        self.calls = []
        self.next_message_id = 1000

    def reset(self):
        self.calls.clear()

    def sent_texts(self):
        return [d.get("text", "") for e, d in self.calls if e == "sendMessage"]

    def edited_texts(self):
        return [d.get("text", "") for e, d in self.calls if e == "editMessageText"]

    async def do_post(self, endpoint, data, **kwargs):
        self.calls.append((endpoint, dict(data or {})))
        if endpoint == "getMe":
            return {"id": 1, "is_bot": True, "first_name": "TestBot", "username": "testbot"}
        if endpoint in ("sendMessage", "editMessageText"):
            self.next_message_id += 1
            return {
                "message_id": self.next_message_id,
                "date": 1700000000,
                "chat": {"id": data.get("chat_id", CHAT_ID), "type": "private"},
                "from": {"id": 1, "is_bot": True, "first_name": "TestBot", "username": "testbot"},
                "text": data.get("text", ""),
            }
        return True


TRANSPORT = Transport()


def _build_application():
    """Run the real main() with polling stubbed, and capture the built Application."""
    captured = {}

    def _fake_run_polling(self, *a, **k):
        captured["app"] = self

    patches = [
        mock.patch.object(bot.Application, "run_polling", _fake_run_polling),
        mock.patch.object(bot, "_wait_for_exclusive_polling", lambda *a, **k: True),
        mock.patch("requests.post", mock.MagicMock()),
        mock.patch.object(bot.Config, "validate", classmethod(lambda cls: True)),
        mock.patch.object(telegram.Bot, "_do_post", TRANSPORT.do_post),
    ]
    for p in patches:
        p.start()
    try:
        bot.main()
    finally:
        for p in patches:
            p.stop()
    return captured["app"]


def _text_update(app, text, mid):
    return Update.de_json({
        "update_id": mid,
        "message": {
            "message_id": mid,
            "date": 1700000000,
            "chat": {"id": CHAT_ID, "type": "private"},
            "from": {"id": USER_ID, "is_bot": False, "first_name": "Tester", "username": "tester"},
            "text": text,
        },
    }, app.bot)


_REVIEW_DATA = {
    "name": "-", "address": "-", "city_state_zip": "-",
    "delivery_address": "-", "delivery_city_state_zip": "-",
    "vin": "-", "car": "-", "color": "-", "insurance_company": "-",
    "insurance_policy_number": "-", "extra_info": "-",
    "pending_phone_number": "", "pending_price": "",
    "email": "", "driver_license_id": "",
    "special_request_issuers": "", "special_request_drivers": "",
    "selected_group_name": "All Dispatchers", "selected_group_id": "all",
    "selected_driver_names": "All Drivers", "selected_driver_ids": ["d1"],
    "selected_source_label": "Facebook",
}


# The EXACT shape a freshly-opened (empty) review card has in production: only the
# dispatch selections were ever written, so every field key is MISSING. Joining those
# missing values crashed _clean_vin_and_car and silently killed the edit.
_EMPTY_CARD_DATA = {
    "selected_group_name": "All Dispatchers", "selected_group_id": "all",
    "selected_driver_names": "All Drivers", "selected_driver_ids": ["d1"],
    "selected_source_label": "Facebook",
}


class EmptyCardEditTest(unittest.TestCase):
    """Regression: an edit typed on a brand-new EMPTY card must apply, not crash."""

    def _scenario(self, park_state, edit_text):
        async def run():
            app = _build_application()
            with mock.patch.object(telegram.Bot, "_do_post", TRANSPORT.do_post):
                await app.initialize()
                try:
                    conv = bot._MAIN_CONV_HANDLER
                    key = (CHAT_ID, USER_ID)
                    FAKE_DB.states.clear()
                    FAKE_DB.set_user_state(USER_ID, "phase1", dict(_EMPTY_CARD_DATA))
                    app.user_data[USER_ID]["review_message_id"] = 900
                    app.user_data[USER_ID]["review_chat_id"] = CHAT_ID
                    if park_state is None:
                        conv._conversations.pop(key, None)
                    else:
                        conv._conversations[key] = park_state
                    TRANSPORT.reset()
                    await app.process_update(_text_update(app, edit_text, 7101))
                    return {"db": FAKE_DB.get_user_state(USER_ID),
                            "edited": TRANSPORT.edited_texts(),
                            "sent": TRANSPORT.sent_texts()}
                finally:
                    await app.shutdown()
        return asyncio.run(run())

    def test_name_on_empty_card(self):
        r = self._scenario(bot.STATE_AI_REVIEW, "name John Damian")
        self.assertEqual(r["db"]["data"].get("name"), "John Damian", r)

    def test_price_on_empty_card(self):
        r = self._scenario(bot.STATE_AI_REVIEW, "price 150")
        self.assertEqual(r["db"]["data"].get("pending_price"), "$150", r)

    def test_price_on_empty_card_from_edit_menu(self):
        r = self._scenario(bot.STATE_AI_EDIT_MENU, "price 150")
        self.assertEqual(r["db"]["data"].get("pending_price"), "$150", r)

    def test_name_on_empty_card_after_restart(self):
        r = self._scenario(None, "name John Damian")
        self.assertEqual(r["db"]["data"].get("name"), "John Damian", r)

    def test_clean_vin_and_car_survives_missing_keys(self):
        """The direct unit-level repro of the production TypeError."""
        d = dict(_EMPTY_CARD_DATA)
        bot._clean_vin_and_car(d)                       # must not raise
        self.assertIsInstance(d["vehicle_details"], str)
        d2 = dict(_EMPTY_CARD_DATA)
        self.assertEqual(bot._apply_inline_review_text(d2, "name John Damian"), ["name"])
        self.assertEqual(d2["name"], "John Damian")


class RealRoutingTest(unittest.TestCase):
    """A typed single-line edit must land on the card from EVERY parked state."""

    def _scenario(self, park_state, edit_text="name John Damian", db_state="phase1"):
        """Drive the real app: seed a live review card, force the conversation into
        `park_state`, then send `edit_text` and report what the bot did."""
        async def run():
            app = _build_application()
            with mock.patch.object(telegram.Bot, "_do_post", TRANSPORT.do_post):
                await app.initialize()
                try:
                    conv = bot._MAIN_CONV_HANDLER
                    key = (CHAT_ID, USER_ID)

                    # A live lead in the DB + a live card id in RAM (as after a real parse)
                    FAKE_DB.states.clear()
                    FAKE_DB.set_user_state(USER_ID, db_state, dict(_REVIEW_DATA))
                    app.user_data[USER_ID]["review_message_id"] = 900
                    app.user_data[USER_ID]["review_chat_id"] = CHAT_ID
                    if park_state is None:
                        conv._conversations.pop(key, None)
                    else:
                        conv._conversations[key] = park_state

                    TRANSPORT.reset()
                    await app.process_update(_text_update(app, edit_text, 7001))

                    return {
                        "db": FAKE_DB.get_user_state(USER_ID),
                        "edited": TRANSPORT.edited_texts(),
                        "sent": TRANSPORT.sent_texts(),
                        "conv_state": conv._conversations.get(key),
                    }
                finally:
                    await app.shutdown()
        return asyncio.run(run())

    # --- the states the card can be parked in ---------------------------
    def test_edit_from_review_state(self):
        r = self._scenario(bot.STATE_AI_REVIEW)
        self.assertEqual(r["db"]["data"]["name"], "John Damian", r)

    def test_edit_from_edit_menu_state(self):
        r = self._scenario(bot.STATE_AI_EDIT_MENU)
        self.assertEqual(r["db"]["data"]["name"], "John Damian", r)

    def test_edit_from_select_group_state(self):
        r = self._scenario(bot.STATE_SELECT_GROUP)
        self.assertEqual(r["db"]["data"]["name"], "John Damian", r)

    def test_edit_from_select_driver_state(self):
        r = self._scenario(bot.STATE_SELECT_DRIVER)
        self.assertEqual(r["db"]["data"]["name"], "John Damian", r)

    def test_edit_from_contact_source_state(self):
        r = self._scenario(bot.STATE_SELECT_CONTACT_SOURCE)
        self.assertEqual(r["db"]["data"]["name"], "John Damian", r)

    def test_edit_from_ghost_state(self):
        """An unknown/legacy state (e.g. STATE_WAITING_FILE) must still apply."""
        r = self._scenario(bot.STATE_WAITING_FILE)
        self.assertEqual(r["db"]["data"]["name"], "John Damian", r)

    def test_edit_after_restart_conversation_lost(self):
        """PTB conversation wiped (redeploy) - the DB row is the only survivor."""
        r = self._scenario(None)
        self.assertEqual(r["db"]["data"]["name"], "John Damian", r)

    def test_edit_when_notes_state_row(self):
        """Accept flipped the DB row to special_request_drivers; card still editable."""
        r = self._scenario(None, db_state="special_request_drivers")
        self.assertEqual(r["db"]["data"]["name"], "John Damian", r)

    # --- the price case the user reported -------------------------------
    def test_price_from_edit_menu_state(self):
        r = self._scenario(bot.STATE_AI_EDIT_MENU, "price 150")
        self.assertEqual(r["db"]["data"]["pending_price"], "$150", r)

    def test_price_from_select_driver_state(self):
        r = self._scenario(bot.STATE_SELECT_DRIVER, "price 150")
        self.assertEqual(r["db"]["data"]["pending_price"], "$150", r)

    def test_price_from_review_state(self):
        r = self._scenario(bot.STATE_AI_REVIEW, "price 150")
        self.assertEqual(r["db"]["data"]["pending_price"], "$150", r)

    # --- the card itself must visibly change ----------------------------
    def test_card_is_edited_on_screen(self):
        r = self._scenario(bot.STATE_AI_REVIEW)
        blob = chr(10).join(r["edited"] + r["sent"])
        # the card splits a full name across the First/Last name lines
        self.assertIn("John", blob, r)
        self.assertIn("Damian", blob, r)


if __name__ == "__main__":
    unittest.main()
