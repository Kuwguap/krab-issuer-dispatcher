r"""The success page settles the payment when the webhook does not.

The webhook is the designed path to `instant_pdf_paid_at`, and it is the one path
this repo cannot prove is alive: it fires only if somebody registered
/api/stripe/webhook in the Stripe dashboard, and verify_stripe_signature refuses
EVERY event when STRIPE_WEBHOOK_SECRET is blank -- so a half-configured account
400s the lot in silence. paid_at is never written, the bot's sweep never finds the
lead, and the driver has paid for a tag that never arrives. The sibling coverage
product shipped exactly that, with no webhook registered at all.

The driver's browser always comes back to /instant/success. These tests pin what it
does there: verify the session with Stripe itself, claim the same null column the
webhook claims, and never -- for any Stripe or database failure -- fail to say
thank you.

They also pin the quieter failure underneath both paths: an UPDATE that changes no
rows. That is either "somebody settled it first" or "nothing was recorded at all",
and answering the second with a green log and a 2xx is the most deceptive thing this
chain can do -- Stripe marks the event delivered, every surface reads paid, and the
tag is lost with nobody looking for it.

Run:  venv\Scripts\python.exe -m pytest tests/test_instant_settlement.py -q
"""
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
import admin_dashboard as ad  # noqa: E402
import instant_pdf  # noqa: E402

LEAD = "11111111-2222-3333-4444-555555555555"
DRIVER = "d1"
SESSION = "cs_live_1"
SECRET = "whsec_test"


def _stripe_session(status="paid", lead_id=LEAD, driver_id=DRIVER):
    """What GET /v1/checkout/sessions/{id} answers for a paid instant tag."""
    return {"id": SESSION, "payment_status": status, "client_reference_id": lead_id,
            "metadata": {"lead_id": lead_id, "driver_id": driver_id,
                         "kind": "instant_pdf"}}


PAID_AT = "2026-08-31T12:00:00+00:00"


def _db(updated_rows=None, reread_rows=None):
    """A database double that answers the way PostgREST does.

    `updated_rows` is what the UPDATE reports touching: one row means this call
    stamped, an empty list means the null-claim matched nothing. `reread_rows`
    is the follow-up SELECT that tells the two zero-row cases apart -- a row
    carrying instant_pdf_paid_at means somebody else settled it, an empty list
    means nothing was recorded and the payment is lost.

    Left unset, both answer with MagicMocks: not lists, which the code reads as
    unknown rather than empty.
    """
    db = mock.MagicMock()
    if updated_rows is not None:
        chain = (db.client.table.return_value.update.return_value
                 .eq.return_value.is_.return_value)
        chain.execute.return_value = mock.MagicMock(data=updated_rows)
    if reread_rows is not None:
        chain = (db.client.table.return_value.select.return_value
                 .eq.return_value.limit.return_value)
        chain.execute.return_value = mock.MagicMock(data=reread_rows)
    return db


