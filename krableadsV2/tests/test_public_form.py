r"""tristatetags.com/form — the page a client fills in themselves.

No login, no payment, no price. On submit the lead must reach every dispatcher
group AND every active driver, and it must be the same row shape every other
lead source creates, or the bot's cards render blanks.

It is also the only endpoint in this codebase a stranger can POST to, so the
refusals matter as much as the happy path.

Run:  venv\Scripts\python.exe -m pytest tests/test_public_form.py -q
"""
import os
import re
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy-key")
os.environ.setdefault("PUBLIC_FORM_SECRET", "test-secret-for-the-form-nonce")

import public_form                                            # noqa: E402

from flask import Flask                                       # noqa: E402


GOOD = {
    "first_name": "Magnolia",
    "last_name": "Diaz",
    "phone": "8454239476",
    "email": "magnolia@example.com",
    "address": "3125 Park Ave",
    "city_state_zip": "Bronx, NY 10451",
    "delivery_address": "12 Mulberry St",
    "delivery_city_state_zip": "Newark, NJ 07102",
    "vin": "JTLKT324364094480",
    "car": "2006 Scion xB",
    "color": "Grey",
    "insurance_company": "Geico",
    "insurance_policy_number": "POL-9",
    "extra_info": "Tomorrow after 5pm",
}


class FakeDB:
    """Just enough to accept one lead."""

    def __init__(self, groups=None, fail=False):
        self.created = []
        self._groups = groups if groups is not None else [
            {"id": "g1", "group_name": "HighKage", "is_active": True,
             "group_telegram_id": "-100123"}]
        self._fail = fail

    def get_all_groups(self):
        return list(self._groups)

    def create_lead(self, payload):
        if self._fail:
            raise RuntimeError("supabase down")
        self.created.append(payload)
        return dict(payload, id="lead-1")


def _app(db=None):
    app = Flask(__name__)
    app.config["TESTING"] = True
    db = db or FakeDB()
    with mock.patch.object(public_form, "OneTimeSecret") as ots:
        ots.return_value.encrypt_phone.return_value = {
            "secret_key": "sk", "metadata_key": "mk",
            "link": "https://ots.example/x"}
        public_form.register(app, lambda: db)
    return app, db


def _nonce(html: str) -> str:
    m = re.search(r'name="form_nonce" value="([^"]+)"', html)
    assert m, "the form did not carry a nonce"
    return m.group(1)


def _post(client, db, over=None, *, nonce=None, aged=True):
    page = client.get("/form").get_data(as_text=True)
    data = dict(GOOD)
    data.update(over or {})
    data["form_nonce"] = nonce or _nonce(page)
    if aged:
        # The form refuses anything filled in faster than a person could.
        data["form_nonce"] = public_form._mint_nonce().split(".")[0]
        ts = str(int(time.time()) - 30)
        import hashlib, hmac
        sig = hmac.new(public_form._secret(), ts.encode(), hashlib.sha256).hexdigest()[:32]
        data["form_nonce"] = f"{ts}.{sig}"
    with mock.patch.object(public_form, "OneTimeSecret") as ots:
        ots.return_value.encrypt_phone.return_value = {
            "secret_key": "sk", "metadata_key": "mk", "link": "https://ots.example/x"}
        return client.post("/form", data=data, follow_redirects=False)


class FormTestCase(unittest.TestCase):
    """Every test starts with an empty rate-limit book.

    _RECENT is module state, so without this the sixth successful submission in
    a class 429s and the failure lands on whichever test happened to be sixth.
    """

    def setUp(self):
        public_form._RECENT.clear()
        self.app, self.db = _app()
        self.client = self.app.test_client()

    def tearDown(self):
        public_form._RECENT.clear()


