"""GET /dispatch/lead/<id>/tag.pdf — the bot's temp-tag PDF, regenerated on demand.

Field resolution is a line-for-line mirror of ``bot._tag_fields_from_lead``
(and the private helpers it leans on), NOT an import of bot.py: importing the
bot would drag python-telegram-bot and its handler registration into the
dashboard process. Any drift between the two must be resolved by copying the
bot's code here again, never by "improving" this copy.

Plate + control number are REUSED from storage so a re-download matches the
tag this endpoint already served: car 1's live in the lead row's
``plate``/``tag_control_number`` columns (the columns the bot persists into
after allocating), cars 2+ in their ``extra_vehicles`` entry. Allocation
happens only when a value is missing — a lead created outside the bot's
submit path — and is persisted into those same columns so the NEXT web
download reuses it.

PLATE REUSE — now identical to the bot. Both surfaces reuse car 1's assigned
plate off the lead row: this mirror injects the row's ``plate``/
``tag_control_number`` inside ``_tag_fields_from_lead``, and bot.py does the
same (``_assigned_plate = phase1.get("plate") or (lead.get("plate") if
vehicle <= 1 else "")``). So a web download and the Telegram tag carry the
SAME plate for the same car. (Before a72f539 the bot re-minted car 1 on every
send — its ``_phase1_from_stored_lead`` blob has no ``plate`` key — and this
mirror's injection was the only side honouring the "reuse the assigned values
so re-sends are identical" promise; that gap is closed on both sides now.)
Keep this injection: it is what keeps the two surfaces agreeing.

No Telegram calls, no renewal path: a renewal mints a fresh plate and that
decision stays with the bot.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta

import pytz
from flask import Response, request

from utils import tag_pdf

from .core import bp, get_db, require_login

logger = logging.getLogger(__name__)

# ── Mirrors of bot.py internals (same names, same behavior) ──────────────────

EXTRA_VEHICLES_KEY = "extra_vehicles"

# Exactly 17 alphanumeric: the only valid VIN structure. Never cut or truncate.
VIN_PATTERN = re.compile(r"\b[A-Za-z0-9]{17}\b")


def _extract_vin_17(text: str) -> str | None:
    """Return the first 17-character alphanumeric VIN found in text, or None."""
    if not text:
        return None
    m = VIN_PATTERN.search(text)
    return m.group(0) if m else None


def _dt_from_lead_field(val) -> datetime | None:
    """NEW YORK time — see utils.timezone. The web mirror prints the same tag
    as the bot, so it has to agree with the bot about what day it is."""
    from utils.timezone import to_ny
    return to_ny(val)


def _dt_from_lead_field_unused(val) -> datetime | None:
    """Parse issue_date / expiration_date from DB (ISO string or datetime)."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    s = str(val).strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except ValueError:
        pass
    for candidate in (s, s.replace(" ", "T", 1)):
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            continue
    try:
        return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        pass
    try:
        return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        pass
    return None


def _issue_and_expiration(lead: dict) -> tuple[datetime | None, datetime | None]:
    """Issue/expiration — use DB; if missing (race), NY now + 30 days."""
    issue_dt = _dt_from_lead_field(lead.get("issue_date"))
    exp_dt = _dt_from_lead_field(lead.get("expiration_date"))
    if issue_dt and not exp_dt:
        exp_dt = issue_dt + timedelta(days=30)
    if issue_dt and exp_dt:
        return issue_dt, exp_dt
    if lead.get("id"):
        ny = pytz.timezone("America/New_York")
        issue_dt = datetime.now(ny)
        exp_dt = issue_dt + timedelta(days=30)
        return issue_dt, exp_dt
    return issue_dt, exp_dt


def _extra_vehicles(obj) -> list:
    """The extra cars on a lead row, always as a list of dicts.

    Tolerant on purpose: the column may be absent (migration not run yet), JSON
    null, or a string rather than parsed JSON. Any of those must read as "no
    extra cars" and never raise in the middle of a download.
    """
    raw = (obj or {}).get(EXTRA_VEHICLES_KEY)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return []
    if not isinstance(raw, list):
        return []
    return [dict(v) for v in raw if isinstance(v, dict)]


def _vehicle_count(obj) -> int:
    return len(_extra_vehicles(obj)) + 1


