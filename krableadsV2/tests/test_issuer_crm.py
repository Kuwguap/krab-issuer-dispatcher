r"""Every issuer gets their own CRM, and the Tags column opens the tag.

Two things the office asked for.

  * ``tristatetags.com/receipts/KINGKRAB`` — the same board, showing ONLY the
    tags that issuer raised, with their own pricing. Issuers here charge
    different money for the same work, so the shared board's totals described
    nobody: "$150 · 12 tags" is a fact about one person, not about the house.

  * The Tags column, between Client phone and Client contact, links to the tag
    the client was actually issued. It used to hold a bare "2×" on multi-car
    leads and nothing at all on ordinary ones — a column spending its width
    repeating a number already on the Car line.

The scoping is the part worth testing hard. It happens in the QUERY, not in the
browser: the board reads the newest N leads, so a filter applied afterwards
would show a quiet issuer almost none of their own work while a busy colleague
filled the window. And a handle that cannot be an issuer must return NOTHING —
a dropped filter would answer a request for one person's CRM with everybody's
customers.

Run:  venv\Scripts\python.exe -m pytest tests/test_issuer_crm.py -q
"""
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


def _client(signed_in=True):
    ad.app.config["TESTING"] = True
    c = ad.app.test_client()
    if signed_in:
        c.post("/receipts/login",
               data={"password": receipts_page._receipts_password()})
    return c


# ── A recording stand-in for PostgREST's builder ────────────────────────────

class FakeQuery:
    """Enough of the query builder to see what was asked for."""

    def __init__(self, sink, rows):
        self.sink = sink
        self.rows = rows

    def select(self, cols):
        self.sink["select"] = cols
        return self

    def or_(self, expr):
        self.sink.setdefault("or_", []).append(expr)
        return self

    def eq(self, *a):
        return self

    def is_(self, *a):
        return self

    def in_(self, *a):
        return self

    def order(self, *a, **k):
        return self

    def limit(self, n):
        self.sink["limit"] = n
        return self

    def execute(self):
        return mock.MagicMock(data=self.rows)


class FakeClient:
    def __init__(self, leads, receipt_files=None):
        self.leads = leads
        self.receipt_files = receipt_files or []
        self.seen = {}

    def table(self, name):
        if name == "leads":
            return FakeQuery(self.seen, self.leads)
        if name == "receipt_files":
            return FakeQuery({}, self.receipt_files)
        return FakeQuery({}, [])


def _db(leads, receipt_files=None):
    db = ad.AdminDatabase.__new__(ad.AdminDatabase)
    db.client = FakeClient(leads, receipt_files)
    db._check_tables_exist = lambda: True
    return db


ROWS = [
    {"id": LEAD, "reference_id": "0MK4R4BB", "price": "$150.00",
     "telegram_username": "KINGKRAB", "phone_number": "2039222968",
     "vehicle_details": "MARQUISE REID\n-\n-\n-\n-\n-\n2020 ram 1500",
     "created_at": "2026-09-05T17:36:36+00:00", "receipt_image_url": ""},
    {"id": "22222222-2222-4222-8222-222222222222", "reference_id": "REF2",
     "price": "$200", "telegram_username": "@kingkrab",
     "vehicle_details": "SECOND CLIENT", "created_at": "2026-09-04T10:00:00+00:00",
     "receipt_image_url": "https://x/y.jpg"},
    {"id": "33333333-3333-4333-8333-333333333333", "reference_id": "REF3",
     "price": "$90", "telegram_username": "otherissuer",
     "vehicle_details": "NOT THEIRS", "created_at": "2026-09-03T10:00:00+00:00",
     "receipt_image_url": ""},
]


