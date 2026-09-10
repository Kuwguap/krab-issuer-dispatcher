"""A website order is on our insurance only when the order PAID for it.

The website sells the temp tag and the $100 insurance separately. The bot used
to arm our cover on any lead with no insurer + policy on file -- which, on a
tristatetags.com order that paid for the tag only, gave the insurance away: our
carrier and a minted policy number on the tag, then the card and the portal.

Now:
  * the ingest message says when the customer paid for the cover
    ("Coverage: TriState insurance (paid)"), the parser reads it into
    wants_insurance, and ingest writes it on the new lead;
  * a website lead (external_order_id) that did not pay is never armed by the
    tag builder, by _arm_insurance_for_lead or by Skip Dispatch -- the
    supervisors get ONE notice instead;
  * an issuer lead (no external_order_id) is armed exactly as before;
  * an already-armed website lead (e.g. 1K3AX0XS) keeps its cover and its
    existing policy number.

Mocks only -- no network, no database.

Run:  venv/Scripts/python.exe -m pytest tests/test_website_lead_insurance.py -q
"""
import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy-key")

import bot  # noqa: E402
import utils.database as udb  # noqa: E402
from utils import lead_ingest  # noqa: E402
from utils import tag_pdf as _tag_pdf  # noqa: E402
from utils.external_lead_parser import (  # noqa: E402
    PAID_COVERAGE_LINE,
    SAMPLE_MESSAGE,
    coverage_is_paid_tristate,
    parse_external_lead_fields,
    parse_external_lead_message,
)

LEAD_ID = "11111111-2222-3333-4444-555555555555"

# name / addr / csz / dl-addr / dl-csz / vin / car / colour / insurer / policy / notes
VD_UNINSURED = ("Josue Pavon\n2815 Dewey Avenue\nBronx, NY 10465\n-\n-\n"
                "JTLKT324364094480\n2006 Scion xB\nGrey\n-\n-\n-")
VD_HAS_GEICO = VD_UNINSURED.replace("\nGrey\n-\n-\n", "\nGrey\nGeico\nPOL-9\n")

# A tag-only website message: no Insurance / Policy # lines, no Coverage line.
TAG_ONLY_MESSAGE = "\n".join(
    line for line in SAMPLE_MESSAGE.splitlines()
    if not line.startswith(("Insurance:", "Policy #:")))
PAID_MESSAGE = TAG_ONLY_MESSAGE + "\n" + PAID_COVERAGE_LINE


def _lead(**over):
    lead = {"id": LEAD_ID, "reference_id": "WEBTAG01",
            "vehicle_details": VD_UNINSURED,
            "delivery_details": "2815 Dewey Avenue\nBronx, NY 10465",
            "plate": "T123456", "tag_control_number": "C00001",
            "price": "$150", "wants_insurance": False}
    lead.update(over)
    return lead


def _website(**over):
    fields = {"external_order_id": "ab12cd34", "contact_info_source": "External API"}
    fields.update(over)
    return _lead(**fields)


def _fields(lead, db=None):
    db = db or mock.MagicMock()
    with mock.patch.object(bot, "db", db), \
            mock.patch.object(_tag_pdf, "decode_vin_for_tag", lambda v: None):
        return asyncio.run(bot._tag_fields_from_lead(lead, vehicle=1)), db


def _writes(db, key):
    return [c.args[1] for c in db.update_lead.call_args_list if key in c.args[1]]


class RowDB:
    """One lead row that update_lead really changes, so a re-read sees it."""

    def __init__(self, lead):
        self.lead = dict(lead)
        self.updates = []

    def get_lead_by_id(self, lead_id):
        return dict(self.lead) if str(lead_id) == str(self.lead["id"]) else None

    def update_lead(self, lead_id, updates):
        self.updates.append(dict(updates))
        self.lead.update(updates)
        return True

    def armed_writes(self):
        return [u for u in self.updates if "wants_insurance" in u]


