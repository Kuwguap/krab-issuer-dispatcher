"""dispatch_web core — blueprint, auth, DB handle, registration.

Everything the view modules need lives here so they can `from .core import
bp, get_db, require_login` without touching admin_dashboard or each other.
The login page is inline (render_template_string) on purpose: core must work
before any other agent's templates/static exist.

Auth hardening (all of it lives here, none of it in the view modules):
- The session cookie is issued Secure + HttpOnly + SameSite=Lax (register()).
- Every non-GET request is refused when standard browser headers
  (Sec-Fetch-Site / Origin / Referer) positively identify it as cross-site —
  central CSRF cover for every mutation route, layered under any per-form
  token a view adds on top (settings' dw_csrf, newlead's idempotency key);
  GET /logout gets the same check because SameSite=Lax still lets a
  cross-site top-level GET navigation carry the cookie.
- Failed logins are throttled per source address with an escalating lockout.
  The counters are per-process: under gunicorn each worker holds its own, so
  the effective bound is N_workers x the limit — still bounded, but a
  WAF/proxy rate-limit in front remains a good idea.
- Sessions never sign with the raw SUPABASE_KEY. DISPATCH_WEB_SECRET is used
  verbatim when set; otherwise the signing key is DERIVED from
  SUPABASE_KEY + DISPATCH_WEB_PASSWORD (see _session_secret).
"""
import hashlib
import hmac
import logging
import math
import os
import threading
import time
from datetime import datetime
from functools import wraps
from urllib.parse import urlsplit

import pytz
from flask import (
    Blueprint,
    Response,
    jsonify,
    redirect,
    render_template_string,
    request,
    session,
    url_for,
)

# Import is safe (config only loads .env, never raises); CONSTRUCTION is not —
# create_client with missing env blows up, so the Database is built lazily.
from utils.database import Database

logger = logging.getLogger(__name__)

bp = Blueprint(
    "dispatch_web",
    __name__,
    url_prefix="/dispatch",
    template_folder="templates",
    static_folder="static",
)

_NY_TZ = pytz.timezone("America/New_York")

_db = None
_db_lock = threading.Lock()


def get_db():
    """Module-cached Database — the bot's own wrapper, same Supabase.

    Cached only on success: a failed construction (env missing, network down at
    boot) must not poison the process; the next request retries.
    """
    global _db
    if _db is None:
        with _db_lock:
            if _db is None:
                _db = Database()
    return _db


def _password():
    """DISPATCH_WEB_PASSWORD, read per-request so tests and late env land.

    Stripped: Render dashboard copy-paste adds whitespace (same scar as the
    OneTimeSecret keys in config.py). Empty after strip == unset == fail closed.
    """
    return (os.environ.get("DISPATCH_WEB_PASSWORD") or "").strip()


@bp.before_request
def _fail_closed():
    """No password configured → the whole blueprint is a 503, not an open door.

    Exempts exactly one endpoint: /dispatch/health, so Render and monitoring can
    see the service alive (and its config gap) without any auth.
    """
    if request.endpoint == "dispatch_web.health":
        return None
    if not _password():
        return Response("set DISPATCH_WEB_PASSWORD", status=503, mimetype="text/plain")
    return None


_SAFE_METHODS = frozenset(("GET", "HEAD", "OPTIONS"))


def _request_is_cross_site():
    """True only on positive browser evidence that this request came from
    another site — the CSRF case: a hostile page making the victim's browser
    attach the session cookie to a request it authored.

    Evidence, in trust order: Sec-Fetch-Site (browser-asserted, unforgeable by
    page script; every evergreen browser sends it), then the Origin/Referer
    hostname against the Host this request actually hit. Clients that send
    none of these (curl, the test client, monitors) are not browsers carrying
    an ambient cookie, so they pass — CSRF is a browser problem.
    """
    sfs = (request.headers.get("Sec-Fetch-Site") or "").strip().lower()
    if sfs in ("same-origin", "none"):  # "none" = user-typed / bookmark
        return False
    if sfs == "cross-site":
        return True
    # "same-site" (sibling subdomain) or a legacy browser: decide by hostname.
    source = request.headers.get("Origin") or request.headers.get("Referer")
    if not source:
        return False
    try:
        # urlsplit on "//host" keeps IPv6 brackets/ports straight for both.
        src_host = urlsplit(source).hostname or ""
        req_host = urlsplit("//" + (request.host or "")).hostname or ""
    except ValueError:
        return True  # malformed header: fail closed
    # Hostname-only compare on purpose: behind the tristatetags.com TLS proxy
    # the app sees http while the browser sends an https Origin. An Origin of
    # literal "null" parses to no hostname and lands here as cross-site.
    return src_host.lower() != req_host.lower() or not src_host


