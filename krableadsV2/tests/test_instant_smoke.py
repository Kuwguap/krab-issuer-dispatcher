r"""The whole Instant Tag chain, joined up — money in, tag out, supervisor told.

Every link of this chain already has its own suite, and all of them pass. What
nothing covered was the JOIN: Stripe's webhook writing `instant_pdf_paid_at`
onto a lead row, and the bot's sweep reading that SAME row a tick later and
putting the tag in the paying driver's chat. Two green unit suites either side
of a seam prove nothing about the seam.

So this drives steps 3 -> 4 -> 5 as one flow:

    /api/stripe/webhook  ->  leads.instant_pdf_paid_at
                         ->  deliver_paid_instant_pdfs
                         ->  _deliver_skip_dispatch(how="paid")
                         ->  the driver's chat, the groups, the supervisors
                         ->  db.mark_instant_pdf_delivered

against ONE fake database shared by both ends, and one wire-level Telegram
double, so "the driver got the tag" means a sendDocument really carried that
driver's chat id — not that a mock was awaited.

The two things the operator actually cares about:

  A. the driver who paid gets the tag PDF, and
  B. a supervisor is told who paid and for which reference.

And the three things that keep the money honest: delivered is stamped exactly
once, a second tick does not re-send, and a delivery that FAILED is not stamped
delivered — because the stamp is the only thing standing between a paid driver
and a tag that never arrives.

Run:  venv\Scripts\python.exe -m pytest tests/test_instant_smoke.py -q
"""
import asyncio
import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy-key")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11")

# The existing suites either side of the seam. Imported, not re-implemented:
# `_signed` is the only correct way to build a Stripe header (it uses
# verify_stripe_signature's own scheme), and `Transport` is the wire double
# every Skip Dispatch assertion is already written against.
from test_instant_pdf import SECRET, _signed        # noqa: E402
from test_skip_dispatch import Transport            # noqa: E402

import admin_dashboard as ad                        # noqa: E402
import bot                                          # noqa: E402
import telegram                                     # noqa: E402
from telegram.error import Forbidden                # noqa: E402

LEAD_ID = "33333333-3333-4333-8333-333333333333"
GROUP_ID = "66666666-6666-4666-8666-666666666666"
DRIVER_ID = "55555555-5555-4555-8555-555555555555"
GROUP_CHAT = -100777
DRIVER_TG = 900600
SUPERVISOR_TG = 900700
REFERENCE = "ABC12345"
SESSION_ID = "cs_test_smoke"

DRIVER = {"id": DRIVER_ID, "driver_name": "Susan",
          "driver_telegram_id": str(DRIVER_TG)}

# Frozen stand-ins for PostgREST's now(). Nothing here reads a wall clock, so a
# slow machine and a fast one see the same row.
PAID_AT = "2026-08-31T12:00:00+00:00"
DELIVERED_AT = "2026-08-31T12:00:30+00:00"

VEHICLE = "\n".join([
    "CHARLES JONES", "9 hibiscus Lane", "Monticello New York 13701",
    "9 hibiscus Lane", "Monticello New York 13701",
    "1N4AL3AP0HC166043", "2017 Nissan Altima", "Grey",
    "Geico", "0407306000", "now 1 hour",
])


# --------------------------------------------------------------------------
# One database, read from both ends of the seam.
# --------------------------------------------------------------------------
class _Update:
    """`.update(...).eq(...).is_(col, "null").execute()` — the exact chain
    record_paid_session builds, applied to the in-memory rows so the sweep can
    then read what the webhook actually wrote."""

    def __init__(self, rows, patch):
        self._rows, self._patch = rows, dict(patch or {})
        self._eq, self._null = {}, []

    def eq(self, col, val):
        self._eq[col] = str(val)
        return self

    def is_(self, col, val):
        if str(val).strip().lower() == "null":
            self._null.append(col)
        return self

    def execute(self):
        touched = []
        for row in self._rows.values():
            if any(str(row.get(c) or "") != v for c, v in self._eq.items()):
                continue
            # The null-claim: a row already stamped matches nothing, which is
            # what makes a Stripe retry a no-op.
            if any(str(row.get(c) or "").strip() for c in self._null):
                continue
            for k, v in self._patch.items():
                row[k] = PAID_AT if v == "now()" else v
            touched.append(dict(row))
        return SimpleNamespace(data=touched)


class _Select:
    def __init__(self, rows):
        self._rows, self._eq, self._limit = rows, {}, None

    def eq(self, col, val):
        self._eq[col] = str(val)
        return self

    def limit(self, n):
        self._limit = n
        return self

    def execute(self):
        out = [dict(r) for r in self._rows.values()
               if all(str(r.get(c) or "") == v for c, v in self._eq.items())]
        return SimpleNamespace(data=out[:self._limit] if self._limit else out)