class ThePageIsServedTest(FormTestCase):


    def test_the_form_is_public(self):
        r = self.client.get("/form")
        self.assertEqual(200, r.status_code)

    def test_every_field_the_office_asked_for_is_on_it(self):
        html = self.client.get("/form").get_data(as_text=True)
        for name, _label, _req in public_form.FIELDS:
            with self.subTest(field=name):
                self.assertIn(f'name="{name}"', html)

    def test_the_insurance_opt_in_is_there_and_sits_before_the_two_boxes(self):
        html = self.client.get("/form").get_data(as_text=True)
        self.assertIn('name="wants_insurance"', html)
        self.assertIn("Opt in for insurance", html)
        self.assertLess(html.index('name="wants_insurance"'),
                        html.index('id="insurance_company"'),
                        "the opt-in must come before the insurer boxes")

    def test_the_opt_in_disables_the_two_boxes(self):
        html = self.client.get("/form").get_data(as_text=True)
        self.assertIn("data-insurance", html)
        self.assertIn("disabled = box.checked", html)

    def test_the_required_fields_are_marked_required(self):
        html = self.client.get("/form").get_data(as_text=True)
        for name, _label, required in public_form.FIELDS:
            if not required:
                continue
            block = html[html.index(f'id="{name}"'):]
            with self.subTest(field=name):
                self.assertIn("required", block[:400])

    def test_the_page_is_not_indexed(self):
        """A form for named clients has no business in search results."""
        self.assertIn('name="robots" content="noindex"',
                      self.client.get("/form").get_data(as_text=True))


class AGoodSubmissionBecomesALeadTest(FormTestCase):


    def test_it_creates_exactly_one_lead_and_thanks_the_client(self):
        r = _post(self.client, self.db)
        self.assertEqual(303, r.status_code)
        self.assertIn("/form/thanks", r.headers["Location"])
        self.assertEqual(1, len(self.db.created))

    def test_the_client_is_given_their_reference(self):
        r = _post(self.client, self.db)
        ref = r.headers["Location"].split("ref=")[-1]
        self.assertEqual(self.db.created[0]["reference_id"], ref)
        page = self.client.get(f"/form/thanks?ref={ref}").get_data(as_text=True)
        self.assertIn(ref, page)

    def test_the_name_is_the_two_name_fields_joined(self):
        _post(self.client, self.db)
        self.assertIn("Magnolia Diaz",
                      self.db.created[0]["vehicle_details"].splitlines()[0])

    def test_the_blob_carries_every_line_the_bot_reads(self):
        _post(self.client, self.db)
        lines = self.db.created[0]["vehicle_details"].splitlines()
        self.assertGreaterEqual(len(lines), 11, lines)
        self.assertEqual("JTLKT324364094480", lines[5])
        self.assertEqual("2006 Scion xB", lines[6])
        self.assertEqual("Grey", lines[7])

    def test_no_price_is_invented(self):
        """This form takes no payment, so it must not put a number on the lead."""
        _post(self.client, self.db)
        self.assertEqual("", self.db.created[0]["price"])

    def test_the_phone_is_wrapped_not_stored_raw(self):
        _post(self.client, self.db)
        self.assertEqual("https://ots.example/x", self.db.created[0]["encrypted_link"])

    def test_the_bot_will_pick_it_up(self):
        _post(self.client, self.db)
        row = self.db.created[0]
        self.assertTrue(row["ingest_dispatch_pending"])
        self.assertTrue(row["external_order_id"],
                        "a falsy external_order_id never reaches the drivers")

    def test_it_is_marked_as_a_client_form_lead(self):
        """This is what tells the bot to fan out to drivers immediately."""
        _post(self.client, self.db)
        self.assertEqual(public_form.PUBLIC_FORM_SOURCE,
                         self.db.created[0]["contact_info_source"])