def _phase1_from_stored_lead(lead: dict) -> dict:
    """Rebuild phase1 field dict directly from the lead row.
    Always produces correct fields, even if stored string was misaligned."""
    vd = (lead.get("vehicle_details") or "").strip()
    dd = (lead.get("delivery_details") or "").strip()
    extra = (lead.get("extra_info") or "").strip()

    lines = [ln.strip() for ln in vd.splitlines()]
    while len(lines) < 11:
        lines.append("-")

    # Force VIN into line index 5 (6th line) if a real VIN exists anywhere in
    # the raw blob — the bot does the same, so a misaligned paste still tags.
    vin_found = _extract_vin_17(vd)
    if vin_found:
        lines[5] = vin_found

    if extra:
        lines[10] = extra

    phase1 = {
        "name": lines[0] if lines[0] != "-" else "",
        "address": lines[1] if lines[1] != "-" else "",
        "city_state_zip": lines[2] if lines[2] != "-" else "",
        "delivery_address": lines[3] if lines[3] != "-" else "",
        "delivery_city_state_zip": lines[4] if lines[4] != "-" else "",
        "vin": lines[5] if lines[5] != "-" else "",
        "car": lines[6] if lines[6] != "-" else "",
        "color": lines[7] if lines[7] != "-" else "",
        "insurance_company": lines[8] if lines[8] != "-" else "",
        "insurance_policy_number": lines[9] if lines[9] != "-" else "",
        "extra_info": extra or lines[10] if lines[10] != "-" else "",
    }

    if dd:
        phase1["delivery_details"] = dd
        dlines = [L.strip() for L in dd.splitlines() if L.strip()]
        if len(dlines) >= 1:
            phase1["delivery_address"] = dlines[0]
        if len(dlines) >= 2:
            phase1["delivery_city_state_zip"] = dlines[1]

    return phase1


def _extra_vehicle_phase1(lead: dict, vehicle: int) -> dict:
    """An extra car shaped like ``_phase1_from_stored_lead`` output.

    The delivery lines come from the LEAD: there is one delivery, one phone and
    one price however many cars are on it.
    """
    vehicles = _extra_vehicles(lead)
    idx = vehicle - 2
    v = dict(vehicles[idx]) if 0 <= idx < len(vehicles) else {}
    base = _phase1_from_stored_lead(lead)
    return {
        "name": v.get("name") or "",
        "address": v.get("address") or "",
        "city_state_zip": v.get("city_state_zip") or "",
        "delivery_address": base.get("delivery_address") or "",
        "delivery_city_state_zip": base.get("delivery_city_state_zip") or "",
        "vin": v.get("vin") or "",
        "car": v.get("car") or "",
        "color": v.get("color") or "",
        "insurance_company": v.get("insurance_company") or "",
        "insurance_policy_number": v.get("insurance_policy_number") or "",
        "extra_info": base.get("extra_info") or "",
        "plate": v.get("plate") or "",
        "tag_control_number": v.get("tag_control_number") or "",
    }


def _persist_lead_plate(db, lead: dict, plate: str, control: str) -> tuple[str, str]:
    """Persist car 1's minted plate/control onto the lead row, exactly one
    winner. Returns the (plate, control) to PRINT — the winner's, always.

    The blocking NHTSA decode (up to 10s) sits between this request's lead
    read and this write, plenty of time for the live bot (an accept re-build)
    or a sibling download to mint and persist first. A blind ``update_lead``
    here would be last-writer-wins with the loser's plate already printed on
    a delivered legal document — so claim the columns with
    ``.is_("plate", "null")`` (the ``claim_insurance_email`` shape) and, on a
    miss, re-read the row and serve the winner's values instead of ours.
    """
    lead_id = str(lead.get("id"))
    claimed = (
        db.client.table("leads")
        .update({"plate": plate, "tag_control_number": control})
        .eq("id", lead_id)
        .is_("plate", "null")
        .execute()
    )
    if getattr(claimed, "data", None):
        lead["plate"], lead["tag_control_number"] = plate, control
        return plate, control

    # Claim missed: a concurrent writer landed first — or the column holds ''
    # (which ``is null`` cannot claim; no writer in this codebase produces '').
    fresh = db.get_lead_by_id(lead_id) or {}
    won_plate = (fresh.get("plate") or "").strip()
    won_control = (fresh.get("tag_control_number") or "").strip()
    if won_plate:
        if not won_control:
            # Plate stored without a control (hand-edited row): claim just the
            # control the same guarded way so the next download reprints it.
            c2 = (
                db.client.table("leads")
                .update({"tag_control_number": control})
                .eq("id", lead_id)
                .is_("tag_control_number", "null")
                .execute()
            )
            if getattr(c2, "data", None):
                won_control = control
            else:
                fresh2 = db.get_lead_by_id(lead_id) or {}
                won_control = (fresh2.get("tag_control_number") or "").strip() or control
        lead["plate"], lead["tag_control_number"] = won_plate, won_control
        return won_plate, won_control

    # Corner: the row still shows no plate ('' stored), so there is no winner
    # to defer to — keep the old blind write rather than serving values that
    # never persist and change on every download.
    db.update_lead(lead_id, {"plate": plate, "tag_control_number": control})
    lead["plate"], lead["tag_control_number"] = plate, control
    return plate, control