class _Client:
    def __init__(self, rows):
        self._rows = rows

    def table(self, name):
        assert name == "leads", name
        return SimpleNamespace(
            update=lambda patch: _Update(self._rows, patch),
            select=lambda *cols: _Select(self._rows),
        )


class FakeInstantDB:
    """The lead row, the sweep's two methods, and just enough of the rest of
    Database for a real Skip Dispatch delivery to run end to end.

    Same shape as tests/test_skip_dispatch.py's FakeDB (a `__getattr__` catch-all
    under a handful of real methods) — but `accept_lead_assignment` has to
    answer for real here: _book_delivery_against_driver refuses the whole
    delivery when it comes back empty.
    """

    def __init__(self, *, paid=False):
        self.rows = {LEAD_ID: {
            "id": LEAD_ID, "reference_id": REFERENCE, "price": "$250",
            "driver_amount": "$200", "group_id": GROUP_ID,
            "instant_tag": True, "phone_number": "845-423-9476",
            "vehicle_details": VEHICLE, "extra_info": "now 1 hour",
            "instant_pdf_driver_id": DRIVER_ID,
            "instant_pdf_requested_at": "2026-08-31T11:59:00+00:00",
            "instant_pdf_session_id": SESSION_ID,
            "instant_pdf_paid_at": PAID_AT if paid else None,
            "instant_pdf_delivered_at": None,
        }}
        self.client = _Client(self.rows)
        self.delivered_marks = []
        self._held = None

    # -- what the sweep reads and writes -------------------------------
    def get_paid_instant_pdfs_undelivered(self):
        return [dict(r) for r in self.rows.values()
                if str(r.get("instant_pdf_paid_at") or "").strip()
                and not str(r.get("instant_pdf_delivered_at") or "").strip()]

    def mark_instant_pdf_delivered(self, lead_id):
        self.delivered_marks.append(str(lead_id))
        self.rows[str(lead_id)]["instant_pdf_delivered_at"] = DELIVERED_AT
        return True

    # -- what the delivery reads and writes ----------------------------
    def get_lead_by_id(self, lead_id):
        row = self.rows.get(str(lead_id))
        return dict(row) if row else None

    def update_lead(self, lead_id, patch):
        row = self.rows.get(str(lead_id))
        if row is not None:
            row.update(patch or {})
        return True

    def get_all_groups(self):
        return [{"id": GROUP_ID, "group_name": "HighKage", "is_active": True,
                 "group_telegram_id": str(GROUP_CHAT)}]

    def get_group_by_id(self, group_id):
        return self.get_all_groups()[0] if str(group_id) == GROUP_ID else None

    def get_all_drivers(self):
        return [dict(DRIVER, is_active=True)]

    def get_lead_assignment_status(self, lead_id):
        return self._held

    def create_lead_assignment(self, lead_id, driver_id, group_id=None):
        return {"id": "asg1"}

    def accept_lead_assignment(self, lead_id, driver_id):
        self._held = {"driver_id": str(driver_id),
                      "driver": {"driver_name": DRIVER["driver_name"]}}
        return {"id": "asg1", "driver_id": str(driver_id)}

    def allocate_temp_plate(self, is_nj):
        return {"plate": "000001V", "control_number": "1234567890"}

    def get_setting(self, key):
        return None

    def __getattr__(self, name):
        def _noop(*a, **k):
            return [] if name.startswith("get_") else None
        return _noop


class BlockedDriverTransport(Transport):
    """Telegram refusing everything aimed at one chat — a driver who blocked the
    bot, which is the ordinary way a paid delivery fails in production."""

    def __init__(self, blocked_chat_id):
        super().__init__()
        self.blocked = str(blocked_chat_id)

    async def do_post(self, endpoint, data, **kwargs):
        if (endpoint in ("sendMessage", "sendDocument")
                and str((data or {}).get("chat_id")) == self.blocked):
            self.calls.append((endpoint, dict(data or {})))
            raise Forbidden("bot was blocked by the user")
        return await super().do_post(endpoint, data, **kwargs)


async def _make_bot(transport):
    """A real telegram.Bot whose only exit is the transport double."""
    b = telegram.Bot(os.environ["TELEGRAM_BOT_TOKEN"])
    with mock.patch.object(telegram.Bot, "_do_post", transport.do_post):
        await b.initialize()
    return b


