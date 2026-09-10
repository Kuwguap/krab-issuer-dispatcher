r"""The insurance card is made for the client even before we have their email.

Asked for: "show clearly if they opt in for insurance and create it for them and
also send to the group as well so supervisory id can either add email if not
available or release to client".

Held still here:
  * a NY lead on our cover with NO email still gets its policy and FS-20, posted
    to the group, the team chat and every supervisor (each once) under a
    "📧 Add client email" button; no portal account, no client email, and
    insurance_card_sent_to_email stays empty;
  * the same lead WITH an email gets the "📧 Email insurance to client" button;
  * an NJ lead with no email gets the notice and the button, and no card (its
    issuer emails the client itself, so it cannot be built without the address);
  * an already-armed lead keeps its policy number (1K3AX0XS: ABP6321786484), and
    /email afterwards makes the portal account under that SAME number and puts
    the release button up -- no second card, no second policy, no client email;
  * a failed build never wipes a stored policy number;
  * an issuer's own lead that has an email goes exactly where it always went.

Mocks only -- no network, no database, no Telegram, no insurance APIs.

Run:  venv\Scripts\python.exe -m pytest tests/test_insurance_card_without_email.py -q
"""
import asyncio
import os
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy-key")

import bot  # noqa: E402

LEAD_ID = "8c0e3a2e-6a55-4c41-9a2b-2f6a1f0f9c11"
GROUP_ID = "5d1f0c7e-2b8a-4f3e-9c6d-7a8b9c0d1e2f"
GROUP_CHAT = -100123
TEAM_CHAT = -100999
SUP_A = 555001
DRIVER_CHAT = 424242
GROUP = {"id": GROUP_ID, "group_name": "HighKage", "is_active": True,
         "group_telegram_id": str(GROUP_CHAT)}
EMAIL = "josue_jeanette@icloud.com"

NY_VEHICLE = "\n".join([
    "JOSUE PAVON", "2815 Dewey Avenue", "Bronx, NY 10465",
    "2815 Dewey Avenue", "Bronx, NY 10465",
    "1N4AL3AP0HC166043", "2017 Nissan Altima", "Grey", "-", "-", "-"])
NJ_VEHICLE = "\n".join([
    "JOSUE PAVON", "247 Knox Ave", "Cliffside Park NJ 07010",
    "247 Knox Ave", "Cliffside Park NJ 07010",
    "1N4AL3AP0HC166043", "2017 Nissan Altima", "Grey", "-", "-", "-"])


class RowDB:
    """One lead row that update_lead really changes, so every re-read sees it."""

    def __init__(self, lead):
        self.lead = dict(lead)
        self.updates = []

    def get_lead_by_id(self, lead_id):
        return dict(self.lead) if str(lead_id) == LEAD_ID else None

    def get_lead_by_reference_id(self, ref):
        return dict(self.lead) if str(ref).upper() == self.lead["reference_id"] else None

    def get_group_by_id(self, group_id):
        return dict(GROUP) if str(group_id) == GROUP_ID else None

    def update_lead(self, lead_id, updates):
        self.updates.append(dict(updates))
        self.lead.update(updates)
        return True


def _lead(**over):
    lead = {"id": LEAD_ID, "reference_id": "1K3AX0XS", "group_id": GROUP_ID,
            "vehicle_details": NY_VEHICLE, "price": "$250",
            "phone_number": "", "email": "", "wants_insurance": True,
            "external_order_id": "f85e6052", "contact_info_source": "Client Form"}
    lead.update(over)
    return lead