def _persist_extra_vehicle_plate(db, lead: dict, vehicle: int, plate: str, control: str) -> tuple[str, str]:
    """Persist ONE extra car's minted plate onto the lead without clobbering
    its siblings. Returns the (plate, control) to PRINT.

    ``extra_vehicles`` is a JSON array shared by every car AND by the live
    bot, and the slow NHTSA decode sat between this request's lead read and
    now — writing the array from that stale read would silently drop any
    plate a concurrent build (a Telegram accept, a sibling ``?car=`` download)
    minted meanwhile. So: RE-READ the row immediately before writing, merge
    only this car's index into the fresh array, and if the fresh array shows
    this car already plated, serve THAT instead of the local mint.
    """
    idx = vehicle - 2
    lead_id = str(lead.get("id"))
    fresh = db.get_lead_by_id(lead_id)
    if fresh is not None:
        lead[EXTRA_VEHICLES_KEY] = fresh.get(EXTRA_VEHICLES_KEY)
    vehicles = _extra_vehicles(lead)
    if not (0 <= idx < len(vehicles)):
        return plate, control
    won_plate = (vehicles[idx].get("plate") or "").strip()
    won_control = (vehicles[idx].get("tag_control_number") or "").strip()
    if won_plate and won_control:
        # A concurrent build already minted THIS car: nothing to write, and
        # the persisted identity is the one that must be printed.
        lead[EXTRA_VEHICLES_KEY] = vehicles
        return won_plate, won_control
    plate = won_plate or plate
    control = won_control or control
    vehicles[idx]["plate"] = plate
    vehicles[idx]["tag_control_number"] = control
    lead[EXTRA_VEHICLES_KEY] = vehicles
    db.update_lead(lead_id, {EXTRA_VEHICLES_KEY: vehicles})
    return plate, control


def _tag_fields_from_lead(db, lead: dict, *, vehicle: int = 1) -> dict:
    """Resolve a stored lead into the field dict tag_pdf.build_tag_pdf expects.

    Sync twin of ``bot._tag_fields_from_lead`` without the renewal branch.
    Stored plate + control are reused; allocation happens only when a car has
    none yet, and its persistence is RACE-GUARDED (unlike the bot's blind
    write — see the module docstring): whoever persists first wins, and the
    winner's values are the ones printed. ``vehicle`` is 1 for the lead's own
    car, 2+ for an extra car, whose own city/state/ZIP picks its plate format
    and PDF template.
    """
    if vehicle <= 1:
        phase1 = _phase1_from_stored_lead(lead)
        # Car 1's assigned plate lives in the lead row's own columns — the
        # exact place bot._tag_fields_from_lead persists into after minting.
        phase1["plate"] = lead.get("plate") or ""
        phase1["tag_control_number"] = lead.get("tag_control_number") or ""
    else:
        phase1 = _extra_vehicle_phase1(lead, vehicle)
    first, last = tag_pdf.split_name(phase1.get("name", ""))
    csz = phase1.get("city_state_zip", "")
    state = tag_pdf.parse_state(csz)
    city, zipc = tag_pdf.parse_city_zip(csz, state)
    city, state, zipc = tag_pdf.normalize_city_state_zip(city, state, zipc)
    vin = phase1.get("vin", "")

    # Blocking NHTSA call, same one the bot makes (it wraps it in to_thread
    # only because the bot is async). Falls back to the typed vehicle line.
    decoded = tag_pdf.decode_vin_for_tag(vin) if vin else None
    if decoded and decoded.get("make"):
        year, make, model, body = decoded["year"], decoded["make"], decoded["model"], decoded["body"]
    else:
        year, make, model = tag_pdf.parse_car_line(phase1.get("car", ""))
        body = ""
    body = tag_pdf.normalize_body_heuristic(body)

    plate = (phase1.get("plate") or "").strip()
    control = (phase1.get("tag_control_number") or "").strip()
    if not plate or not control:
        alloc = db.allocate_temp_plate(state == "NJ")
        plate = plate or alloc["plate"]
        control = control or alloc["control_number"]
        try:
            # Race-guarded: a concurrent build (the live bot, a sibling
            # download) may have persisted first — print the WINNER'S values.
            if vehicle <= 1:
                plate, control = _persist_lead_plate(db, lead, plate, control)
            else:
                plate, control = _persist_extra_vehicle_plate(db, lead, vehicle, plate, control)
        except Exception as e:
            # Persistence must never block the PDF: serve the local mint and
            # let the next download allocate again (the old behavior).
            logger.warning("Could not persist plate for lead %s: %s", lead.get("id"), e)

    issue_dt, _exp_dt = _issue_and_expiration(lead)
    issued = issue_dt.date() if issue_dt else None  # expires defaults to issue + 29d

    return {
        "is_nj": state == "NJ",
        "plate": plate,
        "control_number": control,
        "vin": vin,
        "year": year,
        "make": make,
        "model": model,
        "color": phase1.get("color", ""),
        "body": body,
        "first": first,
        "last": last,
        "address": phase1.get("address", ""),
        "city": city,
        "state": state,
        "zip": zipc,
        "insurance_company": phase1.get("insurance_company", ""),
        "policy": phase1.get("insurance_policy_number", ""),
        "issued": issued,
    }


