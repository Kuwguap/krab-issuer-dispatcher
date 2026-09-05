r"""tristatetags.com/receipts as a full back-office page — contact columns for
every party (Client, Driver, Issuer, Dispatcher) and per-party send buttons
wired to the REUSED senders: client_outreach's Resend→SendGrid email, and
GoHighLevel SMS (utils/ghl_client) with the existing Twilio sender as fallback.

Run:  venv\Scripts\python.exe -m pytest tests/test_receipts_board_v2.py -q
"""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy-key")

import admin_dashboard as ad  # noqa: E402
import receipts_page  # noqa: E402

LEAD = "11111111-2222-3333-4444-555555555555"


def _chained(data):
    """A query-builder double: every chained method returns itself, execute()
    hands back the canned rows."""
    m = mock.MagicMock()
    for meth in ("select", "order", "limit", "in_", "eq", "is_"):
        getattr(m, meth).return_value = m
    m.execute.return_value = mock.MagicMock(data=data)
    return m


LEAD_ROW = {
    "id": LEAD, "reference_id": "REF1", "price": "$150",
    "phone_number": "609-555-0000", "email": "john@example.com",
    "receipt_image_url": "", "delivery_status": "on_the_way",
    "status_updated_at": "2026-08-25T10:00:00Z", "status_updated_by": "kita",
    "created_at": "2026-08-25T09:00:00Z", "updated_at": None,
    "group_id": "G1", "telegram_username": "tester", "user_id": 42,
    "vehicle_details": "John Damian\n\n\n\n\n\n2017 M Benz",
    "delivery_details": "19 Pennwood Dr", "extra_info": "", "extra_vehicles": None,
}
ASSIGN_ROW = {"lead_id": LEAD, "status": "accepted",
              "driver": {"driver_name": "Kita", "phone_number": "+1 609 555 1111",
                         "email": "kita@example.com", "driver_telegram_id": "777"}}
GROUP_ROW = {"id": "G1", "group_name": "HighKage",
             "group_telegram_id": "-100123", "supervisory_telegram_id": "555"}
RF_ROW = {"lead_id": LEAD, "uploaded_at": "2026-08-25T10:05:00Z"}


class TheBigBoardRendersTest(unittest.TestCase):

    def setUp(self):
        ad.app.config["TESTING"] = True
        self.client = ad.app.test_client()
        # /receipts now carries a password (receipts_page._receipts_password).
        # Signing in here means these tests exercise the board the way a real
        # operator reaches it, rather than around the gate.
        self.client.post("/receipts/login",
                         data={"password": receipts_page._receipts_password()})
        self.body = self.client.get("/receipts").get_data(as_text=True)

    def test_the_columns_come_in_the_asked_order(self):
        heads = ("<th>Client</th>", "<th>Receipt</th>", "<th>Client phone</th>",
                 "<th>Client contact</th>", "<th>Driver</th>", "<th>Issuer</th>",
                 "<th>Dispatcher</th>", "<th>Status</th>")
        positions = [self.body.index(h) for h in heads]
        self.assertEqual(positions, sorted(positions), "columns out of order")

    def test_every_party_gets_send_buttons(self):
        # Both channels are wired through the one button builder, on the table
        # blocks and the phone cards alike.
        self.assertIn('"email", "✉ Email"', self.body)
        self.assertIn('"sms", "💬 SMS"', self.body)
        self.assertIn('data-ch="${ch}"', self.body)
        for party in ("client", "driver", "issuer", "dispatcher"):
            self.assertIn(f'block(r, "{party}")', self.body, party)

    def test_the_receipt_is_a_thumbnail_that_enlarges(self):
        self.assertIn('class="thumb"', self.body)
        self.assertIn("lightbox", self.body)
        self.assertIn("/receipts/receipt/", self.body)

    def test_the_agency_branding_is_injected_not_a_placeholder(self):
        self.assertIn("const AGENCY = {", self.body)
        self.assertNotIn("__AGENCY__", self.body)

    def test_the_alias_routes_are_mounted_for_the_vercel_rewrite(self):
        have = {str(r) for r in ad.app.url_map.iter_rules()}
        for rule in ("/receipts/api/transmissions",
                     "/receipts/api/transmissions/<lead_id>/status",
                     "/receipts/api/transmissions/<lead_id>/notify",
                     "/api/transmissions/<lead_id>/notify",
                     "/receipts/api/sendconfig",
                     "/receipts/receipt/<lead_id>"):
            self.assertIn(rule, have, rule)