def _skip_dispatch(lead):
    """Run Skip Dispatch up to the claim. The claim is refused, so the send
    never happens -- the arming decision before it is what is under test."""
    rdb = RowDB(lead)
    ctx = mock.MagicMock()
    ctx.bot.send_message = mock.AsyncMock()
    with mock.patch.object(bot, "db", rdb), \
            mock.patch.object(bot, "_book_delivery_against_driver",
                              lambda _lead, _driver: (False, "Someone else")):
        delivered = asyncio.run(bot._deliver_skip_dispatch(
            ctx, dict(lead), {"id": "d1", "driver_name": "Susan"}, how="password"))
    return rdb, delivered


class _NoticeCase(unittest.TestCase):
    """Every test starts with nobody told and _tell_supervisors captured."""

    def setUp(self):
        bot._WEBSITE_UNINSURED_NOTIFIED.clear()
        self.tell = mock.AsyncMock()
        patcher = mock.patch.object(bot, "_tell_supervisors", self.tell)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(bot._WEBSITE_UNINSURED_NOTIFIED.clear)


# ---------------------------------------------------------------- the parser

class CoverageLineTest(unittest.TestCase):

    def test_the_paid_coverage_line_arms_the_lead(self):
        state, errors = parse_external_lead_message(PAID_MESSAGE)
        self.assertEqual([], errors)
        self.assertIs(True, state["wants_insurance"])

    def test_case_and_spacing_are_forgiven(self):
        for line in ("coverage :  tristate   insurance ( PAID )",
                     "COVERAGE:TRISTATE INSURANCE(PAID)",
                     "   Coverage: Tri State insurance (paid)   ",
                     "Coverage:\tTriState\tinsurance (paid)"):
            with self.subTest(line=line):
                state, errors = parse_external_lead_message(TAG_ONLY_MESSAGE + "\n" + line)
                self.assertEqual([], errors)
                self.assertIs(True, state["wants_insurance"])

    def test_no_coverage_line_is_not_insurance(self):
        for msg in (TAG_ONLY_MESSAGE, SAMPLE_MESSAGE):
            with self.subTest(msg=msg.splitlines()[-1]):
                state, errors = parse_external_lead_message(msg)
                self.assertEqual([], errors)
                self.assertIs(False, state["wants_insurance"])

    def test_only_the_paid_wording_counts(self):
        for line in ("Coverage: TriState insurance (unpaid)",
                     "Coverage: TriState insurance",
                     "Coverage: none",
                     "Coverage:",
                     "Insurance: TriState insurance (paid)"):
            with self.subTest(line=line):
                state, _ = parse_external_lead_message(TAG_ONLY_MESSAGE + "\n" + line)
                self.assertIs(False, state["wants_insurance"])

    def test_the_line_does_not_disturb_the_other_fields(self):
        state, _ = parse_external_lead_message(SAMPLE_MESSAGE + "\n" + PAID_COVERAGE_LINE)
        self.assertEqual("AC Insurance", state["insurance_company"])
        self.assertEqual("279-06071-913", state["insurance_policy_number"])
        self.assertEqual("bf6923ca", state["external_order_id"])
        self.assertEqual(11, len(state["vehicle_details"].splitlines()))
        self.assertNotIn("Coverage", state["vehicle_details"])

    def test_the_fields_body_reads_it_too(self):
        fields = {"name": "Test User", "phone": "7322418338", "price": "$250.00",
                  "vin": "SCA665C56HUX86704", "vehicle": "2017 Honda Civic, red",
                  "registration address": "543 Garden Place Keyport NJ 07735",
                  "Coverage": "TriState insurance (paid)"}
        state, errors = parse_external_lead_fields(fields)
        self.assertEqual([], errors)
        self.assertIs(True, state["wants_insurance"])
        fields.pop("Coverage")
        state, _ = parse_external_lead_fields(fields)
        self.assertIs(False, state["wants_insurance"])

    def test_the_value_check_itself(self):
        self.assertTrue(coverage_is_paid_tristate("TriState insurance (paid)"))
        self.assertFalse(coverage_is_paid_tristate(None))
        self.assertFalse(coverage_is_paid_tristate(""))


