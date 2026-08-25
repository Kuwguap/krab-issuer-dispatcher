r"""Receipts live in the database, reachable from a web page — never on Telegram.

Reported: "the receipts still dont load cause telegram links die … instead host a
web portal where they can upload and it directly sends to database and directly
uses the image from database and nothing about telegram".

Telegram file URLs expire after about an hour and file_ids are scoped to the bot
that received them, so a receipt became a dead link the same day and no amount of
re-signing brought it back. A row in a table cannot expire.

Run:  venv\Scripts\python.exe -m pytest tests/test_receipt_portal.py -q
"""
import base64
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy-key")

import utils.database as udb  # noqa: E402

if "bot" not in sys.modules:
    udb.Database = mock.MagicMock()
import bot  # noqa: E402
import admin_dashboard as ad  # noqa: E402

# The real Database class — bot's import replaced the module attribute with a mock.
import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location(
    "_real_db_for_receipt_test", ROOT / "utils" / "database.py")
_real_db = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_real_db)
Database = _real_db.Database

LEAD = "11111111-2222-3333-4444-555555555555"
PNG = b"\x89PNG\r\n\x1a\n" + b"receipt bytes" * 8


class TheLinkIsUnguessableAndShared(unittest.TestCase):
    """Both services build it from the same secret, or the page rejects the link."""

    def test_the_bot_and_the_portal_agree(self):
        self.assertEqual(bot.receipt_portal_url(LEAD), ad.receipt_portal_url(LEAD))

    def test_a_token_names_its_lead(self):
        self.assertEqual(LEAD, ad.receipt_lead_from_token(ad.receipt_token(LEAD)))

    def test_a_tampered_token_is_refused(self):
        self.assertIsNone(ad.receipt_lead_from_token(LEAD + ".deadbeefdeadbeefdeadbeef"))

    def test_another_lead_cannot_borrow_it(self):
        other = "99999999-8888-7777-6666-555555555555"
        stolen = other + "." + ad.receipt_token(LEAD).split(".", 1)[1]
        self.assertIsNone(ad.receipt_lead_from_token(stolen))

    def test_rubbish_is_refused(self):
        for bad in ("", "rubbish", "no-dot-here", "."):
            with self.subTest(bad=bad):
                self.assertIsNone(ad.receipt_lead_from_token(bad))

    def test_it_is_a_real_url(self):
        self.assertTrue(bot.receipt_portal_url(LEAD).startswith("https://"))
        self.assertIn("/r/", bot.receipt_portal_url(LEAD))


class TheBytesGoInTheDatabase(unittest.TestCase):

    def _db(self):
        d = Database.__new__(Database)
        d.client = mock.MagicMock()
        d._check_tables_exist = lambda: True
        return d

    def test_saving_stores_the_bytes(self):
        d = self._db()
        d.client.table.return_value.insert.return_value.execute.return_value = (
            mock.MagicMock(data=[{"id": "r1"}]))
        self.assertEqual("r1", d.save_receipt_file(LEAD, data=PNG, content_type="image/png"))
        row = d.client.table.return_value.insert.call_args[0][0]
        self.assertEqual(PNG, base64.b64decode(row["data_base64"]))
        self.assertEqual(len(PNG), row["size_bytes"])
        self.assertEqual("image/png", row["content_type"])

    def test_reading_gives_them_back_unchanged(self):
        d = self._db()
        (d.client.table.return_value.select.return_value.eq.return_value
         .order.return_value.limit.return_value.execute.return_value) = mock.MagicMock(
            data=[{"content_type": "image/png",
                   "data_base64": base64.b64encode(PNG).decode(),
                   "uploaded_at": "now"}])
        got = d.get_receipt_file(LEAD)
        self.assertEqual(PNG, got["data"])
        self.assertEqual("image/png", got["content_type"])

    def test_nothing_stored_reads_as_nothing(self):
        d = self._db()
        (d.client.table.return_value.select.return_value.eq.return_value
         .order.return_value.limit.return_value.execute.return_value) = mock.MagicMock(data=[])
        self.assertIsNone(d.get_receipt_file(LEAD))

    def test_an_empty_upload_is_not_stored(self):
        self.assertIsNone(self._db().save_receipt_file(LEAD, data=b""))

    def test_a_database_error_is_reported_not_raised(self):
        d = self._db()
        d.client.table.side_effect = Exception("down")
        self.assertIsNone(d.save_receipt_file(LEAD, data=PNG))
        self.assertIsNone(d.get_receipt_file(LEAD))