@bp.before_request
def _block_cross_site_writes():
    """Blueprint-wide CSRF gate: no cross-site request may mutate anything.

    Runs after _fail_closed (registration order), so the 503 still answers
    first when the password is unset. Covers every unsafe method on every
    /dispatch route — login and logout included — so even a mutation form
    with no per-form token of its own (strike/restore, roster toggles) has
    CSRF cover; views that mint their own token (settings, /new) get both.
    """
    if request.method in _SAFE_METHODS:
        return None
    if _request_is_cross_site():
        return Response("cross-site request refused", status=403, mimetype="text/plain")
    return None


def require_login(view):
    """Redirect to the login page unless this browser session already passed it."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        if session.get("dw_ok") is not True:
            # Preserve where they were headed (board filters live in the query
            # string); login re-validates before redirecting back.
            nxt = request.full_path if request.query_string else request.path
            return redirect(url_for("dispatch_web.login", next=nxt))
        return view(*args, **kwargs)

    return wrapped


def _safe_next(target):
    """Same-origin only: attacker-supplied ?next= must not bounce a fresh login
    off-site. Anything not rooted under /dispatch falls back to the board."""
    if (
        target
        and target.startswith("/dispatch")
        and "\\" not in target
        and "\r" not in target
        and "\n" not in target
    ):
        return target
    return "/dispatch/"


@bp.app_template_filter("fmt_ts")
def fmt_ts(value):
    """ISO timestamp → "Aug 27, 3:04 PM" in America/New_York.

    NY because every other surface in this repo (receipts, supervisory texts)
    shows NY time — a board that showed raw UTC would look 4-5 hours wrong.
    Hour is computed by hand: Windows strftime has no %-I. Unparseable input
    comes back as-is; a weird timestamp must not 500 a page.
    """
    if value is None or value == "":
        return ""
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return str(value)
    if dt.tzinfo is None:
        dt = pytz.utc.localize(dt)  # Supabase naive timestamps are UTC
    dt = dt.astimezone(_NY_TZ)
    hour = dt.hour % 12 or 12
    ampm = "AM" if dt.hour < 12 else "PM"
    return "{} {}, {}:{:02d} {}".format(dt.strftime("%b"), dt.day, hour, dt.minute, ampm)


@bp.route("/health")
def health():
    """Unauthenticated liveness probe — the one hole in the 503 gate."""
    return jsonify({"ok": True, "service": "dispatch-web"})


# ---------------------------------------------------------------------------
# Login throttle: the board is guarded by ONE shared password, so an untouched
# login endpoint is an unbounded online brute-force oracle. Failed attempts
# are counted per source address; past the threshold the address is locked out
# with an escalating (doubling, capped) delay, and while locked the submitted
# password is NOT even compared — a lockout that still evaluated guesses would
# bound nothing.

_THROTTLE_MAX_FAILURES = 5  # free tries; the Nth failure engages the lock
_THROTTLE_BASE_LOCK_SECONDS = 30.0  # first lock; doubles per further failure
_THROTTLE_MAX_LOCK_SECONDS = 900.0  # escalation ceiling (15 min)
_THROTTLE_FORGET_SECONDS = 3600.0  # idle, unlocked entries are forgotten
_THROTTLE_MAX_KEYS = 4096  # hard memory cap on distinct sources tracked

# key -> {"count": failures so far, "locked_until": _now() deadline, "seen": last activity}
_login_failures = {}
_throttle_lock = threading.Lock()


def _now():
    """Module seam so tests can warp time past a lockout."""
    return time.monotonic()


def _throttle_key():
    """remote_addr on purpose, never X-Forwarded-For: XFF's leading hops are
    attacker-typed, and a lock keyed on a spoofable value is dodged by rotating
    it. Behind the tristatetags.com proxy every browser can share the proxy's
    address, so a sustained brute force locks login for everyone behind it —
    fail closed by choice: the attacker stays bounded no matter what they send,
    and the legit dispatcher waiting out a lock is the alarm bell."""
    return request.remote_addr or "?"


def _login_retry_after(key):
    """Seconds this key must still wait, or 0 when it may try now."""
    with _throttle_lock:
        entry = _login_failures.get(key)
        if not entry:
            return 0.0
        return max(0.0, entry["locked_until"] - _now())


def _record_login_failure(key):
    now = _now()
    with _throttle_lock:
        if key not in _login_failures and len(_login_failures) >= _THROTTLE_MAX_KEYS:
            # Bound memory against wide-source floods: drop idle unlocked
            # entries first, then oldest-seen (a 4096-address botnet evicting
            # its own locks is the accepted trade for a bounded dict).
            cutoff = now - _THROTTLE_FORGET_SECONDS
            for stale in [
                k
                for k, v in _login_failures.items()
                if v["seen"] < cutoff and v["locked_until"] <= now
            ]:
                del _login_failures[stale]
            while len(_login_failures) >= _THROTTLE_MAX_KEYS:
                del _login_failures[min(_login_failures, key=lambda k: _login_failures[k]["seen"])]
        entry = _login_failures.setdefault(
            key, {"count": 0, "locked_until": 0.0, "seen": now}
        )
        entry["count"] += 1
        entry["seen"] = now
        over = entry["count"] - _THROTTLE_MAX_FAILURES
        if over >= 0:
            entry["locked_until"] = now + min(
                _THROTTLE_BASE_LOCK_SECONDS * (2.0 ** over), _THROTTLE_MAX_LOCK_SECONDS
            )
            # The address is never logged raw (the no-PII-in-logs contract); a
            # hash prefix still lets ops tell distinct sources apart in Sentry.
            logger.warning(
                "dispatch_web: login locked for source %s after %d failures (%.0fs)",
                hashlib.sha256(key.encode("utf-8")).hexdigest()[:12],
                entry["count"],
                entry["locked_until"] - now,
            )


def _clear_login_failures(key):
    with _throttle_lock:
        _login_failures.pop(key, None)


# Standalone by design: no base.html, no static/, so login renders even if every
# other agent's files are missing or broken.
_LOGIN_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Krab Dispatch &mdash; Sign in</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; margin: 0; }
  body { min-height: 100vh; display: flex; align-items: center; justify-content: center;
         background: #101418; color: #e8ecf1;
         font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
  form { background: #1a2028; border: 1px solid #2a323d; border-radius: 10px;
         padding: 32px 28px; width: 100%; max-width: 340px; }
  h1 { font-size: 18px; margin-bottom: 4px; }
  .sub { color: #8a96a3; font-size: 13px; margin-bottom: 20px; }
  .err { background: #3a1d20; border: 1px solid #6e2a30; color: #ffb4bb;
         border-radius: 6px; padding: 8px 10px; font-size: 13px; margin-bottom: 14px; }
  input[type=password] { width: 100%; padding: 10px 12px; border-radius: 6px;
         border: 1px solid #2f3945; background: #0d1116; color: inherit;
         font-size: 15px; margin-bottom: 14px; }
  input[type=password]:focus { outline: 2px solid #3b82f6; border-color: transparent; }
  button { width: 100%; padding: 10px; border: 0; border-radius: 6px;
           background: #2563eb; color: #fff; font-size: 15px; font-weight: 600;
           cursor: pointer; }
  button:hover { background: #1d4ed8; }
</style>
</head>
<body>
<form method="post" action="{{ url_for('dispatch_web.login') }}">
  <h1>Krab Dispatch</h1>
  <p class="sub">Enter the dispatch password to continue.</p>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
  <input type="password" name="password" placeholder="Password"
         autofocus autocomplete="current-password" required>
  {% if next_path %}<input type="hidden" name="next" value="{{ next_path }}">{% endif %}
  <button type="submit">Sign in</button>
</form>
</body>
</html>"""


