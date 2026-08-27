"""Skip Dispatch: pull the lead back, pick a driver, pay or type the password.

Driven through the REAL handler graph, because every part of this feature is a
routing question -- which handler hears the password, whether the picker button
survives a restart, whether the deletes actually go to Telegram.

Run:  venv\\Scripts\\python.exe -m pytest tests/test_skip_dispatch.py -q
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
os.environ.pop("OPENAI_API_KEY", None)

import utils.database as udb  # noqa: E402

LEAD_ID = "11111111-1111-4111-8111-111111111111"
GROUP_ID = "22222222-2222-4222-8222-222222222222"
GROUP_CHAT = -100123
ISSUER = 900500
DRIVER_TG = 900600
PASSWORD = "AdminPassword123!"

VEHICLE = "\n".join([
    "CHARLES JONES", "9 hibiscus Lane", "Monticello New York 13701",
    "9 hibiscus Lane", "Monticello New York 13701",
    "1N4AL3AP0HC166043", "2017 Nissan Altima", "Grey",
    "Geico", "0407306000", "now 1 hour",
])


class FakeDB:
    def __init__(self):
        self.lead = {
            "id": LEAD_ID, "reference_id": "ABC12345", "price": "$150",
            "phone_number": "845-423-9476", "group_id": GROUP_ID,
            "vehicle_details": VEHICLE, "extra_info": "now 1 hour",
        }

    def get_lead_by_id(self, lead_id):
        return dict(self.lead) if str(lead_id) == LEAD_ID else None

    def get_group_by_id(self, group_id):
        return self.get_all_groups()[0] if str(group_id) == GROUP_ID else None

    def get_all_groups(self):
        return [{"id": GROUP_ID, "group_name": "HighKage", "is_active": True,
                 "group_telegram_id": str(GROUP_CHAT)}]

    def get_all_drivers(self):
        return [{"id": "d1", "driver_name": "Susan", "is_active": True,
                 "driver_telegram_id": str(DRIVER_TG)}]

    def get_group_lead_offers(self, lead_id):
        return [{"group_id": GROUP_ID, "group_chat_id": str(GROUP_CHAT),
                 "group_message_id": 4242}]

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


class Transport:
    def __init__(self):
        self.calls = []
        self.next_message_id = 2000

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
                "chat": {"id": data.get("chat_id", ISSUER), "type": "private"},
                "from": {"id": 1, "is_bot": True, "first_name": "TestBot",
                         "username": "testbot"},
                "text": data.get("text", ""),
            }
        return True


TRANSPORT = Transport()


def _build_application():
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


def _tap(app, data, mid):
    return Update.de_json({
        "update_id": mid,
        "callback_query": {
            "id": str(mid),
            "from": {"id": ISSUER, "is_bot": False, "first_name": "Boss",
                     "username": "boss"},
            "chat_instance": "1",
            "data": data,
            "message": {
                "message_id": 900, "date": 1700000000,
                "chat": {"id": ISSUER, "type": "private"},
                "from": {"id": 1, "is_bot": True, "first_name": "TestBot",
                         "username": "testbot"},
                "text": "Lead sent",
            },
        },
    }, app.bot)


def _typed(app, text, mid):
    return Update.de_json({
        "update_id": mid,
        "message": {
            "message_id": mid, "date": 1700000000,
            "chat": {"id": ISSUER, "type": "private"},
            "from": {"id": ISSUER, "is_bot": False, "first_name": "Boss",
                     "username": "boss"},
            "text": text,
        },
    }, app.bot)


class SkipDispatchTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = _build_application()

    def _run(self, *updates):
        TRANSPORT.reset()

        async def go():
            with mock.patch.object(telegram.Bot, "_do_post", TRANSPORT.do_post), \
                 mock.patch.object(bot, "db", FAKE_DB), \
                 mock.patch.object(bot, "request_instant_pdf_link",
                                   mock.AsyncMock(return_value=("https://pay.test/x", None))):
                await self.app.initialize()
                for u in updates:
                    await self.app.process_update(u)
        asyncio.run(go())

    # -- the button ------------------------------------------------------
    def test_the_button_says_skip_dispatch(self):
        kb = bot._after_send_keyboard(LEAD_ID)
        labels = [b.text for row in kb.inline_keyboard for b in row]
        self.assertIn("🚫 Skip Dispatch", labels)
        self.assertFalse([l for l in labels if "Instant PDF" in l],
                         "the old label is still on the card")

    # -- step 1: pull it back -------------------------------------------
    def test_tapping_it_deletes_what_went_to_the_team(self):
        self._run(_tap(self.app, f"{bot.INSTANT_PDF_CB}{LEAD_ID}", 8001))
        deletes = TRANSPORT.of("deleteMessage")
        self.assertTrue(deletes, f"nothing was recalled; calls={TRANSPORT.endpoints()}")
        self.assertEqual(str(deletes[0].get("chat_id")), str(GROUP_CHAT))
        self.assertEqual(int(deletes[0].get("message_id")), 4242)

    def test_it_then_offers_the_drivers(self):
        self._run(_tap(self.app, f"{bot.INSTANT_PDF_CB}{LEAD_ID}", 8002))
        sent = "".join(str(d) for d in TRANSPORT.of("sendMessage"))
        self.assertIn("Susan", sent, "the driver picker never appeared")
        self.assertIn(bot.SKIP_DISPATCH_DRIVER_CB, sent)

    # -- step 2: pick, then pay or type ---------------------------------
    def test_picking_a_driver_offers_both_ways_to_release_it(self):
        self._run(_tap(self.app, f"{bot.SKIP_DISPATCH_DRIVER_CB}{LEAD_ID}|d1", 8003))
        sent = " ".join(d.get("text", "") for d in TRANSPORT.of("sendMessage"))
        self.assertIn("password", sent.lower())
        self.assertIn("100", sent)

    def test_the_password_releases_the_tag(self):
        self._run(
            _tap(self.app, f"{bot.SKIP_DISPATCH_DRIVER_CB}{LEAD_ID}|d1", 8004),
            _typed(self.app, PASSWORD, 8005),
        )
        docs = TRANSPORT.of("sendDocument")
        self.assertTrue(docs, f"no tag went out; calls={TRANSPORT.endpoints()}")
        chats = {str(d.get("chat_id")) for d in docs}
        self.assertIn(str(DRIVER_TG), chats, "the driver did not get the tag")
        self.assertIn(str(GROUP_CHAT), chats, "the groups did not get the tag")

    def test_the_password_is_removed_from_the_chat(self):
        self._run(
            _tap(self.app, f"{bot.SKIP_DISPATCH_DRIVER_CB}{LEAD_ID}|d1", 8006),
            _typed(self.app, PASSWORD, 8007),
        )
        deleted = [int(d.get("message_id")) for d in TRANSPORT.of("deleteMessage")]
        self.assertIn(8007, deleted,
                      "the password is still sitting in the chat history")

    def test_a_wrong_password_releases_nothing(self):
        self._run(
            _tap(self.app, f"{bot.SKIP_DISPATCH_DRIVER_CB}{LEAD_ID}|d1", 8008),
            _typed(self.app, "AdminPassword123", 8009),      # one character short
        )
        self.assertEqual(TRANSPORT.of("sendDocument"), [],
                         "a near-miss password released the tag")

    def test_a_password_typed_with_nothing_pending_is_ignored(self):
        """This listener sits above every conversation. If it acted on a chat that
        never tapped Skip Dispatch, it would swallow whatever that chat was
        actually saying."""
        self._run(_typed(self.app, PASSWORD, 8010))
        self.assertEqual(TRANSPORT.of("sendDocument"), [],
                         "the tag went out without anyone asking for it")

    # -- the operator can rotate it -------------------------------------
    def test_the_password_comes_from_the_environment(self):
        with mock.patch.dict(os.environ, {"SKIP_DISPATCH_PASSWORD": "something-else"}):
            self.assertEqual(bot._skip_dispatch_password(), "something-else")
        self.assertEqual(bot._skip_dispatch_password(), PASSWORD)


if __name__ == "__main__":
    unittest.main()