class ThePortalPage(unittest.TestCase):

    def setUp(self):
        ad.app.config["TESTING"] = True
        self.client = ad.app.test_client()
        self.lead_row = mock.MagicMock(
            data=[{"id": LEAD, "reference_id": "REF1", "receipt_image_url": ""}])

    def _db(self, has_file=False, saves="r1", stored=None):
        d = mock.MagicMock()
        (d.client.table.return_value.select.return_value.eq.return_value
         .limit.return_value.execute.return_value) = self.lead_row
        d.has_receipt_file.return_value = has_file
        d.save_receipt_file.return_value = saves
        d.get_receipt_file.return_value = stored
        return d

    def test_it_shows_an_upload_form(self):
        with mock.patch.object(ad, "db", self._db()):
            r = self.client.get(f"/r/{ad.receipt_token(LEAD)}")
        self.assertEqual(200, r.status_code)
        body = r.get_data(as_text=True)
        self.assertIn("<form", body)
        self.assertIn('type="file"', body)
        self.assertIn("REF1", body)

    def test_it_lets_them_use_the_camera(self):
        with mock.patch.object(ad, "db", self._db()):
            body = self.client.get(f"/r/{ad.receipt_token(LEAD)}").get_data(as_text=True)
        self.assertIn("capture=", body)
        self.assertIn("accept=\"image/*\"", body)

    def test_a_bad_link_is_refused(self):
        r = self.client.get("/r/not-a-real-token")
        self.assertEqual(404, r.status_code)

    def test_uploading_stores_it_and_says_so(self):
        d = self._db()
        with mock.patch.object(ad, "db", d):
            r = self.client.post(
                f"/r/{ad.receipt_token(LEAD)}",
                data={"receipt": (__import__("io").BytesIO(PNG), "receipt.png")},
                content_type="multipart/form-data")
        self.assertEqual(200, r.status_code)
        self.assertIn("Receipt received", r.get_data(as_text=True))
        d.save_receipt_file.assert_called_once()
        self.assertEqual(PNG, d.save_receipt_file.call_args.kwargs["data"])

    def test_the_lead_is_pointed_at_our_own_url(self):
        """Not at a Telegram file that will be gone by tomorrow."""
        d = self._db()
        with mock.patch.object(ad, "db", d):
            self.client.post(
                f"/r/{ad.receipt_token(LEAD)}",
                data={"receipt": (__import__("io").BytesIO(PNG), "r.png")},
                content_type="multipart/form-data")
        wrote = d.client.table.return_value.update.call_args[0][0]
        self.assertIn(f"/receipt/{LEAD}", wrote["receipt_image_url"])
        self.assertNotIn("telegram", wrote["receipt_image_url"].lower())

    def test_an_empty_post_says_so(self):
        with mock.patch.object(ad, "db", self._db()):
            r = self.client.post(f"/r/{ad.receipt_token(LEAD)}",
                                 data={}, content_type="multipart/form-data")
        self.assertEqual(400, r.status_code)

    def test_a_video_is_refused(self):
        with mock.patch.object(ad, "db", self._db()):
            r = self.client.post(
                f"/r/{ad.receipt_token(LEAD)}",
                data={"receipt": (__import__("io").BytesIO(b"x" * 100), "clip.mp4",
                                  "video/mp4")},
                content_type="multipart/form-data")
        self.assertEqual(415, r.status_code)

    def test_a_receipt_already_in_says_so_instead_of_asking_again(self):
        with mock.patch.object(ad, "db", self._db(has_file=True)):
            body = self.client.get(f"/r/{ad.receipt_token(LEAD)}").get_data(as_text=True)
        self.assertIn("Receipt received", body)
        self.assertNotIn("<form", body)


class TheImageComesFromTheDatabase(unittest.TestCase):

    def setUp(self):
        ad.app.config["TESTING"] = True
        self.client = ad.app.test_client()

    def test_it_serves_the_stored_bytes(self):
        d = mock.MagicMock()
        d.get_receipt_file.return_value = {"data": PNG, "content_type": "image/png"}
        with mock.patch.object(ad, "db", d):
            r = self.client.get(f"/receipt/{LEAD}")
        self.assertEqual(200, r.status_code)
        self.assertEqual(PNG, r.get_data())
        self.assertEqual("image/png", r.mimetype)

    def test_nothing_stored_is_a_clean_404(self):
        d = mock.MagicMock()
        d.get_receipt_file.return_value = None
        with mock.patch.object(ad, "db", d):
            self.assertEqual(404, self.client.get(f"/receipt/{LEAD}").status_code)

    def test_the_old_resolver_prefers_the_database(self):
        """No Telegram round trip at all when the bytes are on hand."""
        d = mock.MagicMock()
        d.get_receipt_file.return_value = {"data": PNG, "content_type": "image/png"}
        with mock.patch.object(ad, "db", d):
            r = self.client.get(f"/api/receipts/image/{LEAD}")
        self.assertEqual(200, r.status_code)
        self.assertEqual(PNG, r.get_data())
        d.client.table.assert_not_called()