# ---------------------------------------------------------------- the ingest

class IngestDB:
    def __init__(self):
        self.payloads = []

    def create_lead(self, payload):
        self.payloads.append(dict(payload))
        return {"id": LEAD_ID, "reference_id": payload.get("reference_id")}

    def get_all_groups(self):
        return [{"id": "g1", "group_name": "HighKage", "is_active": True}]

    def auto_close_followups_matching_lead(self, lead):
        return []


def _ingest(message):
    idb = IngestDB()
    ots = mock.MagicMock()
    ots.return_value.encrypt_phone.return_value = None
    ots.return_value.last_error = "not configured"
    with mock.patch.object(lead_ingest, "resolve_api_lead_user_sync",
                           lambda _db, _u: (7, "tristatetag", None)), \
            mock.patch.object(lead_ingest, "OneTimeSecret", ots), \
            mock.patch("utils.ledger.is_configured", lambda: False):
        result, errors = lead_ingest.ingest_external_lead(idb, message=message)
    return idb, result, errors


class IngestWritesTheFlagTest(unittest.TestCase):

    def test_a_paid_order_is_written_armed(self):
        idb, result, errors = _ingest(PAID_MESSAGE)
        self.assertEqual([], errors)
        self.assertIsNotNone(result)
        self.assertIs(True, idb.payloads[0]["wants_insurance"])

    def test_a_tag_only_order_is_written_unarmed(self):
        idb, _, errors = _ingest(TAG_ONLY_MESSAGE)
        self.assertEqual([], errors)
        self.assertIs(False, idb.payloads[0]["wants_insurance"])

    def test_the_database_layer_keeps_the_key(self):
        """An unknown key would be silently dropped on a retry; this one is a
        known optional column, so it is only ever dropped if it is missing."""
        self.assertIn("wants_insurance", udb._OPTIONAL_LEADS_WRITE_KEYS)

    def test_a_paid_order_straight_from_ingest_prints_our_cover(self):
        idb, _, _ = _ingest(PAID_MESSAGE)
        row = dict(idb.payloads[0], id=LEAD_ID, plate="T1", tag_control_number="C1")
        fields, _ = _fields(row)
        self.assertEqual(bot.TAG_INSURER_NAME, fields["insurance_company"])
        self.assertRegex(fields["policy"], r"^ABP63\d{8}$")


# ------------------------------------------------- a website order, tag only

class WebsiteTagOnlyIsNotArmedTest(_NoticeCase):

    def test_the_tag_does_not_print_our_cover(self):
        lead = _website()
        fields, db = _fields(lead)
        self.assertNotEqual(bot.TAG_INSURER_NAME, fields["insurance_company"])
        self.assertEqual("", fields["insurance_company"])
        self.assertEqual("", fields["policy"])
        self.assertFalse(_writes(db, "insurance_card_policy_number"),
                         "no policy may be minted for a tag-only sale")
        self.assertFalse(_writes(db, "wants_insurance"))
        self.assertFalse(lead.get("wants_insurance"))
        self.assertNotIn("insurance_card_policy_number", lead)

    def test_arm_refuses(self):
        lead = _website()
        db = mock.MagicMock()
        with mock.patch.object(bot, "db", db):
            asyncio.run(bot._arm_insurance_for_lead(lead))
        db.update_lead.assert_not_called()
        self.assertFalse(lead.get("wants_insurance"))

    def test_skip_dispatch_does_not_arm(self):
        rdb, delivered = _skip_dispatch(_website())
        self.assertFalse(delivered)
        self.assertEqual([], rdb.armed_writes())
        self.assertFalse(rdb.lead.get("wants_insurance"))

    def test_exactly_one_notice_across_every_path(self):
        lead = _website()
        ctx = mock.MagicMock()
        # Dispatch poll, then Skip Dispatch, then the helper again directly.
        run_dispatch_poll(lead)
        _skip_dispatch(lead)
        self.assertFalse(asyncio.run(
            bot._tell_supervisors_website_lead_uninsured(ctx, lead)))
        self.assertEqual(1, self.tell.await_count)
        text = self.tell.await_args.args[1]
        self.assertIn("WEBTAG01", text)
        self.assertIn("ab12cd34", text)
        self.assertIn("$100", text)
        self.assertIn("tag only", text)
        self.assertIn("Paid $150", text)

    def test_the_notice_is_sent_at_dispatch_before_any_accept(self):
        run_dispatch_poll(_website())
        self.assertEqual(1, self.tell.await_count)

    def test_their_own_insurer_needs_no_notice(self):
        lead = _website(vehicle_details=VD_HAS_GEICO)
        fields, db = _fields(lead)
        self.assertEqual("Geico", fields["insurance_company"])
        self.assertEqual("POL-9", fields["policy"])
        self.assertFalse(asyncio.run(
            bot._tell_supervisors_website_lead_uninsured(mock.MagicMock(), lead)))
        _skip_dispatch(lead)
        self.tell.assert_not_awaited()