class World:
    """Every process boundary, faked and recorded."""

    def __init__(self, lead):
        self.db = RowDB(lead)
        self.portal, self.resend, self.nj, self.pdfs, self.minted = [], [], [], [], []
        self.ctx = mock.MagicMock()
        self.ctx.bot.send_message = mock.AsyncMock()
        self.ctx.bot.send_document = mock.AsyncMock()

    def _portal(self, payload, pdf_bytes=None):
        self.portal.append(dict(payload))
        return SimpleNamespace(ok=True, status_code=200, error=None, payload={}, warning=None)

    def _resend(self, **kw):
        self.resend.append(kw)
        return SimpleNamespace(ok=True, error=None, status_code=200)

    def _nj(self, payload):
        self.nj.append(payload)
        return SimpleNamespace(ok=True, error=None, status_code=200,
                               policy_number="NJP1", email=EMAIL)

    def _pdf(self, card_input):
        self.pdfs.append(card_input)
        return b"%PDF-1.4 fake"

    def _mint(self):
        self.minted.append(1)
        return f"ABP63NEW{len(self.minted):05d}"

    def run(self, coro_fn):
        patches = [
            mock.patch.object(bot, "db", self.db),
            mock.patch.object(bot, "followup_team_chat_id", lambda: TEAM_CHAT),
            # The group listed again (as a string) and one supervisor twice:
            # every chat must still get exactly one copy.
            mock.patch.object(bot, "_all_supervisory_chat_ids",
                              lambda: [SUP_A, str(GROUP_CHAT), SUP_A]),
            mock.patch.object(bot.Config, "INTEGRATIONS_API_KEY", "test-key"),
            mock.patch.object(bot.Config, "RESEND_API_KEY", "re_test"),
            mock.patch.object(bot.Config, "RESEND_FROM", "Cards <cards@x.com>"),
            mock.patch.object(bot.Config, "BARCODE_APP_BASE_URL", "https://nj.example"),
            mock.patch("utils.tristatecoverage_api.create_portal_client", self._portal),
            mock.patch("utils.resend_client.send_insurance_card_email", self._resend),
            mock.patch("utils.nj_card_api.send_nj_insurance_email", self._nj),
            mock.patch("utils.insurance_card.decode_vin_from_nhtsa", lambda v: None),
            mock.patch("utils.insurance_card.build_ny_insurance_id_card_pdf", self._pdf),
            mock.patch("utils.insurance_card.generate_policy_number", self._mint),
        ]
        for p in patches:
            p.start()
        try:
            return asyncio.run(coro_fn())
        finally:
            for p in reversed(patches):
                p.stop()

    def ride(self, target_chat_ids):
        return self.run(lambda: bot._maybe_ride_insurance_with_tag(
            self.ctx, dict(self.db.lead), list(target_chat_ids)))

    # -- what happened
    def doc_chats(self):
        return [c.kwargs["chat_id"] for c in self.ctx.bot.send_document.await_args_list]

    def messages(self):
        return [c.kwargs for c in self.ctx.bot.send_message.await_args_list]

    def buttons(self, kw):
        kb = kw.get("reply_markup")
        return [b.callback_data for row in (kb.inline_keyboard if kb else ()) for b in row]


def _norm(chats):
    return [bot._norm_chat_id(c) for c in chats]


class NoEmailNewYorkCardTest(unittest.TestCase):

    def test_the_card_is_built_and_posted_with_the_add_email_button(self):
        w = World(_lead())
        w.ride([GROUP_CHAT])
        self.assertEqual(1, len(w.pdfs), "the FS-20 was not built")
        self.assertEqual([], w.portal, "a portal account with no email")
        self.assertEqual([], w.resend, "the client was emailed")
        self.assertEqual(sorted([GROUP_CHAT, TEAM_CHAT, SUP_A]), sorted(_norm(w.doc_chats())),
                         "group + team + supervisors, once each")
        msgs = w.messages()
        self.assertEqual(sorted([GROUP_CHAT, TEAM_CHAT, SUP_A]),
                         sorted(_norm(m["chat_id"] for m in msgs)))
        for m in msgs:
            self.assertEqual([f"setem_{LEAD_ID}"], w.buttons(m))
            self.assertIn("/email 1K3AX0XS ", m["text"])
            self.assertLessEqual(len(f"setem_{LEAD_ID}".encode()), 64)
        row = w.db.lead
        self.assertTrue(str(row.get("insurance_card_sent_at") or "").strip())
        self.assertEqual(w.pdfs[0].policy_number, row["insurance_card_policy_number"])
        self.assertFalse(str(row.get("insurance_card_sent_to_email") or "").strip(),
                         "the missing email must stay visible")
        for u in w.db.updates:
            self.assertNotIn("portal_password", u)
            self.assertNotIn("insurance_emailed_at", u)

    def test_it_runs_once(self):
        w = World(_lead())
        w.ride([GROUP_CHAT])
        w.ride([GROUP_CHAT])
        self.assertEqual(1, len(w.pdfs), "a tag re-send re-issued the card")
        self.assertEqual(1, len(w.minted))

    def test_the_add_email_button_is_the_email_pickers_own(self):
        """Its tap is handled by handle_client_email_pick, which hands back the
        ready-to-send /email line -- registered top-level, so it works in groups."""
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        self.assertIn('handle_client_email_pick, pattern=f"^{SET_EMAIL_CB}"', src)
        kb = bot._insurance_card_keyboard(_lead())
        self.assertTrue(kb.inline_keyboard[0][0].callback_data.startswith(bot.SET_EMAIL_CB))


