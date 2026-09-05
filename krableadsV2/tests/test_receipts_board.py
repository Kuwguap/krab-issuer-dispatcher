r"""tristatetags.com/receipts — the transmissions board with a status column.

Asked for: "create triStateTags.com/receipts full page backend with receipts
working…. And complete status column of where we at in the transaction everyone can
update like monday — On the way / Delivered / Paid … basically the summary
transmissions from /backend under the krab issuer tab but with added features and
big with same expand buttons".

Run:  venv\Scripts\python.exe -m pytest tests/test_receipts_board.py -q
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

ROWS = [
    {"lead_id": LEAD, "reference_id": "REF1", "client_name": "John Damian",
     "car": "2017 M Benz", "price": "$150", "driver_name": "Kita",
     "group_name": "HighKage", "issuer": "tester", "status": "on_the_way",
     "status_updated_at": "2026-08-25T10:00:00Z", "status_updated_by": "kita",
     "created_at": "2026-08-25T09:00:00Z", "has_receipt": True, "receipt_in_db": True,
     "delivery": "19 Pennwood Dr, Ewing NJ", "notes": "call first",
     "email": "a@b.com"},
    {"lead_id": "L2", "reference_id": "REF2", "client_name": "Maria Gonzalez",
     "car": "2019 Honda", "price": "$200", "driver_name": "Marco",
     "group_name": "HighKage", "issuer": "tester", "status": "new",
     "status_updated_at": None, "status_updated_by": "", "created_at": None,
     "has_receipt": False, "receipt_in_db": False, "delivery": "", "notes": "",
     "email": ""},
]


class ThePageLoadsTest(unittest.TestCase):

    def setUp(self):
        ad.app.config["TESTING"] = True
        self.client = ad.app.test_client()
        # /receipts now carries a password (receipts_page._receipts_password).
        # Signing in here means these tests exercise the board the way a real
        # operator reaches it, rather than around the gate.
        self.client.post("/receipts/login",
                         data={"password": receipts_page._receipts_password()})

    def test_the_board_is_served(self):
        r = self.client.get("/receipts")
        self.assertEqual(200, r.status_code)
        self.assertIn("text/html", r.content_type)

    def test_it_names_every_status_on_the_ladder(self):
        body = self.client.get("/receipts").get_data(as_text=True)
        for label in ("New Lead", "Followup", "Tag issued", "Tag emailed",
                      "Tag printed", "Driver on the way", "Delivered",
                      "Receipt uploaded"):
            self.assertIn(label, body, label)

    def test_it_has_the_expand_control(self):
        body = self.client.get("/receipts").get_data(as_text=True)
        self.assertIn('class="exp"', body)
        self.assertIn("d-", body)

    def test_it_shows_the_receipt_from_our_own_url(self):
        body = self.client.get("/receipts").get_data(as_text=True)
        self.assertIn("/receipt/", body)
        self.assertNotIn("api.telegram.org", body)

    def test_it_offers_search(self):
        self.assertIn('type="search"', self.client.get("/receipts").get_data(as_text=True))

    def test_the_status_list_is_injected_as_real_json(self):
        body = self.client.get("/receipts").get_data(as_text=True)
        self.assertIn('const STATUSES = ["new", "followup", "tag_issued", '
                      '"tag_emailed", "tag_printed", "on_the_way", "delivered", '
                      '"receipt_uploaded"]', body)
        self.assertNotIn("__STATUSES__", body)
        self.assertNotIn("__LABELS__", body)
        self.assertNotIn("__AGENCY__", body)


class TheDataEndpointTest(unittest.TestCase):

    def setUp(self):
        ad.app.config["TESTING"] = True
        self.client = ad.app.test_client()
        # /receipts now carries a password (receipts_page._receipts_password).
        # Signing in here means these tests exercise the board the way a real
        # operator reaches it, rather than around the gate.
        self.client.post("/receipts/login",
                         data={"password": receipts_page._receipts_password()})

    def test_it_returns_the_rows(self):
        db = mock.MagicMock()
        db.get_transmissions.return_value = ROWS
        with mock.patch.object(receipts_page, "db", db, create=True), \
                mock.patch.object(ad, "db", db):
            r = self.client.get("/api/transmissions")
        self.assertEqual(200, r.status_code)
        got = r.get_json()
        self.assertEqual(2, len(got))
        self.assertEqual("REF1", got[0]["reference_id"])

    def test_a_limit_is_honoured(self):
        db = mock.MagicMock()
        db.get_transmissions.return_value = []
        with mock.patch.object(ad, "db", db):
            self.client.get("/api/transmissions?limit=42")
        self.assertEqual(42, db.get_transmissions.call_args.kwargs["limit"])

    def test_rubbish_in_the_limit_does_not_explode(self):
        db = mock.MagicMock()
        db.get_transmissions.return_value = []
        with mock.patch.object(ad, "db", db):
            r = self.client.get("/api/transmissions?limit=drop-table")
        self.assertEqual(200, r.status_code)

    def test_a_database_error_is_reported(self):
        db = mock.MagicMock()
        db.get_transmissions.side_effect = Exception("down")
        with mock.patch.object(ad, "db", db):
            r = self.client.get("/api/transmissions")
        self.assertEqual(500, r.status_code)
        self.assertIn("down", r.get_json()["error"])


class AnyoneCanMoveTheStatusTest(unittest.TestCase):
    """Like a Monday column — that is the requirement."""

    def setUp(self):
        ad.app.config["TESTING"] = True
        self.client = ad.app.test_client()
        # /receipts now carries a password (receipts_page._receipts_password).
        # Signing in here means these tests exercise the board the way a real
        # operator reaches it, rather than around the gate.
        self.client.post("/receipts/login",
                         data={"password": receipts_page._receipts_password()})

    def _post(self, status, by="kita"):
        db = mock.MagicMock()
        db.set_lead_status.return_value = True
        with mock.patch.object(ad, "db", db):
            r = self.client.post(
                f"/api/transmissions/{LEAD}/status",
                data=json.dumps({"status": status, "by": by}),
                content_type="application/json")
        return r, db

    def test_each_status_can_be_set(self):
        for s in ("followup", "tag_issued", "tag_emailed", "tag_printed",
                  "on_the_way", "delivered", "receipt_uploaded", "paid", "new"):
            with self.subTest(status=s):
                r, db = self._post(s)
                self.assertEqual(200, r.status_code)
                db.set_lead_status.assert_called_once_with(LEAD, s, "kita")

    def test_it_records_who_moved_it(self):
        _, db = self._post("paid", by="marco")
        self.assertEqual("marco", db.set_lead_status.call_args[0][2])

    def test_an_unknown_status_is_refused(self):
        r, db = self._post("finished")
        self.assertEqual(400, r.status_code)
        db.set_lead_status.assert_not_called()

    def test_a_failed_write_is_reported_not_swallowed(self):
        db = mock.MagicMock()
        db.set_lead_status.return_value = False
        with mock.patch.object(ad, "db", db):
            r = self.client.post(f"/api/transmissions/{LEAD}/status",
                                 data=json.dumps({"status": "paid"}),
                                 content_type="application/json")
        self.assertEqual(500, r.status_code)


class TheQueryIsBatchedTest(unittest.TestCase):
    """Hundreds of rows at once — the old gallery did two queries per lead."""

    def test_only_the_statuses_the_board_offers_are_accepted(self):
        self.assertEqual(("new", "followup", "tag_issued", "tag_emailed",
                          "tag_printed", "on_the_way", "delivered", "paid",
                          "receipt_uploaded"),
                         ad.AdminDatabase.DELIVERY_STATUSES)

    def test_set_lead_status_refuses_anything_else(self):
        d = ad.AdminDatabase.__new__(ad.AdminDatabase)
        d.client = mock.MagicMock()
        self.assertFalse(d.set_lead_status(LEAD, "invented"))
        d.client.table.assert_not_called()

    def test_it_survives_the_migration_not_being_run(self):
        """delivery_status may not exist yet — the board must still render."""
        d = ad.AdminDatabase.__new__(ad.AdminDatabase)
        d.client = mock.MagicMock()
        d._check_tables_exist = lambda: True
        calls = {"n": 0}

        def _table(name):
            t = mock.MagicMock()
            if name == "leads":
                calls["n"] += 1
                if calls["n"] == 1:      # the full select, with the new column
                    t.select.return_value.order.return_value.limit.return_value \
                        .execute.side_effect = Exception("42703 column does not exist")
                else:                     # the lean retry
                    t.select.return_value.order.return_value.limit.return_value \
                        .execute.return_value = mock.MagicMock(data=[])
            return t
        d.client.table.side_effect = _table
        self.assertEqual([], d.get_transmissions())
        self.assertGreaterEqual(calls["n"], 2, "it should retry without the new column")

    def test_the_real_class_has_both_methods(self):
        """A mocked db hides a missing method — this is how the portal shipped dead."""
        for m in ("get_transmissions", "set_lead_status"):
            with self.subTest(method=m):
                self.assertTrue(callable(getattr(ad.AdminDatabase, m, None)), m)


if __name__ == "__main__":
    unittest.main()