class TheInsuranceOptInTest(FormTestCase):


    def test_opting_in_arms_insurance_and_clears_their_carrier(self):
        _post(self.client, self.db, {"wants_insurance": "on"})
        row = self.db.created[0]
        self.assertTrue(row["wants_insurance"])
        lines = row["vehicle_details"].splitlines()
        self.assertEqual("-", lines[8], "their insurer survived the opt-in")
        self.assertEqual("-", lines[9], "their policy survived the opt-in")

    def test_a_forged_post_cannot_keep_both(self):
        """The checkbox disables the inputs in a browser. A POST is not a
        browser, so the server clears them too."""
        _post(self.client, self.db,
              {"wants_insurance": "on",
               "insurance_company": "Geico", "insurance_policy_number": "POL-9"})
        lines = self.db.created[0]["vehicle_details"].splitlines()
        self.assertEqual("-", lines[8])
        self.assertEqual("-", lines[9])

    def test_not_opting_in_keeps_the_carrier_they_typed(self):
        _post(self.client, self.db)
        lines = self.db.created[0]["vehicle_details"].splitlines()
        self.assertEqual("Geico", lines[8])
        self.assertEqual("POL-9", lines[9])
        self.assertFalse(self.db.created[0]["wants_insurance"])


class TheRequiredFieldsAreEnforcedTest(FormTestCase):


    def test_every_starred_field_is_refused_when_blank(self):
        for name, _label, required in public_form.FIELDS:
            if not required:
                continue
            with self.subTest(field=name):
                public_form._RECENT.clear()
                app, db = _app(FakeDB())
                r = _post(app.test_client(), db, {name: ""})
                self.assertEqual(400, r.status_code)
                self.assertEqual([], db.created, f"{name} was not required")

    def test_the_optional_ones_really_are_optional(self):
        r = _post(self.client, self.db, {"insurance_company": "",
                                         "insurance_policy_number": "",
                                         "extra_info": ""})
        self.assertEqual(303, r.status_code)
        self.assertEqual(1, len(self.db.created))

    def test_a_short_vin_is_refused(self):
        r = _post(self.client, self.db, {"vin": "ABC123"})
        self.assertEqual(400, r.status_code)
        self.assertEqual([], self.db.created)

    def test_a_nonsense_phone_is_refused(self):
        r = _post(self.client, self.db, {"phone": "12"})
        self.assertEqual(400, r.status_code)
        self.assertEqual([], self.db.created)

    def test_a_nonsense_email_is_refused(self):
        r = _post(self.client, self.db, {"email": "not-an-email"})
        self.assertEqual(400, r.status_code)
        self.assertEqual([], self.db.created)

    def test_what_they_typed_survives_an_error(self):
        """Re-typing fourteen fields because one was wrong is how a client
        gives up and calls somebody else."""
        r = _post(self.client, self.db, {"vin": "ABC123"})
        html = r.get_data(as_text=True)
        self.assertIn("Magnolia", html)
        self.assertIn("3125 Park Ave", html)


class AStrangerCanPostHereTest(FormTestCase):
    """The refusals. This is the only endpoint in the codebase open to anyone."""

    def setUp(self):
        self.app, self.db = _app()
        self.client = self.app.test_client()

    def test_the_honeypot_swallows_a_bot_without_telling_it(self):
        r = _post(self.client, self.db, {"website": "http://spam.example"})
        self.assertEqual(303, r.status_code, "a bot must not learn it was caught")
        self.assertEqual([], self.db.created)

    def test_a_submission_with_no_nonce_is_refused(self):
        page = self.client.get("/form").get_data(as_text=True)   # noqa: F841
        data = dict(GOOD, form_nonce="")
        r = self.client.post("/form", data=data)
        self.assertEqual(400, r.status_code)
        self.assertEqual([], self.db.created)

    def test_a_forged_nonce_is_refused(self):
        r = self.client.post("/form", data=dict(GOOD, form_nonce="9999999999.deadbeef"))
        self.assertEqual(400, r.status_code)
        self.assertEqual([], self.db.created)

    def test_an_expired_nonce_is_refused(self):
        import hashlib, hmac
        ts = str(int(time.time()) - public_form._NONCE_TTL_S - 60)
        sig = hmac.new(public_form._secret(), ts.encode(), hashlib.sha256).hexdigest()[:32]
        r = self.client.post("/form", data=dict(GOOD, form_nonce=f"{ts}.{sig}"))
        self.assertEqual(400, r.status_code)
        self.assertEqual([], self.db.created)

    def test_something_filled_in_instantly_is_not_a_person(self):
        page = self.client.get("/form").get_data(as_text=True)
        r = self.client.post("/form", data=dict(GOOD, form_nonce=_nonce(page)))
        self.assertEqual(303, r.status_code)
        self.assertEqual([], self.db.created)

    def test_a_flood_from_one_address_is_stopped(self):
        for _ in range(public_form._RATE_MAX):
            _post(self.client, self.db)
        r = _post(self.client, self.db)
        self.assertEqual(429, r.status_code)
        self.assertEqual(public_form._RATE_MAX, len(self.db.created))

    def test_an_enormous_value_is_cut_down(self):
        r = _post(self.client, self.db, {"car": "x" * 5000})
        self.assertEqual(303, r.status_code)
        self.assertLessEqual(len(self.db.created[0]["vehicle_details"].splitlines()[6]),
                             public_form._MAX_LEN)

    def test_newlines_cannot_forge_extra_blob_lines(self):
        """The vehicle blob is newline-delimited, so a newline in a value would
        shift every line the bot reads after it."""
        _post(self.client, self.db, {"car": "2006 Scion\nFORGED\nLINES"})
        lines = self.db.created[0]["vehicle_details"].splitlines()
        self.assertNotIn("FORGED", lines)
        self.assertEqual("Grey", lines[7], "the colour line moved")


