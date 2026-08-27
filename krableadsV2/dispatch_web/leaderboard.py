"""Leaderboard — the web face of /leaderboard: who has entered the most clients.

Names and counts, nothing else — same reasoning as the bot's command: a board
with contact details on it is a poach list. get_lead_counts_by_sender already
applies every board rule (telegram_name over @handle, struck rows excluded,
most-first with ties alphabetical), so this view only adds rank marks. The
bot's 40-row cap is a Telegram message-length constraint, not a board rule;
a page scrolls, so every row renders.
"""

from flask import render_template

from .core import bp, get_db, require_login

_MEDALS = {1: "\U0001F947", 2: "\U0001F948", 3: "\U0001F949"}  # 🥇 🥈 🥉


@bp.route("/leaderboard")
@require_login
def leaderboard():
    error = None
    try:
        rows = get_db().get_lead_counts_by_sender()
    except Exception:
        # The helper swallows its own query errors (returns []), but get_db()
        # construction or the transport can still raise — recycled HTTP/2
        # connections do. Banner, never a traceback.
        error = "Could not reach the database — the board may be out of date."
        rows = []
    total = sum(n for _, n in rows)
    board = [
        {"rank": i, "mark": _MEDALS.get(i, f"{i}."), "name": name, "count": n}
        for i, (name, n) in enumerate(rows, start=1)
    ]
    return render_template(
        "dispatch/leaderboard.html", board=board, total=total, error=error
    )