def _tag_filename(fields: dict, vehicle: int, total: int) -> str:
    """The bot's file naming, verbatim: the CLIENT's name (Arturo_Torne.pdf),
    car index appended on a multi-car lead so two files never collide, plate
    as the fallback when the lead has no name at all."""
    plate = fields.get("plate") or "tag"
    _client_file = re.sub(r"[^A-Za-z0-9]+", "_",
                          f"{fields.get('first') or ''} {fields.get('last') or ''}".strip()).strip("_")
    _sfx = f"_{vehicle}" if total > 1 else ""
    return ((f"{_client_file}{_sfx}.pdf") if _client_file
            else f"tag_{re.sub(r'[^A-Za-z0-9]+', '', plate) or 'tag'}{_sfx}.pdf")


def _plain(status: int, text: str) -> Response:
    """Non-PDF outcome for a file endpoint: a short plain-text body, never a
    traceback page."""
    return Response(text + "\n", status=status, mimetype="text/plain")


@bp.get("/lead/<lead_id>/tag.pdf")
@require_login
def lead_tag_pdf(lead_id: str):
    raw_car = (request.args.get("car") or "1").strip()
    try:
        car = int(raw_car)
    except ValueError:
        return _plain(400, "?car must be a number (1 for the lead's own car, 2+ for extras).")

    try:
        db = get_db()
        lead = db.get_lead_by_id(str(lead_id))
    except Exception as e:
        logger.error("Tag PDF: DB unavailable for lead %s: %s", lead_id, e)
        return _plain(503, "Database unavailable — try again shortly.")
    if not lead:
        return _plain(404, "Lead not found.")

    total = _vehicle_count(lead)
    if not (1 <= car <= total):
        return _plain(404, f"This lead has {total} car(s); ?car must be between 1 and {total}.")

    try:
        fields = _tag_fields_from_lead(db, lead, vehicle=car)
        pdf = tag_pdf.build_tag_pdf(fields)
    except Exception as e:
        # No client values in the log line — the lead id is enough to replay.
        logger.error("Tag PDF build failed for lead %s car %s: %s", lead_id, car, e)
        return _plain(500, "Tag PDF could not be generated — check the server log.")

    resp = Response(pdf, mimetype="application/pdf")
    # Filename is ASCII by construction (the regex strips everything else), so
    # the plain filename= form is always well-formed.
    resp.headers["Content-Disposition"] = f'attachment; filename="{_tag_filename(fields, car, total)}"'
    # A first download may have just minted + persisted a plate, and the file
    # is a legal document with PII — never let a proxy or browser cache it.
    resp.headers["Cache-Control"] = "no-store"
    return resp