class WithEmailTest(unittest.TestCase):

    def test_a_web_client_with_an_email_gets_the_release_button_everywhere(self):
        w = World(_lead(email=EMAIL))
        w.ride([GROUP_CHAT])
        self.assertEqual(1, len(w.portal))
        self.assertEqual([], w.resend, "release is a person's tap, never automatic")
        self.assertEqual(sorted([GROUP_CHAT, TEAM_CHAT, SUP_A]), sorted(_norm(w.doc_chats())))
        for m in w.messages():
            self.assertEqual([f"ins_email_{LEAD_ID}"], w.buttons(m))
        self.assertEqual(EMAIL, w.db.lead["insurance_card_sent_to_email"])
        self.assertFalse(str(w.db.lead.get("insurance_emailed_at") or "").strip())


class NoEmailNewJerseyTest(unittest.TestCase):

    def test_a_notice_with_the_button_and_no_card(self):
        w = World(_lead(vehicle_details=NJ_VEHICLE))
        w.ride([GROUP_CHAT])
        self.assertEqual([], w.nj, "the NJ issuer was called with no address")
        self.assertEqual([], w.pdfs)
        self.assertEqual([], w.portal)
        self.assertEqual([], w.doc_chats())
        msgs = w.messages()
        self.assertEqual(sorted([GROUP_CHAT, TEAM_CHAT, SUP_A]),
                         sorted(_norm(m["chat_id"] for m in msgs)))
        for m in msgs:
            self.assertIn("not issued", m["text"])
            self.assertEqual([f"setem_{LEAD_ID}"], w.buttons(m))
        self.assertEqual([], w.db.updates, "nothing may be stamped for an unissued card")


def _email_command(w, email, chat_id=SUP_A):
    message = mock.MagicMock()
    message.reply_text = mock.AsyncMock()
    message.chat_id = chat_id
    update = mock.MagicMock()
    update.effective_message = message
    update.effective_user = types.SimpleNamespace(id=SUP_A, username="boss")
    w.ctx.args = ["1K3AX0XS", email]

    async def go():
        with mock.patch.object(bot, "_user_is_global_supervisor", lambda _uid: True):
            await bot.cmd_set_client_email(update, w.ctx)
    w.run(go)
    return message


