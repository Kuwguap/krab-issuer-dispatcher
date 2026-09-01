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
            heading="Request your temp tag",
            standfirst=("Fill this in and we will take it from here. "
                        "No payment is taken on this page."),
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
        return render_template_string(
            _THANKS_PAGE, reference_id=ref,
            heading="Thank you",
            standfirst="Your request is on its way to our dispatch team.")

    logger.info("Public client form mounted at /form")


# The site's own tokens, copied from speedy-tags-main/src/index.css. This page
# is Flask-rendered and cannot import the React bundle's stylesheet, so the
# values live here -- if the site's palette is ever retuned, retune these too.
_STYLE = """
 /* Light only, like the rest of the site: the app defines its dark palette
    under a .dark CLASS (tailwind darkMode:["class"]) and nothing ever adds it,
    so honouring prefers-color-scheme made /form the one dark page a client
    ever saw. */
 :root{
   color-scheme:light;
   --background:220 14% 96%; --foreground:222 47% 11%;
   --card:0 0% 100%; --card-foreground:222 47% 11%;
   --primary:172 66% 38%; --primary-foreground:0 0% 100%;
   --muted:220 14% 92%; --muted-foreground:220 9% 46%;
   --accent:172 66% 96%; --accent-foreground:172 66% 32%;
   --destructive:0 84% 60%; --border:220 13% 91%; --input:220 13% 91%;
   --ring:172 66% 38%; --radius:0.75rem;
   --success:152 60% 42%;
   --hero-gradient:linear-gradient(135deg,hsl(172 66% 38% / 0.9),hsl(222 47% 11% / 0.85));
   --card-shadow:0 1px 3px 0 rgb(0 0 0 / .04),0 1px 2px -1px rgb(0 0 0 / .04);
   --card-shadow-hover:0 20px 40px -12px hsl(172 66% 38% / .15),0 8px 16px -8px rgb(0 0 0 / .08);
 }
 *{box-sizing:border-box}
 html{-webkit-text-size-adjust:100%}
 body{margin:0;background:hsl(var(--background));color:hsl(var(--foreground));
      font-family:"DM Sans",system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
      font-size:16px;line-height:1.55;-webkit-font-smoothing:antialiased}
 h1,h2,.brand{font-family:"Outfit","DM Sans",system-ui,sans-serif}

 /* The site's hero band, so the page opens the way every other page does. */
 .hero{background:var(--hero-gradient);color:#fff;padding:26px 20px 30px}
 .hero-in{max-width:38rem;margin:0 auto}
 .brand{display:flex;align-items:center;gap:9px;font-weight:700;font-size:1.05rem;
        letter-spacing:-.01em}
 .brand .dot{width:9px;height:9px;border-radius:50%;background:#fff;opacity:.9;
        flex:0 0 auto}
 .brand small{display:block;font-weight:500;font-size:.72rem;opacity:.82;
        letter-spacing:.02em;font-family:"DM Sans",sans-serif}
 .hero h1{margin:18px 0 6px;font-size:1.55rem;line-height:1.2;letter-spacing:-.02em}
 .hero p{margin:0;opacity:.9;font-size:.95rem;max-width:30rem}

 .wrap{max-width:38rem;margin:-18px auto 0;padding:0 16px 56px}
 .card{background:hsl(var(--card));color:hsl(var(--card-foreground));
       border:1px solid hsl(var(--border));border-radius:var(--radius);
       padding:26px 22px;box-shadow:var(--card-shadow-hover)}

 .legend{font-size:.75rem;font-weight:700;letter-spacing:.09em;text-transform:uppercase;
         color:hsl(var(--muted-foreground));margin:26px 0 2px}
 .legend:first-of-type{margin-top:4px}

 label{display:block;margin:15px 0 6px;font-weight:600;font-size:.9rem}
 .req{color:hsl(var(--destructive))}
 input[type=text],input[type=tel],input[type=email],textarea{
      width:100%;padding:12px 14px;border:1px solid hsl(var(--input));
      border-radius:calc(var(--radius) - 3px);background:hsl(var(--background));
      color:hsl(var(--foreground));font:inherit;transition:border-color .15s,box-shadow .15s}
 input::placeholder,textarea::placeholder{color:hsl(var(--muted-foreground));opacity:.75}
 input:focus,textarea:focus{outline:none;border-color:hsl(var(--ring));
      box-shadow:0 0 0 3px hsl(var(--ring) / .18)}
 input:disabled{opacity:.45;cursor:not-allowed;background:hsl(var(--muted))}
 textarea{min-height:84px;resize:vertical}

 .optin{display:flex;gap:11px;align-items:flex-start;margin:24px 0 2px;
        padding:14px 15px;border:1px solid hsl(var(--border));
        border-radius:calc(var(--radius) - 3px);background:hsl(var(--accent));
        color:hsl(var(--accent-foreground));cursor:pointer}
 .optin input{margin-top:2px;width:18px;height:18px;flex:0 0 auto;
        accent-color:hsl(var(--primary));cursor:pointer}
 .optin strong{font-weight:700}
 .optin small{display:block;margin-top:3px;opacity:.85;line-height:1.45}

 button{width:100%;margin-top:26px;padding:15px;border:0;
        border-radius:calc(var(--radius) - 3px);background:hsl(var(--primary));
        color:hsl(var(--primary-foreground));font:inherit;font-weight:700;
        font-size:1rem;cursor:pointer;transition:filter .15s,transform .06s}
 button:hover{filter:brightness(1.07)}
 button:active{transform:translateY(1px)}

 .errors{border:1px solid hsl(var(--destructive));background:hsl(var(--destructive) / .07);
         border-radius:calc(var(--radius) - 3px);padding:13px 15px;margin:0 0 20px}
 .errors p{margin:0 0 6px;font-weight:700;color:hsl(var(--destructive))}
 .errors ul{margin:0;padding-left:19px}
 .errors li{margin:2px 0}

 .hp{position:absolute;left:-9999px;width:1px;height:1px;overflow:hidden}
 .foot{max-width:38rem;margin:0 auto;padding:22px 16px 0;text-align:center;
       color:hsl(var(--muted-foreground));font-size:.82rem}

 .tick{width:66px;height:66px;border-radius:50%;margin:0 auto 18px;
       background:hsl(var(--success) / .13);color:hsl(var(--success));
       font-size:32px;line-height:66px;text-align:center}
 .ref{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:1.35rem;
      font-weight:700;letter-spacing:.08em;color:hsl(var(--primary))}
 .muted{color:hsl(var(--muted-foreground));font-size:.9rem}
 .back{display:inline-block;margin-top:22px;color:hsl(var(--primary));
       text-decoration:none;font-weight:600}
 .back:hover{text-decoration:underline}
"""


