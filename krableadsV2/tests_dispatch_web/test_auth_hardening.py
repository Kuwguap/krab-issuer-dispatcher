"""Auth-hardening pins for dispatch_web core: cookie flags, the cross-site
(CSRF) gate, the login throttle, and the session-signing secret.

Import-order rules are the directory's (see test_flows_dispatch.py):
SENTRY_DSN blanked and utils.database.Database replaced with a MagicMock
BEFORE dispatch_web is imported. Alphabetically this file collects FIRST in a
full-directory run, so ITS fresh-import snapshot is the one that arms the
"Database is constructed lazily" guard — the assertion is repeated here.

Throttle state is process-global in core (_login_failures); the autouse
fixture clears it around every test so this module can hammer the login
endpoint without locking out the other suites' 127.0.0.1 client.
"""
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

os.environ["SENTRY_DSN"] = ""
os.environ.setdefault("SUPABASE_URL", "https://authtest-dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "authtest-dummy-supabase-key")

import utils.database  # noqa: E402

utils.database.Database = MagicMock(name="DatabaseClassMock(auth)")

import pytest  # noqa: E402
from flask import Flask  # noqa: E402
from flask.sessions import SecureCookieSessionInterface  # noqa: E402

_FRESH_IMPORT = "dispatch_web" not in sys.modules

import dispatch_web  # noqa: E402
from dispatch_web import core  # noqa: E402

_DB_CALLS_AT_IMPORT = utils.database.Database.call_count

PASSWORD = "auth-test-password"


class _FakeQuery:
    """Any builder-method chain returns itself; execute() answers the canned
    result — same stand-in the flows suite uses."""

    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result

    def __getattr__(self, _name):
        def _chain(*_args, **_kwargs):
            return self

        return _chain


def _refuse_real_database(*_args, **_kwargs):
    raise AssertionError(
        "dispatch_web tried to construct the real Database during a test — "
        "the module cache should have been primed with the fake"
    )


def _make_fake_db():
    """The flows suite's shapes, trimmed to what these tests touch (board GET
    to prove a session survived, settings/strike POSTs to prove the CSRF gate
    answers before any write)."""
    db = MagicMock(name="FakeDatabase(auth)")
    lead = {
        "id": "a3d9c0de-77aa-4b1c-9e21-5c0ffee0c0de",
        "reference_id": "K7Q2M9ZX",
        "telegram_name": "Marcus Webb",
        "telegram_username": "marcusw",
        "user_id": 555001,
        "vehicle_details": "\n".join(
            ["DANA WHITLOCK", "12 MAIN ST", "NEWARK, NJ 07102", "44 OCEAN AVE",
             "JERSEY CITY, NJ 07305", "1FTFW1E50MFA10001", "2021 FORD F-150",
             "WHITE", "PROGRESSIVE", "POL-778812", "-"]
        ),
        "created_at": "2026-08-27T14:30:00+00:00",
        "exclude_from_count": False,
    }
    db.list_recent_leads_for_review.return_value = ([dict(lead)], 1)
    db.get_lead_by_id.return_value = dict(lead)
    db.get_lead_counts_by_sender.return_value = [("Alice Chen", 12), ("Marcus Webb", 8)]
    db.set_lead_excluded.return_value = True
    db.update_lead.return_value = True
    db.set_setting.return_value = True
    db.get_setting.return_value = "0"
    db.get_all_groups.return_value = [
        {"id": "grp-alpha", "group_name": "Team Alpha", "is_active": True},
    ]
    db.get_group_by_id.return_value = {"id": "grp-alpha", "group_name": "Team Alpha"}
    db.create_lead.return_value = {"id": "lead-new-001"}
    db.client.table.side_effect = lambda name: _FakeQuery(
        SimpleNamespace(data=[], count=None)
    )
    return db


@pytest.fixture(autouse=True)
def _clean_throttle_state():
    core._login_failures.clear()
    yield
    core._login_failures.clear()


@pytest.fixture()
def db(monkeypatch):
    fake = _make_fake_db()
    monkeypatch.setattr(core, "_db", fake)
    monkeypatch.setattr(core, "Database", _refuse_real_database)
    return fake


@pytest.fixture()
def app(monkeypatch):
    monkeypatch.setenv("DISPATCH_WEB_PASSWORD", PASSWORD)
    monkeypatch.setenv("DISPATCH_WEB_SECRET", "auth-test-secret")
    application = Flask("dispatch_web_auth_test")
    application.config["TESTING"] = True
    dispatch_web.register(application)
    return application


@pytest.fixture()
def client(app, db):
    return app.test_client()


@pytest.fixture()
def authed(client):
    resp = client.post("/dispatch/login", data={"password": PASSWORD})
    assert resp.status_code == 302
    return client


# ------------------------------------------------------------- laziness guard


def test_import_did_not_construct_database():
    """This file collects first in a full run — the guard must arm HERE."""
    if not _FRESH_IMPORT:
        pytest.skip("dispatch_web was already imported by an earlier test module")
    assert _DB_CALLS_AT_IMPORT == 0, (
        "Database() was constructed during `import dispatch_web` (%d call(s)); "
        "the contract says construct lazily inside get_db()" % _DB_CALLS_AT_IMPORT
    )


# ------------------------------------------------------- session cookie flags


def test_session_cookie_secure_httponly_samesite(app, client):
    """The cookie is a full replayable credential: it must never transmit over
    a cleartext hop (Secure), never be script-readable (HttpOnly), and carry
    an EXPLICIT SameSite instead of leaning on browser defaults."""
    assert app.config["SESSION_COOKIE_SECURE"] is True
    assert app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"

    resp = client.post("/dispatch/login", data={"password": PASSWORD})
    assert resp.status_code == 302
    set_cookie = resp.headers.get("Set-Cookie", "")
    assert "session=" in set_cookie, "login success did not set the session cookie"
    assert "Secure" in set_cookie, "Set-Cookie missing Secure: %r" % set_cookie
    assert "HttpOnly" in set_cookie, "Set-Cookie missing HttpOnly: %r" % set_cookie
    assert "SameSite=Lax" in set_cookie, "Set-Cookie missing SameSite: %r" % set_cookie


# ------------------------------------------------------------- CSRF gate


def test_cross_site_post_refused_before_any_write(authed, db):
    """A cross-site browser POST (evil Origin, or the browser's own
    Sec-Fetch-Site verdict) must 403 without the view — or the DB — running."""
    resp = authed.post(
        "/dispatch/settings/instant-all",
        data={"value": "1"},
        headers={"Origin": "https://evil.example"},
    )
    assert resp.status_code == 403
    db.set_setting.assert_not_called()

    resp = authed.post(
        "/dispatch/lead/a3d9c0de-77aa-4b1c-9e21-5c0ffee0c0de/strike",
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert resp.status_code == 403
    db.set_lead_excluded.assert_not_called()

    # The gate covers login itself: a cross-site page cannot even submit guesses.
    resp = authed.post(
        "/dispatch/login",
        data={"password": PASSWORD},
        headers={"Origin": "https://evil.example"},
    )
    assert resp.status_code == 403


def test_same_origin_posts_still_pass_the_gate(authed, db):
    """Real browser submissions carry a matching Origin (or Sec-Fetch-Site:
    same-origin) — those, and header-less non-browser clients, sail through.
    Pinned on strike/restore: mutation routes with NO per-form token of their
    own, whose only CSRF cover is this gate (the https Origin against an http
    request.host also proves the compare is hostname-only — the TLS-proxy
    deployment shape)."""
    lead_id = "a3d9c0de-77aa-4b1c-9e21-5c0ffee0c0de"
    resp = authed.post(
        f"/dispatch/lead/{lead_id}/strike",
        headers={"Origin": "https://localhost"},
    )
    assert resp.status_code == 302
    assert "ok=struck" in resp.headers["Location"]
    db.set_lead_excluded.assert_called_once_with(lead_id, True)

    db.set_lead_excluded.reset_mock()
    resp = authed.post(
        f"/dispatch/lead/{lead_id}/restore",
        headers={"Sec-Fetch-Site": "same-origin"},
    )
    assert resp.status_code == 302
    assert "ok=restored" in resp.headers["Location"]
    db.set_lead_excluded.assert_called_once_with(lead_id, False)


def test_cross_site_get_logout_keeps_the_session(authed):
    """SameSite=Lax still sends the cookie on cross-site top-level GET
    navigation, and base.html links logout as a plain GET anchor — so logout
    must ignore a navigation that positively identifies as cross-site."""
    resp = authed.get("/dispatch/logout", headers={"Sec-Fetch-Site": "cross-site"})
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/dispatch/"
    assert authed.get("/dispatch/").status_code == 200, "cross-site GET killed the session"

    # Legacy browser shape: no Sec-Fetch-Site, hostile Referer.
    resp = authed.get(
        "/dispatch/logout", headers={"Referer": "https://evil.example/prank"}
    )
    assert resp.status_code == 302
    assert authed.get("/dispatch/").status_code == 200, "evil-Referer GET killed the session"

    # The dispatcher's own click (no cross-site evidence) still logs out.
    resp = authed.get("/dispatch/logout")
    assert resp.status_code == 302
    assert "/dispatch/login" in resp.headers["Location"]
    board = authed.get("/dispatch/")
    assert board.status_code == 302
    assert "/dispatch/login" in board.headers["Location"]


# ------------------------------------------------------------- login throttle


def _wrong(client, addr):
    return client.post(
        "/dispatch/login",
        data={"password": "not-it"},
        environ_overrides={"REMOTE_ADDR": addr},
    )


def _right(client, addr):
    return client.post(
        "/dispatch/login",
        data={"password": PASSWORD},
        environ_overrides={"REMOTE_ADDR": addr},
    )


def test_login_locks_out_after_repeated_failures(app, client, monkeypatch):
    addr = "203.0.113.7"
    for i in range(core._THROTTLE_MAX_FAILURES):
        resp = _wrong(client, addr)
        assert resp.status_code == 401, "failure %d answered %d" % (i + 1, resp.status_code)

    # Locked now: further guesses get 429 + Retry-After, and — the point of a
    # lockout — even the RIGHT password is not evaluated while locked.
    resp = _wrong(client, addr)
    assert resp.status_code == 429
    retry_after = int(resp.headers["Retry-After"])
    assert 0 < retry_after <= core._THROTTLE_MAX_LOCK_SECONDS
    assert "Try again in" in resp.get_data(as_text=True)

    resp = _right(client, addr)
    assert resp.status_code == 429, "correct password bypassed an active lockout"
    board = client.get("/dispatch/", environ_overrides={"REMOTE_ADDR": addr})
    assert board.status_code == 302, "a 429'd login still granted a session"

    # Warp past the lock: the legit dispatcher gets in, and success RESETS the
    # counter — the next single failure is a fresh 401, not an escalated lock.
    real_now = core._now
    monkeypatch.setattr(
        core, "_now", lambda: real_now() + core._THROTTLE_MAX_LOCK_SECONDS + 1
    )
    resp = _right(client, addr)
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/dispatch/"
    # Fresh client: `client` now HOLDS a session, and an authed POST /login
    # short-circuits to a 302 before the throttle would even look.
    assert _wrong(app.test_client(), addr).status_code == 401


def test_lockout_is_per_source_address(client):
    """One ground-away address must not lock the dispatcher's own out."""
    for _ in range(core._THROTTLE_MAX_FAILURES + 1):
        _wrong(client, "198.51.100.9")
    assert _wrong(client, "198.51.100.9").status_code == 429

    resp = _right(client, "203.0.113.50")
    assert resp.status_code == 302


# --------------------------------------------------------- session secret key


def _fresh_app(monkeypatch, *, secret, password):
    if secret is None:
        monkeypatch.delenv("DISPATCH_WEB_SECRET", raising=False)
    else:
        monkeypatch.setenv("DISPATCH_WEB_SECRET", secret)
    monkeypatch.setenv("DISPATCH_WEB_PASSWORD", password)
    application = Flask("dispatch_web_secret_test")
    application.config["TESTING"] = True
    dispatch_web.register(application)
    return application


def test_dedicated_secret_used_verbatim(monkeypatch):
    application = _fresh_app(monkeypatch, secret="a-dedicated-secret", password=PASSWORD)
    assert application.secret_key == "a-dedicated-secret"


def test_fallback_secret_is_derived_not_supabase_key(monkeypatch, caplog):
    """The finding: signing with SUPABASE_KEY verbatim let any key-holder mint
    {"dw_ok": true}. The fallback must be independent of the raw key, bound to
    the password (rotation revokes sessions), deterministic across workers,
    and loudly flagged so ops sets a dedicated secret."""
    supabase_key = os.environ["SUPABASE_KEY"]
    with caplog.at_level(logging.ERROR, logger="dispatch_web.core"):
        application = _fresh_app(monkeypatch, secret=None, password=PASSWORD)

    assert application.secret_key, "no secret at all — sessions would 500"
    assert application.secret_key != supabase_key, (
        "secret_key is still the raw SUPABASE_KEY — any key-holder can forge sessions"
    )
    assert any("DISPATCH_WEB_SECRET" in r.message for r in caplog.records), (
        "the derived-fallback path must warn loudly (ERROR reaches Sentry)"
    )

    # Deterministic: a second worker booting with the same env signs identically.
    twin = _fresh_app(monkeypatch, secret=None, password=PASSWORD)
    assert twin.secret_key == application.secret_key

    # Password-bound: rotating the password rotates the signing key, which
    # force-expires every outstanding session (the old fallback never did).
    rotated = _fresh_app(monkeypatch, secret=None, password="rotated-password")
    assert rotated.secret_key != application.secret_key


def test_cookie_signed_with_raw_supabase_key_is_rejected(monkeypatch, db):
    """The finding's actual exploit, replayed: mint {"dw_ok": true} with the
    raw SUPABASE_KEY (what the old fallback signed with) and present it. The
    board must bounce it to login, not answer 200."""
    application = _fresh_app(monkeypatch, secret=None, password=PASSWORD)

    forge = Flask("supabase_key_holder")
    forge.secret_key = os.environ["SUPABASE_KEY"]
    serializer = SecureCookieSessionInterface().get_signing_serializer(forge)
    forged = serializer.dumps({"dw_ok": True})

    intruder = application.test_client()
    intruder.set_cookie("session", forged)
    resp = intruder.get("/dispatch/")
    assert resp.status_code == 302, (
        "a session signed with the raw SUPABASE_KEY was accepted (%d)" % resp.status_code
    )
    assert "/dispatch/login" in resp.headers.get("Location", "")

    # Sanity: the derivation still yields working sessions end-to-end.
    legit = application.test_client()
    assert legit.post("/dispatch/login", data={"password": PASSWORD}).status_code == 302
    assert legit.get("/dispatch/").status_code == 200