class TheRowsCarryEveryContactTest(unittest.TestCase):
    """One batched query set — and the row says how to reach each party."""

    def _db(self):
        d = ad.AdminDatabase.__new__(ad.AdminDatabase)
        d.client = mock.MagicMock()
        d._check_tables_exist = lambda: True
        d.client.table.side_effect = lambda name: {
            "leads": _chained([dict(LEAD_ROW)]),
            "lead_assignments": _chained([dict(ASSIGN_ROW)]),
            "groups": _chained([dict(GROUP_ROW)]),
            "receipt_files": _chained([dict(RF_ROW)]),
        }[name]
        return d

    def test_the_contacts_are_on_the_row(self):
        row = self._db().get_transmissions()[0]
        self.assertEqual("609-555-0000", row["client_phone"])
        self.assertEqual("john@example.com", row["email"])
        self.assertEqual("Kita", row["driver_name"])
        self.assertEqual("+1 609 555 1111", row["driver_phone"])
        self.assertEqual("kita@example.com", row["driver_email"])
        self.assertEqual("tester", row["issuer_username"])
        self.assertEqual("42", row["issuer_tg_id"])
        self.assertEqual("HighKage", row["group_name"])
        self.assertEqual("555", row["dispatcher_tg_id"])
        self.assertEqual("2026-08-25T10:05:00Z", row["receipt_at"])
        self.assertTrue(row["receipt_in_db"])

    def test_a_database_without_drivers_email_keeps_the_driver(self):
        """drivers.email arrived by migration — its absence must not blank the
        driver column, only the contact details."""
        d = ad.AdminDatabase.__new__(ad.AdminDatabase)
        d.client = mock.MagicMock()
        d._check_tables_exist = lambda: True
        calls = []

        def _assignments():
            m = _chained([{"lead_id": LEAD, "status": "accepted",
                           "driver": {"driver_name": "Kita"}}])
            real_select = m.select

            def select(cols):
                calls.append(cols)
                if "email" in cols:
                    raise Exception("42703 column drivers.email does not exist")
                return real_select(cols)
            m.select = select
            return m

        d.client.table.side_effect = lambda name: {
            "leads": _chained([dict(LEAD_ROW)]),
            "lead_assignments": _assignments(),
            "groups": _chained([dict(GROUP_ROW)]),
            "receipt_files": _chained([]),
        }[name]
        row = d.get_transmissions()[0]
        self.assertEqual("Kita", row["driver_name"])
        self.assertEqual("", row["driver_email"])
        self.assertGreaterEqual(len(calls), 2, "it should retry with names only")