# Loaded the way the site loads them; the stack falls back to system fonts if
# Google is unreachable, so the page is never blank waiting on a font.
_FONTS = """
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=Outfit:wght@600;700&display=swap" rel="stylesheet">
"""


_HERO = """
<div class="hero"><div class="hero-in">
  <div class="brand"><span class="dot"></span>
    <span>TriStateTags<small>Licensed NJ Dealer</small></span>
  </div>
  <h1>{{ heading }}</h1>
  <p>{{ standfirst }}</p>
</div></div>
"""


_FORM_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<meta name="theme-color" content="#0f172a">
<link rel="icon" href="/favicon.ico" sizes="any">
<title>Temp Tag Request | TriStateTags</title>
""" + _FONTS + """<style>""" + _STYLE + """</style></head><body>
""" + _HERO + """
<div class="wrap"><div class="card">

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
      {% if name == 'first_name' %}<div class="legend">Your details</div>{% endif %}
      {% if name == 'address' %}<div class="legend">Registration</div>{% endif %}
      {% if name == 'delivery_address' %}<div class="legend">Delivery</div>{% endif %}
      {% if name == 'vin' %}<div class="legend">Vehicle</div>{% endif %}

      {% if name == 'insurance_company' %}
      <div class="legend">Insurance</div>
      <label class="optin" for="wants_insurance">
        <input type="checkbox" id="wants_insurance" name="wants_insurance"
               {% if values.get('wants_insurance') %}checked{% endif %}>
        <span><strong>Opt in for insurance</strong>
          <small>Tick this and we will arrange the cover for you &mdash; leave the
                 two boxes below blank.</small></span>
      </label>
      {% endif %}

      {% if name == 'extra_info' %}
      <div class="legend">Anything else</div>
      <label for="{{ name }}">{{ label }}</label>
      <textarea id="{{ name }}" name="{{ name }}" maxlength="{{ max_notes }}"
                placeholder="e.g. tomorrow after 5pm, ring the top bell"
                >{{ values.get(name, '') }}</textarea>
      {% else %}
      <label for="{{ name }}">{{ label }}{% if required %} <span class="req">*</span>{% endif %}</label>
      <input id="{{ name }}" name="{{ name }}" maxlength="{{ max_len }}"
             value="{{ values.get(name, '') }}"
             {% if name == 'phone' %}type="tel" inputmode="tel" autocomplete="tel"
             {% elif name == 'email' %}type="email" inputmode="email" autocomplete="email"
             {% elif name == 'first_name' %}type="text" autocomplete="given-name"
             {% elif name == 'last_name' %}type="text" autocomplete="family-name"
             {% elif name == 'vin' %}type="text" autocapitalize="characters" spellcheck="false"
             {% else %}type="text"{% endif %}
             {% if required %}required{% endif %}
             {% if name in ('insurance_company','insurance_policy_number') %}
               data-insurance="1"{% endif %}>
      {% endif %}
    {% endfor %}

    <button type="submit">Submit request</button>
  </form>
</div>
<p class="foot">No payment is taken on this page. We will call you to confirm.</p>
</div>
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
<meta name="theme-color" content="#0f172a">
<link rel="icon" href="/favicon.ico" sizes="any">
<title>Request received | TriStateTags</title>
""" + _FONTS + """<style>""" + _STYLE + """</style></head><body>
""" + _HERO + """
<div class="wrap"><div class="card" style="text-align:center">
  <div class="tick">&#10003;</div>
  <h2 style="margin:0 0 8px;font-size:1.25rem">We have your details</h2>
  <p class="muted" style="margin:0 0 22px">Your request is with our dispatch team
     now. Someone will call you shortly on the number you gave us.</p>
  {% if reference_id %}
  <p class="muted" style="margin:0 0 4px">Your reference</p>
  <p class="ref">{{ reference_id }}</p>
  <p class="muted" style="margin-top:14px">Keep this &mdash; quote it if you call
     us about this request.</p>
  {% endif %}
  <a class="back" href="https://tristatetags.com/">&larr; Back to TriStateTags</a>
</div></div>
</body></html>"""