@bp.route("/login", methods=["GET", "POST"])
def login():
    if session.get("dw_ok") is True:
        return redirect("/dispatch/")
    error = None
    next_path = _safe_next(request.values.get("next"))
    if request.method == "POST":
        key = _throttle_key()
        wait = _login_retry_after(key)
        if wait > 0:
            # Locked: refuse BEFORE comparing — even the right password waits,
            # otherwise the lock bounds nothing. 429 + Retry-After, and the
            # page says so in words for the human at the keyboard.
            retry_after = int(math.ceil(wait))
            body = render_template_string(
                _LOGIN_PAGE,
                error="Too many failed attempts. Try again in %d seconds." % retry_after,
                next_path=next_path if next_path != "/dispatch/" else None,
            )
            resp = Response(body, status=429, mimetype="text/html")
            resp.headers["Retry-After"] = str(retry_after)
            return resp
        submitted = request.form.get("password") or ""
        # compare_digest over bytes: constant-time, and str would TypeError on
        # non-ascii input. The submitted value is never logged — Sentry-safe.
        if hmac.compare_digest(submitted.encode("utf-8"), _password().encode("utf-8")):
            _clear_login_failures(key)
            session["dw_ok"] = True
            return redirect(next_path)
        _record_login_failure(key)
        error = "Wrong password."
    body = render_template_string(
        _LOGIN_PAGE,
        error=error,
        # Only carry a next that survived validation; the default round-trips as
        # no hidden field at all.
        next_path=next_path if next_path != "/dispatch/" else None,
    )
    return (body, 401) if error else body


