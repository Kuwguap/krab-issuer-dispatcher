"""The $100 insurance add-on holds the client's email until a dispatcher releases it.

Drives the REAL Application (production handler graph, real update processor),
like test_tag_pdf_reaches_the_group.py: a team taps Accept, the tag and the
FS-20 go to the group with the portal login and the release button — and the
CLIENT's inbox stays empty until someone taps 📧 Email insurance to client.
Exactly one email, however many taps, in however many chats.

Only the process boundaries are faked: Telegram transport, the portal HTTP call,
Resend, the two VIN decoders (network). The FS-20 PDF is built for real.

Run:  venv\\Scripts\\python.exe -m pytest tests/test_insurance_email_gate.py -q
"""
import os
import sys
import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy-key")
os.environ.pop("OPENAI_API_KEY", None)

import utils.database as udb  # noqa: E402

LEAD_ID = "33333333-3333-4333-8333-333333333333"
GROUP_ID = "44444444-4444-4444-8444-444444444444"
GROUP_CHAT = -100456
ACCEPTOR = 910001
CLIENT_EMAIL = "client@example.com"

NY_VEHICLE = "\n".join([
    "CHARLES JONES", "9 hibiscus Lane", "Monticello New York 13701",
    "9 hibiscus Lane", "Monticello New York 13701",
    "1N4AL3AP0HC166043", "2017 Nissan Altima", "Grey",
    "-", "-", "now 1 hour",
])

NJ_VEHICLE = "\n".join([
    "CHARLES JONES", "247 Knox Ave", "Cliffside Park NJ 07010",
    "247 Knox Ave", "Cliffside Park NJ 07010",
    "1N4AL3AP0HC166043", "2017 Nissan Altima", "Grey",
    "-", "-", "now 1 hour",
])


class FakeDB:
    """One lead, one group, and a real ledger of every update_lead payload."""

    def __init__(self):
        self.reset()

    def reset(self, *, email=CLIENT_EMAIL, vehicle=NY_VEHICLE):
        self.lead = {
            "id": LEAD_ID, "reference_id": "GATE1234", "price": "$250",
            "phone_number": "845-423-9476", "group_id": GROUP_ID,
            "vehicle_details": vehicle, "extra_info": "now 1 hour",
            "email": email, "wants_insurance": True,
        }
        self.update_payloads = []
        self.group_accepted = False

    def get_lead_by_id(self, lead_id):
        return dict(self.lead) if str(lead_id) == LEAD_ID else None

    def get_lead_by_reference_id(self, ref):
        return dict(self.lead) if str(ref).upper() == "GATE1234" else None

    def get_group_by_id(self, group_id):
        if str(group_id) != GROUP_ID:
            return None
        return {"id": GROUP_ID, "group_name": "HighKage", "is_active": True,
                "group_telegram_id": str(GROUP_CHAT)}

    def accept_group_lead_offer(self, lead_id, group_id, **kw):
        self.group_accepted = True
        return True

    def get_accepted_group_for_lead(self, lead_id):
        if not self.group_accepted:
            return None
        return {"lead_id": LEAD_ID, "group_id": GROUP_ID}

    def get_group_lead_offers(self, lead_id):
        return [{"group_id": GROUP_ID, "group_chat_id": str(GROUP_CHAT),
                 "group_message_id": 700}]

    def allocate_temp_plate(self, is_nj):
        return {"plate": "000003V", "control_number": "1234567890"}

    def update_lead(self, lead_id, updates):
        # Merge like the real row would, so claims and stamps are visible to the
        # re-reads the handlers do.
        self.update_payloads.append(dict(updates))
        if str(lead_id) == LEAD_ID:
            self.lead.update(updates)
        return True

    def claim_insurance_email(self, lead_id):
        if str(lead_id) != LEAD_ID:
            return False
        if str(self.lead.get("insurance_emailed_at") or "").strip():
            return False
        self.lead["insurance_emailed_at"] = "2026-08-26T12:00:00+00:00"
        return True

    def release_insurance_email_claim(self, lead_id, error=None):
        return self.update_lead(lead_id, {
            "insurance_emailed_at": None,
            "insurance_email_error": (error or "")[:500] or None,
        })

    def get_all_groups(self):
        return [self.get_group_by_id(GROUP_ID)]

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

    async def do_post(self, endpoint, data, **kwargs):
        self.calls.append((endpoint, dict(data or {})))
        if endpoint == "getMe":
            return {"id": 1, "is_bot": True, "first_name": "TestBot",
                    "username": "testbot"}
        if endpoint in ("sendMessage", "editMessageText", "sendDocument", "sendPhoto"):
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


def _portal_ok(payload, pdf_bytes=None):
    return SimpleNamespace(ok=True, status_code=200, error=None, payload={})


