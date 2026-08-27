"""Web mirror of lead entry: paste -> parse -> review grid -> hand to the bot.

Nothing here talks to Telegram. We only write the lead row shaped exactly like
the bot's own create payload with ingest_dispatch_pending=True; the RUNNING
bot's process_pending_api_lead_dispatches poll (~10s) claims it and offers it
to every active Dispatcher group — first team to accept wins the lead.
"""
from __future__ import annotations

import hmac
import logging
import os
import secrets
import string

from flask import flash, jsonify, redirect, render_template, request, session
from werkzeug.exceptions import RequestEntityTooLarge

from utils import external_lead_parser
from utils.database import record_is_active
from utils.lead_validation import normalize_phone
from utils.onetimesecret import OneTimeSecret

from .core import bp, get_db, require_login

logger = logging.getLogger(__name__)

# The 11 lines of leads.vehicle_details, in blob order (build_vehicle_details_11).
GRID_11 = (
    "name",
    "address",
    "city_state_zip",
    "delivery_address",
    "delivery_city_state_zip",
    "vin",
    "car",
    "color",
    "insurance_company",
    "insurance_policy_number",
    "extra_info",
)
# Lead columns the grid edits beyond the 11-line blob.
GRID_EXTRA = ("phone", "price", "email", "external_order_id")

# (field name, label) in display order — the template renders the grid from this
# so field names can never drift between the form, the parser, and the payload.
GRID_SPEC = (
    ("name", "Customer"),
    ("phone", "Phone"),
    ("price", "Price"),
    ("email", "Email"),
    ("external_order_id", "Order #"),
    ("address", "Registration street"),
    ("city_state_zip", "Registration city, state zip"),
    ("delivery_address", "Delivery street"),
    ("delivery_city_state_zip", "Delivery city, state zip"),
    ("vin", "VIN"),
    ("car", "Vehicle"),
    ("color", "Color"),
    ("insurance_company", "Insurance company"),
    ("insurance_policy_number", "Policy #"),
    ("extra_info", "Extra info"),
)

_REF_ALPHABET = string.ascii_uppercase + string.digits

# /api/parse body cap. A real New Lead paste is under 2KB; 256KB is generous
# headroom. This endpoint runs inside admin_dashboard — the production
# tristatetags.com/backend process — so one oversized POST must never buffer
# unbounded into that process (Werkzeug's get_data reads the whole stream and
# no MAX_CONTENT_LENGTH is configured anywhere in the host app).
_PARSE_MAX_BYTES = 256 * 1024

# One-shot submit tokens for POST /new, stored in the signed session cookie.
# A small list (not a single slot) so a dispatcher with two New Lead tabs open
# can still submit both; oldest tokens fall off.
_NONCE_KEY = "dw_new_nonces"
_NONCE_KEEP = 8


def _generate_reference_id() -> str:
    # Mirrors bot.generate_reference_id (8 uppercase alphanumerics) without importing bot.
    return "".join(secrets.choice(_REF_ALPHABET) for _ in range(8))


def _mint_form_nonce() -> str:
    """One-shot token rendered into the /new form as a hidden field.

    POST consumes it, so a double-click racing the redirect, a back-button
    resubmit, or a replayed POST re-renders a banner instead of creating a
    second fully dispatchable lead (the bot's ~10s poll would offer BOTH to
    every Dispatcher group as two separate clients). Because no cross-site
    form can ever carry it, it doubles as CSRF protection for this
    side-effecting POST — same posture as the settings toggle's dw_csrf."""
    tok = secrets.token_urlsafe(24)
    outstanding = session.get(_NONCE_KEY)
    if not isinstance(outstanding, list):
        outstanding = []
    outstanding = [t for t in outstanding if isinstance(t, str) and t]
    outstanding.append(tok)
    session[_NONCE_KEY] = outstanding[-_NONCE_KEEP:]  # reassign: mark session dirty
    return tok


def _consume_form_nonce() -> bool:
    """True exactly once per minted token; fail closed on no/unknown token.

    compare_digest over bytes per candidate — the submitted value is
    attacker-picked and str comparison would TypeError on non-ascii (the same
    scar core.login guards against)."""
    got = (request.form.get("form_nonce") or "").encode("utf-8")
    outstanding = session.get(_NONCE_KEY)
    if not got or not isinstance(outstanding, list) or not outstanding:
        return False
    for tok in outstanding:
        if isinstance(tok, str) and tok and hmac.compare_digest(tok.encode("utf-8"), got):
            session[_NONCE_KEY] = [t for t in outstanding if t != tok]  # burn it
            return True
    return False


