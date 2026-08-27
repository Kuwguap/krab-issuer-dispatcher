"""/dispatch/receipts — the stored-receipt wall, newest first.

Receipts live as URLs on the lead row itself (receipt_image_url): usually a
Supabase storage public URL, sometimes an api.telegram.org file link — and
those EMBED THE BOT TOKEN in their path (bot.py's last-ditch fallback writes
fresh ones), so the stored URL must never reach the page. This view therefore
only asks WHICH leads hold a receipt — the URL column is filtered on, never
selected — and the template reaches every image through the host app's
token-free resolver /api/receipts/image/<lead_id> (admin_dashboard.py, same
Flask process), which streams DB/storage bytes and re-signs expired Telegram
links via their #tgfid= fragment. Attaching and re-hosting stay the bot's
job; ordering is by updated_at, which the receipt attach itself bumps.
"""

import logging

from flask import render_template

from .core import bp, get_db, require_login

logger = logging.getLogger(__name__)

# One screenful of proof-of-payment. No pager: anything older is reached from
# its lead page, not by scrolling a wall of thumbnails.
RECEIPTS_LIMIT = 30


@bp.route("/receipts", methods=["GET"])
@require_login
def receipts():
    rows, error = [], None
    try:
        # receipt_image_url is deliberately NOT in the select: a stored value
        # can be a Telegram file link with the bot token in its path, and any
        # value handed to the template is one {{ }} away from the page source.
        # Filtering on an unselected column is fine (PostgREST filters are
        # independent of select). neq "" alone would drop NULLs too
        # (three-valued <>), but both filters spelled out is the repo's idiom
        # and survives someone writing "".
        r = (
            get_db().client.table("leads")
            .select("id, reference_id, updated_at")
            .not_.is_("receipt_image_url", "null")
            .neq("receipt_image_url", "")
            .order("updated_at", desc=True)
            .limit(RECEIPTS_LIMIT)
            .execute()
        )
        rows = r.data or []
    except Exception as e:
        # The exception carries query shape at worst — lead contents (client
        # names, phones) are never selected here, so nothing PII can leak into
        # Sentry from this line.
        logger.error("receipts page query failed: %s", e)
        # Fixed banner text, per the contract: str(e) stays in the log, never
        # in the served page.
        error = "Could not load receipts — the database did not answer."
    return render_template("dispatch/receipts.html", rows=rows, error=error)
