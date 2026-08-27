"""THE CHAT LAYER, proven in the REAL handler graph.

test_chat_layer.py pins the layer's behavior by calling
handle_phase1_review_message directly — so all of it would still pass if the
front door were unreachable, mis-registered, or outranked by the wrong net.
This file drives the production Application (bot.main() with polling stubbed,
real PTB routing, Telegram intercepted at Bot._do_post) and proves the four
things only ROUTING can break:

  * with the layer on, a review-state text reaches the model FIRST and the
    model's tool call edits the card — the deterministic ladder is never run;
  * the moment the model abstains (classify -> None), the SAME text lands via
    the untouched deterministic ladder;
  * a driver answering "accept" on an open offer is settled at group -4 before
    the layer can read it: the lead is accepted and classify is never called;
  * the Skip Dispatch password net (group -46) outranks everything — the
    password releases the tag and classify is never called, even though the
    same user is parked on a live review card the layer would otherwise read.

Run:  venv\\Scripts\\python.exe -m pytest tests/test_chat_layer_e2e.py -q
"""
import os
import sys
import time
import asyncio
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy-key")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11")
os.environ.pop("OPENAI_API_KEY", None)

import utils.database as udb  # noqa: E402

LEAD_ID = "33333333-3333-4333-8333-333333333333"
GROUP_ID = "44444444-4444-4444-8444-444444444444"
GROUP_CHAT = -100777
USER_ID = 555000777          # the operator, working a review card in their DM
CHAT_ID = USER_ID
DRIVER_TG = 900700           # the driver, in their own DM

VEHICLE = "\n".join([
    "CHARLES JONES", "9 hibiscus Lane", "Monticello New York 13701",
    "9 hibiscus Lane", "Monticello New York 13701",
    "1N4AL3AP0HC166043", "2017 Nissan Altima", "Grey",
    "Geico", "0407306000", "now 1 hour",
])


