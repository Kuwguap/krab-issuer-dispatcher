r"""The receipts board has a password, and a wrong receipt can be corrected.

Until now /receipts carried no authentication of any kind while showing every
customer's name, phone, address and a photograph of their receipt — on a public
domain, through the tristatetags.com rewrite. Anyone with the URL had all of it.

The fixer exists because a driver photographed the wrong slip and there was no
way to correct it without going back to the driver. It is destructive, so it
lives behind the password AND behind a typed reference id that the SERVER
re-reads from the database — the page never supplies both sides of its own
confirmation.

Run:  venv\Scripts\python.exe -m pytest tests/test_receipts_login_and_fix.py -q
"""
import io
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy-key")

import admin_dashboard as ad                                   # noqa: E402
import receipts_page                                           # noqa: E402

LEAD = "11111111-2222-3333-4444-555555555555"
REF = "ABC12345"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def _client(signed_in=True):
    ad.app.config["TESTING"] = True
    c = ad.app.test_client()
    if signed_in:
        c.post("/receipts/login",
               data={"password": receipts_page._receipts_password()})
    return c


class TheBoardIsNoLongerOpenToAnyoneTest(unittest.TestCase):

    def setUp(self):
        self.anon = _client(signed_in=False)

    def test_the_board_itself_asks_for_the_password(self):
        r = self.anon.get("/receipts")
        self.assertEqual(302, r.status_code)
        self.assertIn("/receipts/login", r.headers["Location"])

    def test_the_data_is_refused_as_json_not_as_a_login_page(self):
        """The board's own fetch() must get something it can parse."""
        for path in ("/api/transmissions", "/receipts/api/transmissions"):
            with self.subTest(path=path):
                r = self.anon.get(path)
                self.assertEqual(401, r.status_code)
                self.assertIn("error", r.get_json() or {})

    def test_a_customers_receipt_image_is_not_public(self):
        r = self.anon.get(f"/receipts/receipt/{LEAD}")
        self.assertEqual(302, r.status_code)

    def test_the_login_page_itself_is_reachable(self):
        self.assertEqual(200, self.anon.get("/receipts/login").status_code)

    def test_a_wrong_password_does_not_get_in(self):
        r = self.anon.post("/receipts/login", data={"password": "not it"})
        self.assertEqual(401, r.status_code)
        self.assertEqual(302, self.anon.get("/receipts").status_code)

    def test_the_right_password_gets_in_and_stays_in(self):
        r = self.anon.post("/receipts/login",
                           data={"password": receipts_page._receipts_password()})
        self.assertEqual(302, r.status_code)
        self.assertEqual(200, self.anon.get("/receipts").status_code)

    def test_signing_out_locks_it_again(self):
        c = _client()
        self.assertEqual(200, c.get("/receipts").status_code)
        c.get("/receipts/logout")
        self.assertEqual(302, c.get("/receipts").status_code)

    def test_it_will_not_bounce_you_off_site_after_login(self):
        """An open redirect here would make a tristatetags.com link land
        anywhere the sender liked."""
        r = self.anon.post("/receipts/login",
                           data={"password": receipts_page._receipts_password(),
                                 "next": "https://evil.example/x"})
        self.assertEqual("/receipts", r.headers["Location"])

    def test_the_rest_of_the_dashboard_is_left_alone(self):
        """The gate is scoped to the board: this service mounts a great deal
        more, and each of those has its own arrangements."""
        self.assertNotEqual(302, self.anon.get("/api/health").status_code)


class TheSessionCookieCannotBeForgedTest(unittest.TestCase):

    def test_a_made_up_cookie_is_refused(self):
        c = ad.app.test_client()
        c.set_cookie("krab_receipts", "9999999999.deadbeef")
        self.assertEqual(302, c.get("/receipts").status_code)

    def test_an_expired_session_is_refused(self):
        import hashlib, hmac, time
        exp = str(int(time.time()) - 60)
        sig = hmac.new(receipts_page._session_secret(), exp.encode(),
                       hashlib.sha256).hexdigest()
        c = ad.app.test_client()
        c.set_cookie("krab_receipts", f"{exp}.{sig}")
        self.assertEqual(302, c.get("/receipts").status_code)

    def test_a_genuine_session_is_accepted(self):
        c = ad.app.test_client()
        c.set_cookie("krab_receipts", receipts_page._mint_session())
        self.assertEqual(200, c.get("/receipts").status_code)