def run_dispatch_poll(lead):
    """process_pending_api_lead_dispatches for one website lead, everything
    around the notice mocked out."""
    group = {"id": "g1", "group_name": "HighKage", "is_active": True,
             "group_telegram_id": "-100123"}
    db = mock.MagicMock()
    db.list_leads_pending_ingest_dispatch.return_value = [dict(lead)]
    db.get_all_groups.return_value = [group]
    db.get_group_by_id.return_value = group
    db.get_group_lead_offers.return_value = [{"group_id": "g1"}]
    ctx = mock.MagicMock()
    with mock.patch.object(bot, "db", db), \
            mock.patch.object(bot, "_post_lead_to_all_groups_for_approval",
                              mock.AsyncMock()), \
            mock.patch.object(bot, "_send_web_order_supervisory_notice",
                              mock.AsyncMock()), \
            mock.patch.object(bot, "_reaches_drivers_at_once", lambda _l: False):
        asyncio.run(bot.process_pending_api_lead_dispatches(ctx))
    return db


# --------------------------------------------- a website order that paid

class WebsitePaidIsArmedTest(_NoticeCase):

    def test_the_tag_prints_our_cover(self):
        fields, _ = _fields(_website(wants_insurance=True))
        self.assertEqual(bot.TAG_INSURER_NAME, fields["insurance_company"])
        self.assertRegex(fields["policy"], r"^ABP63\d{8}$")

    def test_no_notice_and_no_rewrite(self):
        lead = _website(wants_insurance=True)
        run_dispatch_poll(lead)
        rdb, _ = _skip_dispatch(lead)
        self.assertEqual([], rdb.armed_writes())
        self.assertIs(True, rdb.lead["wants_insurance"])
        self.tell.assert_not_awaited()


# ------------------------------------- an issuer lead: exactly as before

class IssuerLeadIsArmedAsBeforeTest(_NoticeCase):

    def test_the_tag_prints_our_cover_and_arms(self):
        lead = _lead()
        self.assertNotIn("external_order_id", lead)
        fields, db = _fields(lead)
        self.assertEqual(bot.TAG_INSURER_NAME, fields["insurance_company"])
        self.assertRegex(fields["policy"], r"^ABP63\d{8}$")
        self.assertTrue(lead.get("wants_insurance"))
        self.assertEqual([{"wants_insurance": True}], _writes(db, "wants_insurance"))

    def test_arm_writes(self):
        lead = _lead(external_order_id=None)
        db = mock.MagicMock()
        with mock.patch.object(bot, "db", db):
            asyncio.run(bot._arm_insurance_for_lead(lead))
        db.update_lead.assert_called_once_with(LEAD_ID, {"wants_insurance": True})
        self.assertTrue(lead["wants_insurance"])

    def test_a_blank_order_id_is_not_a_website_lead(self):
        lead = _lead(external_order_id="   ")
        _fields(lead)
        self.assertTrue(lead.get("wants_insurance"))

    def test_skip_dispatch_arms(self):
        rdb, _ = _skip_dispatch(_lead())
        self.assertEqual([{"wants_insurance": True}], rdb.armed_writes())
        self.assertIs(True, rdb.lead["wants_insurance"])
        self.tell.assert_not_awaited()


