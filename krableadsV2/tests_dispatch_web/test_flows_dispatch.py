"""End-to-end flows through the dispatch_web Flask blueprint, on a fake DB.

Import-order rules (same as the wiring test): SENTRY_DSN is blanked and
utils.database.Database is replaced with a MagicMock BEFORE dispatch_web is
imported. The class patch is load-bearing across FILES, not just here —
whichever suite module imports dispatch_web first is the one whose
utils.database.Database core binds forever, so every suite in this directory
must patch before importing or it poisons the others' process-shared state.
On top of that, each test primes core's module cache with its own fake and
booby-traps core.Database, so no code path constructs anything real.

The FakeDB returns the shapes utils/database.py really produces:
list_recent_leads_for_review -> (rows, total); get_lead_counts_by_sender ->
[(name, count)]; create_lead(dict) -> inserted row dict; get_setting -> str;
set_setting/set_lead_excluded/update_lead -> bool; client.table(...) -> a
query whose every builder method chains and whose execute() answers .data.
"""
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

# Repo root on sys.path no matter how pytest was invoked (plain `pytest` from
# anywhere does not add it; `python -m pytest` only adds the cwd).
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Before ANY project import — same guarantee tests/conftest.py gives that suite.
os.environ["SENTRY_DSN"] = ""
# config.py's load_dotenv never overrides existing env: setdefault BEFORE the
# first repo import beats a dev box's real .env and fills the gap on bare CI,
# so even a bug that constructed a real client would dial a dummy host.
os.environ.setdefault("SUPABASE_URL", "https://flowtest-dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "flowtest-dummy-supabase-key")

import utils.database  # noqa: E402

utils.database.Database = MagicMock(name="DatabaseClassMock(flows)")

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

# In a full-directory run pytest collects this file FIRST (alphabetical), so
# the wiring file's fresh-import laziness check never arms there — this
# module's snapshot is the one that actually guards `pytest tests_dispatch_web/`.
_FRESH_IMPORT = "dispatch_web" not in sys.modules

import dispatch_web  # noqa: E402
from dispatch_web import core  # noqa: E402
from utils import external_lead_parser  # noqa: E402

_DB_CALLS_AT_IMPORT = utils.database.Database.call_count

PASSWORD = "flow-test-password"

LEAD_ID = "a3d9c0de-77aa-4b1c-9e21-5c0ffee0c0de"

# An 11-line vehicle_details blob exactly as the bot writes it (positional,
# "-" for missing) — line 0 is the client name the board must show.
SEEDED_VEHICLE_DETAILS = "\n".join(
    [
        "DANA WHITLOCK",
        "12 MAIN ST",
        "NEWARK, NJ 07102",
        "44 OCEAN AVE",
        "JERSEY CITY, NJ 07305",
        "1FTFW1E50MFA10001",
        "2021 FORD F-150",
        "WHITE",
        "PROGRESSIVE",
        "POL-778812",
        "-",
    ]
)

SEEDED_LEAD = {
    "id": LEAD_ID,
    "reference_id": "K7Q2M9ZX",
    "telegram_name": "Marcus Webb",
    "telegram_username": "marcusw",
    "user_id": 555001,
    "vehicle_details": SEEDED_VEHICLE_DETAILS,
    "created_at": "2026-08-27T14:30:00+00:00",
    "exclude_from_count": False,
}


class _FakeQuery:
    """Stand-in for a supabase query builder: any chain of builder methods
    (.select().eq().in_().order().range().limit()...) returns itself — even
    through the real builder's property-style hops like .not_.is_(...) — and
    execute() answers the canned result, so a view can compose whatever query
    it likes without the fake needing to know the method names."""

    class _Chain:
        """Callable AND attribute-traversable: q.eq(...) chains back to the
        query, and so does q.not_.is_(...), where .not_ is dereferenced as a
        property before .is_ is looked up on what it returned."""

        def __init__(self, query):
            self._query = query

        def __call__(self, *_args, **_kwargs):
            return self._query

        def __getattr__(self, name):
            return getattr(self._query, name)

    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result

    def __getattr__(self, _name):
        return _FakeQuery._Chain(self)


def _refuse_real_database(*_args, **_kwargs):
    raise AssertionError(
        "dispatch_web tried to construct the real Database during a test — "
        "the module cache should have been primed with the fake"
    )


def _make_fake_db():
    db = MagicMock(name="FakeDatabase")

    # Board / lead page reads — real signatures from utils/database.py.
    db.list_recent_leads_for_review.return_value = ([dict(SEEDED_LEAD)], 1)
    db.get_lead_by_id.return_value = dict(SEEDED_LEAD)
    db.get_lead_counts_by_sender.return_value = [
        ("Alice Chen", 12),
        ("Marcus Webb", 8),
        ("Dee Ford", 5),
        ("Ray Ortiz", 2),
    ]

    # Writes report success unless a test flips them.
    db.set_lead_excluded.return_value = True
    db.update_lead.return_value = True
    db.set_setting.return_value = True
    db.get_setting.return_value = "0"

    # First group is inactive on purpose: record_is_active must filter it and
    # the dispatch group picker must land on the active one.
    db.get_all_groups.return_value = [
        {"id": "grp-idle", "group_name": "Team Idle", "is_active": False},
        {"id": "grp-alpha", "group_name": "Team Alpha", "is_active": True},
    ]
    db.get_group_by_id.return_value = {"id": "grp-alpha", "group_name": "Team Alpha"}
    # Default: no team has accepted (a bare MagicMock would answer a TRUTHY
    # auto-mock and silently flip every awaiting-accept derivation).
    db.get_accepted_group_for_lead.return_value = None

    db.create_lead.return_value = {"id": "lead-new-001"}

    db.get_plate_settings.return_value = {
        "nj_plate_prefix": "H",
        "non_nj_plate_suffix": "V",
        "plate_digits": 6,
        "nj_plate_next_number": 209861,
        "non_nj_plate_next_number": 100244,
        "nj_car_next_number": 3121,
        "non_nj_car_next_number": 887,
    }

    # Raw client.table(...) access (the board's price merge, driver lookups).
    tables = {
        "leads": [{"id": LEAD_ID, "price": "$150.00"}],
    }
    db.client.table.side_effect = lambda name: _FakeQuery(
        SimpleNamespace(data=list(tables.get(name, [])), count=None)
    )
    return db


@pytest.fixture()
def db(monkeypatch):
    fake = _make_fake_db()
    monkeypatch.setattr(core, "_db", fake)  # prime get_db()'s module cache
    monkeypatch.setattr(core, "Database", _refuse_real_database)
    return fake


@pytest.fixture()
def app(monkeypatch):
    monkeypatch.setenv("DISPATCH_WEB_PASSWORD", PASSWORD)
    monkeypatch.setenv("DISPATCH_WEB_SECRET", "flow-test-secret")
    application = Flask("dispatch_web_flow_test")
    application.config["TESTING"] = True
    dispatch_web.register(application)
    return application


@pytest.fixture()
def client(app, db):
    # db first-class here: no request may run before the fake is in the cache.
    return app.test_client()


@pytest.fixture()
def authed(client):
    resp = client.post("/dispatch/login", data={"password": PASSWORD})
    assert resp.status_code == 302
    return client


class _FailingOTS:
    """Offline stand-in shaped like utils.onetimesecret.OneTimeSecret when the
    service is unconfigured/down: encrypt_phone answers None, last_error set."""

    last_error = "Encryption service not configured (flow test)"

    def encrypt_phone(self, _phone):
        return None


class _WrappingOTS:
    """The service-up shape: the dict OneTimeSecret.encrypt_phone returns."""

    last_error = ""

    def encrypt_phone(self, phone):
        # The already-normalized number is what must be wrapped.
        assert phone == "9735550142", "OTS got a non-normalized phone: %r" % phone
        return {
            "secret_key": "sk-flowtest01",
            "metadata_key": "mk-flowtest01",
            "link": "https://clientsphonenumber.com/secret/sk-flowtest01",
        }


@pytest.fixture(autouse=True)
def _no_network_ots(monkeypatch):
    """POST /new wraps the client phone via OneTimeSecret exactly like HTTP
    ingest (utils/lead_ingest.py) — on a dev box whose .env holds real OTS
    creds that would be a LIVE 15s-timeout call per submitted form. Every test
    defaults to the deterministic failure path (raw-phone fallback); the
    success-path test swaps in _WrappingOTS itself."""
    from dispatch_web import newlead

    monkeypatch.setattr(newlead, "OneTimeSecret", _FailingOTS)


# ---------------------------------------------------------------- auth


def test_import_did_not_construct_database():
    """The snapshot above is THIS suite's laziness guard: in a full-directory
    run this file is collected first, so the wiring file's fresh-import check
    never arms — without this assertion the contract is unpinned."""
    if not _FRESH_IMPORT:
        pytest.skip("dispatch_web was already imported by an earlier test module")
    assert _DB_CALLS_AT_IMPORT == 0, (
        "Database() was constructed during `import dispatch_web` (%d call(s)); "
        "the contract says construct lazily inside get_db()" % _DB_CALLS_AT_IMPORT
    )


def test_blueprint_fails_closed_without_password(client, monkeypatch):
    monkeypatch.delenv("DISPATCH_WEB_PASSWORD", raising=False)
    resp = client.get("/dispatch/")
    assert resp.status_code == 503
    assert "set DISPATCH_WEB_PASSWORD" in resp.get_data(as_text=True)
    # Even the login page is behind the 503 — the whole blueprint fails closed…
    assert client.get("/dispatch/login").status_code == 503
    # …except the liveness probe, which must stay visible to monitoring.
    health = client.get("/dispatch/health")
    assert health.status_code == 200
    assert health.get_json()["ok"] is True


def test_login_rejects_wrong_password(client):
    resp = client.post("/dispatch/login", data={"password": "not-the-password"})
    assert resp.status_code == 401
    assert "Wrong password." in resp.get_data(as_text=True)
    # The failed attempt granted nothing: protected pages still bounce to login.
    board = client.get("/dispatch/")
    assert board.status_code == 302
    assert board.headers["Location"].startswith("/dispatch/login")


def test_login_accepts_password_and_opens_board(client):
    resp = client.post("/dispatch/login", data={"password": PASSWORD})
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/dispatch/"
    assert client.get("/dispatch/").status_code == 200


def test_login_next_never_leaves_dispatch(app, db):
    """_safe_next: an attacker-supplied ?next= must not bounce a fresh login
    off-site (or into a header-splitting/backslash path). Fresh client per
    case — a success sets the session cookie, and an already-authed POST
    /login short-circuits before _safe_next would even run."""
    for evil in ("https://evil.example", "//evil.example", "/admin", "/dispatch\\..\\x"):
        c = app.test_client()
        resp = c.post(
            "/dispatch/login", query_string={"next": evil}, data={"password": PASSWORD}
        )
        assert resp.status_code == 302
        assert resp.headers["Location"] == "/dispatch/", (
            "next=%r escaped the /dispatch origin: went to %r"
            % (evil, resp.headers["Location"])
        )
    # And the legitimate deep link (what require_login stashed) round-trips.
    c = app.test_client()
    resp = c.post(
        "/dispatch/login",
        query_string={"next": "/dispatch/leaderboard"},
        data={"password": PASSWORD},
    )
    assert resp.headers["Location"] == "/dispatch/leaderboard"


# ---------------------------------------------------------------- board


def test_board_renders_seeded_lead(authed, db):
    resp = authed.get("/dispatch/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "K7Q2M9ZX" in html  # reference id
    assert "DANA WHITLOCK" in html  # client name = blob line 0
    assert "Marcus Webb (@marcusw)" in html  # entrant, the bot's NAME (@handle) form
    assert "$150.00" in html  # price merged in via the raw table query
    # The board read the bot's own browser helper, first page from the top.
    args, _kwargs = db.list_recent_leads_for_review.call_args
    assert args[0] == 0 and args[1] >= 1


def test_board_db_exception_banners_and_never_leaks_str_e(authed, db):
    """Contract: every view catches DB errors into banners — a raise that
    escaped would hit the dashboard's global JSON errorhandler and leak
    str(e). The banner itself must not carry str(e) either."""
    db.list_recent_leads_for_review.side_effect = RuntimeError("sb-h0st-1nternal detail")

    resp = authed.get("/dispatch/")
    assert resp.status_code == 200  # banner on the page, not a 500
    html = resp.get_data(as_text=True)
    assert "reach the database" in html  # the fixed banner text
    assert "sb-h0st-1nternal" not in html  # exception text stays out of the page

    feed = authed.get("/dispatch/data.json")
    # 503, never 200 — the transport-level failure signal every consumer sees.
    # dispatch.js now ALSO refuses ok:false envelopes and rowless tbody_html
    # fragments client-side (pinned in test_frontend_dispatch.py), but that is
    # belt-and-braces: the wire contract stays "failure is non-OK".
    assert feed.status_code == 503
    j = feed.get_json()
    assert j["ok"] is False and j["rows"] == [] and j["tbody_html"] == ""
    assert "sb-h0st-1nternal" not in feed.get_data(as_text=True)


# ------------------------------------------------- board auto-refresh contract


def test_board_arms_dispatch_js_poller_and_ships_no_poller_of_its_own(authed):
    """The board's refresh belongs to dispatch.js's shared poller
    (initAutoRefresh) and to nobody else. History being pinned: the page used
    to advertise data-refresh-url/-ms — attributes no script reads — while an
    inline fallback did the actual polling against the absolute
    '/dispatch/data.json', which resolves outside the tristatetags.com
    /backend proxy and 404s forever, guarded by a window.dwBoardPoll flag
    dispatch.js never sets. One poller now, dispatch.js's, armed by the one
    attribute it actually reads."""
    html = authed.get("/dispatch/").get_data(as_text=True)

    # The BARE arming attribute on the table (host must contain the tbody —
    # dispatch.js takes host.tBodies[0], so the attribute cannot sit on the
    # tbody itself), plus the explicit 10s period.
    assert re.search(r"<table[^>]*\sdata-autorefresh[\s>]", html), (
        "the board table must carry a bare data-autorefresh so "
        "dispatch.js initAutoRefresh arms on it"
    )
    assert 'data-autorefresh-interval="10000"' in html
    assert '<tbody id="board-tbody">' in html  # the poller's swap target

    # NO data-autorefresh-url: dispatch.js does rebase attribute-carried URLs
    # onto the public prefix now (pinned in test_frontend_dispatch.py), so an
    # absolute URL here would survive the proxy — but the bare attribute stays
    # the canonical arming form: dispatch.js derives BASE + "/data.json" from
    # location.pathname itself, leaving no path in the markup to rot.
    assert "data-autorefresh-url" not in html

    # The fictional contract and the inline fallback are gone: no attributes
    # nothing reads, no second poller, no ghost guard, no hard-coded absolute
    # feed URL anywhere in the page (base.html keeps every URL relative).
    assert "data-refresh-url" not in html
    assert "data-refresh-ms" not in html
    assert "setInterval" not in html
    assert "dwBoardPoll" not in html
    assert "/dispatch/data.json" not in html

    # The bits the shared poller updates between swaps: the total counter
    # subscribes via data-refresh-text, and the stamp makes a failing refresh
    # visible ("Refresh failed — showing last data") instead of silent.
    assert 'data-refresh-text="total"' in html
    assert "data-refresh-stamp" in html


def test_board_data_route_matches_dispatch_js_derived_url(app):
    """dispatch.js computes its feed URL itself — BASE + "/data.json", BASE
    ending at the first "/dispatch" segment of the page's own path — which is
    exactly what keeps the poll inside the tristatetags.com /backend proxy.
    That only works while the feed really lives at /dispatch/data.json;
    moving the route would kill the refresh silently, so the path IS the
    contract."""
    rules = [
        r.rule
        for r in app.url_map.iter_rules()
        if r.endpoint == "dispatch_web.board_data"
    ]
    assert rules == ["/dispatch/data.json"]


def test_board_data_empty_board_still_ships_a_tr(authed, db):
    """dispatch.js swaps tbody_html in whenever it is whitespace-only OR
    contains "<tr" — so a 200 must never carry a rowless-and-blank fragment.
    A genuinely empty board therefore still renders its placeholder <tr>
    (the "No leads yet." row from _board_rows.html); the DB-failure case
    travels as a 503 instead (asserted in the leak test above)."""
    db.list_recent_leads_for_review.return_value = ([], 0)
    feed = authed.get("/dispatch/data.json")
    assert feed.status_code == 200
    j = feed.get_json()
    assert j["ok"] is True and j["total"] == 0 and j["rows"] == []
    assert "<tr" in j["tbody_html"]
    assert "No leads yet" in j["tbody_html"]


# ---------------------------------------------------------------- /api/parse


def test_api_parse_fills_fields_from_realistic_paste(authed):
    # The parser is pure regex (utils/external_lead_parser.py) — no network,
    # so this runs it for REAL on the module's own sample paste, posted the
    # way new.html's fetch posts it: raw text/plain body.
    resp = authed.post(
        "/dispatch/api/parse",
        data=external_lead_parser.SAMPLE_MESSAGE.encode("utf-8"),
        content_type="text/plain;charset=UTF-8",
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["errors"] == []
    fields = data["fields"]
    assert fields["name"] == "Zebin Fang Fang"
    assert fields["vin"] == "SCA665C56HUX86704"
    assert fields["phone"] == "2138622301"  # normalize_phone: bare 10 digits
    assert fields["price"] == "$150.00"
    assert fields["email"] == "zebinfang1002@gmail.com"
    assert fields["external_order_id"] == "bf6923ca"
    assert fields["car"] == "2017 ROLLS-ROYCE Wraith"
    assert fields["color"] == "black"
    assert "MA 02169" in fields["city_state_zip"]
    # Every line of the 11-line blob has a grid field to land in.
    for key in (
        "name", "address", "city_state_zip", "delivery_address",
        "delivery_city_state_zip", "vin", "car", "color",
        "insurance_company", "insurance_policy_number", "extra_info",
    ):
        assert key in fields


def test_api_parse_reports_errors_for_garbage(authed):
    resp = authed.post(
        "/dispatch/api/parse", data=b"hello there", content_type="text/plain"
    )
    assert resp.status_code == 200  # always 200 JSON, never an HTML error page
    data = resp.get_json()
    assert data["ok"] is False
    assert data["errors"]  # the validation reasons, not a crash
    assert isinstance(data["fields"], dict)


def test_api_parse_refuses_oversized_body_before_parsing(authed, monkeypatch):
    """One oversized POST must not buffer into (or OOM) the process that also
    hosts the production admin backend: a body over the cap is refused as the
    contract's 200 JSON, and the paste never reaches the parser."""
    from dispatch_web import newlead

    def _must_not_run(_raw):
        raise AssertionError("oversized body reached parse_external_lead_message")

    monkeypatch.setattr(
        newlead.external_lead_parser, "parse_external_lead_message", _must_not_run
    )
    resp = authed.post(
        "/dispatch/api/parse",
        data=b"x" * (300 * 1024),  # > the 256KB cap; a real paste is < 2KB
        content_type="text/plain",
    )
    assert resp.status_code == 200  # never a 413/HTML error page
    data = resp.get_json()
    assert data["ok"] is False
    assert any("too large" in e for e in data["errors"])
    assert data["fields"] == {}


def test_api_parse_caps_read_of_length_less_stream(authed, monkeypatch):
    """A chunked-style body (no Content-Length, wsgi.input_terminated set by
    the real WSGI server) sidesteps the declared-length check — the endpoint
    must read at most the cap, not get_data()'s everything."""
    import io

    from dispatch_web import newlead

    monkeypatch.setattr(
        newlead.external_lead_parser,
        "parse_external_lead_message",
        lambda _raw: (_ for _ in ()).throw(
            AssertionError("length-less oversized body reached the parser")
        ),
    )

    class _CountingStream(io.BytesIO):
        read_total = 0

        def read(self, n=-1):
            data = super().read(n)
            _CountingStream.read_total += len(data)
            return data

    resp = authed.post(
        "/dispatch/api/parse",
        input_stream=_CountingStream(b"y" * (2 * 1024 * 1024)),
        content_type="text/plain",
        environ_overrides={"wsgi.input_terminated": True, "CONTENT_LENGTH": ""},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is False
    assert any("too large" in e for e in data["errors"])
    # The proof of bounded memory: nowhere near the 2MB was ever read.
    assert _CountingStream.read_total <= 300 * 1024, (
        "endpoint read %d bytes of a length-less stream — unbounded buffering"
        % _CountingStream.read_total
    )


# ---------------------------------------------------------------- POST /new


def _new_lead_form(**overrides):
    form = {
        "entered_by": "Web Tester",
        "name": "DANA WHITLOCK",
        "phone": "(973) 555-0142",
        "price": "150",  # no $ on purpose: the view must not bounce plain digits
        "email": "dana@example.com",
        "external_order_id": "ab12cd34",
        "address": "12 MAIN ST",
        "city_state_zip": "NEWARK, NJ 07102",
        "delivery_address": "",
        "delivery_city_state_zip": "",
        "vin": "1ftfw1e50mfa10001",  # lowercase on purpose: must be uppercased
        "car": "2021 FORD F-150",
        "color": "WHITE",
        "insurance_company": "PROGRESSIVE",
        "insurance_policy_number": "POL-778812",
        "extra_info": "Deliver after 5pm",
    }
    form.update(overrides)
    return form


def _new_form_nonce(client):
    """Render /new and pull the one-shot submit token from the form — the
    same round-trip a real dispatcher's browser makes (mirrors _settings_csrf)."""
    page = client.get("/dispatch/new")
    assert page.status_code == 200
    m = re.search(r'name="form_nonce" value="([^"]+)"', page.get_data(as_text=True))
    assert m, "new-lead page did not render a form_nonce hidden field"
    return m.group(1)


def _created_payload(db):
    args, kwargs = db.create_lead.call_args
    return args[0] if args else next(iter(kwargs.values()))


def test_new_lead_page_renders_form(authed):
    resp = authed.get("/dispatch/new")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'name="entered_by"' in html
    assert 'name="form_nonce"' in html  # the one-shot double-submit token


def test_new_lead_template_disables_submit_on_first_click(authed):
    """First defense layer against the double-click double-dispatch: the page
    JS must wire a submit listener that disables the button — without it two
    POSTs race the redirect and the bot broadcasts the client twice."""
    html = authed.get("/dispatch/new").get_data(as_text=True)
    assert "addEventListener('submit'" in html
    assert "disabled = true" in html


def test_new_lead_post_creates_bot_shaped_dispatch_pending_lead(authed, db):
    resp = authed.post(
        "/dispatch/new", data=_new_lead_form(form_nonce=_new_form_nonce(authed))
    )
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/dispatch/lead/lead-new-001"

    assert db.create_lead.call_count == 1
    args, kwargs = db.create_lead.call_args
    payload = args[0] if args else next(iter(kwargs.values()))

    # THE point of the web mirror: the running bot's ~10s poll only claims
    # rows flagged exactly like its own HTTP-ingest leads.
    assert payload["ingest_dispatch_pending"] is True
    assert payload["awaiting_group_accept"] is True

    lines = payload["vehicle_details"].split("\n")
    assert len(lines) == 11  # the positional blob every bot card is built from
    assert lines[0] == "DANA WHITLOCK"
    assert lines[5] == "1FTFW1E50MFA10001"  # VIN uppercased into slot 5
    # One address given -> it serves as both registration and delivery.
    assert lines[3] == "12 MAIN ST"
    assert lines[4] == "NEWARK, NJ 07102"
    assert payload["delivery_details"] == "12 MAIN ST\nNEWARK, NJ 07102"

    assert re.fullmatch(r"[A-Z0-9]{8}", payload["reference_id"])
    assert payload["phone_number"] == "9735550142"  # normalized bare digits
    assert payload["price"] == "$150"  # the $ the parser demands, added for free
    # encrypted_link mirrors utils/lead_ingest.py's OTS policy; this suite
    # defaults OTS to down, so the raw phone rides in the link column (the
    # ingest fallback shape) — NEVER None, a shape no other source creates.
    assert payload["encrypted_link"] == "9735550142"
    assert payload["group_id"] == "grp-alpha"  # the ACTIVE group, not the idle one
    assert payload["telegram_name"] == "Web Tester"
    assert isinstance(payload["user_id"], int)
    assert payload["email"] == "dana@example.com"
    # A typed Order # passes through verbatim (the fallback below never clobbers it).
    assert payload["external_order_id"] == "ab12cd34"


def test_new_lead_without_order_number_still_reads_as_website_lead(authed, db):
    """Order # is optional in the form, but the bot's group-accept handler
    detects 'website lead -> fan drivers out' SOLELY by external_order_id
    being truthy — a None would fall into the issuer-DM branch, a no-op for
    web entrants, and the accepted lead would silently never reach a driver.
    The payload must fall back to the generated reference_id (which the bot's
    supervisory text already prints on the Order # line when no external id
    exists, so display parity holds)."""
    resp = authed.post(
        "/dispatch/new",
        data=_new_lead_form(external_order_id="", form_nonce=_new_form_nonce(authed)),
    )
    assert resp.status_code == 302
    payload = _created_payload(db)
    assert payload["external_order_id"] == payload["reference_id"]
    assert re.fullmatch(r"[A-Z0-9]{8}", payload["external_order_id"])


def test_new_lead_wraps_phone_in_ots_link_when_service_answers(authed, db, monkeypatch):
    """The web lead must not be the ONLY source shaped encrypted_link=None:
    the bot renders `encrypted_link or raw phone` in the accepted team's
    copy-paste block (None printed an EMPTY phone line) and in the driver card
    under /driverblock (None silently handed drivers the raw digits). With OTS
    up, the one-time link plus BOTH OTS keys are stored — the bot-lead shape,
    ingest key names, raw digits nowhere in the link."""
    from dispatch_web import newlead

    monkeypatch.setattr(newlead, "OneTimeSecret", _WrappingOTS)
    resp = authed.post(
        "/dispatch/new", data=_new_lead_form(form_nonce=_new_form_nonce(authed))
    )
    assert resp.status_code == 302
    payload = _created_payload(db)
    assert payload["encrypted_link"] == "https://clientsphonenumber.com/secret/sk-flowtest01"
    assert payload["onetimesecret_token"] == "sk-flowtest01"
    assert payload["onetimesecret_secret_key"] == "mk-flowtest01"
    assert "9735550142" not in payload["encrypted_link"]  # redaction preserved
    assert payload["phone_number"] == "9735550142"  # raw number stays on its own column


def test_new_lead_ots_failure_stores_raw_phone_like_http_ingest(authed, db):
    """utils/lead_ingest.py's fallback, verbatim: OTS down -> the raw phone
    rides in encrypted_link, so the copy block still shows a number and the
    bot's _validate_lead_row_for_resend ("Missing encrypted link.") cannot
    hard-fail the lead. Never None — the shape no other lead source creates."""
    resp = authed.post(
        "/dispatch/new", data=_new_lead_form(form_nonce=_new_form_nonce(authed))
    )
    assert resp.status_code == 302
    payload = _created_payload(db)
    assert payload["encrypted_link"] == "9735550142"
    assert payload["onetimesecret_token"] is None
    assert payload["onetimesecret_secret_key"] is None


def test_new_lead_missing_vin_never_reaches_the_db(authed, db):
    resp = authed.post(
        "/dispatch/new",
        data=_new_lead_form(vin="", form_nonce=_new_form_nonce(authed)),
    )
    assert resp.status_code == 200  # re-rendered form with the errors, not a redirect
    assert "VIN" in resp.get_data(as_text=True)
    db.create_lead.assert_not_called()


# ------------------------------------------- POST /new double-submit guard


def test_new_lead_double_submit_creates_only_one_lead(authed, db):
    """The bot's ~10s poll offers every ingest_dispatch_pending row to every
    Dispatcher group — a double-click that wrote two rows would double-issue
    one client to two teams. The second POST of the SAME form (same nonce)
    must re-render a banner, keep the fields, and create nothing."""
    form = _new_lead_form(form_nonce=_new_form_nonce(authed))
    first = authed.post("/dispatch/new", data=form)
    assert first.status_code == 302
    assert db.create_lead.call_count == 1

    second = authed.post("/dispatch/new", data=form)  # the re-click / replay
    assert second.status_code == 200  # banner page, not a second redirect
    html = second.get_data(as_text=True)
    assert "already submitted" in html
    assert 'value="DANA WHITLOCK"' in html  # fields kept for a real second lead
    assert db.create_lead.call_count == 1  # THE point: still exactly one row


def test_new_lead_refuses_missing_or_forged_nonce(authed, db):
    """No token, or a token this session never minted, must never create —
    this is also the CSRF stance for the one side-effecting lead POST."""
    for extra in ({}, {"form_nonce": "forged-nonce-value"}):
        resp = authed.post("/dispatch/new", data=_new_lead_form(**extra))
        assert resp.status_code == 200
        assert "already submitted" in resp.get_data(as_text=True)
    db.create_lead.assert_not_called()


def test_new_lead_error_rerender_mints_fresh_usable_nonce(authed, db):
    """A validation bounce consumes the token but must hand back a fresh one
    with the kept fields — fixing the VIN and resubmitting has to work."""
    tok = _new_form_nonce(authed)
    resp = authed.post("/dispatch/new", data=_new_lead_form(vin="", form_nonce=tok))
    assert resp.status_code == 200
    m = re.search(r'name="form_nonce" value="([^"]+)"', resp.get_data(as_text=True))
    assert m, "error re-render lost the form_nonce hidden field"
    fresh = m.group(1)
    assert fresh != tok  # one-shot: the spent token is never re-issued

    resp = authed.post("/dispatch/new", data=_new_lead_form(form_nonce=fresh))
    assert resp.status_code == 302
    assert db.create_lead.call_count == 1


def test_new_lead_two_open_tabs_each_submit_once(authed, db):
    """Two New Lead tabs = two outstanding tokens; each submits exactly once
    (the guard is per-form, not a single per-session slot)."""
    tok1 = _new_form_nonce(authed)
    tok2 = _new_form_nonce(authed)
    assert (
        authed.post("/dispatch/new", data=_new_lead_form(form_nonce=tok1)).status_code
        == 302
    )
    assert (
        authed.post("/dispatch/new", data=_new_lead_form(form_nonce=tok2)).status_code
        == 302
    )
    assert db.create_lead.call_count == 2
    # And a replay of the first tab's spent token stays refused.
    resp = authed.post("/dispatch/new", data=_new_lead_form(form_nonce=tok1))
    assert resp.status_code == 200
    assert db.create_lead.call_count == 2


# ---------------------------------------------------------------- strike / restore


def test_strike_and_restore_call_set_lead_excluded(authed, db):
    resp = authed.post(f"/dispatch/lead/{LEAD_ID}/strike")
    assert resp.status_code == 302
    assert "ok=struck" in resp.headers["Location"]
    db.set_lead_excluded.assert_called_once_with(LEAD_ID, True)

    db.set_lead_excluded.reset_mock()
    resp = authed.post(f"/dispatch/lead/{LEAD_ID}/restore")
    assert resp.status_code == 302
    assert "ok=restored" in resp.headers["Location"]
    db.set_lead_excluded.assert_called_once_with(LEAD_ID, False)


def test_strike_write_refusal_surfaces_not_silently_passes(authed, db):
    db.set_lead_excluded.return_value = False
    resp = authed.post(f"/dispatch/lead/{LEAD_ID}/strike")
    assert resp.status_code == 302
    assert "err=write" in resp.headers["Location"]


# ---------------------------------------------------------------- lead detail


def test_lead_detail_get_db_failure_banners_and_never_leaks_str_e(authed, monkeypatch):
    """Contract: Database() CONSTRUCTION failing inside get_db() must not escape
    lead_detail — an escaped raise reaches the host dashboard's global JSON
    errorhandler, which returns str(e) (creds, hostnames) verbatim. Lead URLs
    are shareable deep links, so this page can be the FIRST db touch of a bad
    boot. TESTING=True propagates an escaped exception, so a raise fails loud."""
    from dispatch_web import leaddetail

    def _boom():
        raise RuntimeError("SUPABASE creds rejected: host=db-1nternal key=sk-l3akme")

    monkeypatch.setattr(leaddetail, "get_db", _boom)
    resp = authed.get(f"/dispatch/lead/{LEAD_ID}")
    assert resp.status_code == 404  # the lead=None banner page, not a JSON 500
    assert resp.mimetype == "text/html"
    html = resp.get_data(as_text=True)
    assert "Database unavailable" in html  # the fixed banner text
    assert "sk-l3akme" not in html and "db-1nternal" not in html


def test_lead_detail_unpaid_checkout_shows_quoted_never_charged(authed, db):
    """instant_pdf_amount_cents is stamped at checkout CREATION (with
    requested_at, before any payment) — an awaiting-payment lead must say
    Quoted, never state the money as already Charged."""
    db.get_lead_by_id.return_value = dict(
        SEEDED_LEAD,
        instant_pdf_amount_cents=10000,
        instant_pdf_requested_at="2026-08-27T15:00:00+00:00",
        instant_pdf_paid_at=None,
    )
    resp = authed.get(f"/dispatch/lead/{LEAD_ID}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "awaiting payment" in html  # the status badge tells the truth…
    assert "$100.00" in html
    assert "Quoted" in html
    assert "Charged" not in html  # …and no row contradicts it


def test_lead_detail_paid_checkout_shows_charged_not_quoted(authed, db):
    db.get_lead_by_id.return_value = dict(
        SEEDED_LEAD,
        instant_pdf_amount_cents=10000,
        instant_pdf_requested_at="2026-08-27T15:00:00+00:00",
        instant_pdf_paid_at="2026-08-27T15:05:00+00:00",
    )
    resp = authed.get(f"/dispatch/lead/{LEAD_ID}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Charged" in html
    assert "$100.00" in html
    assert "Quoted" not in html


# ------------------------------------- lead detail: awaiting-accept derivation

# The exact "Awaiting accept: yes" badge cell — nothing else on a lead page
# without ingest_dispatch_pending renders a pending badge whose text is "yes".
_AWAITING_YES = 'badge badge-pending">yes</span>'


def test_lead_detail_awaiting_accept_derives_from_accepted_offer(authed, db):
    """awaiting_group_accept is WRITE-ONLY: the web sets it at create, the bot
    at ingest claim, and NOTHING ever clears it (accept updates group_id
    only) — read raw, every web/ingest lead says "Awaiting accept: yes"
    forever. The page must consult the accepted-offer state instead: one
    group_lead_offers row per lead with status=accepted, DB-enforced."""
    db.get_lead_by_id.return_value = dict(SEEDED_LEAD, awaiting_group_accept=True)

    # No accepted offer row -> genuinely still awaiting: badge stays.
    db.get_accepted_group_for_lead.return_value = None
    html = authed.get(f"/dispatch/lead/{LEAD_ID}").get_data(as_text=True)
    assert _AWAITING_YES in html

    # A team accepted (offer row recorded; the raw flag left dangling True).
    db.get_accepted_group_for_lead.return_value = {
        "lead_id": LEAD_ID, "group_id": "grp-alpha", "status": "accepted",
    }
    html = authed.get(f"/dispatch/lead/{LEAD_ID}").get_data(as_text=True)
    assert _AWAITING_YES not in html
    assert "a team accepted" in html
    db.get_accepted_group_for_lead.assert_called_with(LEAD_ID)


def test_lead_detail_awaiting_derivation_survives_offer_lookup_failure(authed, db):
    """The offers lookup failing must degrade to the raw flag (the status-quo
    reading), never crash the page — and str(e) stays out of the HTML, per the
    banner contract."""
    db.get_lead_by_id.return_value = dict(SEEDED_LEAD, awaiting_group_accept=True)
    db.get_accepted_group_for_lead.side_effect = RuntimeError("sb-0ffer-1nternal")
    resp = authed.get(f"/dispatch/lead/{LEAD_ID}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert _AWAITING_YES in html
    assert "sb-0ffer-1nternal" not in html


def test_lead_detail_without_awaiting_flag_never_queries_offers(authed, db):
    """Bot-entered leads (and ingest leads still False) don't carry the flag —
    the derivation must cost them zero extra queries."""
    resp = authed.get(f"/dispatch/lead/{LEAD_ID}")  # SEEDED_LEAD: no flag
    assert resp.status_code == 200
    db.get_accepted_group_for_lead.assert_not_called()


def test_badges_macro_dispatching_clears_on_accepted_offer_stamp(app):
    """_macros.badges reads row data only (a macro cannot query): a view that
    renders full rows stamps lead.group_offer_accepted from
    Database.get_accepted_group_for_lead, and the write-only raw flag stops
    labeling long-accepted leads "dispatching". Rows without the stamp keep
    the raw reading; ingest_dispatch_pending stays independent (the bot
    clears that one itself at claim)."""
    from flask import render_template_string

    tmpl = '{% from "dispatch/_macros.html" import badges %}{{ badges(lead) }}'
    with app.app_context():
        still = render_template_string(tmpl, lead={"awaiting_group_accept": True})
        taken = render_template_string(
            tmpl, lead={"awaiting_group_accept": True, "group_offer_accepted": True}
        )
        queued = render_template_string(
            tmpl, lead={"ingest_dispatch_pending": True, "group_offer_accepted": True}
        )
    assert "dispatching" in still
    assert "dispatching" not in taken
    assert "dispatching" in queued


# ---------------------------------------------------------------- tag.pdf


def test_tag_pdf_reuses_stored_plate_and_names_file_after_client(authed, db, monkeypatch):
    """The contract highlight: tag PDFs identical to the bot's, CLIENT-NAME
    filename included. A lead the bot already tagged carries plate + control on
    the row — the web download must REUSE them (no re-mint, no write-back)."""
    from dispatch_web import tagpdf

    db.get_lead_by_id.return_value = dict(
        SEEDED_LEAD, plate="H209861", tag_control_number="C0912345"
    )
    # Offline seams only: the VIN decode is a live NHTSA call and the renderer
    # rasterizes a template PDF — both are the bot's own utils, not this view's
    # logic. Everything between (field resolution, filename) runs for real.
    monkeypatch.setattr(tagpdf.tag_pdf, "decode_vin_for_tag", lambda vin: None)
    captured = {}

    def _fake_build(fields):
        captured.update(fields)
        return b"%PDF-1.4 flowtest"

    monkeypatch.setattr(tagpdf.tag_pdf, "build_tag_pdf", _fake_build)

    resp = authed.get(f"/dispatch/lead/{LEAD_ID}/tag.pdf")
    assert resp.status_code == 200
    assert resp.mimetype == "application/pdf"
    assert resp.data.startswith(b"%PDF")
    # The bot's file naming, verbatim: the CLIENT's name, never the reference.
    assert 'filename="DANA_WHITLOCK.pdf"' in resp.headers["Content-Disposition"]
    # A legal document with PII: never cacheable by proxy or browser.
    assert resp.headers["Cache-Control"] == "no-store"
    # Stored identity reused byte-for-byte; NEWARK, NJ picks the NJ template.
    assert captured["plate"] == "H209861"
    assert captured["control_number"] == "C0912345"
    assert captured["is_nj"] is True
    assert captured["vin"] == "1FTFW1E50MFA10001"
    db.allocate_temp_plate.assert_not_called()
    db.update_lead.assert_not_called()


def _tag_seams(monkeypatch):
    """The two offline seams every tag.pdf test needs (live NHTSA call and the
    template rasterizer), returning the dict build_tag_pdf's fields land in."""
    from dispatch_web import tagpdf

    monkeypatch.setattr(tagpdf.tag_pdf, "decode_vin_for_tag", lambda vin: None)
    captured = {}

    def _fake_build(fields):
        captured.update(fields)
        return b"%PDF-1.4 flowtest"

    monkeypatch.setattr(tagpdf.tag_pdf, "build_tag_pdf", _fake_build)
    return captured


class _RecordingQuery(_FakeQuery):
    """_FakeQuery that also logs every builder call, so a test can pin the
    exact guard (`.is_("plate", "null")`) that makes a plate write race-safe."""

    def __init__(self, result, log):
        super().__init__(result)
        self._log = log

    def __getattr__(self, name):
        def _chain(*args, **_kwargs):
            self._log.append((name,) + args)
            return self

        return _chain


def test_tagpdf_docstring_documents_bot_car1_remint_divergence():
    """Finding pin: the module docstring used to claim the bot reuses car 1's
    stored plate — it does not (bot._phase1_from_stored_lead carries no plate
    key, so every non-renewal bot build re-mints). The corrected docstring must
    flag the divergence so the next "copy the bot's code here again" pass does
    not mechanically delete the mirror's lead-row injection and import the
    bot's re-mint bug."""
    from dispatch_web import tagpdf

    doc = tagpdf.__doc__ or ""
    assert "KNOWN BOT DIVERGENCE" in doc
    assert "re-mints car 1's plate" in doc
    # The old false claim — that the bot itself reuses these columns — is gone.
    assert "(and the bot itself)" not in doc


def test_tag_pdf_mint_claims_car1_columns_with_null_guard(authed, db, monkeypatch):
    """A plate-less lead mints — but persists through a guarded claim
    (`update ... where plate is null`), never a blind update_lead: the blind
    write raced the live bot last-writer-wins on a printed legal document."""
    db.get_lead_by_id.return_value = dict(SEEDED_LEAD)  # no plate on the row
    db.allocate_temp_plate.return_value = {"plate": "H300001", "control_number": "9912345678"}
    log = []
    # Claim WINS: the update matched the still-null row.
    db.client.table.side_effect = lambda name: _RecordingQuery(
        SimpleNamespace(data=[{"id": LEAD_ID}]), log
    )
    captured = _tag_seams(monkeypatch)

    resp = authed.get(f"/dispatch/lead/{LEAD_ID}/tag.pdf")
    assert resp.status_code == 200
    assert captured["plate"] == "H300001"
    assert captured["control_number"] == "9912345678"
    db.allocate_temp_plate.assert_called_once()
    # The write was the guarded shape, on the leads table, and nothing blind.
    db.client.table.assert_called_once_with("leads")
    assert ("update", {"plate": "H300001", "tag_control_number": "9912345678"}) in log
    assert ("eq", "id", LEAD_ID) in log
    assert ("is_", "plate", "null") in log
    db.update_lead.assert_not_called()


def test_tag_pdf_lost_race_serves_the_winners_plate(authed, db, monkeypatch):
    """The guarded claim MISSES (the live bot persisted while we sat in the
    NHTSA decode): the endpoint must re-read and print the winner's plate —
    never the local mint, which would put a second legal plate on this car."""
    db.allocate_temp_plate.return_value = {"plate": "H300002", "control_number": "9900000002"}
    db.get_lead_by_id.side_effect = [
        dict(SEEDED_LEAD),  # the view's read: still plate-less
        dict(SEEDED_LEAD, plate="H999777", tag_control_number="7770001111"),  # re-read: bot won
    ]
    log = []
    db.client.table.side_effect = lambda name: _RecordingQuery(
        SimpleNamespace(data=[]), log  # claim matched 0 rows
    )
    captured = _tag_seams(monkeypatch)

    resp = authed.get(f"/dispatch/lead/{LEAD_ID}/tag.pdf")
    assert resp.status_code == 200
    assert captured["plate"] == "H999777"  # the persisted winner, not H300002
    assert captured["control_number"] == "7770001111"
    assert 'filename="DANA_WHITLOCK.pdf"' in resp.headers["Content-Disposition"]
    db.update_lead.assert_not_called()  # the loser never blind-overwrites


def test_tag_pdf_persist_failure_still_serves_the_pdf(authed, db, monkeypatch):
    """Contract: DB trouble degrades, it never blocks the file — a claim that
    raises is logged and the locally minted plate is served anyway."""
    db.get_lead_by_id.return_value = dict(SEEDED_LEAD)
    db.allocate_temp_plate.return_value = {"plate": "H300003", "control_number": "9900000003"}
    db.client.table.side_effect = RuntimeError("sb-h0st-goaway")
    captured = _tag_seams(monkeypatch)

    resp = authed.get(f"/dispatch/lead/{LEAD_ID}/tag.pdf")
    assert resp.status_code == 200
    assert resp.mimetype == "application/pdf"
    assert captured["plate"] == "H300003"
    assert "sb-h0st-goaway" not in resp.get_data(as_text=True)


_EXTRA_CARS_UNPLATED = [
    {
        "name": "LENA OKAFOR", "address": "9 PINE ST",
        "city_state_zip": "EDISON, NJ 08817", "vin": "1HGCM82633A004352",
        "car": "2019 HONDA ACCORD", "color": "BLUE",
        "insurance_company": "GEICO", "insurance_policy_number": "GK-1",
    },
    {
        "name": "RAY OKAFOR", "address": "9 PINE ST",
        "city_state_zip": "EDISON, NJ 08817", "vin": "2HGFB2F50EH542771",
        "car": "2016 HONDA CIVIC", "color": "GREY",
        "insurance_company": "GEICO", "insurance_policy_number": "GK-2",
    },
]


def test_tag_pdf_extra_car_merge_preserves_sibling_plates(authed, db, monkeypatch):
    """extra_vehicles is ONE shared JSON array: persisting car 2's mint must
    merge into a FRESH read of the row, not rewrite the array from the stale
    pre-decode read — which would silently drop the plate a concurrent build
    (bot accept, sibling ?car=3 download) minted for car 3 meanwhile."""
    from dispatch_web import tagpdf

    stale = [dict(v) for v in _EXTRA_CARS_UNPLATED]
    fresh = [dict(v) for v in _EXTRA_CARS_UNPLATED]
    fresh[1]["plate"], fresh[1]["tag_control_number"] = "H555001", "5550001111"
    db.get_lead_by_id.side_effect = [
        dict(SEEDED_LEAD, extra_vehicles=stale),  # the view's read
        dict(SEEDED_LEAD, extra_vehicles=fresh),  # persist-time re-read: car 3 got plated
    ]
    db.allocate_temp_plate.return_value = {"plate": "H300004", "control_number": "9900000004"}
    captured = _tag_seams(monkeypatch)

    resp = authed.get(f"/dispatch/lead/{LEAD_ID}/tag.pdf?car=2")
    assert resp.status_code == 200
    assert captured["plate"] == "H300004"
    # Multi-car filename: THAT car's client name, with the car index suffix.
    assert 'filename="LENA_OKAFOR_2.pdf"' in resp.headers["Content-Disposition"]

    db.update_lead.assert_called_once()
    args, _ = db.update_lead.call_args
    assert args[0] == LEAD_ID
    written = args[1][tagpdf.EXTRA_VEHICLES_KEY]
    assert written[0]["plate"] == "H300004"  # our car landed…
    assert written[1]["plate"] == "H555001"  # …and the sibling's mint SURVIVED
    assert written[1]["tag_control_number"] == "5550001111"


def test_tag_pdf_extra_car_lost_race_serves_persisted_plate(authed, db, monkeypatch):
    """If the persist-time re-read shows THIS car already plated by a
    concurrent build, its persisted identity is printed and nothing is
    written — the mint is discarded, not the delivered plate."""
    stale = [dict(v) for v in _EXTRA_CARS_UNPLATED]
    fresh = [dict(v) for v in _EXTRA_CARS_UNPLATED]
    fresh[0]["plate"], fresh[0]["tag_control_number"] = "H424242", "4242424242"
    db.get_lead_by_id.side_effect = [
        dict(SEEDED_LEAD, extra_vehicles=stale),
        dict(SEEDED_LEAD, extra_vehicles=fresh),
    ]
    db.allocate_temp_plate.return_value = {"plate": "H300005", "control_number": "9900000005"}
    captured = _tag_seams(monkeypatch)

    resp = authed.get(f"/dispatch/lead/{LEAD_ID}/tag.pdf?car=2")
    assert resp.status_code == 200
    assert captured["plate"] == "H424242"
    assert captured["control_number"] == "4242424242"
    db.update_lead.assert_not_called()


# ---------------------------------------------------------------- leaderboard


def test_leaderboard_shows_medals_and_ranked_names(authed):
    resp = authed.get("/dispatch/leaderboard")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    for medal in ("\U0001F947", "\U0001F948", "\U0001F949"):  # gold silver bronze
        assert medal in html
    assert "4." in html  # off the podium, plain rank number
    assert "Alice Chen" in html and "Ray Ortiz" in html
    # Names must appear in rank order — most clients first.
    assert html.index("Alice Chen") < html.index("Marcus Webb") < html.index("Dee Ford")
    assert "27" in html  # 12 + 8 + 5 + 2 clients entered


# ---------------------------------------------------------------- receipts

# The poison a stored-URL regression would spill onto the wall: bot.py's
# last-ditch fallback stores api.telegram.org file links whose path EMBEDS the
# bot token (full bot control — sendMessage as the bot, getFile on every
# receipt ever uploaded).
_TG_BOT_TOKEN = "123456789:AAH-flowtest-SECRET-t0ken"
_TG_RECEIPT_URL = (
    "https://api.telegram.org/file/bot%s/photos/file_77.jpg#tgfid=AgACAgFlow"
    % _TG_BOT_TOKEN
)


def test_receipts_wall_never_emits_stored_urls_or_bot_token(authed, db):
    """The stored receipt_image_url must NEVER reach the page. The wall must
    route thumbnail and open-link through the host app's token-free resolver
    (/api/receipts/image/<lead_id>) — and the view must not even SELECT the
    column, so a future template regression has nothing to spill."""
    selected = {}

    class _RecordingQuery(_FakeQuery):
        def select(self, cols, *args, **kwargs):
            selected["cols"] = cols
            return self

    # The fake answers the FULL row no matter what was selected — the worst
    # case on purpose: if the template still referenced row.receipt_image_url,
    # the token would land in the HTML asserted below.
    row = {
        "id": LEAD_ID,
        "reference_id": "K7Q2M9ZX",
        "receipt_image_url": _TG_RECEIPT_URL,
        "updated_at": "2026-08-27T14:30:00+00:00",
    }
    db.client.table.side_effect = lambda name: _RecordingQuery(
        SimpleNamespace(data=[dict(row)], count=None)
    )

    resp = authed.get("/dispatch/receipts")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    assert _TG_BOT_TOKEN not in html, "the BOT TOKEN is in the served page"
    assert "api.telegram.org" not in html, "a stored Telegram file URL reached the page"

    # Both affordances go through the token-free resolver on the same host app.
    assert 'href="/api/receipts/image/%s"' % LEAD_ID in html
    assert 'src="/api/receipts/image/%s"' % LEAD_ID in html
    assert "K7Q2M9ZX" in html  # the card still names its lead

    # Defense in depth: the URL column is filtered on but never fetched.
    assert "receipt_image_url" not in selected.get("cols", ""), (
        "receipts view selects receipt_image_url again — one {{ }} away from "
        "serving the bot token to every dispatch viewer"
    )


def test_lead_detail_receipt_link_never_emits_stored_url_or_bot_token(authed, db):
    """Same poison, other page: the lead page's Receipt row used to render the
    stored receipt_image_url verbatim, serving a Telegram-fallback row's BOT
    TOKEN to every DISPATCH_WEB_PASSWORD holder. The view must hand the
    template a presence boolean only (and pop the URL off the row it passes
    for the raw view), with the link routed through the host app's token-free
    resolver — exactly like the receipts wall above."""
    db.get_lead_by_id.return_value = dict(
        SEEDED_LEAD, receipt_image_url=_TG_RECEIPT_URL
    )
    resp = authed.get(f"/dispatch/lead/{LEAD_ID}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    assert _TG_BOT_TOKEN not in html, "the BOT TOKEN is in the served page"
    assert "api.telegram.org" not in html, "a stored Telegram file URL reached the page"

    # The Receipt row survives — pointed at the resolver on the same host app.
    assert 'href="/api/receipts/image/%s"' % LEAD_ID in html
    assert ">image</a>" in html

    # The gate is "has a receipt", not the URL value: without one there is no
    # Receipt row, and nothing on the page mentions the resolver.
    db.get_lead_by_id.return_value = dict(SEEDED_LEAD)
    html = authed.get(f"/dispatch/lead/{LEAD_ID}").get_data(as_text=True)
    assert "/api/receipts/image/" not in html


def test_receipts_db_failure_banners_without_leaking_str_e(authed, db):
    """Contract: DB errors become fixed-text banners; str(e) stays in the log,
    never in the served page (nor in the dashboard's global JSON errorhandler,
    which a raise would reach)."""
    db.client.table.side_effect = RuntimeError("sb-r3ceipt-1nternal host detail")
    resp = authed.get("/dispatch/receipts")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Could not load receipts" in html
    assert "sb-r3ceipt-1nternal" not in html


# ---------------------------------------------------------------- settings


def _settings_csrf(client):
    """Render the settings page and pull the per-session token from the form —
    the same round-trip a real dispatcher's browser makes."""
    page = client.get("/dispatch/settings")
    assert page.status_code == 200
    m = re.search(r'name="csrf" value="([^"]+)"', page.get_data(as_text=True))
    assert m, "settings page did not render a csrf hidden field"
    return m.group(1)


def test_settings_toggle_flips_set_setting(authed, db):
    tok = _settings_csrf(authed)
    # Switch reads "0" now, so a blind flip (no value key) must write "1".
    resp = authed.post("/dispatch/settings/instant-all", data={"csrf": tok})
    assert resp.status_code == 302
    assert "toggle=ok" in resp.headers["Location"]
    db.set_setting.assert_called_once_with("instant_all_drivers", "1")

    # The form's explicit value wins: a stale double-submit of "0" writes "0",
    # it does not flip twice — and the token is per-session, so the SAME token
    # still passes (the stale-resubmit idempotency story survives CSRF).
    db.set_setting.reset_mock()
    resp = authed.post(
        "/dispatch/settings/instant-all", data={"csrf": tok, "value": "0"}
    )
    assert "toggle=ok" in resp.headers["Location"]
    db.set_setting.assert_called_once_with("instant_all_drivers", "0")

    # The redirect target renders (the settings page survives the fake shapes).
    assert authed.get("/dispatch/settings").status_code == 200


def test_settings_toggle_reports_a_refused_write(authed, db):
    tok = _settings_csrf(authed)
    db.set_setting.return_value = False
    resp = authed.post(
        "/dispatch/settings/instant-all", data={"csrf": tok, "value": "1"}
    )
    assert resp.status_code == 302
    assert "toggle=fail" in resp.headers["Location"]


def test_settings_toggle_refuses_without_csrf(authed, db):
    """A cross-site form POST carries the session cookie but can never carry
    the token — the switch must not move and the DB must not be touched."""
    for payload in ({}, {"value": "1"}, {"csrf": "forged-token", "value": "1"}):
        resp = authed.post("/dispatch/settings/instant-all", data=payload)
        assert resp.status_code == 302
        assert "toggle=csrf" in resp.headers["Location"]
    db.set_setting.assert_not_called()

    # The bounce target explains itself as a banner, leaking nothing.
    page = authed.get("/dispatch/settings?toggle=csrf")
    assert "Security check failed" in page.get_data(as_text=True)


def test_settings_toggle_refuses_before_first_render(app, db):
    """Fail closed: a session that never rendered the settings page has no
    token yet, and a token-less POST must refuse rather than blind-flip."""
    c = app.test_client()
    resp = c.post("/dispatch/login", data={"password": PASSWORD})
    assert resp.status_code == 302
    resp = c.post("/dispatch/settings/instant-all", data={"value": "1"})
    assert "toggle=csrf" in resp.headers["Location"]
    db.set_setting.assert_not_called()


def test_session_cookie_is_samesite_lax(app, client):
    """dispatch_web.register() must stamp SameSite=Lax onto the host app's
    session cookie (Flask's default emits none at all — the CSRF surface).
    Asserted on the wire: the login response's Set-Cookie header."""
    assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
    resp = client.post("/dispatch/login", data={"password": PASSWORD})
    cookie = resp.headers.get("Set-Cookie", "")
    assert "session=" in cookie
    assert "SameSite=Lax" in cookie


def test_register_respects_host_samesite_choice(monkeypatch):
    """A host app that already chose a stricter policy must not be downgraded."""
    monkeypatch.setenv("DISPATCH_WEB_PASSWORD", PASSWORD)
    monkeypatch.setenv("DISPATCH_WEB_SECRET", "flow-test-secret")
    application = Flask("dispatch_web_samesite_strict_test")
    application.config["TESTING"] = True
    application.config["SESSION_COOKIE_SAMESITE"] = "Strict"
    dispatch_web.register(application)
    assert application.config["SESSION_COOKIE_SAMESITE"] == "Strict"
