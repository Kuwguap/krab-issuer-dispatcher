r"""$100 instant PDF — pay, and the tag goes straight to the chosen driver.

Asked for: "add a $100 payment for driver to receive INSTANT PDF = 100 payment and
it bypasses dispatch group goes straight to the driver chosen, but itll still go to
tristatetags.com/backend also itll use the same stripe and a proper call back so
transactions arent hanging".

"Not hanging" is the design constraint: the webhook only ever writes `paid_at`, and
the bot delivers off a database sweep and stamps `delivered_at` only once the
document is really in the chat. A crash between the two delays a tag; it cannot lose
one, and it cannot take money without eventually delivering.

Run:  venv\Scripts\python.exe -m pytest tests/test_instant_pdf.py -q
"""
import asyncio
import hashlib
import hmac
import json
import os
import sys
import time
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
import instant_pdf  # noqa: E402

LEAD = "11111111-2222-3333-4444-555555555555"
DRIVER = "d1"
SECRET = "whsec_test"


def _signed(payload: dict, secret=SECRET, ts=None):
    raw = json.dumps(payload).encode()
    ts = ts or int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()
    return raw, f"t={ts},v1={sig}"


class TheWebhookCannotBeForgedTest(unittest.TestCase):
    """The webhook is the only thing that can mark a lead paid."""

    def test_a_correct_signature_verifies(self):
        raw, header = _signed({"type": "checkout.session.completed"})
        self.assertTrue(instant_pdf.verify_stripe_signature(raw, header, SECRET))

    def test_a_wrong_secret_does_not(self):
        raw, header = _signed({"a": 1})
        self.assertFalse(instant_pdf.verify_stripe_signature(raw, header, "whsec_other"))

    def test_a_tampered_body_does_not(self):
        raw, header = _signed({"a": 1})
        self.assertFalse(instant_pdf.verify_stripe_signature(raw + b"x", header, SECRET))

    def test_an_old_signature_is_refused(self):
        """A captured body must not be replayable tomorrow."""
        raw, header = _signed({"a": 1}, ts=int(time.time()) - 4000)
        self.assertFalse(instant_pdf.verify_stripe_signature(raw, header, SECRET))

    def test_rubbish_headers_are_refused(self):
        raw = b"{}"
        for header in ("", "nonsense", "t=abc,v1=def", "v1=onlysig", "t=123"):
            with self.subTest(header=header):
                self.assertFalse(instant_pdf.verify_stripe_signature(raw, header, SECRET))

    def test_no_secret_configured_refuses_everything(self):
        raw, header = _signed({"a": 1})
        self.assertFalse(instant_pdf.verify_stripe_signature(raw, header, ""))


class ThePaymentIsRecordedOnceTest(unittest.TestCase):

    def setUp(self):
        ad.app.config["TESTING"] = True
        self.client = ad.app.test_client()

    def _hook(self, event, secret=SECRET, env_secret=SECRET):
        raw, header = _signed(event, secret)
        db = mock.MagicMock()
        with mock.patch.object(ad, "db", db), \
                mock.patch.dict(os.environ, {"STRIPE_WEBHOOK_SECRET": env_secret}):
            r = self.client.post("/api/stripe/webhook", data=raw,
                                 headers={"Stripe-Signature": header,
                                          "Content-Type": "application/json"})
        return r, db

    def _completed(self):
        return {"type": "checkout.session.completed",
                "data": {"object": {"id": "cs_1", "payment_status": "paid",
                                    "metadata": {"lead_id": LEAD, "driver_id": DRIVER,
                                                 "kind": "instant_pdf"}}}}

    def test_a_paid_session_marks_the_lead(self):
        r, db = self._hook(self._completed())
        self.assertEqual(200, r.status_code)
        wrote = db.client.table.return_value.update.call_args[0][0]
        self.assertIn("instant_pdf_paid_at", wrote)

    def test_it_only_writes_paid_never_delivered(self):
        """Delivery is the bot's to claim, once the tag is really sent."""
        _, db = self._hook(self._completed())
        wrote = db.client.table.return_value.update.call_args[0][0]
        self.assertNotIn("instant_pdf_delivered_at", wrote)

    def test_a_repeat_delivery_of_the_same_event_is_a_no_op(self):
        """Stripe retries until it gets a 2xx — the write must be idempotent."""
        _, db = self._hook(self._completed())
        chain = db.client.table.return_value.update.return_value.eq.return_value
        chain.is_.assert_called_with("instant_pdf_paid_at", "null")

    def test_an_unsigned_call_changes_nothing(self):
        db = mock.MagicMock()
        with mock.patch.object(ad, "db", db), \
                mock.patch.dict(os.environ, {"STRIPE_WEBHOOK_SECRET": SECRET}):
            r = self.client.post("/api/stripe/webhook", json=self._completed())
        self.assertEqual(400, r.status_code)
        db.client.table.assert_not_called()

    def test_an_unpaid_session_is_ignored(self):
        ev = self._completed()
        ev["data"]["object"]["payment_status"] = "unpaid"
        r, db = self._hook(ev)
        self.assertEqual(200, r.status_code)
        db.client.table.assert_not_called()

    def test_other_event_types_get_a_2xx_so_stripe_stops_retrying(self):
        r, _ = self._hook({"type": "invoice.paid", "data": {"object": {}}})
        self.assertEqual(200, r.status_code)

    def test_a_failed_write_asks_stripe_to_retry(self):
        raw, header = _signed(self._completed())
        db = mock.MagicMock()
        db.client.table.side_effect = Exception("db down")
        with mock.patch.object(ad, "db", db), \
                mock.patch.dict(os.environ, {"STRIPE_WEBHOOK_SECRET": SECRET}):
            r = self.client.post("/api/stripe/webhook", data=raw,
                                 headers={"Stripe-Signature": header,
                                          "Content-Type": "application/json"})
        self.assertEqual(500, r.status_code, "a 5xx is what makes Stripe try again")