def _ots_wrap(phone: str):
    """(onetimesecret_token, onetimesecret_secret_key, encrypted_link) —
    utils/lead_ingest.py's exact OTS policy, key names included.

    encrypted_link=None was a lead shape NO other source creates: bot leads
    cannot exist without a live OTS link, and HTTP ingest stores the raw phone
    when OTS is down. The bot renders `encrypted_link or raw phone` in the
    accepted team's copy-paste block and in the driver card under /driverblock,
    so None printed an EMPTY phone line for teams and silently handed drivers
    the raw digits. Wrap when the service answers (redaction preserved); fall
    back to the raw phone (the ingest fallback every bot renderer handles)."""
    try:
        ots = OneTimeSecret()
        encrypted = ots.encrypt_phone(phone)
        err = (getattr(ots, "last_error", "") or "Phone encryption skipped").strip()
    except Exception as e:  # config/constructor trouble must not kill the POST
        encrypted, err = None, type(e).__name__
    if encrypted:
        return (
            encrypted.get("secret_key"),
            encrypted.get("metadata_key"),
            encrypted.get("link"),
        )
    # last_error is admin-safe by contract (never carries the secret/phone).
    logger.warning("dispatch_web newlead: skipping phone encryption — %s", err)
    return None, None, phone


def _entrant_user_id() -> int:
    # leads.user_id is the entrant's Telegram id; the web has none, so a fixed
    # id from env keeps attribution stable (0 = anonymous web entry).
    try:
        return int(os.environ.get("DISPATCH_WEB_USER_ID") or 0)
    except (TypeError, ValueError):
        return 0


def _dash(v) -> str:
    # The bot's cards join these lines verbatim: a missing value MUST be "-",
    # never ""/None (an empty line shifts nothing, but None once killed handlers).
    v = str(v or "").strip()
    return v if v else "-"


def _present(v: str) -> bool:
    return bool(v) and v != "-"


def _grid_from_state(state: dict) -> dict:
    """Parser phase-1 state -> the flat field dict the preview form edits."""
    fields = {k: str(state.get(k) or "-") for k in GRID_11}
    fields["phone"] = str(state.get("pending_phone_number") or "")
    fields["price"] = str(state.get("pending_price") or "")
    fields["email"] = str(state.get("email") or "")
    fields["external_order_id"] = str(state.get("external_order_id") or "")
    return fields


def _grid_best_effort(raw: str) -> dict:
    """Grid values for text the parser rejected — same label/address/vehicle
    splits, no validation, so the operator can finish the gaps by hand.
    Uses the parser module's own helpers (utils.lead_ingest sets the precedent
    of importing its underscore functions)."""
    labeled = external_lead_parser._parse_labeled_fields(raw or "")
    reg = labeled.get("registration_address", "")
    dlv = labeled.get("delivery_address_full", "")
    address, csz = external_lead_parser._split_us_address(reg) if reg else ("", "")
    daddr, dcsz = external_lead_parser._split_us_address(dlv) if dlv else ("", "")
    car, color = ("", "")
    if labeled.get("vehicle"):
        car, color = external_lead_parser._split_vehicle(labeled["vehicle"])
    return {
        "name": labeled.get("name", ""),
        "address": address,
        "city_state_zip": csz,
        "delivery_address": daddr,
        "delivery_city_state_zip": dcsz,
        "vin": labeled.get("vin", ""),
        "car": car,
        "color": color,
        "insurance_company": labeled.get("insurance_company", ""),
        "insurance_policy_number": labeled.get("insurance_policy_number", ""),
        "extra_info": external_lead_parser._build_extra_info(labeled),
        "phone": labeled.get("phone", ""),
        "price": labeled.get("price", ""),
        "email": labeled.get("email", ""),
        "external_order_id": labeled.get("external_order_id", ""),
    }


def _pick_dispatch_group(errors: list):
    """First active Dispatcher's id, or None with the reason appended to errors.
    The bot re-picks the primary group at dispatch time anyway; this only has
    to prove there is somewhere for the lead to go."""
    try:
        groups = get_db().get_all_groups()
    except Exception as e:  # DB down must banner, never traceback
        logger.error("dispatch_web newlead: get_all_groups failed: %s", type(e).__name__)
        errors.append("Could not reach the database to pick a Dispatcher — try again")
        return None
    active = [g for g in groups if record_is_active(g)]
    if not active:
        errors.append("No active Dispatchers configured — the bot would have nowhere to send this")
        return None
    return active[0].get("id")