class TheScopeIsAppliedInTheQueryTest(unittest.TestCase):
    """Not in the browser, and not after the fact."""

    def test_an_issuer_narrows_the_query(self):
        db = _db(ROWS)
        db.get_transmissions(limit=500, issuer="KINGKRAB")
        exprs = db.client.seen.get("or_") or []
        self.assertTrue(exprs, "the query was not scoped at all")
        self.assertIn("telegram_username.ilike.kingkrab", exprs[0])

    def test_it_matches_the_handle_with_and_without_the_at(self):
        """Both spellings are in the table — the bot writes what Telegram gave
        it, older rows carry the '@'."""
        db = _db(ROWS)
        db.get_transmissions(issuer="kingkrab")
        self.assertIn("telegram_username.ilike.@kingkrab", db.client.seen["or_"][0])

    def test_the_match_has_no_wildcard(self):
        """`ilike` with a % would hand @krab's tags to @krab2."""
        db = _db(ROWS)
        db.get_transmissions(issuer="krab")
        self.assertNotIn("%", db.client.seen["or_"][0])

    def test_no_issuer_means_no_scope(self):
        db = _db(ROWS)
        db.get_transmissions()
        self.assertNotIn("or_", db.client.seen)

    def test_the_limit_still_applies_to_their_rows(self):
        """The point of scoping in the query: 500 of THEIRS, not 500 of
        everybody's filtered down to a handful."""
        db = _db(ROWS)
        db.get_transmissions(limit=500, issuer="kingkrab")
        self.assertEqual(500, db.client.seen["limit"])


class AnUnusableHandleReturnsNothingTest(unittest.TestCase):
    """Dropping the filter would answer a request for one person's CRM with
    every customer on the board."""

    def test_a_handle_with_a_space_returns_no_rows(self):
        db = _db(ROWS)
        self.assertEqual([], db.get_transmissions(issuer="king krab"))

    def test_it_does_not_fall_back_to_everybody(self):
        db = _db(ROWS)
        db.get_transmissions(issuer="'; drop--")
        self.assertNotIn("select", db.client.seen,
                         "an unusable handle still ran a query")

    def test_the_normaliser_accepts_only_telegram_handles(self):
        n = ad.AdminDatabase.normalize_issuer
        self.assertEqual("kingkrab", n("KINGKRAB"))
        self.assertEqual("kingkrab", n("@KingKrab"))
        self.assertEqual("krab_99", n("  @krab_99 "))
        for bad in ("", None, "bad name", "a" * 65, "drop;--", "a/b", "x%"):
            with self.subTest(bad=bad):
                self.assertEqual("", n(bad))


class TheirOwnPricingTest(unittest.TestCase):
    """A house rate would be a number that describes nobody."""

    def setUp(self):
        self.people = _db(ROWS).get_issuer_directory()
        self.by = {p["issuer"]: p for p in self.people}

    def test_every_issuer_is_listed_once(self):
        self.assertEqual({"kingkrab", "otherissuer"}, set(self.by))

    def test_the_two_spellings_are_one_person(self):
        self.assertEqual(2, self.by["kingkrab"]["leads"])

    def test_their_takings_are_their_own(self):
        self.assertEqual(350.0, self.by["kingkrab"]["gross"])
        self.assertEqual(90.0, self.by["otherissuer"]["gross"])

    def test_the_price_book_is_read_off_their_own_tags(self):
        prices = {p["price"] for p in self.by["kingkrab"]["prices"]}
        self.assertEqual({150.0, 200.0}, prices)

    def test_receipted_money_is_counted_separately(self):
        """Billed is not collected. The second lead has a receipt, the first
        does not."""
        self.assertEqual(200.0, self.by["kingkrab"]["receipted"])
        self.assertEqual(1, self.by["kingkrab"]["with_receipt"])

    def test_an_unparseable_price_is_worth_nothing(self):
        """`price` is free text. Counting "TBD" as anything but zero silently
        moves somebody's takings."""
        self.assertEqual(0.0, ad._price_number("TBD"))
        self.assertEqual(150.0, ad._price_number("$150.00"))
        self.assertEqual(1200.5, ad._price_number("1,200.50"))


class TheCrmIsBehindTheSamePasswordTest(unittest.TestCase):

    def setUp(self):
        self.anon = _client(signed_in=False)

    def test_an_issuers_crm_is_not_public(self):
        r = self.anon.get("/receipts/KINGKRAB")
        self.assertEqual(302, r.status_code)
        self.assertIn("/receipts/login", r.headers["Location"])

    def test_the_directory_is_not_public(self):
        self.assertEqual(302, self.anon.get("/receipts/issuers").status_code)

    def test_the_tag_pdf_is_not_public(self):
        """It carries the client's name and home address."""
        self.assertEqual(302, self.anon.get(f"/receipts/tag/{LEAD}").status_code)

    def test_the_issuer_api_is_refused_as_json(self):
        r = self.anon.get("/receipts/api/issuers")
        self.assertEqual(401, r.status_code)