class TheSuccessPageSettlesTest(unittest.TestCase):
    """The browser's return trip is a settlement path, not just a thank-you."""

    def setUp(self):
        ad.app.config["TESTING"] = True
        self.client = ad.app.test_client()

    def _visit(self, session=None, db=None, get=None, sid=SESSION,
               stripe_key="sk_test", ok=True):
        if get is None:
            resp = mock.MagicMock(ok=ok, status_code=200 if ok else 404, content=b"x")
            resp.json.return_value = (session if session is not None
                                      else {"error": {"message": "No such session"}})
            get = mock.MagicMock(return_value=resp)
        db = db if db is not None else _db([{"id": LEAD}])
        with mock.patch.object(ad, "db", db), \
                mock.patch.object(instant_pdf.requests, "get", get), \
                mock.patch.dict(os.environ, {"STRIPE_SECRET_KEY": stripe_key}):
            url = f"/instant/success?session_id={sid}" if sid else "/instant/success"
            r = self.client.get(url)
        return r, db, get

    def _paid_write(self, db):
        return db.client.table.return_value.update.call_args[0][0]

    def test_a_paid_session_the_webhook_missed_is_stamped(self):
        """The whole point: no webhook, and the tag still goes out."""
        r, db, _ = self._visit(_stripe_session())
        self.assertEqual(200, r.status_code)
        wrote = self._paid_write(db)
        self.assertEqual("now()", wrote["instant_pdf_paid_at"])
        self.assertEqual(SESSION, wrote["instant_pdf_session_id"])
        self.assertEqual(DRIVER, wrote["instant_pdf_driver_id"])

    def test_it_asks_stripe_rather_than_believing_the_url(self):
        """A session_id in a query string is something anybody can type."""
        _, _, get = self._visit(_stripe_session())
        self.assertEqual(f"{instant_pdf._STRIPE_API}/{SESSION}", get.call_args[0][0])
        self.assertEqual(("sk_test", ""), get.call_args.kwargs["auth"])

    def test_a_free_session_counts_as_paid(self):
        """Stripe says no_payment_required for a fully discounted total."""
        _, db, _ = self._visit(_stripe_session(status="no_payment_required"))
        self.assertIn("instant_pdf_paid_at", self._paid_write(db))

    def test_an_unpaid_session_stamps_nothing(self):
        r, db, _ = self._visit(_stripe_session(status="unpaid"))
        self.assertEqual(200, r.status_code)
        db.client.table.return_value.update.assert_not_called()

    def test_it_claims_the_null_exactly_as_the_webhook_does(self):
        """Same guard, or the two paths would race to overwrite each other."""
        _, db, _ = self._visit(_stripe_session())
        chain = db.client.table.return_value.update.return_value.eq.return_value
        chain.is_.assert_called_with("instant_pdf_paid_at", "null")

    def test_it_never_writes_delivered(self):
        """Delivery stays the bot's to claim, once the tag is really sent."""
        _, db, _ = self._visit(_stripe_session())
        self.assertNotIn("instant_pdf_delivered_at", self._paid_write(db))

    def test_a_session_with_no_lead_is_not_guessed_at(self):
        s = _stripe_session()
        s["metadata"] = {}
        s["client_reference_id"] = ""
        _, db, _ = self._visit(s)
        db.client.table.return_value.update.assert_not_called()


class TheWebhookStillWinsTest(unittest.TestCase):
    """A webhook that DID land must not be re-stamped or overwritten."""

    def setUp(self):
        ad.app.config["TESTING"] = True
        self.client = ad.app.test_client()

    def _visit_already_paid(self):
        resp = mock.MagicMock(ok=True, status_code=200, content=b"x")
        resp.json.return_value = _stripe_session()
        # The null-claim matched no rows, and the re-read says why: paid already.
        db = _db([], [{"instant_pdf_paid_at": PAID_AT}])
        with mock.patch.object(ad, "db", db), \
                mock.patch.object(instant_pdf.requests, "get",
                                  mock.MagicMock(return_value=resp)), \
                mock.patch.dict(os.environ, {"STRIPE_SECRET_KEY": "sk_test"}), \
                self.assertLogs("instant_pdf", level="INFO") as logs:
            r = self.client.get(f"/instant/success?session_id={SESSION}")
        return r, db, "\n".join(logs.output)

    def test_the_page_still_renders(self):
        r, _, _ = self._visit_already_paid()
        self.assertEqual(200, r.status_code)
        self.assertIn("payment received", r.get_data(as_text=True))

    def test_the_timestamp_is_not_moved(self):
        """The update is issued, but the null-claim is what makes it a no-op."""
        _, db, _ = self._visit_already_paid()
        chain = db.client.table.return_value.update.return_value.eq.return_value
        chain.is_.assert_called_with("instant_pdf_paid_at", "null")

    def test_already_settled_says_so(self):
        _, _, logged = self._visit_already_paid()
        self.assertIn("already settled", logged)

    def test_settling_here_shouts_about_the_webhook(self):
        """Reaching this line at all is the signal the webhook is dead."""
        resp = mock.MagicMock(ok=True, status_code=200, content=b"x")
        resp.json.return_value = _stripe_session()
        with mock.patch.object(ad, "db", _db([{"id": LEAD}])), \
                mock.patch.object(instant_pdf.requests, "get",
                                  mock.MagicMock(return_value=resp)), \
                mock.patch.dict(os.environ, {"STRIPE_SECRET_KEY": "sk_test"}), \
                self.assertLogs("instant_pdf", level="INFO") as logs:
            self.client.get(f"/instant/success?session_id={SESSION}")
        logged = "\n".join(logs.output)
        self.assertIn("SUCCESS PAGE", logged)
        self.assertIn("STRIPE_WEBHOOK_SECRET", logged)

    def test_both_paths_write_the_same_thing(self):
        """One helper, so the webhook and the page can never drift apart."""
        raw = json.dumps({"type": "checkout.session.completed",
                          "data": {"object": _stripe_session()}}).encode()
        ts = int(time.time())
        sig = hmac.new(SECRET.encode(), f"{ts}.".encode() + raw,
                       hashlib.sha256).hexdigest()
        hook_db = _db([{"id": LEAD}])
        with mock.patch.object(ad, "db", hook_db), \
                mock.patch.dict(os.environ, {"STRIPE_WEBHOOK_SECRET": SECRET}):
            self.client.post("/api/stripe/webhook", data=raw,
                             headers={"Stripe-Signature": f"t={ts},v1={sig}",
                                      "Content-Type": "application/json"})
        resp = mock.MagicMock(ok=True, status_code=200, content=b"x")
        resp.json.return_value = _stripe_session()
        page_db = _db([{"id": LEAD}])
        with mock.patch.object(ad, "db", page_db), \
                mock.patch.object(instant_pdf.requests, "get",
                                  mock.MagicMock(return_value=resp)), \
                mock.patch.dict(os.environ, {"STRIPE_SECRET_KEY": "sk_test"}):
            self.client.get(f"/instant/success?session_id={SESSION}")
        self.assertEqual(hook_db.client.table.return_value.update.call_args[0][0],
                         page_db.client.table.return_value.update.call_args[0][0])