class FakeDB:
    """States for the review flow + one lead a driver can accept or be skipped to."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.states = {}
        self.driver_accepted = False
        self.group_accepted = False
        self.offer_open = False
        self.lead = {
            "id": LEAD_ID, "reference_id": "XYZ98765", "price": "$150",
            "phone_number": "845-423-9476", "group_id": GROUP_ID,
            "vehicle_details": VEHICLE, "extra_info": "now 1 hour",
            "status": "pending",
        }

    # --- user states (the review card's persistence) --------------------
    def get_user_state(self, user_id):
        row = self.states.get(int(user_id))
        return {"state": row["state"], "data": dict(row["data"])} if row else None

    def set_user_state(self, user_id, state, data=None):
        self.states[int(user_id)] = {"state": state, "data": dict(data or {})}
        return True

    def clear_user_state(self, user_id):
        self.states.pop(int(user_id), None)
        return True

    # --- lookup tables the review card renders from ---------------------
    def get_all_groups(self):
        return [{"id": GROUP_ID, "group_name": "HighKage", "is_active": True,
                 "group_telegram_id": str(GROUP_CHAT)}]

    def get_group_by_id(self, group_id):
        return self.get_all_groups()[0] if str(group_id) == GROUP_ID else None

    def get_all_drivers(self):
        return [{"id": "d1", "driver_name": "Susan", "is_active": True,
                 "driver_telegram_id": str(DRIVER_TG)}]

    def get_contact_info_sources(self):
        return [{"id": "s1", "label": "Facebook", "is_active": True}]

    def get_suspended_drivers(self):
        return []

    def get_driver_penalties(self):
        return []

    # --- the lead, and the driver's open offer ---------------------------
    def get_lead_by_id(self, lead_id):
        return dict(self.lead) if str(lead_id) == LEAD_ID else None

    def get_driver_by_telegram_id(self, tid):
        if str(tid) != str(DRIVER_TG):
            return None
        return {"id": "d1", "driver_name": "Susan", "is_active": True,
                "driver_telegram_id": str(DRIVER_TG)}

    def get_driver_pending_assignment(self, driver_id):
        # The switch handle_driver_word_answer reads BEFORE it reads the text:
        # no open offer means the word "accept" is ordinary conversation.
        if self.offer_open and str(driver_id) == "d1":
            return {"lead_id": LEAD_ID, "driver_id": "d1", "status": "pending"}
        return None

    def accept_lead_assignment(self, lead_id, driver_id):
        self.driver_accepted = True
        return {"id": "a1", "lead_id": LEAD_ID, "driver_id": str(driver_id)}

    def get_lead_assignment_status(self, lead_id):
        if not self.driver_accepted:
            return None
        return {"driver_id": "d1", "status": "accepted"}

    def get_accepted_group_for_lead(self, lead_id):
        if not self.group_accepted:
            return None
        return {"lead_id": LEAD_ID, "group_id": GROUP_ID}

    def get_group_lead_offers(self, lead_id):
        # A team was asked, so a driver accept defers the tag to the team's
        # release — the accept itself is what this file cares about.
        return [{"group_id": GROUP_ID, "group_chat_id": str(GROUP_CHAT),
                 "group_message_id": 500}]

    def apply_paper_on_lead_accept(self, *a, **k):
        return None

    def allocate_temp_plate(self, is_nj):
        return {"plate": "000001V", "control_number": "1234567890"}

    def update_lead(self, *a, **k):
        return True

    def __getattr__(self, name):
        def _noop(*a, **k):
            return [] if name.startswith("get_") else None
        return _noop


FAKE_DB = FakeDB()
udb.Database = mock.MagicMock(return_value=FAKE_DB)

import bot  # noqa: E402
import telegram  # noqa: E402
from telegram import Update  # noqa: E402
from utils import nl_router  # noqa: E402

# NOT bound at module scope: bot.db is a global another e2e module may own for
# the duration of a full-suite run. Every test below patches it for itself.


class Transport:
    def __init__(self):
        self.calls = []
        self.next_message_id = 3000

    def reset(self):
        self.calls.clear()

    def endpoints(self):
        return [e for e, _ in self.calls]

    def of(self, endpoint):
        return [d for e, d in self.calls if e == endpoint]

    async def do_post(self, endpoint, data, **kwargs):
        self.calls.append((endpoint, dict(data or {})))
        if endpoint == "getMe":
            return {"id": 1, "is_bot": True, "first_name": "TestBot",
                    "username": "testbot"}
        if endpoint in ("sendMessage", "editMessageText", "sendDocument"):
            self.next_message_id += 1
            return {
                "message_id": self.next_message_id, "date": 1700000000,
                "chat": {"id": data.get("chat_id", CHAT_ID), "type": "private"},
                "from": {"id": 1, "is_bot": True, "first_name": "TestBot",
                         "username": "testbot"},
                "text": data.get("text", ""),
            }
        return True


TRANSPORT = Transport()


def _build_application():
    """The REAL handler graph: run main() with polling stubbed, capture the app."""
    captured = {}

    def _fake_run_polling(self, *a, **k):
        captured["app"] = self

    patches = [
        mock.patch.object(bot.Application, "run_polling", _fake_run_polling),
        mock.patch.object(bot, "_wait_for_exclusive_polling", lambda *a, **k: True),
        mock.patch("requests.post", mock.MagicMock()),
        mock.patch.object(bot.Config, "validate", classmethod(lambda cls: True)),
        mock.patch.object(telegram.Bot, "_do_post", TRANSPORT.do_post),
        mock.patch.object(bot, "db", FAKE_DB),
    ]
    for p in patches:
        p.start()
    try:
        bot.main()
    finally:
        for p in patches:
            p.stop()
    return captured["app"]


def _text_update(app, text, mid, uid=USER_ID):
    return Update.de_json({
        "update_id": mid,
        "message": {
            "message_id": mid, "date": 1700000000,
            "chat": {"id": uid, "type": "private"},
            "from": {"id": uid, "is_bot": False, "first_name": "Tester",
                     "username": "tester"},
            "text": text,
        },
    }, app.bot)


def _tool(name, **args):
    """The exact shape nl_router.classify returns for a chosen tool."""
    return {"intent": name, "args": args, "tool": name}


# A live review card, mid-flow (the shape test_real_routing_e2e proved real).
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


class ChatLayerRoutingTest(unittest.TestCase):
    """One text into the real graph; who answered it is the whole question."""

    @classmethod
    def setUpClass(cls):
        cls.app = _build_application()

    # ------------------------------------------------------------------ helpers
    def _reset(self):
        TRANSPORT.reset()
        FAKE_DB.reset()
        # Module globals other test files in the same pytest process feed.
        bot._ALL_DRIVERS_CACHE = None
        for uid in (USER_ID, DRIVER_TG):
            try:
                self.app.user_data[uid].clear()
            except Exception:
                pass
        conv = bot._MAIN_CONV_HANDLER
        if conv is not None:
            conv._conversations.pop((CHAT_ID, USER_ID), None)
            conv._conversations.pop((DRIVER_TG, DRIVER_TG), None)

    def _seed_review(self):
        """The operator mid-review: a phase1 row in the DB, a live card id in
        RAM, the conversation parked exactly where a real parse leaves it."""
        FAKE_DB.set_user_state(USER_ID, "phase1", dict(_REVIEW_DATA))
        self.app.user_data[USER_ID]["review_message_id"] = 900
        self.app.user_data[USER_ID]["review_chat_id"] = CHAT_ID
        bot._MAIN_CONV_HANDLER._conversations[(CHAT_ID, USER_ID)] = bot.STATE_AI_REVIEW

    def _drive(self, update, *, classify_result, layer_on=True):
        """Push one real update through Application.process_update.

        Returns {"classify": n, "ladder": n}: how many times the model was
        consulted, and how many times the deterministic inline parser ran.
        classify is a MOCK — no network — but the gate in front of it
        (_chat_layer_enabled, the parked-slot OR, the handler ladder) is the
        production code under test.
        """
        calls = {"classify": 0, "ladder": 0}
        real_ladder = bot._apply_inline_review_text

        def _classify(text, **kw):
            calls["classify"] += 1
            return dict(classify_result) if isinstance(classify_result, dict) \
                else classify_result

        def _ladder(*a, **k):
            calls["ladder"] += 1
            return real_ladder(*a, **k)

        env = {"KRAB_CHAT_LAYER": "1" if layer_on else "0"}

        async def go():
            with mock.patch.object(telegram.Bot, "_do_post", TRANSPORT.do_post), \
                 mock.patch.object(bot, "db", FAKE_DB), \
                 mock.patch.dict(os.environ, env), \
                 mock.patch.object(nl_router, "classify", _classify), \
                 mock.patch.object(nl_router, "is_configured", lambda: True), \
                 mock.patch.object(nl_router, "breaker_open", lambda: False), \
                 mock.patch.object(bot, "_apply_inline_review_text", _ladder), \
                 mock.patch.object(bot.Config, "is_ai_vision_configured",
                                   classmethod(lambda cls: False)):
                await self.app.initialize()
                await self.app.process_update(update)
        asyncio.run(go())
        return calls

    def _card(self):
        st = FAKE_DB.get_user_state(USER_ID)
        return (st or {}).get("data") or {}

    # ------------------------------------------------- (1) the model reads first
    def test_the_model_answers_a_review_text_before_any_parser_runs(self):
        """Layer on, classify -> update_lead(color): the card changes and the
        deterministic ladder is never even consulted."""
        self._reset()
        self._seed_review()
        calls = self._drive(
            _text_update(self.app, "color white", 9101),
            classify_result=_tool("update_lead", field="color", value="white"))
        self.assertEqual(calls["classify"], 1,
                         "the model must be consulted exactly once")
        self.assertEqual(calls["ladder"], 0,
                         "the deterministic parser ran although the model had answered")
        self.assertEqual((self._card().get("color") or "").lower(), "white",
                         self._card())
        toasts = " ".join(d.get("text", "") for d in TRANSPORT.of("sendMessage"))
        self.assertIn("Updated", toasts,
                      f"no feedback reached the chat; calls={TRANSPORT.endpoints()}")

    # -------------------------------------------- (2) abstain -> the old ladder
    def test_when_the_model_abstains_the_same_text_lands_via_the_old_ladder(self):
        self._reset()
        self._seed_review()
        calls = self._drive(_text_update(self.app, "color white", 9102),
                            classify_result=None)
        self.assertEqual(calls["classify"], 1,
                         "with the layer on the model is still consulted first")
        self.assertGreaterEqual(calls["ladder"], 1,
                                "the deterministic ladder never saw the text")
        self.assertEqual((self._card().get("color") or "").lower(), "white",
                         self._card())

    def test_the_kill_switch_keeps_the_model_out_and_the_ladder_working(self):
        """KRAB_CHAT_LAYER=0 — the contract conftest.py holds the other ~34
        suites to, proven at graph level: zero model calls, same edit."""
        self._reset()
        self._seed_review()
        calls = self._drive(_text_update(self.app, "color white", 9103),
                            classify_result=None, layer_on=False)
        self.assertEqual(calls["classify"], 0,
                         "the kill switch did not keep the model out")
        self.assertEqual((self._card().get("color") or "").lower(), "white",
                         self._card())

    # ------------------------------------- (3) a driver's word answer outranks it
    def test_a_drivers_accept_is_settled_at_group_minus_4_not_by_the_model(self):
        """An open offer + the word "accept": the lead is accepted through the
        button's own handler, and the layer — armed, configured, breaker
        closed — never reads the message (group -4 stops the update)."""
        self._reset()
        FAKE_DB.offer_open = True
        calls = self._drive(
            _text_update(self.app, "accept", 9104, uid=DRIVER_TG),
            classify_result=_tool("update_lead", field="color", value="white"))
        self.assertEqual(calls["classify"], 0,
                         "the chat layer read a driver's answer before group -4")
        self.assertTrue(FAKE_DB.driver_accepted,
                        f"the offer was not accepted; calls={TRANSPORT.endpoints()}")

    # ------------------------------------ (4) the password net outranks everything
    def test_the_skip_dispatch_password_outranks_the_layer_and_the_review(self):
        """The operator is parked on a live review card — the exact situation
        where the layer WOULD read their next text — with Skip Dispatch armed.
        The password must be consumed at group -46: tag released, model never
        called, and not a character of the password lands on the card."""
        self._reset()
        self._seed_review()
        self.app.user_data[USER_ID][bot.SKIP_DISPATCH_PENDING_KEY] = {
            "lead_id": LEAD_ID, "driver_id": "d1", "at": time.time(),
        }
        calls = self._drive(
            _text_update(self.app, bot._skip_dispatch_password(), 9105),
            classify_result=_tool("update_lead", field="color", value="white"))
        self.assertEqual(calls["classify"], 0,
                         "the chat layer read the Skip Dispatch password")
        docs = TRANSPORT.of("sendDocument")
        self.assertTrue(docs,
                        f"the password did not release the tag; calls={TRANSPORT.endpoints()}")
        chats = {str(d.get("chat_id")) for d in docs}
        self.assertIn(str(DRIVER_TG), chats, "the driver did not get the tag")
        self.assertEqual(self._card().get("color"), "-",
                         "the password leaked into the review card")
        deleted = [int(d.get("message_id")) for d in TRANSPORT.of("deleteMessage")]
        self.assertIn(9105, deleted,
                      "the password is still sitting in the chat history")


if __name__ == "__main__":
    unittest.main()