class TheCrmPageItselfTest(unittest.TestCase):

    def setUp(self):
        self.c = _client()

    def test_it_serves_the_board_scoped_to_that_issuer(self):
        r = self.c.get("/receipts/KINGKRAB")
        self.assertEqual(200, r.status_code)
        body = r.get_data(as_text=True)
        self.assertIn('const ISSUER = "kingkrab"', body)

    def test_the_shared_board_is_not_scoped(self):
        body = self.c.get("/receipts").get_data(as_text=True)
        self.assertIn('const ISSUER = ""', body)

    def test_a_handle_that_is_not_one_says_so(self):
        """An empty board would read as "this issuer has sold nothing", which is
        a different and much worse answer than "wrong address"."""
        r = self.c.get("/receipts/not%20a%20handle")
        self.assertEqual(404, r.status_code)
        self.assertIn("No such issuer", r.get_data(as_text=True))

    def test_the_real_endpoints_are_not_shadowed_by_the_catch_all(self):
        """/receipts/<issuer> is one segment deep, like /receipts/login."""
        for path in ("/receipts/login", "/receipts/issuers"):
            with self.subTest(path=path):
                self.assertEqual(200, self.c.get(path).status_code)

    def test_a_reserved_word_is_never_treated_as_an_issuer(self):
        self.assertIn("api", receipts_page.RESERVED_SEGMENTS)
        self.assertIn("login", receipts_page.RESERVED_SEGMENTS)
        self.assertIn("tag", receipts_page.RESERVED_SEGMENTS)


