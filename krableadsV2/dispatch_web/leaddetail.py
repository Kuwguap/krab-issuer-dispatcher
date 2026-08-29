"""One lead, the whole story, on one page.

GET  /dispatch/lead/<lead_id>          — every stored fact about the lead, grouped
POST /dispatch/lead/<lead_id>/strike   — exclude_from_count = True (reversible)
POST /dispatch/lead/<lead_id>/restore  — exclude_from_count = False

Everything the template receives is pre-shaped here as plain strings/bools:
a lead row can be missing any migrated column (or hold None where a blob is
expected), and a page that formats raw row values inline dies exactly like the
bot's empty-card crash did. The row itself is still passed for the raw view.
"""
import json
import re

from flask import redirect, render_template, request, url_for

from .core import bp, get_db, require_login

# Same 17-char rule the bot and utils/external_lead_parser.py use — inlined so a
# parser refactor cannot break the detail page (this is a repair aid, not a parser).
_VIN_PATTERN = re.compile(r"\b[A-Za-z0-9]{17}\b")

# vehicle_details is an 11-line positional blob; this order is bot.py's
# _PHASE1_ADJUST_FIELD_ORDER and must not drift from it.
_PHASE1_FIELDS = (
    ("name", "Name"),
    ("address", "Reg address"),
    ("city_state_zip", "Reg city/ST/ZIP"),
    ("delivery_address", "Delivery address"),
    ("delivery_city_state_zip", "Delivery city/ST/ZIP"),
    ("vin", "VIN"),
    ("car", "Car"),
    ("color", "Color"),
    ("insurance_company", "Insurance"),
    ("insurance_policy_number", "Policy #"),
    ("extra_info", "Date/time"),
)

_DELIVERY_STATUS_LABELS = {
    "new": "New Lead",
    "followup": "Followup",
    "tag_issued": "Tag issued",
    "tag_emailed": "Tag emailed",
    "tag_printed": "Tag printed",
    "on_the_way": "Driver on the way",
    "delivered": "Delivered",
    "paid": "Receipt uploaded",
    "receipt_uploaded": "Receipt uploaded",
}


def _s(lead, key):
    """Row value as a stripped string — None and missing columns read as ''."""
    v = (lead or {}).get(key)
    return str(v).strip() if v is not None else ""


def _phase1_rows(lead):
    """[(label, value)] from the 11-line blob, mirroring bot._phase1_from_stored_lead:
    pad short blobs, snap a real VIN into slot 5, let the dedicated columns
    (extra_info, delivery_details) win over their positional lines."""
    vd = _s(lead, "vehicle_details")
    lines = [ln.strip() for ln in vd.splitlines()]
    while len(lines) < 11:
        lines.append("-")
    vin = _VIN_PATTERN.search(vd)
    if vin:
        lines[5] = vin.group(0)
    extra = _s(lead, "extra_info")
    if extra:
        lines[10] = extra
    dd = [ln.strip() for ln in _s(lead, "delivery_details").splitlines() if ln.strip()]
    if len(dd) >= 1:
        lines[3] = dd[0]
    if len(dd) >= 2:
        lines[4] = dd[1]
    return [
        (label, "" if lines[i] == "-" else lines[i])
        for i, (_key, label) in enumerate(_PHASE1_FIELDS)
    ]


def _extra_vehicle_cards(lead):
    """Extra cars ride on the ONE lead (extra_vehicles jsonb). Tolerant like the
    bot's _extra_vehicles: absent column, JSON null, or a string all read as []."""
    raw = (lead or {}).get("extra_vehicles")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return []
    if not isinstance(raw, list):
        return []
    cards = []
    for v in raw:
        if not isinstance(v, dict):
            continue
        rows = []
        for key, label in _PHASE1_FIELDS:
            val = str(v.get(key) or "").strip()
            if val and val != "-":
                rows.append((label, val))
        for key, label in (("plate", "Plate"), ("tag_control_number", "Tag control #")):
            val = str(v.get(key) or "").strip()
            if val:
                rows.append((label, val))
        if v.get("insurance_card_sent_at"):
            rows.append(("FS-20 sent", str(v.get("insurance_card_sent_at"))))
        cards.append(rows)
    return cards


def _money_from_cents(cents):
    try:
        return "${:,.2f}".format(int(cents) / 100)
    except (TypeError, ValueError):
        return ""