class FixingAWrongReceiptTest(unittest.TestCase):

    def setUp(self):
        self.client = _client()
        self.contact = {"name": "Magnolia", "phone": "845", "email": "",
                        "reference_id": REF}

    def _post(self, data, method="POST"):
        with mock.patch.object(receipts_page, "_party_contact",
                               return_value=dict(self.contact)):
            fn = self.client.post if method == "POST" else self.client.delete
            return fn(f"/api/transmissions/{LEAD}/receipt",
                      data=data, content_type="multipart/form-data")

    def test_a_stranger_cannot_reach_it_at_all(self):
        anon = _client(signed_in=False)
        r = anon.post(f"/api/transmissions/{LEAD}/receipt", data={})
        self.assertEqual(401, r.status_code)

    def test_the_reference_must_be_typed_correctly(self):
        r = self._post({"by": "kita", "confirm": "WRONG",
                        "receipt": (io.BytesIO(PNG), "r.png", "image/png")})
        self.assertEqual(400, r.status_code)

    def test_it_asks_who_you_are(self):
        r = self._post({"confirm": REF,
                        "receipt": (io.BytesIO(PNG), "r.png", "image/png")})
        self.assertEqual(400, r.status_code)

    def test_a_scriptable_file_type_is_refused(self):
        """These bytes are served back from this origin; SVG is a script
        container."""
        r = self._post({"by": "kita", "confirm": REF,
                        "receipt": (io.BytesIO(b"<svg/>"), "x.svg", "image/svg+xml")})
        self.assertEqual(415, r.status_code)

    def test_an_empty_upload_is_refused(self):
        r = self._post({"by": "kita", "confirm": REF})
        self.assertEqual(400, r.status_code)

    def test_a_good_replacement_is_stored_and_the_lead_repointed(self):
        db = mock.MagicMock()
        db.replace_receipt.return_value = (True, "")
        with mock.patch.object(receipts_page, "_party_contact",
                               return_value=dict(self.contact)), \
             mock.patch.object(ad, "db", db):
            r = self.client.post(
                f"/api/transmissions/{LEAD}/receipt",
                data={"by": "kita", "confirm": REF, "reason": "wrong slip",
                      "receipt": (io.BytesIO(PNG), "r.png", "image/png")},
                content_type="multipart/form-data")
        self.assertEqual(200, r.status_code, r.get_data(as_text=True))
        self.assertTrue(db.replace_receipt.called)

    def test_a_write_that_changed_nothing_is_reported_as_a_failure(self):
        """RLS refuses with 200 and an empty list on this anon key — 'it saved'
        has to be earned, not assumed."""
        db = mock.MagicMock()
        db.replace_receipt.return_value = (False, "nothing changed — no rows")
        with mock.patch.object(receipts_page, "_party_contact",
                               return_value=dict(self.contact)), \
             mock.patch.object(ad, "db", db):
            r = self.client.post(
                f"/api/transmissions/{LEAD}/receipt",
                data={"by": "kita", "confirm": REF,
                      "receipt": (io.BytesIO(PNG), "r.png", "image/png")},
                content_type="multipart/form-data")
        self.assertEqual(500, r.status_code)
        self.assertIn("nothing changed", (r.get_json() or {}).get("error", ""))

    def test_clearing_requires_a_reason(self):
        r = self._post({"by": "kita", "confirm": REF}, method="DELETE")
        self.assertEqual(400, r.status_code)


class NothingIsEverDeletedTest(unittest.TestCase):
    """A receipt is a driver's proof they did the job and a customer's proof
    they paid. The wrong one is stepped over, not destroyed."""

    def test_the_replace_supersedes_rather_than_deletes(self):
        src = (ROOT / "admin_dashboard.py").read_text(encoding="utf-8")
        body = src.split("def replace_receipt(", 1)[1].split("\n    def ", 1)[0]
        self.assertIn("_supersede_receipt_files", body)
        self.assertNotIn(".delete()", body)

    def test_the_clear_supersedes_rather_than_deletes(self):
        src = (ROOT / "admin_dashboard.py").read_text(encoding="utf-8")
        body = src.split("def clear_receipt(", 1)[1].split("\n    def ", 1)[0]
        self.assertIn("_supersede_receipt_files", body)
        self.assertNotIn(".delete()", body)

    def test_the_new_bytes_land_before_anything_is_superseded(self):
        """Order matters: a failure to store must change nothing at all."""
        src = (ROOT / "admin_dashboard.py").read_text(encoding="utf-8")
        body = src.split("def replace_receipt(", 1)[1].split("\n    def ", 1)[0]
        self.assertLess(body.index("save_receipt_file"),
                        body.index("_supersede_receipt_files"))
        self.assertLess(body.index("_supersede_receipt_files"),
                        body.index("_point_lead_at_receipt"))


class TheReceiptCellIsWhereYouActTest(unittest.TestCase):
    """Finding a wrong receipt and fixing it should be one motion, in the
    column a person already looks at."""

    def setUp(self):
        src = (ROOT / "receipts_page.py").read_text(encoding="utf-8")
        self.cell = src.split("function receiptCell(r) {", 1)[1]
        self.cell = self.cell.split(chr(10) + "}", 1)[0]
        self.src = src

    def test_the_thumbnail_opens_the_full_image(self):
        self.assertIn("data-full=", self.cell)

    def test_an_empty_cell_offers_to_attach_one(self):
        """'no receipt' is exactly where somebody notices a driver never handed
        one in."""
        self.assertIn("addrec", self.cell)
        empty = self.cell.split('r.has_receipt', 1)[1]
        self.assertIn('type="file"', empty)

    def test_a_filled_cell_offers_change_and_clear(self):
        self.assertIn("Change", self.cell)
        self.assertIn("clr", self.cell)

    def test_both_pickers_carry_the_lead_and_the_reference(self):
        """The reference is what the server checks the typed confirmation
        against, so a control without it cannot complete."""
        # Two file pickers (the empty-state one and Change), plus Clear.
        self.assertEqual(2, self.cell.count('class="rep"'))
        self.assertEqual(3, self.cell.count("data-ref="))

    def test_clear_is_handled_before_the_send_buttons(self):
        """.act is the send-button hook; a Clear wearing it would open the
        compose dialog instead."""
        click = self.src.split('addEventListener("click"', 1)[1]
        self.assertLess(click.index('closest(".clr")'), click.index('closest(".act")'))

    def test_the_cell_controls_do_not_use_the_send_button_class(self):
        self.assertNotIn('class="act"', self.cell)

    def test_picking_a_file_is_wired_to_the_change_event(self):
        """A file input fires change, not click."""
        change = self.src.split('addEventListener("change"', 1)[1]
        self.assertIn('closest("input.rep")', change)
        self.assertIn("replaceReceipt", change)


if __name__ == "__main__":
    unittest.main()
