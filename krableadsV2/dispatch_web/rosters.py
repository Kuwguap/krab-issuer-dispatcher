"""Rosters: Dispatchers (the ``groups`` table — the UI never says "groups")
and Drivers, with the same switches the Telegram /settings flow has.

Reads and writes go through the bot's own Database helpers so the web page
agrees with the dispatch loop about who is on: a SQL NULL ``is_active`` means
ACTIVE (record_is_active), and suspension is manual flag OR 5+ owed receipts —
the receipt half lifts by uploading, so the only button here is the manual one.
"""

import logging

from flask import redirect, render_template, request, url_for

from utils.database import record_is_active

from .core import bp, get_db, require_login

logger = logging.getLogger(__name__)

# Mirrors bot.py SUSPENSION_THRESHOLD (importing bot would drag in its
# side-effects): 5+ pending receipts = suspended until the driver uploads.
SUSPENSION_THRESHOLD = 5

# Redirect flags are CODES resolved to text here, not free text echoed from
# the query string — nothing an edited URL says can be made to look like ours.
_NOTICES = {
    "dispatcher-toggled": "Dispatcher status updated.",
    "driver-toggled": "Driver status updated.",
    "driver-suspended": "Driver suspended. They will not be offered new leads.",
    "driver-unsuspended": "Manual suspension lifted.",
}
_PROBLEMS = {
    "toggle-failed": "The change did not save — the database refused it. Try again.",
    "suspend-failed": (
        "Suspension flag did not save — has "
        "database/migration_driver_manual_suspend.sql been run?"
    ),
}


def _load_rosters():
    """(dispatchers, drivers, error_text) — the Database helpers already swallow
    their own query errors into [] / set(), so only a client-construction
    failure (bad env, unreachable Supabase) surfaces as the banner."""
    try:
        db = get_db()
        groups = db.get_all_groups()
        driver_rows = db.get_all_drivers()
        try:
            receipt_suspended = db.get_driver_ids_with_pending_receipt_count_at_least(
                SUSPENSION_THRESHOLD
            )
        except Exception:
            receipt_suspended = set()
    except Exception as e:
        logger.warning("rosters load failed: %s", type(e).__name__)
        return [], [], "Database unavailable — rosters could not be loaded."

    dispatchers = [
        {
            "id": g.get("id"),
            "name": g.get("group_name") or "(unnamed)",
            "active": record_is_active(g),
        }
        for g in groups
    ]
    drivers = []
    for d in driver_rows:
        manual = bool(d.get("is_suspended"))
        by_receipts = str(d.get("id")) in receipt_suspended
        drivers.append(
            {
                "id": d.get("id"),
                "name": d.get("driver_name") or "(unnamed)",
                "active": record_is_active(d),
                "manual_suspended": manual,
                "receipt_suspended": by_receipts,
                "suspended": manual or by_receipts,
            }
        )
    return dispatchers, drivers, None


@bp.route("/rosters")
@require_login
def rosters():
    dispatchers, drivers, error = _load_rosters()
    return render_template(
        "dispatch/rosters.html",
        dispatchers=dispatchers,
        drivers=drivers,
        error=error,
        notice=_NOTICES.get(request.args.get("notice", "")),
        problem=_PROBLEMS.get(request.args.get("problem", "")),
    )


@bp.route("/rosters/group/<group_id>/toggle", methods=["POST"])
@require_login
def rosters_toggle_group(group_id):
    """Flip a Dispatcher's is_active. The helper reads current state itself
    (NULL-as-active handled there), so this is a plain fire-and-look."""
    try:
        ok = get_db().toggle_group_status(group_id)
    except Exception as e:
        logger.warning("dispatcher toggle failed for %s: %s", group_id, type(e).__name__)
        ok = False
    if ok:
        return redirect(url_for("dispatch_web.rosters", notice="dispatcher-toggled"))
    return redirect(url_for("dispatch_web.rosters", problem="toggle-failed"))


@bp.route("/rosters/driver/<driver_id>/toggle", methods=["POST"])
@require_login
def rosters_toggle_driver(driver_id):
    try:
        ok = get_db().toggle_driver_status(driver_id)
    except Exception as e:
        logger.warning("driver toggle failed for %s: %s", driver_id, type(e).__name__)
        ok = False
    if ok:
        return redirect(url_for("dispatch_web.rosters", notice="driver-toggled"))
    return redirect(url_for("dispatch_web.rosters", problem="toggle-failed"))


@bp.route("/rosters/driver/<driver_id>/suspend", methods=["POST"])
@require_login
def rosters_suspend_driver(driver_id):
    """Set the MANUAL suspend flag to the state the form asked for (explicit
    target, not a blind flip — two tabs cannot double-toggle each other).
    Receipt-debt suspension is not touchable from here by design."""
    want = request.form.get("suspended") == "1"
    try:
        ok = get_db().set_driver_suspended(driver_id, want)
    except Exception as e:
        logger.warning("driver suspend failed for %s: %s", driver_id, type(e).__name__)
        ok = False
    if ok:
        return redirect(
            url_for(
                "dispatch_web.rosters",
                notice="driver-suspended" if want else "driver-unsuspended",
            )
        )
    return redirect(url_for("dispatch_web.rosters", problem="suspend-failed"))
