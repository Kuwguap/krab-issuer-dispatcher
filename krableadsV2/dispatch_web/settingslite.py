"""Settings-lite: the one switch the web mirror may flip, plus read-only truth.

Writable here is only ``instant_all_drivers`` — the same supervisory switch the
bot's ⚡ Instant Tag screen toggles, stored as "1"/"0" via set_setting. Plate
counters are display-only (the bot's photo-driven update flow owns them), and
the env section is presence booleans because key values must never leave the
process — not into HTML, not into logs, not into Sentry.

Because this module owns the blueprint's one state-changing switch, the
anti-CSRF hardening lives here too: a ``record_once`` below stamps
SameSite=Lax onto the host app's session cookie (covers every dispatch_web
POST — Flask's default emits the dw_ok cookie with no SameSite attribute at
all), and the toggle POST additionally requires a per-session token so a
cross-site page can never flip the all-drivers broadcast for a logged-in
dispatcher, even on browsers that don't default cookies to Lax.
"""

import hmac
import os
import re
import secrets

from flask import redirect, render_template, request, session, url_for

from .core import bp, get_db, require_login


def _harden_session_cookie(state):
    """App-level: emit the session cookie with SameSite=Lax.

    Runs when core.register() mounts the blueprint (settingslite is imported
    by __init__ before any registration, so the record is always attached).
    Respects a host app that already chose a real value (e.g. "Strict") —
    Flask's default config carries the key as None, which is what we fix.
    """
    if not state.app.config.get("SESSION_COOKIE_SAMESITE"):
        state.app.config["SESSION_COOKIE_SAMESITE"] = "Lax"


bp.record_once(_harden_session_cookie)


def _csrf_token() -> str:
    """Per-session anti-CSRF token, minted on first settings render.

    Lives in the signed session cookie (next to dw_ok) and is echoed as a
    hidden field the toggle POST must return. Per-session, not per-request,
    so the form's stale-double-submit idempotency story is unchanged.
    """
    tok = session.get("dw_csrf")
    if not isinstance(tok, str) or not tok:
        tok = secrets.token_urlsafe(32)
        session["dw_csrf"] = tok
    return tok


def _csrf_ok() -> bool:
    """True only when the form returned this session's token. Fail closed:
    no session token yet (page never rendered here) refuses too. Bytes
    compare — compare_digest on str TypeErrors on non-ascii input (the same
    scar core.login guards against), and an attacker picks the form value."""
    want = session.get("dw_csrf")
    got = request.form.get("csrf") or ""
    if not isinstance(want, str) or not want:
        return False
    return hmac.compare_digest(want.encode("utf-8"), got.encode("utf-8"))

# Mirror bot._instant_all_drivers_enabled's parse without importing bot
# (importing bot boots the whole Telegram side).
_TRUTHY = ("1", "true", "on", "yes")

# Presence-only env sanity, grouped by the feature each key unlocks.
_ENV_GROUPS = (
    ("Resend — FS-20 card email", ("RESEND_API_KEY", "RESEND_FROM")),
    ("TriStateCoverage portal", ("INTEGRATIONS_API_KEY",)),
    ("Stripe — $100 instant PDF", ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET")),
)


def _env_present(name: str) -> bool:
    # .lstrip("=") mirrors config.py: a pasted "=sk_..." still counts as set.
    return bool((os.getenv(name) or "").strip().lstrip("="))


def _instant_on(db) -> bool:
    return str(db.get_setting("instant_all_drivers") or "").strip().lower() in _TRUTHY


def _plate_display(number, resident, prefix, suffix, width):
    """"209861" -> "H209861" / "209861V" — the number as printed on the tag."""
    digits = re.sub(r"\D", "", str(number or ""))
    if not digits:
        return "—"
    digits = digits.zfill(width)
    return f"{prefix}{digits}" if resident else f"{digits}{suffix}"


@bp.route("/settings")
@require_login
def settings():
    errors = []
    instant_on = False
    plates = None

    db = None
    try:
        db = get_db()
    except Exception:
        errors.append("Database unavailable — settings could not be loaded.")

    if db is not None:
        try:
            instant_on = _instant_on(db)
        except Exception:
            errors.append("Could not read the Instant Tag switch.")
        try:
            # Returns None on any DB trouble (helper swallows internally too).
            plates = db.get_plate_settings()
        except Exception:
            plates = None
        if plates is None:
            errors.append("Plate counters could not be loaded.")

    plate_rows = []
    if plates:
        pre = str(plates.get("nj_plate_prefix") or "H")
        suf = str(plates.get("non_nj_plate_suffix") or "V")
        try:
            width = int(plates.get("plate_digits") or 6)
        except (TypeError, ValueError):
            width = 6

        def _raw(key):
            v = plates.get(key)
            return "—" if v in (None, "") else str(v)

        plate_rows = [
            (f"Resident plate ({pre}{'#' * width}) next",
             _plate_display(plates.get("nj_plate_next_number"), True, pre, suf, width)),
            (f"Non-Resident plate ({'#' * width}{suf}) next",
             _plate_display(plates.get("non_nj_plate_next_number"), False, pre, suf, width)),
            ("Resident car counter", _raw("nj_car_next_number")),
            ("Non-Resident car counter", _raw("non_nj_car_next_number")),
        ]

    env_groups = [
        (label, [(name, _env_present(name)) for name in names])
        for label, names in _ENV_GROUPS
    ]

    toggle = request.args.get("toggle")
    notice = None
    if toggle == "ok":
        notice = "Saved. The bot reads this switch live — no restart needed."
    elif toggle == "fail":
        errors.append("Could not save the Instant Tag switch. Try again.")
    elif toggle == "csrf":
        errors.append(
            "Security check failed — the switch was NOT changed. "
            "This page has a fresh token now; flip it again."
        )

    return render_template(
        "dispatch/settings.html",
        errors=errors,
        notice=notice,
        instant_on=instant_on,
        plate_rows=plate_rows,
        env_groups=env_groups,
        csrf_token=_csrf_token(),
    )


@bp.route("/settings/instant-all", methods=["POST"])
@require_login
def settings_instant_all():
    """Flip the all-drivers broadcast switch, exactly as the bot's button does.

    The form carries the state it wants ("1"/"0" from the page it was rendered
    on) so a stale double-submit lands on the same value instead of flipping
    twice; with no value we blind-flip like tset_itag_all. Before anything
    else it must return this session's CSRF token — this switch makes the
    LIVE bot broadcast Stripe links to every driver, so a cross-site POST
    must die here, before the DB is touched.
    """
    if not _csrf_ok():
        return redirect(url_for("dispatch_web.settings", toggle="csrf"))
    want_raw = (request.form.get("value") or "").strip()
    ok = False
    try:
        db = get_db()
        if want_raw in ("0", "1"):
            want = want_raw == "1"
        else:
            want = not _instant_on(db)
        ok = bool(db.set_setting("instant_all_drivers", "1" if want else "0"))
    except Exception:
        ok = False
    return redirect(url_for("dispatch_web.settings", toggle="ok" if ok else "fail"))