class WhenSomethingIsWrongOurSideTest(unittest.TestCase):

    def setUp(self):
        public_form._RECENT.clear()

    def tearDown(self):
        public_form._RECENT.clear()

    def test_no_dispatchers_means_an_honest_refusal_not_a_lost_lead(self):
        app, db = _app(FakeDB(groups=[]))
        r = _post(app.test_client(), db)
        self.assertEqual(503, r.status_code)
        self.assertEqual([], db.created)
        self.assertIn("call us", r.get_data(as_text=True))

    def test_an_inactive_dispatcher_does_not_count(self):
        app, db = _app(FakeDB(groups=[{"id": "g1", "is_active": False}]))
        r = _post(app.test_client(), db)
        self.assertEqual(503, r.status_code)

    def test_a_database_failure_does_not_show_a_stack_trace(self):
        app, db = _app(FakeDB(fail=True))
        r = _post(app.test_client(), db)
        self.assertEqual(502, r.status_code)
        body = r.get_data(as_text=True)
        self.assertNotIn("Traceback", body)
        self.assertNotIn("supabase down", body)


class TheBotAndTheFormAgreeTest(unittest.TestCase):

    def test_the_source_marker_is_the_same_string_on_both_sides(self):
        """Two literals in two files is how the driver fan-out silently stops."""
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        m = re.search(r'^CLIENT_FORM_SOURCE = "([^"]+)"', src, re.M)
        self.assertIsNotNone(m, "bot.py no longer defines CLIENT_FORM_SOURCE")
        self.assertEqual(public_form.PUBLIC_FORM_SOURCE, m.group(1))

    def test_the_poll_fans_a_client_form_lead_out_to_the_drivers(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        body = src.split("async def process_pending_api_lead_dispatches", 1)[1]
        body = body.split("\nasync def ", 1)[0]
        # The source check moved into _reaches_drivers_at_once, which now covers
        # the /form page AND a paid website order. Assert the fan-out is still
        # gated on that decision rather than on where the literal happens to sit.
        self.assertIn("_reaches_drivers_at_once", body)
        self.assertIn("_send_driver_requests_for_group", body)

    def test_a_paid_website_order_also_reaches_the_drivers(self):
        """A customer who paid on tristatetags.com is not chasing anyone either,
        so their order fans out the same way a /form submission does."""
        import bot
        website = str(getattr(bot.Config, "LEAD_INGEST_SOURCE_LABEL", "") or "External API")
        self.assertTrue(bot._reaches_drivers_at_once(
            {"contact_info_source": public_form.PUBLIC_FORM_SOURCE}))
        self.assertTrue(bot._reaches_drivers_at_once({"contact_info_source": website}))

    def test_the_default_label_is_recognised_whatever_this_process_is_set_to(self):
        """The source string is written by the ADMIN service and compared here in
        the WORKER. render.yaml declared LEAD_INGEST_SOURCE_LABEL on the admin
        only, so a custom label there and nothing here means every paid website
        order quietly stops reaching drivers -- no error, nothing logged. The
        built-in default must always count."""
        import bot
        self.assertTrue(bot._reaches_drivers_at_once(
            {"contact_info_source": "External API"}))

    def test_the_configured_label_is_recognised_too(self):
        import bot
        from unittest import mock
        with mock.patch.object(bot.Config, "LEAD_INGEST_SOURCE_LABEL",
                               "tristatetags.com", create=True):
            self.assertTrue(bot._reaches_drivers_at_once(
                {"contact_info_source": "tristatetags.com"}))
            # ...and the default still does, so the two services can disagree
            # about the label without dropping the customer's order.
            self.assertTrue(bot._reaches_drivers_at_once(
                {"contact_info_source": "External API"}))

    def test_the_match_ignores_case_and_padding(self):
        import bot
        for v in ("external api", "  External API  ", "EXTERNAL API"):
            with self.subTest(value=v):
                self.assertTrue(bot._reaches_drivers_at_once({"contact_info_source": v}))

    def test_both_services_declare_the_label(self):
        """The blueprint is where this divergence starts. If the worker stops
        declaring it, the admin can be given a custom value and nothing will
        say the fan-out has stopped."""
        blueprint = (ROOT / "render.yaml").read_text(encoding="utf-8")
        admin = blueprint.split("name: krab-issuer-admin", 1)[1].split("- type:", 1)[0]
        worker = blueprint.split("name: krab-issuer-bot", 1)[1].split("- type:", 1)[0]
        self.assertIn("LEAD_INGEST_SOURCE_LABEL", admin)
        self.assertIn("LEAD_INGEST_SOURCE_LABEL", worker,
                      "the bot worker no longer declares the label it compares against")

    def test_an_ordinary_lead_still_waits_for_a_team(self):
        import bot
        for src in ("", None, "Telegram", "Dispatch Web"):
            with self.subTest(source=src):
                self.assertFalse(bot._reaches_drivers_at_once({"contact_info_source": src}))

    def test_the_groups_still_get_it_first(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        body = src.split("async def process_pending_api_lead_dispatches", 1)[1]
        body = body.split("\nasync def ", 1)[0]
        self.assertLess(body.index("_post_lead_to_all_groups_for_approval"),
                        body.index("_send_driver_requests_for_group"),
                        "the drivers must not be asked before the groups are told")


class TheSiteRoutesToItTest(unittest.TestCase):

    VERCEL = Path(r"C:\Users\tatia\Downloads\speedy-tags-main\speedy-tags-main\vercel.json")

    def test_tristatetags_com_slash_form_reaches_this_service(self):
        if not self.VERCEL.exists():
            self.skipTest("the site checkout is not on this machine")
        import json
        cfg = json.loads(self.VERCEL.read_text(encoding="utf-8"))
        rewrites = {r["source"]: r["destination"] for r in cfg.get("rewrites", [])}
        self.assertIn("/form", rewrites)
        self.assertTrue(rewrites["/form"].endswith("/form"), rewrites["/form"])
        self.assertIn("krab-issuer-admin", rewrites["/form"])

    def test_the_catch_all_still_comes_last(self):
        """Vercel takes the first match, so a rewrite added below the SPA
        fallback would never fire."""
        if not self.VERCEL.exists():
            self.skipTest("the site checkout is not on this machine")
        import json
        cfg = json.loads(self.VERCEL.read_text(encoding="utf-8"))
        sources = [r["source"] for r in cfg.get("rewrites", [])]
        self.assertLess(sources.index("/form"), sources.index("/(.*)"))


if __name__ == "__main__":
    unittest.main()
