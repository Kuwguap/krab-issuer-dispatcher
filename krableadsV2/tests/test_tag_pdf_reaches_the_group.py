"""The tag PDF must actually leave the process when a group accepts a lead.

Every existing tag test calls ``_send_all_tag_pdfs`` directly, so all of them
would still pass if the accept button never reached it. This one drives the REAL
Application — the production handler graph, the real update processor — with a
real ``ag_`` callback, and asserts a ``sendDocument`` hits the wire.

That distinction is the whole point: "the PDF doesn't send after the leads go
out" is a report about ROUTING, and a unit test on the sender cannot see it.

Run:  venv\\Scripts\\python.exe -m pytest tests/test_tag_pdf_reaches_the_group.py -q
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
ACCEPTOR = 900001
DRIVER_TG = 900002

VEHICLE = "\n".join([
    "CHARLES JONES", "9 hibiscus Lane", "Monticello New York 13701",
    "9 hibiscus Lane", "Monticello New York 13701",
    "1N4AL3AP0HC166043", "2017 Nissan Altima", "Grey",
    "Geico", "0407306000", "now 1 hour",
])

EXTRA = [{
    "name": "CHARLES G JONES", "address": "11530 Mango terrace drive apt.102",
    "city_state_zip": "Seffner Florida 33584", "vin": "4T1BF3EK6AU051219",
    "car": "2010 Toyota Camry", "color": "Grey",
    "insurance_company": "Progressive", "insurance_policy_number": "982658176",
    "plate": "000002V", "control_number": "1234567890",
}]


class FakeDB:
    """Just enough of the wrapper for one lead to be accepted by one group."""

    def __init__(self):
        self.lead = {
            "id": LEAD_ID, "reference_id": "ABC12345", "price": "$150",
            "phone_number": "845-423-9476", "group_id": GROUP_ID,
            "vehicle_details": VEHICLE, "extra_info": "now 1 hour",
            "status": "pending",
        }

    def get_lead_by_id(self, lead_id):
        return dict(self.lead) if str(lead_id) == LEAD_ID else None

    def get_group_by_id(self, group_id):
        if str(group_id) != GROUP_ID:
            return None
        # group_telegram_id is the column the sender reads; a group row without
        # it has no target and the post is skipped with only a log line.
        return {"id": GROUP_ID, "group_name": "HighKage", "is_active": True,
                "group_telegram_id": str(GROUP_CHAT)}

    def accept_group_lead_offer(self, lead_id, group_id, **kw):
        self.lead["group_id"] = GROUP_ID
        self.group_accepted = True
        return True

    def get_accepted_group_for_lead(self, lead_id):
        # Only after a team actually accepted. This row is what tells the driver
        # accept that the tag has already gone out, so a fake that always returns
        # one would hide every case the guard exists for.
        if not getattr(self, "group_accepted", False):
            return None
        return {"lead_id": LEAD_ID, "group_id": GROUP_ID}

    # --- the driver-assignment half of dispatch -------------------------
    def get_driver_by_telegram_id(self, tid):
        if str(tid) != str(DRIVER_TG):
            return None
        return {"id": "d1", "driver_name": "Susan", "is_active": True,
                "driver_telegram_id": str(DRIVER_TG)}

    def accept_lead_assignment(self, lead_id, driver_id):
        self.driver_accepted = True
        return {"id": "a1", "lead_id": LEAD_ID, "driver_id": "d1"}

    def get_lead_assignment_status(self, lead_id):
        # "accepted" only once a driver actually accepted: this row is what tells
        # the TEAM accept that the tag is already out, and a fake that always says
        # yes would suppress the sends the other tests exist to prove.
        if not getattr(self, "driver_accepted", False):
            return None
        return {"driver_id": "d1", "status": "accepted"}

    def apply_paper_on_lead_accept(self, *a, **k):
        return None

    def get_group_lead_offers(self, lead_id):
        return [{"group_id": GROUP_ID, "group_chat_id": str(GROUP_CHAT),
                 "group_message_id": 500}]

    def allocate_temp_plate(self, is_nj):
        return {"plate": "000001V", "control_number": "1234567890"}

    def update_lead(self, *a, **k):
        return True

    def get_all_groups(self):
        return [self.get_group_by_id(GROUP_ID)]

    def get_all_drivers(self):
        return []

    def __getattr__(self, name):
        def _noop(*a, **k):
            return [] if name.startswith("get_") else None
        return _noop


FAKE_DB = FakeDB()
udb.Database = mock.MagicMock(return_value=FAKE_DB)

import bot  # noqa: E402
import telegram  # noqa: E402
from telegram import Update  # noqa: E402

# NOT bound at module scope: bot.db is a global, another e2e module binds its own
# fake there, and whichever imported last would win for the whole suite. Each test
# below patches it for its own duration instead.


class Transport:
    def __init__(self):
        self.calls = []
        self.next_message_id = 1000

    def reset(self):
        self.calls.clear()

    def endpoints(self):
        return [e for e, _ in self.calls]

    async def do_post(self, endpoint, data, **kwargs):
        self.calls.append((endpoint, dict(data or {})))
        if endpoint == "getMe":
            return {"id": 1, "is_bot": True, "first_name": "TestBot",
                    "username": "testbot"}
        if endpoint in ("sendMessage", "editMessageText", "sendDocument"):
            self.next_message_id += 1
            return {
                "message_id": self.next_message_id, "date": 1700000000,
                "chat": {"id": data.get("chat_id", GROUP_CHAT), "type": "supergroup"},
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
    ]
    patches.append(mock.patch.object(bot, "db", FAKE_DB))
    for p in patches:
        p.start()
    try:
        bot.main()
    finally:
        for p in patches:
            p.stop()
    return captured["app"]


def _accept_update(app, mid):
    """The exact callback a group member's Accept tap produces."""
    data = "ag_" + bot._short_uuid(LEAD_ID) + bot._short_uuid(GROUP_ID)
    return Update.de_json({
        "update_id": mid,
        "callback_query": {
            "id": str(mid),
            "from": {"id": ACCEPTOR, "is_bot": False, "first_name": "Kita",
                     "username": "kita"},
            "chat_instance": "1",
            "data": data,
            "message": {
                "message_id": 500, "date": 1700000000,
                "chat": {"id": GROUP_CHAT, "type": "supergroup", "title": "HighKage"},
                "from": {"id": 1, "is_bot": True, "first_name": "TestBot",
                         "username": "testbot"},
                "text": "New lead",
            },
        },
    }, app.bot)


