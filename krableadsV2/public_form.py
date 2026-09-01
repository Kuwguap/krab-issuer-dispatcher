"""tristatetags.com/form — the client fills this in themselves.

A link you can send to a customer. No login, no payment, no price: they type
their own details, and on submit the lead goes out to every dispatcher group
AND every active driver at once.

It lives here, on the admin service, because this is where the database and the
bot's ingest already are. tristatetags.com/form reaches it through the same
Vercel rewrite /receipts uses.

The lead it creates is deliberately the SAME shape dispatch_web/newlead.py
creates -- the 11-line vehicle blob, the OTS-wrapped phone, ingest_dispatch_pending
for the bot's poll to find. Nothing about the dispatch is re-implemented here;
this module's whole job is to turn a stranger's typing into that row safely.

Being public is the difference that matters. Anyone on the internet can POST
here, so every field is bounded, the phone and VIN must be real, and a honeypot,
a signed one-shot nonce and a per-address rate limit stand between a bored
script and the dispatch groups' notifications.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import string
import time

from flask import jsonify, redirect, render_template_string, request

from utils import external_lead_parser
from utils.database import record_is_active
from utils.lead_validation import normalize_phone
from utils.onetimesecret import OneTimeSecret

logger = logging.getLogger(__name__)

# The lead column value that marks a form submission, read by the bot's poll.
PUBLIC_FORM_SOURCE = "Client Form"

# The lead's 11-line vehicle blob, in the order the bot's cards read it.
_BLOB_KEYS = (
    "name", "address", "city_state_zip", "delivery_address",
    "delivery_city_state_zip", "vin", "car", "color",
    "insurance_company", "insurance_policy_number", "extra_info",
)

# What the form itself collects, and whether it is required.
FIELDS = (
    ("first_name",                "\U0001f464 First name",                   True),
    ("last_name",                 "\U0001f464 Last name",                    True),
    ("phone",                     "\U0001f4de Phone",                        True),
    ("email",                     "✉️ Email",                      True),
    ("address",                   "\U0001f3e0 Registration address",         True),
    ("city_state_zip",            "\U0001f3e0 Registration city, state, ZIP", True),
    ("delivery_address",          "\U0001f4cd Delivery address",             True),
    ("delivery_city_state_zip",   "\U0001f4cd Delivery city, state, ZIP",    True),
    ("vin",                       "\U0001f522 VIN",                          True),
    ("car",                       "\U0001f698 Car",                          True),
    ("color",                     "\U0001f3a8 Color",                        True),
    ("insurance_company",         "\U0001f6e1 Insurance company",            False),
    ("insurance_policy_number",   "\U0001f6e1 Insurance policy #",           False),
    ("extra_info",                "\U0001f552 Delivery Date/Time & Notes",   False),
)
_FIELD_NAMES = tuple(f[0] for f in FIELDS)

# One value can never be longer than this. A public endpoint has to bound its
# input somewhere, and the bot renders every one of these into a Telegram card.
_MAX_LEN = 200
_MAX_NOTES = 500

# A form nonce is good for this long. Long enough to fill the thing in on a
# phone, short enough that a harvested one is not a lasting replay ticket.
_NONCE_TTL_S = 3 * 60 * 60
# Nothing genuine is typed this fast; a bot posting the moment it loads is.
_MIN_FILL_SECONDS = 3

# addr -> [timestamps]. In memory on purpose: one worker, and the cost of
# forgetting on restart is a handful of extra submissions, not a breach.
_RECENT: dict = {}
_RATE_MAX = 5
_RATE_WINDOW_S = 600


def _secret() -> bytes:
    """Signs the form nonce. Falls back to a per-process value.

    A restart then invalidates open forms, which costs a resubmit -- the
    alternative, an unsigned nonce, costs nothing to forge.
    """
    raw = (os.getenv("PUBLIC_FORM_SECRET")
           or os.getenv("RECEIPT_LINK_SECRET")
           or os.getenv("SUPABASE_KEY") or "").strip()
    if not raw:
        global _EPHEMERAL_SECRET
        try:
            raw = _EPHEMERAL_SECRET
        except NameError:
            raw = _EPHEMERAL_SECRET = secrets.token_hex(32)
    return raw.encode("utf-8")


def _mint_nonce() -> str:
    ts = str(int(time.time()))
    sig = hmac.new(_secret(), ts.encode("ascii"), hashlib.sha256).hexdigest()[:32]
    return f"{ts}.{sig}"


def _nonce_age(nonce: str):
    """Seconds since this nonce was minted, or None if it is not ours."""
    ts, _, sig = str(nonce or "").partition(".")
    if not ts.isdigit() or not sig:
        return None
    want = hmac.new(_secret(), ts.encode("ascii"), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(want, sig):
        return None
    age = time.time() - int(ts)
    return age if 0 <= age <= _NONCE_TTL_S else None


def _caller() -> str:
    fwd = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    return fwd or (request.remote_addr or "?")


def _rate_limited() -> bool:
    now = time.time()
    who = _caller()
    seen = [t for t in _RECENT.get(who, []) if now - t < _RATE_WINDOW_S]
    if len(_RECENT) > 2000:
        _RECENT.clear()                    # crude, bounded, and never a leak
    _RECENT[who] = seen
    return len(seen) >= _RATE_MAX


def _note_submission() -> None:
    _RECENT.setdefault(_caller(), []).append(time.time())


def _clean(value, limit: int = _MAX_LEN) -> str:
    """One line of user input: trimmed, length-bounded, control chars gone."""
    s = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    s = "".join(ch for ch in s if ch == " " or ch.isprintable())
    return s[:limit].strip()


def _dash(v: str) -> str:
    # The bot's cards join these lines verbatim, so an empty value must be "-".
    return v if v else "-"


def _reference_id() -> str:
    return "".join(secrets.choice(string.ascii_uppercase + string.digits)
                   for _ in range(8))


def _ots_wrap(phone: str):
    """The same OTS policy as every other lead source (see newlead._ots_wrap):
    wrap when the service answers, otherwise store the raw phone, which every
    bot renderer already handles. Never None -- that shape prints a blank
    phone line to the teams."""
    try:
        ots = OneTimeSecret()
        enc = ots.encrypt_phone(phone)
        err = (getattr(ots, "last_error", "") or "Phone encryption skipped").strip()
    except Exception as e:
        enc, err = None, type(e).__name__
    if enc:
        return enc.get("secret_key"), enc.get("metadata_key"), enc.get("link")
    logger.warning("public form: skipping phone encryption — %s", err)
    return None, None, phone


def _entrant_user_id() -> int:
    try:
        return int(os.environ.get("DISPATCH_WEB_USER_ID") or 0)
    except (TypeError, ValueError):
        return 0


def register(app, db_provider):
    """Mount /form. ``db_provider`` is resolved per request, like instant_pdf."""
    _resolve = db_provider if callable(db_provider) else (lambda: db_provider)

    def _render(values: dict, errors: list, *, status: int = 200):
        return render_template_string(
            _FORM_PAGE,
            fields=FIELDS,
            values=values,
            errors=errors,
            nonce=_mint_nonce(),
            max_len=_MAX_LEN,
            max_notes=_MAX_NOTES,
        ), status

    @app.route("/form", methods=["GET"])
    def public_form():
        return _render({name: "" for name in _FIELD_NAMES}, [])

    @app.route("/form", methods=["POST"])
    def public_form_submit():
        form = request.form
        values = {name: _clean(form.get(name),
                               _MAX_NOTES if name == "extra_info" else _MAX_LEN)
                  for name in _FIELD_NAMES}
        wants_insurance = bool(form.get("wants_insurance"))
        if wants_insurance:
            # The checkbox disables those two inputs in the browser, so they
            # arrive empty anyway -- but a POST is not a browser, and we are the
            # ones covering the car.
            values["insurance_company"] = ""
            values["insurance_policy_number"] = ""

        # Silent refusals first: anything that says "not a person" gets the same
        # bland answer, so a script learns nothing about which check caught it.
        if _clean(form.get("website")):                       # honeypot
            logger.info("public form: honeypot tripped from %s", _caller())
            return redirect("/form/thanks", code=303)
        age = _nonce_age(form.get("form_nonce"))
        if age is None:
            return _render(values, ["This form expired before it was sent. "
                                    "Please check the details and submit again."],
                           status=400)
        if age < _MIN_FILL_SECONDS:
            logger.info("public form: submitted in %.1fs from %s", age, _caller())
            return redirect("/form/thanks", code=303)
        if _rate_limited():
            logger.warning("public form: rate limited %s", _caller())
            return _render(values, ["Too many submissions from this connection. "
                                    "Please wait a few minutes and try again."],
                           status=429)

        errors: list = []
        for name, label, required in FIELDS:
            if required and not values[name]:
                errors.append(f"{label.split(' ', 1)[-1]} is required")

        phone = normalize_phone(values["phone"])
        if values["phone"] and not phone:
            errors.append("Phone must be a 9-10 digit US number")

        email = values["email"]
        if email and not re.match(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$", email):
            errors.append("That email address does not look right")

        vin_match = external_lead_parser.VIN_PATTERN.search(values["vin"] or "")
        if values["vin"] and not vin_match:
            errors.append("VIN must be the full 17 characters")

        if errors:
            return _render(values, errors, status=400)

        state = {k: _dash(v) for k, v in values.items() if k in _BLOB_KEYS}
        state["name"] = _dash(f"{values['first_name']} {values['last_name']}".strip())
        state["vin"] = vin_match.group(0).upper()

        reference_id = _reference_id()
        ots_token, ots_meta, ots_link = _ots_wrap(phone)

        try:
            groups = _resolve().get_all_groups() or []
        except Exception as e:
            logger.error("public form: could not read the groups: %s", type(e).__name__)
            groups = []
        active = [g for g in groups if record_is_active(g)]
        if not active:
            logger.error("public form: a client submitted %s but there are no "
                         "active dispatchers to send it to", reference_id)
            return _render(values, ["We could not accept this right now. "
                                    "Please call us and we will take the details."],
                           status=503)

        payload = {
            "user_id": _entrant_user_id(),
            "telegram_username": "tristatetags.com/form",
            "telegram_name": "Client form",
            "vehicle_details": external_lead_parser.build_vehicle_details_11(state),
            "delivery_details": external_lead_parser.build_delivery_details(state),
            "phone_number": phone,
            # No payment on this form, so no price is collected. The bot renders
            # a missing price as blank; it must never be invented here.
            "price": "",
            "onetimesecret_token": ots_token,
            "onetimesecret_secret_key": ots_meta,
            "encrypted_link": ots_link,
            "reference_id": reference_id,
            "group_id": active[0].get("id"),
            "extra_info": values["extra_info"] or "",
            "special_request_issuers": "",
            "special_request_drivers": "",
            "special_request_note": "",
            "email": values["email"] or None,
            # Truthy on purpose: the bot detects "website lead -> fan out to
            # drivers" solely by external_order_id, and a None here drops the
            # lead into the issuer-DM branch, which has no issuer to DM.
            "external_order_id": reference_id,
            # What tells the bot's poll to send this to the DRIVERS as well as
            # the groups, without a new column to migrate.
            "contact_info_source": PUBLIC_FORM_SOURCE,
            "ingest_dispatch_pending": True,
            "awaiting_group_accept": True,
            "wants_insurance": wants_insurance,
        }

        try:
            lead = _resolve().create_lead(payload)
        except Exception as e:
            logger.error("public form: create_lead raised %s", type(e).__name__)
            lead = None
        if not lead:
            return _render(values, ["Something went wrong saving your details. "
                                    "Please try again in a moment."], status=502)

        _note_submission()
        logger.info("public form: %s created from %s (insurance opt-in: %s)",
                    reference_id, _caller(), wants_insurance)
        return redirect(f"/form/thanks?ref={reference_id}", code=303)

    @app.route("/form/thanks", methods=["GET"])
    def public_form_thanks():
        ref = _clean(request.args.get("ref"), 16)
        return render_template_string(_THANKS_PAGE, reference_id=ref)

    logger.info("Public client form mounted at /form")


_STYLE = """
 :root{color-scheme:light dark;--bg:#f4f6f8;--card:#fff;--ink:#12161c;--muted:#6b7280;
       --line:#dfe3e8;--accent:#2f6df6;--bad:#c0392b;--good:#0a7a4f}
 @media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#171a21;--ink:#e8eaed;
       --muted:#8b93a7;--line:#2a2f3a;--bad:#ff8a7a;--good:#5fd08a}}
 *{box-sizing:border-box}
 body{font:16px/1.5 -apple-system,system-ui,Segoe UI,Roboto,sans-serif;margin:0;
      padding:24px 16px 56px;background:var(--bg);color:var(--ink)}
 .wrap{max-width:34rem;margin:0 auto}
 .card{background:var(--card);border-radius:16px;padding:24px 20px;
       box-shadow:0 6px 24px rgba(16,24,40,.08)}
 h1{font-size:1.4rem;margin:0 0 4px;letter-spacing:-.01em}
 .sub{color:var(--muted);margin:0 0 22px;font-size:.95rem}
 label{display:block;margin:14px 0 5px;font-weight:600;font-size:.92rem}
 .req{color:var(--bad)}
 input[type=text],input[type=tel],input[type=email],textarea{
      width:100%;padding:12px 13px;border:1px solid var(--line);border-radius:10px;
      background:transparent;color:var(--ink);font:inherit}
 input:focus,textarea:focus{outline:2px solid var(--accent);outline-offset:1px;
      border-color:transparent}
 input:disabled{opacity:.45;cursor:not-allowed}
 textarea{min-height:78px;resize:vertical}
 .optin{display:flex;gap:10px;align-items:flex-start;margin:22px 0 4px;
        padding:13px 14px;border:1px solid var(--line);border-radius:10px}
 .optin input{margin-top:3px;width:18px;height:18px;flex:0 0 auto}
 .optin span{font-size:.93rem}
 .optin small{display:block;color:var(--muted);margin-top:3px}
 button{width:100%;margin-top:24px;padding:15px;border:0;border-radius:11px;
        background:var(--accent);color:#fff;font-weight:650;font-size:1rem;cursor:pointer}
 button:hover{filter:brightness(1.06)}
 .errors{border:1px solid var(--bad);border-radius:10px;padding:12px 14px;margin:0 0 18px}
 .errors p{margin:0 0 6px;font-weight:650;color:var(--bad)}
 .errors ul{margin:0;padding-left:18px}
 .hp{position:absolute;left:-9999px;width:1px;height:1px;overflow:hidden}
 .tick{width:70px;height:70px;border-radius:50%;margin:0 auto 16px;background:#e7f7ef;
       color:var(--good);font-size:36px;line-height:70px;text-align:center}
 @media (prefers-color-scheme:dark){.tick{background:#12301f}}
 .ref{font-family:ui-monospace,SFMono-Regular,monospace;font-size:1.25rem;
      font-weight:700;letter-spacing:.06em}
"""


_FORM_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>Temp Tag Request — Tri State Tags</title>
<style>""" + _STYLE + """</style></head><body>
<div class="wrap"><div class="card">
  <h1>Temp Tag Request</h1>
  <p class="sub">Fill this in and we will take it from here. No payment is taken
     on this page. Fields marked <span class="req">*</span> are required.</p>

  {% if errors %}
  <div class="errors">
    <p>Please check the following:</p>
    <ul>{% for e in errors %}<li>{{ e }}</li>{% endfor %}</ul>
  </div>
  {% endif %}

  <form method="post" action="/form" novalidate>
    <input type="hidden" name="form_nonce" value="{{ nonce }}">
    <div class="hp" aria-hidden="true">
      <label for="website">Website</label>
      <input type="text" id="website" name="website" tabindex="-1" autocomplete="off">
    </div>

    {% for name, label, required in fields %}
      {% if name == 'insurance_company' %}
      <div class="optin">
        <input type="checkbox" id="wants_insurance" name="wants_insurance"
               {% if values.get('wants_insurance') %}checked{% endif %}>
        <span><strong>Opt in for insurance</strong>
          <small>Tick this and we will arrange the insurance for you — leave the
                 two boxes below alone.</small></span>
      </div>
      {% endif %}

      {% if name == 'extra_info' %}
      <label for="{{ name }}">{{ label }}</label>
      <textarea id="{{ name }}" name="{{ name }}" maxlength="{{ max_notes }}"
                >{{ values.get(name, '') }}</textarea>
      {% else %}
      <label for="{{ name }}">{{ label }}{% if required %} <span class="req">*</span>{% endif %}</label>
      <input id="{{ name }}" name="{{ name }}" maxlength="{{ max_len }}"
             value="{{ values.get(name, '') }}"
             {% if name == 'phone' %}type="tel" inputmode="tel" autocomplete="tel"
             {% elif name == 'email' %}type="email" inputmode="email" autocomplete="email"
             {% else %}type="text"{% endif %}
             {% if required %}required{% endif %}
             {% if name in ('insurance_company','insurance_policy_number') %}
               data-insurance="1"{% endif %}>
      {% endif %}
    {% endfor %}

    <button type="submit">Submit request</button>
  </form>
</div></div>
<script>
  // The opt-in owns the two insurance boxes: if we are arranging the cover,
  // there is nothing for the client to tell us about their own.
  (function () {
    var box = document.getElementById('wants_insurance');
    var owned = document.querySelectorAll('[data-insurance]');
    function sync() {
      for (var i = 0; i < owned.length; i++) {
        owned[i].disabled = box.checked;
        if (box.checked) { owned[i].value = ''; }
      }
    }
    if (box) { box.addEventListener('change', sync); sync(); }
  })();
</script>
</body></html>"""


_THANKS_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>Request received — Tri State Tags</title>
<style>""" + _STYLE + """</style></head><body>
<div class="wrap"><div class="card" style="text-align:center">
  <div class="tick">&#10003;</div>
  <h1>Thank you — we have your details</h1>
  <p class="sub">Your request is with our dispatch team now. Someone will be in
     touch shortly on the number you gave us.</p>
  {% if reference_id %}
  <p style="margin:0 0 4px;color:var(--muted);font-size:.9rem">Your reference</p>
  <p class="ref">{{ reference_id }}</p>
  <p class="sub" style="margin-top:14px">Keep this reference — quote it if you
     call us about this request.</p>
  {% endif %}
</div></div>
</body></html>"""