class TheCheckoutEndpointTest(unittest.TestCase):

    def setUp(self):
        ad.app.config["TESTING"] = True
        self.client = ad.app.test_client()

    def _checkout(self, key="adminkey", auth="adminkey", stripe_ok=True):
        resp = mock.MagicMock(ok=stripe_ok, status_code=200 if stripe_ok else 402,
                              content=b"x")
        resp.json.return_value = ({"id": "cs_1", "url": "https://pay.stripe/x"}
                                  if stripe_ok else {"error": {"message": "card_declined"}})
        db = mock.MagicMock()
        with mock.patch.object(ad, "db", db), \
                mock.patch.object(instant_pdf.requests, "post",
                                  mock.MagicMock(return_value=resp)) as post, \
                mock.patch.dict(os.environ, {"ADMIN_API_KEY": key,
                                             "INTEGRATIONS_API_KEY": key,
                                             "STRIPE_SECRET_KEY": "sk_test"}):
            r = self.client.post("/api/instant/checkout",
                                 json={"lead_id": LEAD, "driver_id": DRIVER,
                                       "reference_id": "REF1"},
                                 headers={"Authorization": f"Bearer {auth}"})
        return r, db, post

    def test_it_returns_a_pay_link(self):
        r, _, _ = self._checkout()
        self.assertEqual(200, r.status_code)
        self.assertEqual("https://pay.stripe/x", r.get_json()["url"])

    def test_it_charges_one_hundred_dollars(self):
        r, _, post = self._checkout()
        self.assertEqual(10000, r.get_json()["amount_cents"])
        form = post.call_args.kwargs["data"]
        self.assertEqual("10000", form["line_items[0][price_data][unit_amount]"])

    def test_the_lead_and_driver_ride_on_the_session(self):
        """So the webhook needs no lookup table of its own."""
        _, _, post = self._checkout()
        form = post.call_args.kwargs["data"]
        self.assertEqual(LEAD, form["metadata[lead_id]"])
        self.assertEqual(DRIVER, form["metadata[driver_id]"])
        self.assertEqual(LEAD, form["client_reference_id"])

    def test_asking_twice_reuses_the_session(self):
        """Or the same lead could be paid for twice."""
        _, _, post = self._checkout()
        self.assertIn("Idempotency-Key", post.call_args.kwargs["headers"])

    def test_it_records_the_request_on_the_lead(self):
        _, db, _ = self._checkout()
        wrote = db.client.table.return_value.update.call_args[0][0]
        self.assertIn("instant_pdf_requested_at", wrote)
        self.assertEqual(DRIVER, wrote["instant_pdf_driver_id"])

    def test_it_needs_the_admin_key(self):
        r, _, _ = self._checkout(key="right", auth="wrong")
        self.assertEqual(401, r.status_code)

    def test_a_stripe_refusal_is_passed_on_not_swallowed(self):
        r, _, _ = self._checkout(stripe_ok=False)
        self.assertEqual(502, r.status_code)
        self.assertIn("card_declined", r.get_json()["error"])

    def test_a_missing_driver_is_refused(self):
        with mock.patch.dict(os.environ, {"ADMIN_API_KEY": "k", "STRIPE_SECRET_KEY": "sk"}):
            r = self.client.post("/api/instant/checkout", json={"lead_id": LEAD},
                                 headers={"Authorization": "Bearer k"})
        self.assertEqual(400, r.status_code)