def _render_form(vals: dict, errors: list, db_banner: str | None = None):
    # Every render mints a fresh one-shot nonce, so an error re-render (kept
    # fields) can always be corrected and legitimately resubmitted.
    return render_template(
        "dispatch/new.html",
        vals=vals,
        errors=errors,
        grid=GRID_SPEC,
        sample=external_lead_parser.SAMPLE_MESSAGE,
        db_banner=db_banner,
        form_nonce=_mint_form_nonce(),
    )


def _parse_too_large():
    # Still 200 JSON — the endpoint's contract; a 413 would surface as the
    # fetch handler's generic "request failed" instead of a real message.
    return jsonify({"ok": False, "errors": ["Pasted text is too large"], "fields": {}})


@bp.route("/api/parse", methods=["POST"])
@require_login
def api_parse_lead():
    """Raw pasted text in (text/plain body, form 'text', or JSON {'text'}),
    the parsed grid field dict out. Always 200 JSON:
    {ok, errors: [..], fields: {..}} — on errors, fields is best-effort."""
    # Never buffer an unbounded body (see _PARSE_MAX_BYTES): refuse a declared
    # oversize before ANY body read; cap the per-request max_content_length so
    # the form/JSON parsers enforce it on chunked (length-less) bodies too; and
    # read the raw text path ourselves, capped, instead of get_data()'s
    # read-everything. RequestEntityTooLarge is caught to keep the 200-JSON
    # contract.
    if (request.content_length or 0) > _PARSE_MAX_BYTES:
        return _parse_too_large()
    request.max_content_length = _PARSE_MAX_BYTES
    raw = ""
    try:
        if request.is_json:
            raw = str((request.get_json(silent=True) or {}).get("text") or "")
        if not raw:
            raw = request.form.get("text") or ""
        if not raw:
            body = request.stream.read(_PARSE_MAX_BYTES + 1) or b""
            # A length-less (chunked) stream is a LimitedStream(is_max=True):
            # it silently STOPS at the cap and only raises on the next read —
            # so probe one more byte, or a truncated paste would parse as if
            # complete. (The probe's own RequestEntityTooLarge lands in the
            # except below; at real EOF it returns b"".)
            if len(body) > _PARSE_MAX_BYTES or request.stream.read(1):
                return _parse_too_large()
            raw = body.decode("utf-8", errors="replace")  # get_data(as_text=True)'s decode
    except RequestEntityTooLarge:
        return _parse_too_large()
    raw = raw.strip()
    if not raw:
        return jsonify({"ok": False, "errors": ["Paste the New Lead text first"], "fields": {}})

    try:
        state, errors = external_lead_parser.parse_external_lead_message(raw)
    except Exception as e:  # keep the pasted text (client PII) out of the logs
        logger.error("dispatch_web newlead: parser raised %s", type(e).__name__)
        return jsonify(
            {"ok": False, "errors": ["Parser choked on this text — fill the grid by hand"], "fields": {}}
        )
    if state:
        return jsonify({"ok": True, "errors": [], "fields": _grid_from_state(state)})
    try:
        fields = _grid_best_effort(raw)
    except Exception as e:
        logger.error("dispatch_web newlead: best-effort parse raised %s", type(e).__name__)
        fields = {}
    return jsonify({"ok": False, "errors": errors, "fields": fields})