class TheDashboardActuallyHasTheMethodsTest(unittest.TestCase):
    """The dashboard has its OWN database class. Methods added to the bot's are NOT
    available here — and mocking `db` hides that completely, which is exactly how a
    dead portal shipped: a MagicMock answers any method you ask it for."""

    PORTAL_NEEDS = ("save_receipt_file", "get_receipt_file", "has_receipt_file")

    def test_the_real_class_has_every_method_the_portal_calls(self):
        missing = [m for m in self.PORTAL_NEEDS
                   if not callable(getattr(ad.AdminDatabase, m, None))]
        self.assertEqual([], missing, f"the portal would die with AttributeError: {missing}")

    def test_the_live_instance_has_them_too(self):
        for m in self.PORTAL_NEEDS:
            with self.subTest(method=m):
                self.assertTrue(callable(getattr(ad.db, m, None)), m)

    def test_they_take_the_arguments_the_portal_passes(self):
        import inspect
        sig = inspect.signature(ad.AdminDatabase.save_receipt_file)
        for kw in ("data", "content_type", "reference_id", "source"):
            self.assertIn(kw, sig.parameters, kw)


class OnlySafeFileTypesTest(unittest.TestCase):
    """The receipt is served back from the admin's own origin."""

    def setUp(self):
        ad.app.config["TESTING"] = True
        self.client = ad.app.test_client()

    def _post(self, mimetype, name="r.svg"):
        db = mock.MagicMock()
        (db.client.table.return_value.select.return_value.eq.return_value
         .limit.return_value.execute.return_value) = mock.MagicMock(
            data=[{"id": LEAD, "reference_id": "REF1", "receipt_image_url": ""}])
        db.save_receipt_file.return_value = "r1"
        with mock.patch.object(ad, "db", db):
            import io as _io
            return self.client.post(
                f"/r/{ad.receipt_token(LEAD)}",
                data={"receipt": (_io.BytesIO(b"<svg onload=alert(1)>"), name, mimetype)},
                content_type="multipart/form-data")

    def test_svg_is_refused(self):
        self.assertEqual(415, self._post("image/svg+xml").status_code)

    def test_html_is_refused(self):
        self.assertEqual(415, self._post("text/html", "r.html").status_code)

    def test_a_photo_is_accepted(self):
        self.assertEqual(200, self._post("image/jpeg", "r.jpg").status_code)

    def test_the_served_type_is_ours_not_the_uploaders(self):
        db = mock.MagicMock()
        db.get_receipt_file.return_value = {"data": b"x", "content_type": "image/svg+xml"}
        with mock.patch.object(ad, "db", db):
            r = self.client.get(f"/receipt/{LEAD}")
        self.assertNotIn("svg", r.mimetype)
        self.assertEqual("nosniff", r.headers.get("X-Content-Type-Options"))


class TheLinkEndpointIsNotOpenTest(unittest.TestCase):
    """The token IS the permission to upload, so minting one must not be free."""

    def setUp(self):
        ad.app.config["TESTING"] = True
        self.client = ad.app.test_client()

    def test_no_key_no_link(self):
        self.assertEqual(401, self.client.get(f"/api/receipts/link/{LEAD}").status_code)

    def test_a_wrong_key_is_refused(self):
        with mock.patch.dict(os.environ, {"ADMIN_API_KEY": "right"}):
            r = self.client.get(f"/api/receipts/link/{LEAD}",
                                headers={"Authorization": "Bearer wrong"})
        self.assertEqual(401, r.status_code)

    def test_the_right_key_gets_it(self):
        with mock.patch.dict(os.environ, {"ADMIN_API_KEY": "right"}):
            r = self.client.get(f"/api/receipts/link/{LEAD}",
                                headers={"Authorization": "Bearer right"})
        self.assertEqual(200, r.status_code)
        self.assertEqual(ad.receipt_portal_url(LEAD), r.get_json()["url"])


class TheBotMirrorsAndOffersIt(unittest.TestCase):

    def test_a_telegram_receipt_is_stored_in_the_database_too(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        self.assertIn("_store_receipt_bytes(", src)
        self.assertIn('source="telegram"', src)

    def test_the_stored_url_points_at_us_when_that_worked(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        self.assertIn('stored_url = f"{RECEIPT_PORTAL_BASE}/receipt/{lead_id}"', src)

    def test_the_receipt_prompt_offers_the_web_page(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        self.assertIn("Upload on the web", src)
        self.assertIn("receipt_portal_url(lead_id)", src)


if __name__ == "__main__":
    unittest.main()