async def _sweep(fake_db, transport, *, ticks=1):
    """Run deliver_paid_instant_pdfs against this database and this wire."""
    tg = await _make_bot(transport)
    ctx = mock.MagicMock()
    ctx.bot = tg
    ctx.application.bot_data = {}
    with mock.patch.object(telegram.Bot, "_do_post", transport.do_post), \
            mock.patch.object(bot, "db", fake_db), \
            mock.patch.object(bot, "_driver_row_by_id",
                              mock.MagicMock(return_value=dict(DRIVER))), \
            mock.patch.object(bot, "_global_supervisory_chat_ids",
                              lambda: [SUPERVISOR_TG]), \
            mock.patch.object(bot, "followup_team_chat_id", lambda: None):
        for _ in range(ticks):
            await bot.deliver_paid_instant_pdfs(ctx)
    return transport


def _docs_to(transport, chat_id):
    return [d for d in transport.of("sendDocument")
            if str(d.get("chat_id")) == str(chat_id)]


def _texts_to(transport, chat_id):
    return [str(d.get("text") or "") for d in transport.of("sendMessage")
            if str(d.get("chat_id")) == str(chat_id)]


class ThePaidTagReachesTheDriverTest(unittest.IsolatedAsyncioTestCase):
    """Steps 4 and 5, driven off a row that is already paid."""

    async def asyncSetUp(self):
        self.db = FakeInstantDB(paid=True)
        self.wire = await _sweep(self.db, Transport())

    async def test_the_driver_who_paid_gets_the_tag(self):
        docs = _docs_to(self.wire, DRIVER_TG)
        self.assertTrue(
            docs,
            "no PDF reached the paying driver's chat; "
            f"documents went to {[d.get('chat_id') for d in self.wire.of('sendDocument')]}")
        self.assertIn(REFERENCE, "".join(str(d.get("caption") or "") for d in docs),
                      "the tag that arrived is not this lead's")

    async def test_the_driver_gets_the_job_ticket_with_the_tag(self):
        """The tag alone is half a delivery — the address and phone are the
        other half, and they are what the deposit bought."""
        said = " ".join(_texts_to(self.wire, DRIVER_TG))
        self.assertIn("LEAD ACCEPTED", said)
        self.assertIn("CHARLES JONES", said.upper())

    async def test_a_supervisor_is_told_who_paid_and_for_which_reference(self):
        said = " ".join(_texts_to(self.wire, SUPERVISOR_TG))
        self.assertIn("Susan", said, "the notice does not name the driver")
        self.assertIn(REFERENCE, said, "the notice does not name the reference")
        self.assertIn("paid", said.lower())

    async def test_the_team_still_sees_the_job(self):
        self.assertTrue(_docs_to(self.wire, GROUP_CHAT),
                        "dispatch was skipped, not kept in the dark")

    async def test_the_lead_is_stamped_delivered_exactly_once(self):
        self.assertEqual([LEAD_ID], self.db.delivered_marks)


class ADeliveredTagIsNotSentTwiceTest(unittest.IsolatedAsyncioTestCase):
    """The sweep runs every 20 seconds. Without the delivered stamp holding,
    the driver is re-sent the same paid tag for as long as the bot is up."""

    async def test_a_second_tick_sends_nothing_more(self):
        db = FakeInstantDB(paid=True)
        first = await _sweep(db, Transport())
        sent_first = len(_docs_to(first, DRIVER_TG))
        second = await _sweep(db, Transport())
        self.assertTrue(sent_first, "nothing went out on the first tick at all")
        self.assertEqual([], _docs_to(second, DRIVER_TG),
                         "the paid tag was delivered a second time")
        self.assertEqual([LEAD_ID], db.delivered_marks)


class AFailedDeliveryIsNotCalledDeliveredTest(unittest.IsolatedAsyncioTestCase):
    """The driver blocked the bot: nothing this sweep sends them arrives.

    The delivered stamp is the ONLY thing that takes a paid lead out of the
    retry sweep. Stamping it for a send that never landed is how a driver pays
    $200 and receives nothing, permanently — no retry, and no record that
    anything went wrong."""

    async def asyncSetUp(self):
        self.db = FakeInstantDB(paid=True)
        self.wire = await _sweep(self.db, BlockedDriverTransport(DRIVER_TG))

    async def test_a_tag_that_never_reached_the_driver_is_not_stamped_delivered(self):
        self.assertEqual(
            [], self.db.delivered_marks,
            "the lead was marked delivered although every send to the driver "
            "was refused — the sweep will never try again")

    async def test_the_paid_lead_is_still_waiting_on_the_next_tick(self):
        self.assertTrue(self.db.get_paid_instant_pdfs_undelivered(),
                        "the retry sweep can no longer see this paid lead")

    async def test_a_driver_who_never_got_it_is_not_reported_as_paid_and_sent(self):
        said = " ".join(_texts_to(self.wire, SUPERVISOR_TG))
        self.assertNotIn(
            "Instant tag paid by", said,
            "supervisors were told the tag went to Susan; it was refused by "
            "Telegram every time")

    async def test_the_failure_is_surfaced_to_a_supervisor(self):
        """Silence here is the worst outcome: the money is taken, the driver has
        nothing, and nobody is told to look."""
        said = " ".join(_texts_to(self.wire, SUPERVISOR_TG))
        self.assertTrue(said, "no supervisor heard anything about the failure")
        self.assertIn(REFERENCE, said, "the alert does not say which lead")
        self.assertTrue(
            any(w in said.lower() for w in
                ("not deliver", "undelivered", "could not", "failed", "blocked",
                 "did not")),
            f"nothing in the supervisor's message says the delivery failed: {said!r}")