def _driver_accept_update(app, mid):
    """The callback a DRIVER's Accept tap produces, in their own chat."""
    return Update.de_json({
        "update_id": mid,
        "callback_query": {
            "id": str(mid),
            "from": {"id": DRIVER_TG, "is_bot": False, "first_name": "Susan",
                     "username": "susan"},
            "chat_instance": "2",
            "data": "accept_lead_" + LEAD_ID,
            "message": {
                "message_id": 600, "date": 1700000000,
                "chat": {"id": DRIVER_TG, "type": "private"},
                "from": {"id": 1, "is_bot": True, "first_name": "TestBot",
                         "username": "testbot"},
                "text": "New lead for you",
            },
        },
    }, app.bot)


class TagPdfReachesTheGroup(unittest.TestCase):
    """Tap Accept for real; a PDF must go out."""

    @classmethod
    def setUpClass(cls):
        cls.app = _build_application()

    def _accept(self, lead_extra=None):
        FAKE_DB.lead.pop("extra_vehicles", None)
        FAKE_DB.group_accepted = False
        FAKE_DB.driver_accepted = False
        if lead_extra is not None:
            FAKE_DB.lead["extra_vehicles"] = lead_extra
        TRANSPORT.reset()

        async def go():
            with mock.patch.object(telegram.Bot, "_do_post", TRANSPORT.do_post), \
                 mock.patch.object(bot, "db", FAKE_DB):
                await self.app.initialize()
                await self.app.process_update(_accept_update(self.app, 7001))
        asyncio.run(go())
        return [d for e, d in TRANSPORT.calls if e == "sendDocument"]

    def test_one_car_sends_one_tag(self):
        docs = self._accept()
        self.assertEqual(len(docs), 1,
                         f"no tag PDF reached the group; calls={TRANSPORT.endpoints()}")

    def test_a_driver_accepting_also_gets_the_tag(self):
        """The other half of dispatch.

        A lead sent to ONE named driver is accepted with accept_lead_, not ag_.
        If that path sends no tag, the operator sees the lead go out and then
        nothing — which is exactly the report this file exists to catch.
        """
        FAKE_DB.lead.pop("extra_vehicles", None)
        FAKE_DB.group_accepted = False
        FAKE_DB.driver_accepted = False
        TRANSPORT.reset()

        async def go():
            with mock.patch.object(telegram.Bot, "_do_post", TRANSPORT.do_post), \
                 mock.patch.object(bot, "db", FAKE_DB):
                await self.app.initialize()
                await self.app.process_update(_driver_accept_update(self.app, 7002))
        asyncio.run(go())
        docs = [d for e, d in TRANSPORT.calls if e == "sendDocument"]
        self.assertEqual(len(docs), 1,
                         f"driver accepted, no tag went out; calls={TRANSPORT.endpoints()}")

    def test_a_driver_accepting_a_two_car_lead_gets_both_tags(self):
        """One client, one receipt, two tags — released by the driver's Accept."""
        FAKE_DB.lead["extra_vehicles"] = EXTRA
        FAKE_DB.group_accepted = False
        FAKE_DB.driver_accepted = False
        TRANSPORT.reset()

        async def go():
            with mock.patch.object(telegram.Bot, "_do_post", TRANSPORT.do_post), \
                 mock.patch.object(bot, "db", FAKE_DB):
                await self.app.initialize()
                await self.app.process_update(_driver_accept_update(self.app, 7005))
        asyncio.run(go())
        FAKE_DB.lead.pop("extra_vehicles", None)
        docs = [d for e, d in TRANSPORT.calls if e == "sendDocument"]
        self.assertEqual(len(docs), 2,
                         f"second car got no tag; calls={TRANSPORT.endpoints()}")

    def test_a_driver_accepting_after_the_team_sends_no_second_tag(self):
        """The guard, from the other side.

        On a website lead the team accepts first and the tag is already out; the
        drivers are notified only afterwards. That driver's Accept must not put a
        duplicate of the same tag in the group.
        """
        FAKE_DB.lead.pop("extra_vehicles", None)
        FAKE_DB.group_accepted = True          # a team already released the tag
        TRANSPORT.reset()

        async def go():
            with mock.patch.object(telegram.Bot, "_do_post", TRANSPORT.do_post), \
                 mock.patch.object(bot, "db", FAKE_DB):
                await self.app.initialize()
                await self.app.process_update(_driver_accept_update(self.app, 7003))
        asyncio.run(go())
        docs = [d for e, d in TRANSPORT.calls if e == "sendDocument"]
        self.assertEqual(len(docs), 0,
                         f"tag sent twice for one lead; calls={TRANSPORT.endpoints()}")

    def test_a_team_accepting_after_the_driver_sends_no_second_tag(self):
        """The mirror of the guard above.

        Both offers are live on a manual lead. Whichever is tapped second must
        not put a duplicate of the same tag in the group.
        """
        FAKE_DB.lead.pop("extra_vehicles", None)
        FAKE_DB.group_accepted = False
        FAKE_DB.driver_accepted = True         # the driver already released it
        TRANSPORT.reset()

        async def go():
            with mock.patch.object(telegram.Bot, "_do_post", TRANSPORT.do_post), \
                 mock.patch.object(bot, "db", FAKE_DB):
                await self.app.initialize()
                await self.app.process_update(_accept_update(self.app, 7004))
        asyncio.run(go())
        docs = [d for e, d in TRANSPORT.calls if e == "sendDocument"]
        self.assertEqual(len(docs), 0,
                         f"tag sent twice for one lead; calls={TRANSPORT.endpoints()}")

    def test_two_cars_send_two_tags(self):
        docs = self._accept(EXTRA)
        self.assertEqual(len(docs), 2,
                         f"expected a tag per car; calls={TRANSPORT.endpoints()}")


if __name__ == "__main__":
    unittest.main()