@bp.route("/logout", methods=["GET", "POST"])
def logout():
    # GET stays supported — base.html links logout as a plain <a> — but
    # SameSite=Lax still sends the cookie on a cross-site TOP-LEVEL GET
    # navigation, so a hostile page could log the dispatcher out mid-shift.
    # On positive cross-site evidence the session is kept and the browser is
    # bounced to the board; cross-site POSTs are already 403'd by the gate.
    if request.method == "GET" and _request_is_cross_site():
        return redirect("/dispatch/")
    session.pop("dw_ok", None)
    return redirect(url_for("dispatch_web.login"))


def _session_secret():
    """The session-signing key. DISPATCH_WEB_SECRET verbatim when set.

    The fallback is DERIVED — HMAC-SHA256 keyed by SUPABASE_KEY over a fixed
    label plus DISPATCH_WEB_PASSWORD — never the raw SUPABASE_KEY: that key is
    pasted into Render, Vercel, the bot AND the dashboard, so signing with it
    verbatim let any key-holder mint the {"dw_ok": true} cookie and walk past
    the password gate entirely. The derived key is useless without ALSO
    knowing the password, is deterministic (every gunicorn worker agrees), and
    rotating the password rotates the signing key — which force-expires every
    outstanding session, the revocation the old fallback lacked. Read at
    register() time; on Render an env change restarts the process, so it
    cannot go stale. The ERROR is loud on purpose (it reaches Sentry): ops
    should still set a dedicated DISPATCH_WEB_SECRET.
    """
    secret = (os.environ.get("DISPATCH_WEB_SECRET") or "").strip()
    if secret:
        return secret
    supabase_key = (os.environ.get("SUPABASE_KEY") or "").strip()
    if not supabase_key:
        # Parity with the old fallback: the host refuses to boot without
        # SUPABASE_KEY, so this leg only runs in stripped-down harnesses.
        return None
    logger.error(
        "dispatch_web: DISPATCH_WEB_SECRET is not set — deriving the session "
        "signing key from SUPABASE_KEY + DISPATCH_WEB_PASSWORD. Set a dedicated "
        "DISPATCH_WEB_SECRET so sessions do not hang off the database credential."
    )
    return hmac.new(
        supabase_key.encode("utf-8"),
        b"dispatch-web-session-signing\x00" + _password().encode("utf-8"),
        hashlib.sha256,
    ).digest()


def register(app):
    """Mount the blueprint on the host Flask app (admin_dashboard's).

    Idempotent: a second call is a no-op, not a Flask "already registered" crash.
    secret_key is only filled if the host app has none — see _session_secret for
    why the fallback is derived rather than SUPABASE_KEY verbatim.

    Cookie hardening is applied app-wide: the dashboard itself issues no
    session cookies (it never sets a secret_key), so the only session on this
    app is ours — a full replayable credential that must never transmit over a
    cleartext hop (Secure), never be script-readable (HttpOnly, Flask's
    default made explicit), and never ride a cross-site subrequest (an
    EXPLICIT SameSite instead of trusting browser defaults). Secure/HttpOnly
    are upgrades and unconditional; SameSite only fills the gap — a host that
    already chose a policy (e.g. Strict) keeps it, because Flask's default is
    None-the-value ("emit nothing"), so any configured string is a deliberate
    choice. Lax, not Strict, as our default: the login redirect back from an
    external link still works, and the cross-site gate above covers the
    top-level-GET hole Lax leaves open.
    """
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=True,
    )
    if not app.config.get("SESSION_COOKIE_SAMESITE"):
        app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    if not app.secret_key:
        app.secret_key = _session_secret()
    if "dispatch_web" not in app.blueprints:
        app.register_blueprint(bp)