class TheWebhookAndTheSweepAgreeTest(unittest.IsolatedAsyncioTestCase):
    """The seam itself. A correctly-signed Stripe event goes into the Flask
    endpoint, and the tag comes out of the bot's sweep — one database between
    them, nothing hand-stamped in the middle."""

    def setUp(self):
        ad.app.config["TESTING"] = True
        self.client = ad.app.test_client()
        self.db = FakeInstantDB(paid=False)

    def _pay(self):
        event = {"type": "checkout.session.completed",
                 "data": {"object": {"id": SESSION_ID, "payment_status": "paid",
                                     "metadata": {"lead_id": LEAD_ID,
                                                  "driver_id": DRIVER_ID,
                                                  "kind": "instant_pdf"}}}}
        raw, header = _signed(event, SECRET)
        with mock.patch.object(ad, "db", self.db), \
                mock.patch.dict(os.environ, {"STRIPE_WEBHOOK_SECRET": SECRET}):
            return self.client.post("/api/stripe/webhook", data=raw,
                                    headers={"Stripe-Signature": header,
                                             "Content-Type": "application/json"})

    async def test_an_unpaid_lead_gets_no_tag(self):
        """The other half of the join: nothing before the money."""
        wire = await _sweep(self.db, Transport())
        self.assertEqual([], _docs_to(wire, DRIVER_TG))
        self.assertEqual([], self.db.delivered_marks)

    async def test_stripes_call_stamps_the_lead_paid(self):
        r = self._pay()
        self.assertEqual(200, r.status_code, r.get_data(as_text=True))
        self.assertEqual(PAID_AT, self.db.rows[LEAD_ID]["instant_pdf_paid_at"])

    async def test_paying_puts_the_tag_in_the_drivers_chat(self):
        self._pay()
        wire = await _sweep(self.db, Transport())
        self.assertTrue(
            _docs_to(wire, DRIVER_TG),
            "Stripe said paid and the sweep still delivered nothing — the "
            "webhook and the sweep are not reading the same lead")

    async def test_paying_tells_a_supervisor(self):
        self._pay()
        wire = await _sweep(self.db, Transport())
        said = " ".join(_texts_to(wire, SUPERVISOR_TG))
        self.assertIn("Susan", said)
        self.assertIn(REFERENCE, said)

    async def test_the_sweep_closes_the_lead_after_the_webhook_opened_it(self):
        self._pay()
        await _sweep(self.db, Transport())
        self.assertEqual([LEAD_ID], self.db.delivered_marks)
        self.assertEqual([], self.db.get_paid_instant_pdfs_undelivered())

    async def test_stripe_retrying_the_same_event_does_not_re_deliver(self):
        """Stripe retries until it gets a 2xx. The second event must not put a
        second tag in the driver's chat."""
        self._pay()
        await _sweep(self.db, Transport())
        self.assertEqual(200, self._pay().status_code)
        again = await _sweep(self.db, Transport())
        self.assertEqual([], _docs_to(again, DRIVER_TG))
        self.assertEqual([LEAD_ID], self.db.delivered_marks)

    async def test_an_unsigned_call_delivers_nothing(self):
        with mock.patch.object(ad, "db", self.db), \
                mock.patch.dict(os.environ, {"STRIPE_WEBHOOK_SECRET": SECRET}):
            r = self.client.post("/api/stripe/webhook", data=json.dumps(
                {"type": "checkout.session.completed",
                 "data": {"object": {"id": SESSION_ID, "payment_status": "paid",
                                     "metadata": {"lead_id": LEAD_ID}}}}),
                headers={"Content-Type": "application/json"})
        self.assertEqual(400, r.status_code)
        self.assertIsNone(self.db.rows[LEAD_ID]["instant_pdf_paid_at"])
        wire = await _sweep(self.db, Transport())
        self.assertEqual([], _docs_to(wire, DRIVER_TG),
                         "an unsigned webhook released a tag nobody paid for")


if __name__ == "__main__":
    unittest.main()
