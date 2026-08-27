"""Frontend pins for dispatch_web: base.html's proxy-safe chrome and
dispatch.js's proxy/confirm/data-integrity behavior.

Why these exist: the public deployment serves the blueprint through the
tristatetags.com/backend reverse proxy, which STRIPS /backend before
forwarding — the server never sees the public prefix, so every absolute
"/dispatch/..." URL (url_for's included) resolves outside the mount and 404s
there. base.html therefore emits depth-aware RELATIVE URLs for all of its
chrome, and dispatch.js rebases every server-authored absolute URL it is
handed via attributes. Those are exactly the regressions pinned here, plus
dispatch.js's other fixed contracts: no double confirm() on top of lead.html's
inline strike confirm, and never blanking the board from a failure envelope
or a rowless fragment.

Two layers, so the pins hold everywhere:
  * pure-Python: render real pages through the blueprint (fake DB) and check
    the chrome resolves inside the mount under BOTH deployments; plus source
    tripwires on dispatch.js for the load-bearing constructs.
  * behavioral (skipped when node is missing): run the REAL dispatch.js under
    tests_dispatch_web/harness_dispatch_js.js's stub DOM and assert what it
    actually fetches, confirms, and swaps.

Import-order rules: test_flows_dispatch owns the patch-before-import
discipline (env dummies + utils.database.Database mocked BEFORE dispatch_web
is imported) — importing it FIRST applies the same guarantees here no matter
which suite module pytest collects first.
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urljoin

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:  # direct single-file invocations included
    sys.path.insert(0, str(_HERE))

import test_flows_dispatch as flows  # noqa: E402  (patches, then imports dispatch_web)

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

import dispatch_web  # noqa: E402
from dispatch_web import core  # noqa: E402

LEAD_ID = flows.LEAD_ID
PASSWORD = flows.PASSWORD

_JS_PATH = flows._REPO_ROOT / "dispatch_web" / "static" / "dispatch.js"
_HARNESS = _HERE / "harness_dispatch_js.js"
_NODE = shutil.which("node")


@pytest.fixture()
def db(monkeypatch):
    fake = flows._make_fake_db()
    monkeypatch.setattr(core, "_db", fake)
    monkeypatch.setattr(core, "Database", flows._refuse_real_database)
    return fake


@pytest.fixture()
def app(monkeypatch):
    monkeypatch.setenv("DISPATCH_WEB_PASSWORD", PASSWORD)
    monkeypatch.setenv("DISPATCH_WEB_SECRET", "frontend-test-secret")
    application = Flask("dispatch_web_frontend_test")
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


# --------------------------------------------- base.html: proxy-safe chrome

# The two real deployments: bare, and behind the prefix-stripping proxy.
_PUBLIC_ROOTS = ("https://x.test", "https://x.test/backend")


def _chrome_urls(html):
    """(kind, url) for every URL base.html ITSELF emits: the stylesheet, the
    deferred script, and each href in the <head>+<header> chrome. Body links
    belong to the page templates (other agents' files) and are not swept."""
    urls = []
    m = re.search(r'<link rel="stylesheet" href="([^"]*)">', html)
    assert m, "stylesheet link missing from chrome"
    urls.append(("stylesheet", m.group(1)))
    m = re.search(r'<script src="([^"]*)" defer>', html)
    assert m, "deferred dispatch.js script tag missing from chrome"
    urls.append(("script", m.group(1)))
    chrome = html.split("</header>", 1)[0]
    for href in re.findall(r'href="([^"]*)"', chrome):
        if href.startswith("data:"):  # the inline favicon
            continue
        urls.append(("nav", href))
    return urls


def _assert_chrome_inside_mount(html, server_path):
    # (The login page is exempt by design: core.py renders it standalone with
    # zero external assets, so base.html's chrome contract never applies.)
    urls = _chrome_urls(html)
    nav = [u for kind, u in urls if kind == "nav"]
    # brand + Board/New Lead/Leaderboard/Rosters/Receipts/Settings + Logout
    assert len(nav) >= 8, "authed chrome lost its nav: %r" % (nav,)
    for root in _PUBLIC_ROOTS:
        page = root + server_path  # request.path is what the server saw
        mount = root + "/dispatch/"
        for kind, u in urls:
            assert not u.startswith("/"), (
                "%s URL %r is server-absolute; behind %s it would resolve "
                "outside the /dispatch mount" % (kind, u, root)
            )
            assert "://" not in u and not u.startswith("//"), (
                "%s URL %r left the site entirely" % (kind, u)
            )
            resolved = urljoin(page, u)
            assert resolved == mount or resolved.startswith(mount), (
                "%s URL %r on page %s resolves to %s — outside the mount %s"
                % (kind, u, page, resolved, mount)
            )
        # Exact targets — the depth math has to land, not merely stay inside.
        assert urljoin(page, dict((k, v) for k, v in urls)["stylesheet"]) == \
            mount + "static/dispatch.css"
        assert urljoin(page, dict((k, v) for k, v in urls)["script"]) == \
            mount + "static/dispatch.js"
        brand = re.search(r'class="brand" href="([^"]*)"', html).group(1)
        assert urljoin(page, brand) == mount, "brand must land on the board"
        logout = re.search(r'href="([^"]*)" class="logout"', html).group(1)
        assert urljoin(page, logout) == mount + "logout"
        lb = re.search(r'<a href="([^"]*)"[^>]*>Leaderboard</a>', html).group(1)
        assert urljoin(page, lb) == mount + "leaderboard"


def test_chrome_relative_urls_survive_proxy_on_depth0_pages(authed):
    for path in ("/dispatch/", "/dispatch/new", "/dispatch/leaderboard",
                 "/dispatch/receipts", "/dispatch/settings"):
        resp = authed.get(path)
        assert resp.status_code == 200, "%s answered %d" % (path, resp.status_code)
        _assert_chrome_inside_mount(resp.get_data(as_text=True), path)


def test_chrome_relative_urls_survive_proxy_one_level_deep(authed):
    """/dispatch/lead/<id> is one directory down — the chrome must climb with
    ../ exactly once. This is the depth the pre-fix absolute URLs (and a naive
    flat-relative scheme) both break at."""
    path = "/dispatch/lead/%s" % LEAD_ID
    resp = authed.get(path)
    assert resp.status_code == 200
    _assert_chrome_inside_mount(resp.get_data(as_text=True), path)


def test_active_nav_still_marks_the_current_page(authed):
    """Relative hrefs must not have cost the active-state: matching stays on
    the server-visible literal path."""
    html = authed.get("/dispatch/leaderboard").get_data(as_text=True)
    assert re.search(r'<a href="[^"]*" class="active">Leaderboard</a>', html)
    assert html.count('class="active"') == 1

    html = authed.get("/dispatch/").get_data(as_text=True)
    assert re.search(r'<a href="[^"]*" class="active">Board</a>', html)
    assert html.count('class="active"') == 1

    # A page with no nav entry of its own marks nothing.
    html = authed.get("/dispatch/lead/%s" % LEAD_ID).get_data(as_text=True)
    assert html.count('class="active"') == 0


# ------------------------------------- dispatch.js: source tripwires (no node)


def _js_src():
    return _JS_PATH.read_text(encoding="utf-8")


def test_dispatch_js_rebases_every_attribute_carried_url():
    """rebase() was once dead code: defined for exactly the attribute-carried
    absolute URLs (data-autorefresh-url / data-parse-url — url_for-shaped) and
    then never called, so those fetches 404'd behind the /backend proxy. Every
    consumption site must stay wrapped."""
    src = _js_src()
    for attr in ("data-autorefresh-url", "data-parse-url"):
        gets = re.findall(r'[\w$]+\.getAttribute\("%s"\)' % attr, src)
        assert gets, "no consumption site for %s left in dispatch.js" % attr
        wrapped = re.findall(r'rebase\(\s*[\w$]+\.getAttribute\("%s"\)\s*\)' % attr, src)
        assert len(wrapped) == len(gets), (
            "%d of %d getAttribute(%r) site(s) are not wrapped in rebase() — "
            "that URL will be fetched verbatim and 404 behind the proxy"
            % (len(gets) - len(wrapped), len(gets), attr)
        )
    # The path-valued bare attribute form rebases too, and rebase() is alive:
    # its definition plus at least the three call sites above.
    assert "rebase(attrVal)" in src
    assert src.count("rebase(") >= 4, "rebase() looks like dead code again"


def test_dispatch_js_confirm_defers_to_inline_onsubmit():
    """lead.html's Strike form carries its own inline onsubmit confirm; the
    delegated handler must neither stack a second dialog on top of it nor ask
    about a submit the inline handler already cancelled (return false prevents
    default but does NOT stop propagation)."""
    src = _js_src()
    assert "if (e.defaultPrevented) return;" in src
    guard = re.search(r'!form\.hasAttribute\("onsubmit"\)', src)
    assert guard, "the pattern-default confirm lost its inline-onsubmit guard"
    assert guard.start() < src.index("(strike|exclude)"), (
        "the onsubmit guard must gate the strike/restore pattern defaults"
    )


def test_dispatch_js_never_swaps_in_rowless_markup():
    """applyPayload once exempted the empty string from the '<tr' sanity check,
    so a blank tbody_html wiped every row. Safe to require <tr always: a
    legitimately empty board still renders its 'No leads yet' placeholder row
    (pinned server-side in test_flows_dispatch)."""
    src = _js_src()
    assert 'html.indexOf("<tr") === -1' in src
    assert 'replace(/\\s/g, "") !== ""' not in src, (
        "the blank-string exemption is back — an empty fragment would blank "
        "the board again"
    )


def test_dispatch_js_skips_the_ok_false_envelope():
    """Belt-and-braces with board_data's 503: any 200 {ok:false} envelope must
    be treated as failure (stamp it, keep rows AND counters), never as a
    legitimate empty table."""
    src = _js_src()
    assert "payload.ok === false" in src


# --------------------------- dispatch.js: behavioral pins (real JS, stub DOM)


@pytest.fixture(scope="module")
def js_report():
    if not _NODE:
        pytest.skip("node not installed — the source tripwires above still hold")
    proc = subprocess.run(
        [_NODE, str(_HARNESS), str(_JS_PATH)],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, (
        "harness failed\nstdout: %s\nstderr: %s" % (proc.stdout, proc.stderr)
    )
    return json.loads(proc.stdout)


def test_js_board_poll_derives_the_proxy_aware_url(js_report):
    """board.html's exact markup (bare data-autorefresh + interval) must poll
    BASE + /data.json: /backend/dispatch/data.json behind the proxy, plain
    /dispatch/data.json at the bare mount."""
    board = js_report["board_proxy"]
    assert board["armed"] is True
    assert board["period"] == 10000
    assert board["cycles"][0]["url"] == "/backend/dispatch/data.json"
    assert js_report["board_root"]["url"] == "/dispatch/data.json"


def test_js_attribute_urls_are_rebased_before_fetching(js_report):
    """A data-autorefresh-url (or path-valued data-autorefresh) carrying the
    url_for-shaped absolute path must be remapped onto the public prefix."""
    assert js_report["attr_url"]["url"] == "/backend/dispatch/data.json"
    assert js_report["attr_path"]["url"] == "/backend/dispatch/data.json"


def test_js_parse_url_is_rebased_and_button_recovers(js_report):
    parse = js_report["parse"]
    assert parse["url"] == "/backend/dispatch/api/parse"
    assert parse["btn_disabled_after"] is False  # re-enabled after the round trip
    assert parse["btn_label_after"] == "Parse"


def test_js_poll_keeps_rows_and_counters_on_every_failure_shape(js_report):
    """The journey: one good payload applies, then ok:false envelope, blank
    fragment, login HTML, and HTTP 503 must each keep the last good rows and
    the last good counter — and the poll chain must survive all of it."""
    cycles = js_report["board_proxy"]["cycles"]
    good = "<tr data-row><td>fresh</td></tr>"

    assert cycles[0]["tbody"] == good, "a real fragment must still be applied"
    assert cycles[0]["total"] == "3"
    assert cycles[0]["stamp"] == "ok"

    for i, label in ((1, "ok:false envelope"), (2, "blank fragment"),
                     (3, "login HTML"), (4, "HTTP 503")):
        assert cycles[i]["tbody"] == good, (
            "%s wiped the board rows" % label
        )
        assert cycles[i]["total"] == "3", (
            "%s zeroed the total counter" % label
        )
    assert cycles[1]["stamp"] == "err"  # the envelope is stamped as a failure
    assert cycles[4]["stamp"] == "err"
    assert js_report["board_proxy"]["poll_alive"] is True


def test_js_confirm_matrix(js_report):
    m = js_report["confirm_matrix"]
    assert m["strike_with_inline"] == 0, (
        "dispatch.js stacked a second confirm on lead.html's inline one"
    )
    assert m["default_prevented"] == 0, (
        "a submit the inline confirm already cancelled was asked about again"
    )
    assert m["restore_plain"]["confirms"] == 1
    assert m["restore_plain"]["msg"] == "Restore this lead to the board?"
    assert m["cancel_prevents"]["confirms"] == 1
    assert m["cancel_prevents"]["prevented"] is True
    assert m["cancel_prevents"]["stopped"] is True
    assert m["explicit_data_confirm"]["confirms"] == 1
    assert m["explicit_data_confirm"]["msg"] == "Really strike?"