class TheTagsColumnOpensTheTagTest(unittest.TestCase):
    """The whole point of the column."""

    def setUp(self):
        self.src = (ROOT / "receipts_page.py").read_text(encoding="utf-8")

    def test_the_cell_renders_links(self):
        self.assertIn("<td>${tagCell(r)}</td>", self.src)
        self.assertIn("function tagCell(r)", self.src)

    def test_a_single_car_lead_gets_a_link_too(self):
        """It used to render an empty cell for the ordinary case."""
        cell = self.src.split("function tagCell(r)", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn('if (n === 1) return tagLink(r, 1, "Tag"', cell)

    def test_a_multi_car_lead_gets_one_link_per_car(self):
        """Each car is its own document; a single link would quietly be car 1."""
        cell = self.src.split("function tagCell(r)", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("for (let c = 1; c <= n; c++)", cell)
        # tagLink adds ?car= for every car but the first, whose tag is the
        # lead's own and needs no parameter.
        link = self.src.split("function tagLink(", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("car > 1 ?", link)
        self.assertIn("?car=", link)

    def test_the_link_points_at_the_boards_own_endpoint(self):
        self.assertIn('const TAG = "/receipts/tag/";', self.src)

    def test_the_cards_carry_the_links_as_well(self):
        card = self.src.split("function cardHtml(r, idx)", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("tagCell(r)", card)


class TheTagIsTheOneTheClientHasTest(unittest.TestCase):
    """Not a lookalike minted fresh on the way to the browser."""

    def setUp(self):
        self.body = (ROOT / "receipts_page.py").read_text(encoding="utf-8")
        self.body = self.body.split("def receipts_tag_pdf(", 1)[1]
        self.body = self.body.split("\n    @app.route", 1)[0]

    def test_it_reuses_the_bots_own_field_resolution(self):
        """dispatch_web.tagpdf reuses the stored plate and control number; a
        fresh build would put a different plate on the same client's tag."""
        self.assertIn("from dispatch_web import tagpdf", self.body)
        self.assertIn("_tag_fields_from_lead(db, lead, vehicle=car)", self.body)

    def test_a_car_number_out_of_range_is_refused(self):
        self.assertIn("if not (1 <= car <= total):", self.body)

    def test_it_is_never_cached(self):
        """A legal document with a home address on it, and a first view can mint
        and persist a plate."""
        self.assertIn('"Cache-Control": "no-store"', self.body)

    def test_a_missing_renderer_is_a_service_error_not_a_crash(self):
        self.assertIn("503", self.body)

    def test_the_lead_id_is_enough_to_replay_a_failure(self):
        """Client values must not reach the log line."""
        self.assertIn("logger.error(\"receipts board: tag build failed for %s car %s: %s\"",
                      self.body)


class TheTagRouteUsesAHandleThatCanBuildTest(unittest.TestCase):
    """The board runs on AdminDatabase, which has none of get_lead_by_id,
    update_lead or allocate_temp_plate. Handing the tag builder that handle is
    an AttributeError on the first real click, on a page that otherwise tests
    perfectly clean -- so the choice of handle is pinned here."""

    def setUp(self):
        body = (ROOT / "receipts_page.py").read_text(encoding="utf-8")
        self.body = body.split("def receipts_tag_pdf(", 1)[1].split("\n    @app.route", 1)[0]

    def test_it_asks_for_the_bots_database(self):
        self.assertIn("from dispatch_web.core import get_db", self.body)
        self.assertIn("db = _bot_db()", self.body)

    def test_it_does_not_use_the_boards_handle(self):
        self.assertNotIn("db = _resolve()", self.body)

    def test_the_board_handle_really_cannot_build_a_tag(self):
        """If AdminDatabase ever grows these, this test is the place to decide
        whether the split still earns its keep."""
        for name in ("get_lead_by_id", "update_lead", "allocate_temp_plate"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(ad.AdminDatabase, name),
                                 f"AdminDatabase now has {name} — revisit the handle split")

    def test_a_database_that_will_not_build_is_a_503(self):
        self.assertIn("bot database unavailable for tags", self.body)


class TheTagActuallyRendersTest(unittest.TestCase):
    """One real trip through the route: signed in, a lead, a PDF out."""

    def _run(self, lead, car="1"):
        c = _client()
        fake = mock.MagicMock()
        fake.get_lead_by_id.return_value = lead
        with mock.patch("dispatch_web.core.get_db", return_value=fake), \
             mock.patch("dispatch_web.tagpdf._tag_fields_from_lead",
                        return_value={"first_name": "MARQUISE", "last_name": "REID",
                                      "plate": "559779V", "state": "NJ"}), \
             mock.patch("utils.tag_pdf.build_tag_pdf", return_value=b"%PDF-1.4 fake"), \
             mock.patch("dispatch_web.tagpdf._tag_filename", return_value="tag.pdf"):
            return c.get(f"/receipts/tag/{LEAD}?car={car}")

    def test_it_serves_a_pdf(self):
        r = self._run({"id": LEAD, "reference_id": "0MK4R4BB"})
        self.assertEqual(200, r.status_code, r.get_data(as_text=True)[:200])
        self.assertEqual("application/pdf", r.mimetype)
        self.assertTrue(r.get_data().startswith(b"%PDF"))

    def test_it_opens_in_the_browser_rather_than_downloading(self):
        """The point is to look at it from the board."""
        r = self._run({"id": LEAD, "reference_id": "0MK4R4BB"})
        self.assertIn("inline", r.headers["Content-Disposition"])

    def test_it_is_never_cached(self):
        r = self._run({"id": LEAD, "reference_id": "0MK4R4BB"})
        self.assertEqual("no-store", r.headers["Cache-Control"])

    def test_a_second_car_is_asked_for_by_number(self):
        lead = {"id": LEAD, "reference_id": "0MK4R4BB",
                "extra_vehicles": [{"car": "2019 CIVIC", "vin": "X" * 17}]}
        self.assertEqual(200, self._run(lead, car="2").status_code)

    def test_a_car_this_lead_does_not_have_is_a_404(self):
        r = self._run({"id": LEAD, "reference_id": "0MK4R4BB"}, car="3")
        self.assertEqual(404, r.status_code)
        self.assertIn("car", (r.get_json() or {}).get("error", ""))

    def test_a_missing_lead_is_a_404(self):
        r = self._run(None)
        self.assertEqual(404, r.status_code)

    def test_a_car_that_is_not_a_number_is_refused(self):
        c = _client()
        self.assertEqual(400, c.get(f"/receipts/tag/{LEAD}?car=one").status_code)


if __name__ == "__main__":
    unittest.main()