class TheWebhookAnswersStripeHonestlyTest(unittest.TestCase):
    """A 2xx tells Stripe to stop asking. It must only mean the money is recorded."""

    def setUp(self):
        ad.app.config["TESTING"] = True
        self.client = ad.app.test_client()

    def _hook(self, db):
        raw = json.dumps({"type": "checkout.session.completed",
                          "data": {"object": _stripe_session()}}).encode()
        ts = int(time.time())
        sig = hmac.new(SECRET.encode(), f"{ts}.".encode() + raw,
                       hashlib.sha256).hexdigest()
        with mock.patch.object(ad, "db", db), \
                mock.patch.dict(os.environ, {"STRIPE_WEBHOOK_SECRET": SECRET}), \
                self.assertLogs("instant_pdf", level="INFO") as logs:
            r = self.client.post("/api/stripe/webhook", data=raw,
                                 headers={"Stripe-Signature": f"t={ts},v1={sig}",
                                          "Content-Type": "application/json"})
        return r, "\n".join(logs.output)

    def test_a_stamped_row_is_a_2xx_and_says_paid(self):
        r, logged = self._hook(_db([{"id": LEAD}]))
        self.assertEqual(200, r.status_code)
        self.assertIn("instant pdf PAID", logged)

    def test_an_already_settled_lead_is_a_2xx_and_says_so(self):
        """The success page, or an earlier retry, got there first — stop asking."""
        r, logged = self._hook(_db([], [{"instant_pdf_paid_at": PAID_AT}]))
        self.assertEqual(200, r.status_code)
        self.assertTrue(r.get_json()["already"])
        self.assertIn("already settled", logged)
        self.assertNotIn("instant pdf PAID for lead", logged)

    def test_a_write_that_recorded_nothing_asks_stripe_to_retry(self):
        """The deceptive failure: green log, 2xx, paid_at still null. Never again."""
        r, logged = self._hook(_db([], []))
        self.assertEqual(500, r.status_code, "a 5xx is what makes Stripe try again")
        self.assertIn("was NOT recorded", logged)
        self.assertNotIn("instant pdf PAID for lead", logged)

    def test_an_unverifiable_write_does_not_loop_stripe(self):
        """Cannot re-read, cannot know — take the 2xx over a days-long retry."""
        db = _db([])
        db.client.table.return_value.select.side_effect = Exception("db down")
        r, _ = self._hook(db)
        self.assertEqual(200, r.status_code)