class ArmedLeadKeepsItsPolicyTest(unittest.TestCase):
    """1K3AX0XS: armed, ABP6321786484 already printed on its tag, no email."""

    def _armed(self, **over):
        return _lead(insurance_card_policy_number="ABP6321786484", **over)

    def test_the_card_carries_the_existing_policy(self):
        w = World(self._armed())
        w.ride([GROUP_CHAT])
        self.assertEqual("ABP6321786484", w.pdfs[0].policy_number)
        self.assertEqual([], w.minted, "a second policy was minted")
        self.assertEqual("ABP6321786484", w.db.lead["insurance_card_policy_number"])

    def test_email_afterwards_makes_the_portal_account_under_the_same_policy(self):
        w = World(self._armed())
        w.ride([GROUP_CHAT])
        issued_at = w.db.lead["insurance_card_sent_at"]
        cards_before = len(w.doc_chats())
        w.ctx.bot.send_message.reset_mock()

        message = _email_command(w, EMAIL)

        self.assertEqual(EMAIL, w.db.lead["email"])
        self.assertEqual(1, len(w.portal), "the portal account was not provisioned")
        self.assertEqual("ABP6321786484", w.portal[0]["policyNumber"])
        self.assertEqual(EMAIL, w.portal[0]["email"])
        self.assertEqual([], w.minted, "a second policy was minted")
        self.assertEqual({"ABP6321786484"}, {p.policy_number for p in w.pdfs})
        self.assertEqual(w.pdfs[0].effective_mm_dd_yyyy, w.pdfs[-1].effective_mm_dd_yyyy,
                         "the portal must carry the card's own dates")
        self.assertEqual(cards_before, len(w.doc_chats()), "a second card was posted")
        self.assertEqual([], w.resend, "the client was emailed without a tap")
        row = w.db.lead
        self.assertEqual("ABP6321786484", row["insurance_card_policy_number"])
        self.assertEqual(issued_at, row["insurance_card_sent_at"])
        self.assertEqual(EMAIL, row["insurance_card_sent_to_email"])
        self.assertEqual(bot.PORTAL_DEFAULT_PASSWORD, row["portal_password"])
        self.assertFalse(str(row.get("insurance_emailed_at") or "").strip())
        msgs = w.messages()
        self.assertEqual(sorted([SUP_A, GROUP_CHAT, TEAM_CHAT]),
                         sorted(_norm(m["chat_id"] for m in msgs)),
                         "everyone who saw the card sees the release button")
        for m in msgs:
            self.assertEqual([f"ins_email_{LEAD_ID}"], w.buttons(m))
            self.assertIn("ABP6321786484", m["text"])
        self.assertIn("portal login", " ".join(str(c.args[0]) for c in
                                                message.reply_text.await_args_list))

    def test_a_second_email_does_not_redo_it(self):
        w = World(self._armed())
        w.ride([GROUP_CHAT])
        _email_command(w, EMAIL)
        _email_command(w, "other@example.com")
        self.assertEqual(1, len(w.portal), "a finished card was provisioned twice")

    def test_a_failed_build_never_wipes_the_policy(self):
        """A build that fails before it holds a policy (here: the portal is not
        configured) returns None for it -- and that None used to be written over
        the number the tag had already printed, so the next try minted another."""
        w = World(self._armed())
        with mock.patch.object(bot.Config, "is_portal_integration_configured", lambda: False):
            w.ride([GROUP_CHAT])
        self.assertEqual([], w.pdfs)
        self.assertTrue(w.db.updates, "the failure was not recorded")
        for u in w.db.updates:
            self.assertNotIn("insurance_card_policy_number", u)
        self.assertEqual("ABP6321786484", w.db.lead["insurance_card_policy_number"])
        self.assertIn("INTEGRATIONS_API_KEY", w.db.lead["insurance_card_error"])
        self.assertFalse(str(w.db.lead.get("insurance_card_sent_at") or "").strip())


class IssuerLeadIsUnchangedTest(unittest.TestCase):
    """No external_order_id, an email on file: exactly the old behaviour."""

    def test_it_goes_where_it_always_went(self):
        w = World(_lead(email="client@example.com", external_order_id=None,
                        contact_info_source=None, reference_id="ISSU1234"))
        w.ride([DRIVER_CHAT])
        self.assertEqual([DRIVER_CHAT, GROUP_CHAT, TEAM_CHAT], _norm(w.doc_chats()),
                         "the supervisors must not start receiving issuer cards")
        captions = {c.kwargs["caption"] for c in w.ctx.bot.send_document.await_args_list}
        self.assertEqual({"🛡 Insurance card — $100 add-on. NOT emailed to the client yet."},
                         captions)
        msgs = w.messages()
        self.assertEqual([DRIVER_CHAT, GROUP_CHAT, TEAM_CHAT], _norm(m["chat_id"] for m in msgs))
        for m in msgs:
            self.assertEqual([f"ins_email_{LEAD_ID}"], w.buttons(m))
            self.assertIn("NOT emailed to the client yet", m["text"])
        self.assertEqual(1, len(w.portal))
        self.assertEqual([], w.resend)
        self.assertEqual(1, len(w.db.updates))
        payload = w.db.updates[0]
        self.assertEqual({"insurance_card_policy_number", "insurance_card_sent_to_email",
                          "insurance_card_sent_at", "insurance_card_error",
                          "portal_email", "portal_password"}, set(payload))
        self.assertEqual("client@example.com", payload["insurance_card_sent_to_email"])
        self.assertEqual("client@example.com", payload["portal_email"])
        self.assertEqual(bot.PORTAL_DEFAULT_PASSWORD, payload["portal_password"])
        self.assertIsNone(payload["insurance_card_error"])


if __name__ == "__main__":
    unittest.main()