# --------------- /form and Dispatch Web leads: an order id, but no checkout

class NonCheckoutOrderIdLeadsAreArmedAsBeforeTest(_NoticeCase):
    """public_form.py and dispatch_web/newlead.py write their own reference_id
    into external_order_id so the group-accept handler fans them out to
    drivers. Neither is a paid tristatetags.com checkout, so neither may lose
    the automatic cover or draw the 'paid for the tag only' notice."""

    SOURCES = ("Client Form", "Dispatch Web", " dispatch web ")

    def _row(self, source):
        return _lead(external_order_id="WEBTAG01", contact_info_source=source)

    def test_the_tag_prints_our_cover_and_arms(self):
        for source in self.SOURCES:
            with self.subTest(source=source):
                lead = self._row(source)
                fields, db = _fields(lead)
                self.assertEqual(bot.TAG_INSURER_NAME, fields["insurance_company"])
                self.assertRegex(fields["policy"], r"^ABP63\d{8}$")
                self.assertTrue(lead.get("wants_insurance"))
                self.assertEqual([{"wants_insurance": True}],
                                 _writes(db, "wants_insurance"))

    def test_skip_dispatch_arms_and_nobody_is_told(self):
        for source in self.SOURCES:
            with self.subTest(source=source):
                rdb, _ = _skip_dispatch(self._row(source))
                self.assertEqual([{"wants_insurance": True}], rdb.armed_writes())
        run_dispatch_poll(self._row("Dispatch Web"))
        self.tell.assert_not_awaited()

    def test_a_checkout_order_or_an_unknown_label_is_still_gated(self):
        for source in ("External API", "Some Custom Label", None, ""):
            with self.subTest(source=source):
                lead = _website(contact_info_source=source)
                fields, db = _fields(lead)
                self.assertEqual("", fields["policy"])
                self.assertFalse(_writes(db, "wants_insurance"))
                self.assertFalse(lead.get("wants_insurance"))


# ------------------------- an already-armed website lead (1K3AX0XS shape)

class AlreadyArmedWebsiteLeadKeepsItsCoverTest(_NoticeCase):
    """Order f85e6052 / lead 1K3AX0XS: paid $150, already armed with a policy
    number. The owner collects the $100 personally; nothing here may change it."""

    def _armed(self):
        return _website(reference_id="1K3AX0XS", external_order_id="f85e6052",
                        wants_insurance=True,
                        insurance_card_policy_number="ABP6321786484")

    def test_the_tag_keeps_the_existing_policy(self):
        lead = self._armed()
        fields, db = _fields(lead)
        self.assertEqual(bot.TAG_INSURER_NAME, fields["insurance_company"])
        self.assertEqual("ABP6321786484", fields["policy"])
        self.assertFalse(_writes(db, "insurance_card_policy_number"))
        self.assertFalse(_writes(db, "wants_insurance"))
        self.assertIs(True, lead["wants_insurance"])
        self.assertEqual("ABP6321786484", lead["insurance_card_policy_number"])

    def test_nothing_un_arms_it(self):
        lead = self._armed()
        db = mock.MagicMock()
        with mock.patch.object(bot, "db", db):
            asyncio.run(bot._arm_insurance_for_lead(lead))
        db.update_lead.assert_not_called()
        rdb, _ = _skip_dispatch(lead)
        self.assertEqual([], rdb.updates)
        self.assertIs(True, rdb.lead["wants_insurance"])
        self.assertEqual("ABP6321786484", rdb.lead["insurance_card_policy_number"])
        self.tell.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