class NothingCostsTheDriverTheirPageTest(unittest.TestCase):
    """The money is already gone. A thank-you is the least they are owed."""

    def setUp(self):
        ad.app.config["TESTING"] = True
        self.client = ad.app.test_client()

    def _visit(self, get, db=None, stripe_key="sk_test"):
        with mock.patch.object(ad, "db", db if db is not None else _db([{"id": LEAD}])), \
                mock.patch.object(instant_pdf.requests, "get", get), \
                mock.patch.dict(os.environ, {"STRIPE_SECRET_KEY": stripe_key}):
            return self.client.get(f"/instant/success?session_id={SESSION}")

    def test_stripe_unreachable_still_renders(self):
        get = mock.MagicMock(side_effect=instant_pdf.requests.RequestException("boom"))
        r = self._visit(get)
        self.assertEqual(200, r.status_code)
        self.assertIn("payment received", r.get_data(as_text=True))

    def test_a_stripe_error_response_still_renders(self):
        resp = mock.MagicMock(ok=False, status_code=404, content=b"x")
        resp.json.return_value = {"error": {"message": "No such checkout.session"}}
        r = self._visit(mock.MagicMock(return_value=resp))
        self.assertEqual(200, r.status_code)
        self.assertIn("payment received", r.get_data(as_text=True))

    def test_unparseable_stripe_json_still_renders(self):
        resp = mock.MagicMock(ok=True, status_code=200, content=b"<html>")
        resp.json.side_effect = ValueError("not json")
        r = self._visit(mock.MagicMock(return_value=resp))
        self.assertEqual(200, r.status_code)

    def test_a_dead_database_still_renders(self):
        resp = mock.MagicMock(ok=True, status_code=200, content=b"x")
        resp.json.return_value = _stripe_session()
        db = mock.MagicMock()
        db.client.table.side_effect = Exception("db down")
        r = self._visit(mock.MagicMock(return_value=resp), db=db)
        self.assertEqual(200, r.status_code)
        self.assertIn("payment received", r.get_data(as_text=True))

    def test_no_stripe_key_renders_and_says_why(self):
        get = mock.MagicMock()
        with self.assertLogs("instant_pdf", level="INFO") as logs:
            r = self._visit(get, stripe_key="")
        self.assertEqual(200, r.status_code)
        get.assert_not_called()
        self.assertIn("STRIPE_SECRET_KEY", "\n".join(logs.output))


class TheSuccessPageSaysWhenNothingWasRecordedTest(unittest.TestCase):
    """No retry channel here, so the log line is the only thing that can chase it."""

    def setUp(self):
        ad.app.config["TESTING"] = True
        self.client = ad.app.test_client()

    def test_a_paid_but_unrecorded_session_is_an_error(self):
        resp = mock.MagicMock(ok=True, status_code=200, content=b"x")
        resp.json.return_value = _stripe_session()
        with mock.patch.object(ad, "db", _db([], [])), \
                mock.patch.object(instant_pdf.requests, "get",
                                  mock.MagicMock(return_value=resp)), \
                mock.patch.dict(os.environ, {"STRIPE_SECRET_KEY": "sk_test"}), \
                self.assertLogs("instant_pdf", level="INFO") as logs:
            r = self.client.get(f"/instant/success?session_id={SESSION}")
        logged = "\n".join(logs.output)
        self.assertEqual(200, r.status_code, "the driver still gets their thank-you")
        self.assertIn("PAID at Stripe", logged)
        self.assertIn("never be delivered", logged)
        self.assertNotIn("instant pdf PAID for lead", logged)


class TheCheckoutStampIsNotAssumedTest(unittest.TestCase):
    """A silent zero-row stamp is why a success page shows a blank Reference."""

    def setUp(self):
        ad.app.config["TESTING"] = True
        self.client = ad.app.test_client()

    def _checkout(self, updated_rows):
        resp = mock.MagicMock(ok=True, status_code=200, content=b"x")
        resp.json.return_value = {"id": SESSION, "url": "https://pay.stripe/x"}
        db = mock.MagicMock()
        (db.client.table.return_value.update.return_value
         .eq.return_value.execute.return_value) = mock.MagicMock(data=updated_rows)
        # Watching logger.error directly rather than assertLogs: the happy path
        # here logs NOTHING, and assertLogs fails an empty capture.
        errors = []
        with mock.patch.object(ad, "db", db), \
                mock.patch.object(instant_pdf.requests, "post",
                                  mock.MagicMock(return_value=resp)), \
                mock.patch.dict(os.environ, {"ADMIN_API_KEY": "k",
                                             "INTEGRATIONS_API_KEY": "k",
                                             "STRIPE_SECRET_KEY": "sk_test"}), \
                mock.patch.object(instant_pdf.logger, "error",
                                  lambda m, *a: errors.append(str(m) % a)):
            r = self.client.post("/api/instant/checkout",
                                 json={"lead_id": LEAD, "driver_id": DRIVER,
                                       "reference_id": "REF1"},
                                 headers={"Authorization": "Bearer k"})
        return r, "\n".join(errors)

    def test_a_zero_row_stamp_is_reported(self):
        r, logged = self._checkout([])
        self.assertEqual(200, r.status_code, "the link is valid, the sale stands")
        self.assertIn("affected no rows", logged)

    def test_a_stamped_row_says_nothing(self):
        r, logged = self._checkout([{"id": LEAD}])
        self.assertEqual(200, r.status_code)
        self.assertNotIn("affected no rows", logged)


