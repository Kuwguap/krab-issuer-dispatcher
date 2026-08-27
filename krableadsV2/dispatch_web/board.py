"""The Board — the bot's 🧾 Recent Leads browser, on the web.

Same anti-gaming purpose as bot._recent_leads_text_kb: the newest leads,
each with who entered it and whether it has been struck (exclude_from_count),
so a fake lead climbing the leaderboard is visible the moment it lands.
Struck rows stay LISTED — the strike must be visible, and reversible from
the lead page. The board only reads; striking lives at /dispatch/lead/<id>.

Two routes: "/" renders the page, "/data.json" feeds the 10-second refresh
(dispatch.js swaps the tbody with the fragment served here, so a refresh can
never drift from what a full page load would have shown).
"""

from flask import current_app, jsonify, render_template

from .core import bp, get_db, require_login

# The bot pages by 10; the web board shows one screenful instead of pages.
BOARD_PAGE_SIZE = 25


def _client_name(vehicle_details) -> str:
    """Line 1 of the stored Phase 1 blob is the registrant's name.

    Mirrors _phase1_from_stored_lead + _client_display_name_from_lead in
    bot.py: strip, and a "-" placeholder means the field was never filled.
    """
    lines = (vehicle_details or "").strip().splitlines()
    first = lines[0].strip() if lines else ""
    return first if first and first != "-" else "—"


def _entrant(lead: dict) -> str:
    """Who put the client into the bot — the bot's Recent Leads "who" line,
    byte for byte: NAME (@handle), one of the two, or "id N" as a last resort.
    """
    nm = (lead.get("telegram_name") or "").strip()
    un = (lead.get("telegram_username") or "").strip().lstrip("@")
    if un.lower() == "unknown":
        un = ""
    if nm and un:
        return f"{nm} (@{un})"
    return nm or (f"@{un}" if un else f"id {lead.get('user_id') or '?'}")


def _prices_for(db, lead_ids: list) -> dict:
    """{lead id: price} for the listed leads.

    list_recent_leads_for_review carries the bot's own column list, which has
    no price — and utils/ is not this package's to edit — so price rides in
    on one extra query. Losing it costs a "—" column, never the board.
    """
    if not lead_ids:
        return {}
    try:
        r = db.client.table("leads").select("id, price").in_("id", lead_ids).execute()
        return {row.get("id"): row.get("price") for row in (r.data or [])}
    except Exception as e:
        current_app.logger.warning("dispatch board: price lookup failed: %s", e)
        return {}


def _shape(lead: dict, prices: dict) -> dict:
    """One display row — the same dict feeds the template and data.json."""
    fmt = current_app.jinja_env.filters.get("fmt_ts") or (lambda v: v)
    lid = str(lead.get("id") or "")
    price = prices.get(lead.get("id"))
    price = str(price).strip() if price not in (None, "") else "—"
    created = str(lead.get("created_at") or "")
    return {
        "id": lid,
        "reference": str(lead.get("reference_id") or "N/A"),
        "client": _client_name(lead.get("vehicle_details")),
        "entrant": _entrant(lead),
        "price": price,
        "created": created,
        "created_fmt": fmt(created) if created else "—",
        "struck": bool(lead.get("exclude_from_count")),
        # The lead page is another module's route; the PATH is the contract.
        # A hardcoded path can never BuildError while modules land separately.
        "url": f"/dispatch/lead/{lid}" if lid else "",
    }


def _board_rows() -> tuple:
    """(shaped rows, total) — newest first, one screenful."""
    db = get_db()
    rows, total = db.list_recent_leads_for_review(0, BOARD_PAGE_SIZE)
    prices = _prices_for(db, [r.get("id") for r in (rows or []) if r.get("id")])
    return [_shape(r, prices) for r in (rows or [])], int(total or 0)


@bp.route("/")
@require_login
def board():
    error = None
    leads, total = [], 0
    try:
        leads, total = _board_rows()
    except Exception as e:
        # The helper swallows its own query errors ([], 0); this catches the
        # rest (Database() construction, the price merge re-raising, …).
        # Exception text carries API detail, never client field values.
        current_app.logger.warning("dispatch board: %s", e)
        error = "Couldn't reach the database — the board retries on its next refresh."
    return render_template("dispatch/board.html", leads=leads, total=total, error=error)


@bp.route("/data.json")
@require_login
def board_data():
    """The 10-second feed: the same rows as JSON, plus the rendered tbody.

    tbody_html comes from the same partial the page itself uses, so the
    swap is a dumb innerHTML and can never drift from a full page load.
    Failure is HTTP 503, never a 200: dispatch.js's poller keeps the last
    good rows only on a non-OK answer (and stamps "Refresh failed"), while
    a 200 whose tbody_html is "" would be swapped in and blank a board that
    had rows on it. The fixed error string leaks no exception text — the
    contract keeps str(e) out of every client-facing body.
    """
    try:
        leads, total = _board_rows()
        tbody_html = render_template("dispatch/_board_rows.html", leads=leads)
    except Exception as e:
        current_app.logger.warning("dispatch board data: %s", e)
        return jsonify({"ok": False, "rows": [], "total": 0, "tbody_html": "",
                        "error": "database unreachable"}), 503
    return jsonify({"ok": True, "rows": leads, "total": total,
                    "tbody_html": tbody_html, "error": None})
