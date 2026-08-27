"""Wiring tests for dispatch_web — the contract every builder codes against.

These tests never touch Supabase: utils.database.Database is replaced with a
MagicMock BEFORE dispatch_web is imported, so core's `from utils.database
import Database` binds the mock and get_db() can only ever construct fakes.
No template-rendering view body executes in this file either (every request
is stopped by the 503 gate or the login redirect; logout is a bare redirect),
so nothing here depends on another agent's templates rendering.

Run from anywhere: the repo root is derived from this file's location, not
from cwd — pytest puts tests_dispatch_web/ on sys.path, never the repo root.
"""
import inspect
import os
import pkgutil
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, Mock

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Before any repo import: config.py runs load_dotenv at import time, and
# load_dotenv never overrides existing env — setdefault here wins over a real
# .env on the dev box, and fills the gap on a bare CI box. SUPABASE_KEY feeds
# register()'s DERIVED secret_key fallback, so sessions sign with something
# (the derivation itself is pinned in test_auth_hardening.py).
os.environ.setdefault("SUPABASE_URL", "https://wiretest-dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "wiretest-dummy-supabase-key")

import utils.database  # noqa: E402

utils.database.Database = MagicMock(name="DatabaseClassMock")

# When another test module imported dispatch_web first, core has already bound
# the previous Database object and the call-count snapshot below is vacuous —
# the laziness assertion only means something on a fresh import.
_FRESH_IMPORT = "dispatch_web" not in sys.modules

import dispatch_web  # noqa: E402

_DB_CALLS_AT_IMPORT = utils.database.Database.call_count

from flask import Blueprint, Flask  # noqa: E402


def _make_app(monkeypatch, password):
    """Fresh host app per test: the gate reads DISPATCH_WEB_PASSWORD per
    request, but a fresh app also proves register() is safe to call against
    more than one Flask instance (admin_dashboard restarts do exactly that)."""
    if password is None:
        monkeypatch.delenv("DISPATCH_WEB_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("DISPATCH_WEB_PASSWORD", password)
    app = Flask(__name__)
    app.config["TESTING"] = True
    dispatch_web.register(app)
    return app


def _dispatch_rules(app):
    return [r for r in app.url_map.iter_rules() if r.rule.startswith("/dispatch")]


def _fill_placeholders(rule):
    """Turn a rule string into a requestable path; values only need to satisfy
    the converter — the 503 gate answers before any view sees them."""
    rule = re.sub(r"<int:[^>]+>", "1", rule)
    rule = re.sub(r"<path:[^>]+>", "x", rule)
    return re.sub(r"<[^>]+>", "TEST1234", rule)


# ---------------------------------------------------------------- (a) exports


def test_package_exports():
    assert callable(dispatch_web.register), "dispatch_web.register missing"
    assert callable(dispatch_web.get_db), "dispatch_web.get_db missing"
    assert callable(dispatch_web.require_login), "dispatch_web.require_login missing"
    assert isinstance(dispatch_web.bp, Blueprint), "dispatch_web.bp is not a Blueprint"
    assert dispatch_web.bp.name == "dispatch_web"
    assert dispatch_web.bp.url_prefix == "/dispatch"
    if _FRESH_IMPORT:
        # Contract: Database is constructed lazily, never at import — an eager
        # construction would crash the whole host service when env is missing.
        assert _DB_CALLS_AT_IMPORT == 0, (
            "Database() was constructed during `import dispatch_web` "
            "(%d call(s)); the contract says construct lazily in get_db()"
            % _DB_CALLS_AT_IMPORT
        )


def test_get_db_cached_and_mocked():
    # Deterministic regardless of suite order: another test module that ran
    # first can have cached a REAL Database in core before this file bound the
    # mock. Reset the cache and rebind core.Database to the mock, so the two
    # things under test here — caching and mock-construction — are actually
    # exercised, not a stale instance from an earlier import.
    from dispatch_web import core
    core._db = None
    core.Database = utils.database.Database
    db = dispatch_web.get_db()
    assert db is dispatch_web.get_db(), "get_db() must return one module-cached instance"
    # If this is a real utils.database.Database, the mock patch was bypassed
    # and these tests are one call away from live Supabase.
    assert isinstance(db, Mock), "get_db() returned a non-mock: %r" % type(db)


# ------------------------------------------- (a) every view module has routes


def test_every_view_module_attaches_routes(monkeypatch):
    app = _make_app(monkeypatch, "wiretest-pw")

    # Mirror __init__'s own discovery policy: core is the hub, underscore
    # modules are helpers that opt out, everything else is a view module.
    expected = {
        name
        for _, name, _ispkg in pkgutil.iter_modules(dispatch_web.__path__)
        if name != "core" and not name.startswith("_")
    }
    assert expected, "no view modules found in dispatch_web/ at all"

    # __init__ swallows a view module's import error (by design — one broken
    # page must not darken the host service), which is exactly why the test
    # suite has to fail loudly on its behalf.
    not_imported = sorted(
        m for m in expected if "dispatch_web." + m not in sys.modules
    )
    assert not not_imported, (
        "view modules failed to import (check the ERROR log from "
        "dispatch_web/__init__.py): %s" % ", ".join(not_imported)
    )

    owners = set()
    for endpoint, func in app.view_functions.items():
        if not endpoint.startswith("dispatch_web."):
            continue
        # require_login uses functools.wraps, so unwrap lands on the real view
        # and __module__ names the module that owns the route.
        owners.add(getattr(inspect.unwrap(func), "__module__", ""))

    routeless = sorted(
        m
        for m in expected
        if "dispatch_web." + m not in owners
        and not any(o.startswith("dispatch_web." + m + ".") for o in owners)
    )
    assert not routeless, (
        "modules imported but attached no route to bp (wrong blueprint "
        "object, or a helper that should be _underscore-prefixed): %s"
        % ", ".join(routeless)
    )


# ------------------------------------------------------------ (b) url_map


def test_url_map_has_contract_routes(monkeypatch):
    app = _make_app(monkeypatch, "wiretest-pw")
    # Param names differ per builder (<lead_id> vs <x>) — normalize them away.
    rules = {re.sub(r"<[^>]+>", "<x>", r.rule) for r in _dispatch_rules(app)}

    assert "/dispatch/" in rules, "board route /dispatch/ missing; have: %s" % sorted(rules)
    for path in ("/dispatch/login", "/dispatch/new", "/dispatch/leaderboard"):
        assert path in rules or path + "/" in rules, (
            "%s missing from url_map; have: %s" % (path, sorted(rules))
        )
    assert "/dispatch/lead/<x>" in rules or "/dispatch/lead/<x>/" in rules, (
        "lead detail route /dispatch/lead/<x> missing; have: %s" % sorted(rules)
    )


# ------------------------------------------------- (c) fail closed without pw


def test_fail_closed_503_when_password_unset(monkeypatch):
    app = _make_app(monkeypatch, None)
    client = app.test_client()

    checked = 0
    for rule in _dispatch_rules(app):
        if rule.rule == "/dispatch/health":
            continue  # the one deliberate hole — covered by its own test
        method = "GET" if "GET" in rule.methods else (
            "POST" if "POST" in rule.methods else None
        )
        assert method is not None, "%s exposes neither GET nor POST" % rule.rule
        path = _fill_placeholders(rule.rule)
        resp = client.open(path, method=method)
        assert resp.status_code == 503, (
            "%s %s answered %d with DISPATCH_WEB_PASSWORD unset; the whole "
            "blueprint must fail closed with 503" % (method, path, resp.status_code)
        )
        checked += 1
    # A registration that silently attached nothing would make the sweep above
    # pass vacuously; the board/login/new/leaderboard/lead-detail minimum is 5.
    assert checked >= 5, "only %d /dispatch/* routes swept — registration broken?" % checked

    body = client.get("/dispatch/").get_data(as_text=True)
    assert "DISPATCH_WEB_PASSWORD" in body, (
        "503 body should tell the operator which env var to set"
    )


def test_health_exempt_from_fail_closed(monkeypatch):
    app = _make_app(monkeypatch, None)
    assert "/dispatch/health" in {r.rule for r in _dispatch_rules(app)}, (
        "/dispatch/health route missing — monitoring has no unauthenticated probe"
    )
    resp = app.test_client().get("/dispatch/health")
    assert resp.status_code == 200, (
        "/dispatch/health answered %d with DISPATCH_WEB_PASSWORD unset; it is "
        "the one endpoint exempt from the 503 gate" % resp.status_code
    )


# ---------------------------------------------- (d) login redirect with pw set


def test_unauthenticated_root_redirects_to_login(monkeypatch):
    app = _make_app(monkeypatch, "wiretest-pw")
    resp = app.test_client().get("/dispatch/")
    assert resp.status_code in (301, 302, 303, 307, 308), (
        "GET /dispatch/ without a session answered %d, expected a redirect"
        % resp.status_code
    )
    assert "/dispatch/login" in resp.headers.get("Location", ""), (
        "redirect went to %r, not the login page" % resp.headers.get("Location")
    )


def test_every_route_requires_login_except_health_and_login(monkeypatch):
    """Password SET, no session: every /dispatch/* route must bounce to the
    login page. A route whose builder forgot @require_login serves content (or
    500s on the fake-less DB) here instead of redirecting — receipts, settings
    and tag.pdf (a PII document) are exactly one forgotten decorator away.
    health stays open by contract; login is the destination itself; logout's
    own bare redirect satisfies the sweep."""
    app = _make_app(monkeypatch, "wiretest-pw")
    client = app.test_client()
    checked = 0
    for rule in _dispatch_rules(app):
        if rule.rule in ("/dispatch/health", "/dispatch/login"):
            continue
        if rule.endpoint == "dispatch_web.static":
            continue  # Flask's asset route: CSS/JS, no PII, open like any static
        method = "GET" if "GET" in rule.methods else "POST"
        path = _fill_placeholders(rule.rule)
        resp = client.open(path, method=method)
        assert resp.status_code in (301, 302, 303, 307, 308), (
            "%s %s answered %d to an unauthenticated browser; every non-health "
            "route must redirect to login" % (method, path, resp.status_code)
        )
        assert "/dispatch/login" in resp.headers.get("Location", ""), (
            "%s %s redirected to %r, not the login page"
            % (method, path, resp.headers.get("Location"))
        )
        checked += 1
    assert checked >= 5, (
        "only %d /dispatch/* routes swept — registration broken?" % checked
    )


# ----------------------------------------------------------- fmt_ts is wired


def test_fmt_ts_filter_registered(monkeypatch):
    app = _make_app(monkeypatch, "wiretest-pw")
    filt = app.jinja_env.filters.get("fmt_ts")
    assert callable(filt), "fmt_ts template filter not registered on the host app"
    # Contract's own example shape: ISO → "Aug 27, 3:04 PM" (NY time; 19:04Z
    # on an EDT date is 3:04 PM).
    assert filt("2026-08-27T19:04:00+00:00") == "Aug 27, 3:04 PM"