def _insurance_view(lead):
    """Compact status + rows. The email gate: a card can be ISSUED while the
    client email is still HELD — insurance_emailed_at NULL means held."""
    wants = bool((lead or {}).get("wants_insurance"))
    sent_at = _s(lead, "insurance_card_sent_at")
    emailed_at = _s(lead, "insurance_emailed_at")
    if sent_at and emailed_at:
        status, tone = "Card issued · emailed to client", "ok"
    elif sent_at:
        status, tone = "Card issued · client email HELD (not released)", "warn"
    elif wants:
        status, tone = "Requested · card not issued yet", "warn"
    else:
        status, tone = "Not requested", "muted"
    return {
        "wants": wants,
        "status": status,
        "tone": tone,
        "policy_number": _s(lead, "insurance_card_policy_number"),
        "sent_to_email": _s(lead, "insurance_card_sent_to_email"),
        "sent_at": sent_at,
        "emailed_at": emailed_at,
        "card_error": _s(lead, "insurance_card_error"),
        "email_error": _s(lead, "insurance_email_error"),
        "portal_email": _s(lead, "portal_email"),
        # Credential: presence only, never the value.
        "portal_password_set": bool(_s(lead, "portal_password")),
    }


def _instant_view(lead, db):
    """The $100 skip-dispatch flow: requested → paid → delivered, each stamped."""
    requested_at = _s(lead, "instant_pdf_requested_at")
    paid_at = _s(lead, "instant_pdf_paid_at")
    delivered_at = _s(lead, "instant_pdf_delivered_at")
    if delivered_at and paid_at:
        status, tone = "Paid · tag delivered", "ok"
    elif delivered_at:
        # Delivered with nothing paid: a supervisor released it with the password.
        # Before that release stamped delivered_at this state could not occur, and
        # reading it as "Paid" counted a giveaway as a sale.
        status, tone = "Released without payment · tag delivered", "warn"
    elif paid_at:
        status, tone = "Paid · delivery pending (bot polls ~10s)", "warn"
    elif requested_at:
        status, tone = "Checkout created · awaiting payment", "warn"
    else:
        status, tone = "Not used", "muted"
    driver_id = _s(lead, "instant_pdf_driver_id")
    driver_name = ""
    if driver_id:
        # No Database helper reads drivers by row id — raw table per contract.
        try:
            r = (db.client.table("drivers").select("driver_name")
                 .eq("id", driver_id).limit(1).execute())
            rows = getattr(r, "data", None) or []
            if rows:
                driver_name = str(rows[0].get("driver_name") or "").strip()
        except Exception:
            driver_name = ""
    # instant_pdf_amount_cents is stamped at checkout CREATION (together with
    # requested_at, before any payment) — only the Stripe webhook's paid_at
    # makes it money actually taken. Split so the page can never say "Charged"
    # about an unpaid checkout.
    amount = _money_from_cents((lead or {}).get("instant_pdf_amount_cents"))
    return {
        "status": status,
        "tone": tone,
        "requested_at": requested_at,
        "paid_at": paid_at,
        "delivered_at": delivered_at,
        "amount_charged": amount if paid_at else "",
        "amount_quoted": "" if paid_at else amount,
        "driver_id": driver_id,
        "driver_name": driver_name,
        "session_id": _s(lead, "instant_pdf_session_id"),
        "instant_tag": bool((lead or {}).get("instant_tag")),
        "tag_sent_at_creation": bool((lead or {}).get("tag_sent_at_creation")),
    }


def _dispatch_view(lead, accepted=False):
    """``accepted`` = an accepted group_lead_offers row exists for this lead
    (at most one, DB-enforced). The raw awaiting_group_accept column is
    WRITE-ONLY on the bot side — set at web create and at ingest claim,
    cleared by nothing — so read alone it says "yes" forever, long after a
    team accepted and the tag went out. Derive: awaiting only while no team
    has accepted."""
    ds = _s(lead, "delivery_status")
    raw_awaiting = bool((lead or {}).get("awaiting_group_accept"))
    return {
        "ingest_pending": bool((lead or {}).get("ingest_dispatch_pending")),
        "awaiting_group_accept": raw_awaiting and not accepted,
        "accepted": bool(accepted),
        "delivery_status": _DELIVERY_STATUS_LABELS.get(ds, ds or "New"),
        "status_updated_at": _s(lead, "status_updated_at"),
        "status_updated_by": _s(lead, "status_updated_by"),
        "external_order_id": _s(lead, "external_order_id"),
        "monday_status": _s(lead, "monday_status"),
    }