def _resend_recorder(record):
    def _send(**kwargs):
        record.append(kwargs)
        return SimpleNamespace(ok=True, error=None, status_code=200)
    return _send


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
                "message_id": 700, "date": 1700000000,
                "chat": {"id": GROUP_CHAT, "type": "supergroup", "title": "HighKage"},
                "from": {"id": 1, "is_bot": True, "first_name": "TestBot",
                         "username": "testbot"},
                "text": "New lead",
            },
        },
    }, app.bot)


def _email_button_update(app, mid):
    return Update.de_json({
        "update_id": mid,
        "callback_query": {
            "id": str(mid),
            "from": {"id": ACCEPTOR, "is_bot": False, "first_name": "Kita",
                     "username": "kita"},
            "chat_instance": "1",
            "data": f"ins_email_{LEAD_ID}",
            "message": {
                "message_id": 800, "date": 1700000000,
                "chat": {"id": GROUP_CHAT, "type": "supergroup", "title": "HighKage"},
                "from": {"id": 1, "is_bot": True, "first_name": "TestBot",
                         "username": "testbot"},
                "text": "🔐 Insurance portal login",
            },
        },
    }, app.bot)


def _command_update(app, mid, text):
    return Update.de_json({
        "update_id": mid,
        "message": {
            "message_id": mid, "date": 1700000000,
            "chat": {"id": GROUP_CHAT, "type": "supergroup", "title": "HighKage"},
            "from": {"id": ACCEPTOR, "is_bot": False, "first_name": "Kita",
                     "username": "kita"},
            "text": text,
            "entities": [{"type": "bot_command", "offset": 0,
                          "length": len(text.split()[0])}],
        },
    }, app.bot)


class InsuranceEmailGateTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = _build_application()

    def _run(self, updates, *, resend_record=None, resend_fail=False):
        record = resend_record if resend_record is not None else []

        def _failing_send(**kwargs):
            record.append(kwargs)
            return SimpleNamespace(ok=False, error="Resend down", status_code=502)

        async def go():
            with mock.patch.object(telegram.Bot, "_do_post", TRANSPORT.do_post), \
                 mock.patch.object(bot, "db", FAKE_DB), \
                 mock.patch.object(bot.Config, "INTEGRATIONS_API_KEY", "test-key"), \
                 mock.patch.object(bot.Config, "RESEND_API_KEY", "re_test"), \
                 mock.patch.object(bot.Config, "RESEND_FROM", "Cards <cards@x.com>"), \
                 mock.patch("utils.tristatecoverage_api.create_portal_client", _portal_ok), \
                 mock.patch("utils.resend_client.send_insurance_card_email",
                            _failing_send if resend_fail else _resend_recorder(record)), \
                 mock.patch("utils.insurance_card.decode_vin_from_nhtsa", lambda v: None), \
                 mock.patch("utils.tag_pdf.decode_vin_for_tag", lambda v: None):
                await self.app.initialize()
                for u in updates:
                    await self.app.process_update(u)
        asyncio.run(go())
        return record

    def test_accept_issues_everything_but_emails_nobody(self):
        FAKE_DB.reset()
        TRANSPORT.reset()
        emails = self._run([_accept_update(self.app, 8001)])

        self.assertEqual([], emails, "the client was emailed before payment")
        docs = [d for e, d in TRANSPORT.calls if e == "sendDocument"]
        self.assertEqual(2, len(docs),
                         f"expected tag + insurance PDFs; calls={TRANSPORT.endpoints()}")
        with_button = [d for e, d in TRANSPORT.calls if e == "sendMessage"
                       and f"ins_email_{LEAD_ID}" in str(d.get("reply_markup"))]
        self.assertTrue(with_button, "no release button reached the group")
        self.assertTrue(str(FAKE_DB.lead.get("insurance_card_sent_at") or "").strip(),
                        "issue was not persisted")
        self.assertTrue(str(FAKE_DB.lead.get("portal_password") or "").strip(),
                        "portal password was not persisted")
        self.assertFalse(str(FAKE_DB.lead.get("insurance_emailed_at") or "").strip(),
                         "the gate was stamped without a tap")
        for p in FAKE_DB.update_payloads:
            self.assertNotIn("portal_password_unchanged", p,
                             "the phantom column is back in a DB payload")

    def test_the_button_emails_exactly_once(self):
        FAKE_DB.reset()
        TRANSPORT.reset()
        emails = self._run([_accept_update(self.app, 8011)])
        self.assertEqual([], emails)
        stored_policy = FAKE_DB.lead.get("insurance_card_policy_number")
        self.assertTrue(stored_policy)

        TRANSPORT.reset()
        emails = self._run([_email_button_update(self.app, 8012)])
        self.assertEqual(1, len(emails), "one tap, one email")
        self.assertEqual(CLIENT_EMAIL, emails[0]["to_address"])
        self.assertIn(stored_policy, emails[0]["pdf_filename"],
                      "the deferred email must carry the ISSUED policy, not a new one")
        confirms = [d for e, d in TRANSPORT.calls if e == "sendMessage"
                    and "emailed to" in str(d.get("text", ""))]
        self.assertTrue(confirms, "nobody was told the email went out")

        # Second tap, wherever its button copy lives: no second email.
        emails2 = self._run([_email_button_update(self.app, 8013)])
        self.assertEqual([], emails2, "a second tap re-emailed the client")

    def test_resend_failure_reopens_the_claim(self):
        FAKE_DB.reset()
        TRANSPORT.reset()
        self._run([_accept_update(self.app, 8021)])
        attempted = self._run([_email_button_update(self.app, 8022)], resend_fail=True)
        self.assertEqual(1, len(attempted))
        self.assertFalse(str(FAKE_DB.lead.get("insurance_emailed_at") or "").strip(),
                         "a failed send kept the claim — retap would be refused")
        self.assertIn("Resend down", str(FAKE_DB.lead.get("insurance_email_error")))
        restored = [d for e, d in TRANSPORT.calls if e == "editMessageReplyMarkup"
                    and f"ins_email_{LEAD_ID}" in str(d.get("reply_markup"))]
        self.assertTrue(restored, "the button was not restored after the failure")
        # And the retry works.
        emails = self._run([_email_button_update(self.app, 8023)])
        self.assertEqual(1, len(emails))

    def test_no_email_lead_recovers_via_setclientemail(self):
        FAKE_DB.reset(email="")
        TRANSPORT.reset()
        emails = self._run([_accept_update(self.app, 8031)])
        self.assertEqual([], emails)
        docs = [d for e, d in TRANSPORT.calls if e == "sendDocument"]
        self.assertEqual(1, len(docs),
                         "only the tag PDF should go out for a no-email lead")
        bails = [d for e, d in TRANSPORT.calls if e == "sendMessage"
                 and "/setclientemail" in str(d.get("text", ""))]
        self.assertTrue(bails, "the group was not told how to fix the missing email")
        self.assertFalse(str(FAKE_DB.lead.get("insurance_card_sent_at") or "").strip())

        # The lead's own group sets the email; the held card is issued right there.
        TRANSPORT.reset()
        emails = self._run([_command_update(
            self.app, 8032, f"/setclientemail GATE1234 {CLIENT_EMAIL}")])
        self.assertEqual([], emails, "setting the email must not email the client")
        self.assertEqual(CLIENT_EMAIL, FAKE_DB.lead.get("email"))
        self.assertTrue(str(FAKE_DB.lead.get("insurance_card_sent_at") or "").strip(),
                        "the held card was not issued after the email arrived")
        with_button = [d for e, d in TRANSPORT.calls if e == "sendMessage"
                       and f"ins_email_{LEAD_ID}" in str(d.get("reply_markup"))]
        self.assertTrue(with_button, "no release button after the recovery issue")

        emails = self._run([_email_button_update(self.app, 8033)])
        self.assertEqual(1, len(emails))
        self.assertEqual(CLIENT_EMAIL, emails[0]["to_address"])

    def test_nj_lead_is_never_gated(self):
        FAKE_DB.reset(vehicle=NJ_VEHICLE)
        TRANSPORT.reset()
        nj_result = SimpleNamespace(ok=True, error=None, status_code=200,
                                    policy_number="NJP123", email=CLIENT_EMAIL)

        async def go():
            with mock.patch.object(telegram.Bot, "_do_post", TRANSPORT.do_post), \
                 mock.patch.object(bot, "db", FAKE_DB), \
                 mock.patch.object(bot.Config, "BARCODE_APP_BASE_URL", "https://nj.example"), \
                 mock.patch("utils.nj_card_api.send_nj_insurance_email", lambda p: nj_result), \
                 mock.patch("utils.insurance_card.decode_vin_from_nhtsa", lambda v: None), \
                 mock.patch("utils.tag_pdf.decode_vin_for_tag", lambda v: None):
                await self.app.initialize()
                await self.app.process_update(_accept_update(self.app, 8041))
        asyncio.run(go())

        self.assertTrue(str(FAKE_DB.lead.get("insurance_emailed_at") or "").strip(),
                        "NJ is emailed upstream at issue — must be stamped done")
        with_button = [d for d in (data for _, data in TRANSPORT.calls)
                       if f"ins_email_{LEAD_ID}" in str(d.get("reply_markup"))]
        self.assertEqual([], with_button, "an NJ lead grew a release button")


if __name__ == "__main__":
    unittest.main()