@bp.route("/new", methods=["GET", "POST"])
@require_login
def new_lead():
    if request.method == "GET":
        vals = {"entered_by": session.get("dw_name", "")}
        # Advisory only on GET: show the no-Dispatchers problem before typing starts.
        probe: list = []
        _pick_dispatch_group(probe)
        return _render_form(vals, [], db_banner=(probe[0] if probe else None))

    form = request.form
    entered_by = (form.get("entered_by") or "").strip()
    vals = {k: (form.get(k) or "").strip() for k in GRID_11 + GRID_EXTRA}
    vals["entered_by"] = entered_by

    # One-shot nonce first: a duplicate/replayed/cross-site POST must not
    # create a second dispatchable lead. Fields are kept (with a fresh nonce),
    # so a genuine second lead costs one more click, never a retype.
    if not _consume_form_nonce():
        return _render_form(
            vals,
            [
                "This lead was already submitted (or the form had expired) — "
                "nothing new was created. If this really is a different lead, "
                "review the grid and press Submit once more."
            ],
        )

    errors: list = []
    if not entered_by:
        errors.append("Your name is required — it marks who entered the lead")

    # Validation mirrors parse_external_lead_fields rule-for-rule; the state we
    # build here is the same phase-1 dict shape its success path produces.
    state = {k: _dash(vals[k]) for k in GRID_11}

    if not _present(state["name"]):
        errors.append("Customer is required")

    phone = normalize_phone(vals["phone"])
    if not phone:
        errors.append("Phone is required (9-10 digit US number)")

    price = vals["price"]
    if price and any(ch.isdigit() for ch in price) and "$" not in price:
        price = f"${price}"  # the parser demands the $; typing "150" shouldn't bounce
    if not price or "$" not in price or not any(ch.isdigit() for ch in price):
        errors.append("Price is required (must include $ and a number)")

    vin_match = external_lead_parser.VIN_PATTERN.search(vals["vin"] or "")
    if vin_match:
        state["vin"] = vin_match.group(0).upper()
    else:
        errors.append("VIN is required (17 characters)")

    if not _present(state["car"]):
        errors.append("Vehicle is required")

    # One address given -> it serves as both (mirrors _apply_single_address_as_both).
    reg_ok = _present(state["address"]) or _present(state["city_state_zip"])
    del_ok = _present(state["delivery_address"]) or _present(state["delivery_city_state_zip"])
    if reg_ok and not del_ok:
        state["delivery_address"] = state["address"]
        state["delivery_city_state_zip"] = state["city_state_zip"]
    elif del_ok and not reg_ok:
        state["address"] = state["delivery_address"]
        state["city_state_zip"] = state["delivery_city_state_zip"]
    elif not reg_ok and not del_ok:
        errors.append("Registration address or Delivery address is required")

    group_id = None
    if not errors:
        group_id = _pick_dispatch_group(errors)

    if errors:
        return _render_form(vals, errors)

    session["dw_name"] = entered_by  # asked once, remembered for the session

    reference_id = _generate_reference_id()
    ots_token, ots_meta, ots_link = _ots_wrap(phone)
    # The bot-shaped create payload (see final_lead_data in bot.py and
    # utils/lead_ingest.py).
    payload = {
        "user_id": _entrant_user_id(),
        "telegram_username": entered_by,  # leaderboard fallback column pre-migration
        "telegram_name": entered_by,
        "vehicle_details": external_lead_parser.build_vehicle_details_11(state),
        "delivery_details": external_lead_parser.build_delivery_details(state),
        "phone_number": phone,
        "price": price,
        "onetimesecret_token": ots_token,
        "onetimesecret_secret_key": ots_meta,
        "encrypted_link": ots_link,
        "reference_id": reference_id,
        "group_id": group_id,
        "extra_info": state.get("extra_info", "") or "",
        "special_request_issuers": "",
        "special_request_drivers": "",
        "special_request_note": "",
        "email": vals["email"] or None,
        # NEVER None: the bot's group-accept handler detects "website lead ->
        # fan out to drivers" solely by external_order_id being truthy; a None
        # here falls into the issuer-DM branch, which is a no-op for web
        # entrants, and the accepted lead silently never reaches any driver.
        # The reference_id fallback keeps display parity too — the bot's
        # supervisory text already prints reference_id on the Order # line
        # when there is no external id.
        "external_order_id": vals["external_order_id"] or reference_id,
        "contact_info_source": "Dispatch Web",
        "ingest_dispatch_pending": True,  # what the bot's 10s poll picks up
        "awaiting_group_accept": True,
    }

    try:
        lead = get_db().create_lead(payload)
    except Exception as e:
        logger.error("dispatch_web newlead: create_lead raised %s", type(e).__name__)
        lead = None
    if not lead:
        errors.append(
            "Saving failed — the database refused the lead (connection or missing "
            "migration_lead_api_ingest.sql). Nothing was dispatched; your fields are kept."
        )
        return _render_form(vals, errors)

    logger.info("dispatch_web newlead: lead %s ref %s queued for bot dispatch", lead.get("id"), reference_id)
    flash(f"Lead {reference_id} saved — the bot is dispatching this to the teams")
    return redirect(f"/dispatch/lead/{lead.get('id')}")