class TheNotifyEndpointTest(unittest.TestCase):

    def setUp(self):
        ad.app.config["TESTING"] = True
        self.client = ad.app.test_client()
        # /receipts now carries a password (receipts_page._receipts_password).
        # Signing in here means these tests exercise the board the way a real
        # operator reaches it, rather than around the gate.
        self.client.post("/receipts/login",
                         data={"password": receipts_page._receipts_password()})
        receipts_page._recent_sends.clear()

    def _post(self, payload, contact=None):
        db = mock.MagicMock()
        contact = contact if contact is not None else {
            "name": "John Damian", "phone": "609-555-0000",
            "email": "john@example.com", "reference_id": "REF1"}
        with mock.patch.object(ad, "db", db), \
                mock.patch.object(receipts_page, "_party_contact",
                                  return_value=contact):
            return self.client.post(
                f"/receipts/api/transmissions/{LEAD}/notify",
                data=json.dumps(payload), content_type="application/json")

    def test_an_unknown_party_or_channel_is_refused(self):
        r = self._post({"party": "stranger", "channel": "email", "message": "hi"})
        self.assertEqual(400, r.status_code)
        r = self._post({"party": "client", "channel": "fax", "message": "hi"})
        self.assertEqual(400, r.status_code)

    def test_an_empty_message_is_refused(self):
        r = self._post({"party": "client", "channel": "email", "message": "  "})
        self.assertEqual(400, r.status_code)

    def test_a_missing_contact_is_a_clear_error_not_a_send(self):
        sent = mock.MagicMock()
        with mock.patch("utils.client_outreach.send_client_email", sent):
            r = self._post({"party": "issuer", "channel": "email", "message": "hi"},
                           contact={"name": "tester", "phone": "", "email": "",
                                    "reference_id": "REF1"})
        self.assertEqual(400, r.status_code)
        self.assertIn("email", r.get_json()["error"].lower())
        sent.assert_not_called()

    def test_email_goes_through_the_reused_sender(self):
        sent = mock.MagicMock(return_value=(True, None))
        with mock.patch("utils.client_outreach.send_client_email", sent):
            r = self._post({"party": "client", "channel": "email",
                            "message": "On the way!", "subject": "Update",
                            "by": "kita"})
        self.assertEqual(200, r.status_code, r.get_data(as_text=True))
        self.assertEqual("john@example.com", sent.call_args[0][0])
        self.assertEqual("Update", sent.call_args[0][1])
        # The response never echoes the full address back.
        self.assertNotIn("john@example.com", r.get_data(as_text=True))

    def test_sms_prefers_gohighlevel_when_configured(self):
        ghl = mock.MagicMock(return_value=(True, None))
        twilio = mock.MagicMock()
        with mock.patch("utils.ghl_client.ghl_configured", return_value=True), \
                mock.patch("utils.ghl_client.send_ghl_sms", ghl), \
                mock.patch("utils.client_outreach.send_client_sms", twilio):
            r = self._post({"party": "client", "channel": "sms", "message": "On the way!"})
        self.assertEqual(200, r.status_code, r.get_data(as_text=True))
        self.assertEqual("gohighlevel", r.get_json()["provider"])
        ghl.assert_called_once()
        twilio.assert_not_called()

    def test_sms_falls_back_to_the_existing_twilio_sender(self):
        twilio = mock.MagicMock(return_value=(True, None))
        with mock.patch("utils.ghl_client.ghl_configured", return_value=False), \
                mock.patch("utils.client_outreach.sms_configured", return_value=True), \
                mock.patch("utils.client_outreach.send_client_sms", twilio):
            r = self._post({"party": "client", "channel": "sms", "message": "hi"})
        self.assertEqual(200, r.status_code)
        self.assertEqual("twilio", r.get_json()["provider"])
        twilio.assert_called_once()

    def test_no_sms_provider_at_all_is_a_503_naming_both(self):
        with mock.patch("utils.ghl_client.ghl_configured", return_value=False), \
                mock.patch("utils.client_outreach.sms_configured", return_value=False):
            r = self._post({"party": "client", "channel": "sms", "message": "hi"})
        self.assertEqual(503, r.status_code)
        err = r.get_json()["error"]
        self.assertIn("GHL_API_KEY", err)
        self.assertIn("TWILIO", err)

    def test_a_double_click_does_not_text_the_client_twice(self):
        sent = mock.MagicMock(return_value=(True, None))
        with mock.patch("utils.client_outreach.send_client_email", sent):
            first = self._post({"party": "client", "channel": "email", "message": "hi"})
            second = self._post({"party": "client", "channel": "email", "message": "hi"})
        self.assertEqual(200, first.status_code)
        self.assertEqual(429, second.status_code)
        self.assertEqual(1, sent.call_count)

    def test_a_failed_send_is_reported_not_swallowed(self):
        sent = mock.MagicMock(return_value=(False, "Resend 401: bad key"))
        with mock.patch("utils.client_outreach.send_client_email", sent):
            r = self._post({"party": "client", "channel": "email", "message": "hi"})
        self.assertEqual(502, r.status_code)
        self.assertIn("Resend 401", r.get_json()["error"])


class TheGhlClientTest(unittest.TestCase):
    """The minimal GoHighLevel client — upsert the contact, then message it."""

    def test_unconfigured_is_a_clean_no(self):
        from utils import ghl_client
        with mock.patch.dict(os.environ, {"GHL_API_KEY": "", "GHL_LOCATION_ID": ""}):
            self.assertFalse(ghl_client.ghl_configured())
            ok, err = ghl_client.send_ghl_sms("6095550000", "hi")
        self.assertFalse(ok)
        self.assertIn("GHL_API_KEY", err)

    def test_the_send_is_upsert_then_message(self):
        from utils import ghl_client
        posts = []

        def _post(url, headers=None, json=None, timeout=None):
            posts.append((url, json))
            resp = mock.MagicMock()
            resp.status_code = 200
            resp.text = "{}"
            resp.json.return_value = (
                {"contact": {"id": "C1"}} if "contacts/upsert" in url else {})
            return resp

        with mock.patch.dict(os.environ, {"GHL_API_KEY": "pit-test",
                                          "GHL_LOCATION_ID": "loc1"}), \
                mock.patch.object(ghl_client.requests, "post", _post):
            ok, err = ghl_client.send_ghl_sms("(609) 555-0000", "On the way!")
        self.assertTrue(ok, err)
        self.assertIn("contacts/upsert", posts[0][0])
        self.assertEqual("+16095550000", posts[0][1]["phone"])
        self.assertIn("conversations/messages", posts[1][0])
        self.assertEqual({"type": "SMS", "contactId": "C1", "message": "On the way!"},
                         posts[1][1])

    def test_a_number_that_is_not_a_phone_is_refused(self):
        from utils import ghl_client
        with mock.patch.dict(os.environ, {"GHL_API_KEY": "pit-test",
                                          "GHL_LOCATION_ID": "loc1"}):
            ok, err = ghl_client.send_ghl_sms("call after 5pm", "hi")
        self.assertFalse(ok)
        self.assertIn("not SMS-able", err)


if __name__ == "__main__":
    unittest.main()