class NoSessionIsANoOpTest(unittest.TestCase):
    """Somebody opening the URL by hand must not touch Stripe or the database."""

    def setUp(self):
        ad.app.config["TESTING"] = True
        self.client = ad.app.test_client()

    def _visit(self, url):
        get = mock.MagicMock()
        db = mock.MagicMock()
        with mock.patch.object(ad, "db", db), \
                mock.patch.object(instant_pdf.requests, "get", get), \
                mock.patch.dict(os.environ, {"STRIPE_SECRET_KEY": "sk_test"}):
            r = self.client.get(url)
        return r, db, get

    def test_a_missing_session_id_touches_nothing(self):
        r, db, get = self._visit("/instant/success")
        self.assertEqual(200, r.status_code)
        get.assert_not_called()
        db.client.table.assert_not_called()

    def test_a_blank_session_id_touches_nothing(self):
        r, db, get = self._visit("/instant/success?session_id=%20")
        self.assertEqual(200, r.status_code)
        get.assert_not_called()
        db.client.table.assert_not_called()

    def test_the_cancelled_page_never_settles(self):
        r, db, get = self._visit("/instant/cancelled")
        self.assertEqual(200, r.status_code)
        self.assertIn("Nothing was charged", r.get_data(as_text=True))
        get.assert_not_called()
        db.client.table.assert_not_called()


class TheHelperIsTheOnlyWriterTest(unittest.TestCase):
    """One function stamps paid_at, or the two callers drift."""

    def test_only_one_place_writes_paid_at(self):
        src = (ROOT / "instant_pdf.py").read_text(encoding="utf-8")
        self.assertEqual(1, src.count('"instant_pdf_paid_at": "now()"'),
                         "paid_at must be written in record_paid_session alone")

    def test_both_callers_go_through_it(self):
        src = (ROOT / "instant_pdf.py").read_text(encoding="utf-8")
        self.assertEqual(3, src.count("record_paid_session("),
                         "the definition, the webhook's call, and the page's")
        self.assertIn("settle_checkout_session(_resolve().client", src)

    def test_an_unknown_row_count_is_read_as_stamped(self):
        """PostgREST answers with rows; anything else is unknown, not 'already'."""
        db = mock.MagicMock()          # execute() returns a MagicMock, not a list
        self.assertEqual(instant_pdf._STAMPED,
                         instant_pdf.record_paid_session(db.client, LEAD, SESSION, DRIVER))

    def test_a_claimed_row_is_stamped(self):
        db = _db([{"id": LEAD}])
        self.assertEqual(instant_pdf._STAMPED,
                         instant_pdf.record_paid_session(db.client, LEAD, SESSION, DRIVER))

    def test_zero_rows_with_paid_at_set_is_already(self):
        """Somebody won the race — the loser must not call that a failure."""
        db = _db([], [{"instant_pdf_paid_at": PAID_AT}])
        self.assertEqual(instant_pdf._ALREADY,
                         instant_pdf.record_paid_session(db.client, LEAD, SESSION, DRIVER))

    def test_zero_rows_with_paid_at_still_null_is_unrecorded(self):
        """The deceptive one: nothing changed and nobody had settled it."""
        db = _db([], [{"instant_pdf_paid_at": None}])
        self.assertEqual(instant_pdf._UNRECORDED,
                         instant_pdf.record_paid_session(db.client, LEAD, SESSION, DRIVER))

    def test_a_vanished_lead_is_unrecorded(self):
        db = _db([], [])
        self.assertEqual(instant_pdf._UNRECORDED,
                         instant_pdf.record_paid_session(db.client, LEAD, SESSION, DRIVER))

    def test_an_unreadable_reread_is_treated_as_already(self):
        """Never 500 in a loop over a question we cannot answer."""
        db = _db([])
        db.client.table.return_value.select.side_effect = Exception("db down")
        self.assertEqual(instant_pdf._ALREADY,
                         instant_pdf.record_paid_session(db.client, LEAD, SESSION, DRIVER))

    def test_a_driverless_session_leaves_the_checkout_stamp_alone(self):
        db = _db([{"id": LEAD}])
        instant_pdf.record_paid_session(db.client, LEAD, SESSION, "")
        wrote = db.client.table.return_value.update.call_args[0][0]
        self.assertNotIn("instant_pdf_driver_id", wrote)


if __name__ == "__main__":
    unittest.main()