@bp.route("/lead/<lead_id>", endpoint="lead_detail")
@require_login
def lead_detail(lead_id):
    db = None
    lead = None
    db_error = None
    try:
        # get_db() INSIDE the try (like board.py / _set_excluded): Database()
        # construction can raise (bad/missing SUPABASE env at boot), and an
        # escaped exception hits the host dashboard's global JSON errorhandler,
        # which returns str(e) — creds and hostnames — verbatim.
        db = get_db()
        lead = db.get_lead_by_id(str(lead_id))
    except Exception:
        # get_lead_by_id swallows its own errors; this guards a broken client.
        db_error = "Database unavailable — try again."
    if lead is None and db_error is None:
        # Can't tell "no such row" from "DB down" through this helper; say both.
        db_error = "Lead not found — or the database did not answer."

    notice = ""
    if request.args.get("ok") == "struck":
        notice = "Lead struck from every count."
    elif request.args.get("ok") == "restored":
        notice = "Lead restored — it counts again."
    error = db_error or ""
    if request.args.get("err") == "write":
        error = "Could not save that change — database did not confirm the write."

    if lead is None:
        return render_template(
            "dispatch/lead.html", lead=None, lead_id=str(lead_id),
            error=error, notice=notice,
        ), 404

    group_name = ""
    gid = _s(lead, "group_id")
    if gid:
        try:
            g = db.get_group_by_id(gid) or {}
            group_name = str(g.get("group_name") or "").strip()
        except Exception:
            group_name = ""

    # The raw awaiting_group_accept flag is never cleared by the bot (accept
    # updates group_id only), so the truth of "a team took it" is the accepted
    # offer row (one per lead, DB-enforced by
    # migration_group_lead_offers_one_accepted_per_lead.sql). Only consulted
    # when the flag is set — for every other lead it can change nothing.
    offer_accepted = False
    if lead.get("awaiting_group_accept"):
        try:
            offer_accepted = bool(db.get_accepted_group_for_lead(str(lead_id)))
        except Exception:
            offer_accepted = False  # can't tell -> show the raw flag (status quo)

    # The stored receipt_image_url must NEVER reach the template: old rows (and
    # bot.py's last-ditch fallback, when the storage upload fails) hold
    # api.telegram.org file links that EMBED THE BOT TOKEN in their path. The
    # page gets a presence boolean and links through the host app's token-free
    # resolver /api/receipts/image/<lead_id> (admin_dashboard.py, same Flask
    # process), which streams DB/storage bytes and re-signs expired Telegram
    # links via their #tgfid= fragment. pop, not just skip: the full row is
    # still handed over for the raw-blob view, so the URL must leave the dict.
    has_receipt = bool(_s(lead, "receipt_image_url"))
    lead.pop("receipt_image_url", None)

    return render_template(
        "dispatch/lead.html",
        lead=lead,
        lead_id=str(lead_id),
        error=error,
        notice=notice,
        struck=bool(lead.get("exclude_from_count")),
        appeal_status=_s(lead, "appeal_status"),
        entrant={
            "name": _s(lead, "telegram_name"),
            "username": _s(lead, "telegram_username"),
            "user_id": _s(lead, "user_id"),
            "group_name": group_name,
            "contact_source": _s(lead, "contact_info_source"),
        },
        phase1=_phase1_rows(lead),
        extra_cars=_extra_vehicle_cards(lead),
        money={
            "price": _s(lead, "price"),
            "driver_amount": _s(lead, "driver_amount"),
            "receipt_price": _s(lead, "receipt_price"),
        },
        contact={
            "phone": _s(lead, "phone_number"),
            "email": _s(lead, "email"),
            "dl": _s(lead, "driver_license_id"),
            "encrypted_link": _s(lead, "encrypted_link"),
        },
        notes={
            "issuers": _s(lead, "special_request_issuers") or _s(lead, "special_request_note"),
            "drivers": _s(lead, "special_request_drivers"),
        },
        insurance=_insurance_view(lead),
        instant=_instant_view(lead, db),
        dispatch=_dispatch_view(lead, accepted=offer_accepted),
        has_receipt=has_receipt,
    )


def _set_excluded(lead_id, excluded, ok_token):
    """Shared strike/restore body: write, then bounce back to the detail page so
    the badge shown is re-read from the row, never assumed."""
    ok = False
    try:
        ok = bool(get_db().set_lead_excluded(str(lead_id), excluded))
    except Exception:
        ok = False
    if ok:
        return redirect(url_for("dispatch_web.lead_detail", lead_id=lead_id, ok=ok_token))
    return redirect(url_for("dispatch_web.lead_detail", lead_id=lead_id, err="write"))


@bp.route("/lead/<lead_id>/strike", methods=["POST"], endpoint="lead_strike")
@require_login
def lead_strike(lead_id):
    return _set_excluded(lead_id, True, "struck")


@bp.route("/lead/<lead_id>/restore", methods=["POST"], endpoint="lead_restore")
@require_login
def lead_restore(lead_id):
    return _set_excluded(lead_id, False, "restored")