class TheBotAsksAndDeliversTest(unittest.TestCase):

    def test_the_button_is_on_the_confirmation(self):
        """Renamed to "Skip Dispatch": the label is the only warning an operator
        gets that tapping it withdraws the lead from the team and every driver."""
        labels = [b.text for row in bot._after_send_keyboard("L1").inline_keyboard
                  for b in row]
        self.assertTrue(any("Skip Dispatch" in l for l in labels), labels)

    def test_the_button_reaches_a_handler_from_anywhere(self):
        """Entry point AND fallback — the lesson from every other button here."""
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        self.assertEqual(2, src.count(
            'CallbackQueryHandler(handle_instant_pdf_request, pattern=f"^{INSTANT_PDF_CB}")'))

    def test_the_sweep_is_scheduled(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        self.assertIn("deliver_paid_instant_pdfs, interval=20", src)

    def _sweep(self, rows, sent=1, chat="111"):
        db = mock.MagicMock()
        db.get_paid_instant_pdfs_undelivered.return_value = rows
        ctx = mock.MagicMock()
        with mock.patch.object(bot, "db", db), \
                mock.patch.object(bot, "_driver_row_by_id",
                                  mock.MagicMock(return_value={
                                      "id": DRIVER, "driver_name": "Kita",
                                      "driver_telegram_id": chat})), \
                mock.patch.object(bot, "_build_and_send_tag_pdf",
                                  mock.AsyncMock(return_value=sent)) as send, \
                mock.patch.object(bot, "followup_team_chat_id",
                                  mock.MagicMock(return_value=-100)):
            asyncio.run(bot.deliver_paid_instant_pdfs(ctx))
        return db, send

    def test_a_paid_tag_is_sent_to_the_driver(self):
        db, send = self._sweep([{"id": LEAD, "instant_pdf_driver_id": DRIVER,
                                 "reference_id": "REF1"}])
        send.assert_awaited()
        self.assertEqual([111], send.await_args[0][2], "straight to the driver's chat")
        db.mark_instant_pdf_delivered.assert_called_once_with(LEAD)

    def test_it_is_not_marked_delivered_when_the_send_failed(self):
        """Otherwise a paid tag is lost — the sweep must try again next tick."""
        db, _ = self._sweep([{"id": LEAD, "instant_pdf_driver_id": DRIVER}], sent=0)
        db.mark_instant_pdf_delivered.assert_not_called()

    def test_a_driver_with_no_chat_id_is_reported_not_silently_dropped(self):
        db, send = self._sweep([{"id": LEAD, "instant_pdf_driver_id": DRIVER}], chat=None)
        send.assert_not_awaited()
        db.mark_instant_pdf_delivered.assert_not_called()

    def test_nothing_paid_means_nothing_sent(self):
        db, send = self._sweep([])
        send.assert_not_awaited()
        db.mark_instant_pdf_delivered.assert_not_called()

    def test_the_dashboard_still_sees_it(self):
        """"itll still go to tristatetags.com/backend" — same lead row, same board."""
        src = (ROOT / "instant_pdf.py").read_text(encoding="utf-8")
        self.assertIn('table("leads")', src)


class TheDatabaseSideExistsTest(unittest.TestCase):
    """A mocked db hides a missing method — that is how the portal shipped dead."""

    def test_the_real_class_has_the_sweep_methods(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_real_db_instant", ROOT / "utils" / "database.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        for m in ("get_paid_instant_pdfs_undelivered", "mark_instant_pdf_delivered"):
            with self.subTest(method=m):
                self.assertTrue(callable(getattr(mod.Database, m, None)), m)


if __name__ == "__main__":
    unittest.main()
