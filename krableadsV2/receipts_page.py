"""The /receipts board — every transmission, its receipt, every party's contacts,
and where it has got to.

This is the full back-office page: month-grouped, numbered rows under sticky
headers; a status ladder that walks the whole journey (New Lead → Followup →
Tag issued → Tag emailed → Tag printed → Driver on the way → Receipt uploaded)
and is advanced automatically by the bot; per-party contact blocks with Send
email / Send SMS buttons; a Renewal countdown (29 days from tag issue); a
month-end money strip (all $, with-receipt $, and the gap); CSV export; and
five views — Table, Cards, Sheet (spreadsheet), Charts, CRM (kanban) — plus a
voice command mic (OpenAI-backed intent + answers) and an optional ambient
"game mode" sprite layer.

Senders are REUSED, not reinvented:
  * email — ``utils/client_outreach.send_client_email`` (Resend first, SendGrid
    fallback; BCCs ``FOLLOWUP_EMAIL_COPY`` like every other client email).
  * SMS   — GoHighLevel via ``utils/ghl_client`` when GHL_API_KEY +
    GHL_LOCATION_ID are set; otherwise the existing Twilio sender
    (``utils/client_outreach.send_client_sms``).

Exposure: tristatetags.com is the speedy-tags Vercel project, whose catch-all
rewrite sends unknown paths to the storefront — and whose ``/api/*`` already
belongs to the quicktags checkout proxy. That is why EVERY route this board
needs also exists under ``/receipts/*`` (page, data, status, notify, image,
voice, assets): one Vercel rewrite pair ``/receipts(/:path*)`` → this Flask
service exposes the whole board without touching the checkout proxy. The
original ``/api/…`` routes stay put for anything already pointed at them.

The heavier view modules (sheet, charts, CRM, themes, voice UI) are served
from ``receipts_assets.py`` and the sprite layer from ``receipts_game.py`` —
both OPTIONAL: delete either file and the board still runs; every integration
point is behind a ``typeof`` guard in the page and a try/except here.

Kept in its own module because admin_dashboard.py is already long, and because
the board is self-contained: it needs ``db`` and ``app`` and nothing else.
"""
import json
import logging
import os
import re
import time

from flask import Response, jsonify, request

logger = logging.getLogger(__name__)

# The journey, in the order the select offers it. 'delivered' is legacy but
# still real; rows stamped 'paid' by the old board DISPLAY as Receipt uploaded
# (see STATUS_ALIAS) and the endpoint still accepts 'paid' for old callers.
STATUS_LABELS = {
    "new": "New Lead",
    "followup": "Followup",
    "tag_issued": "Tag issued",
    "tag_emailed": "Tag emailed",
    "tag_printed": "Tag printed",
    "on_the_way": "Driver on the way",
    "delivered": "Delivered",
    "receipt_uploaded": "Receipt uploaded",
}
STATUS_ORDER = ("new", "followup", "tag_issued", "tag_emailed",
                "tag_printed", "on_the_way", "delivered", "receipt_uploaded")
STATUS_ALIAS = {"paid": "receipt_uploaded"}
ACCEPTED_STATUSES = STATUS_ORDER + ("paid",)
PARTIES = ("client", "driver", "issuer", "dispatcher")

# Receipt bytes are served straight back from this origin — an allowlist, never
# the row's own claim (image/svg+xml is a script container).
RECEIPT_MIME = {
    "image/jpeg": "image/jpeg",
    "image/jpg": "image/jpeg",
    "image/png": "image/png",
    "image/webp": "image/webp",
    "image/heic": "image/heic",
    "image/heif": "image/heif",
    "application/pdf": "application/pdf",
}

# The send buttons are one click on a shared board — a double-click must not
# text a client twice. Per (lead, party, channel), in-process.
_MIN_SECONDS_BETWEEN_SENDS = 15
_recent_sends = {}

# The voice endpoint costs a database sweep and (when configured) an OpenAI
# call per utterance — throttled per client and globally, with the aggregates
# cached, so a hammering script burns neither budget nor pool.
_VOICE_MIN_GAP_SEC = 2.0
_VOICE_GLOBAL_PER_MIN = 30
_voice_last_by_ip = {}
_voice_window = []
_voice_summary_cache = {"at": 0.0, "value": None}


def _agency() -> dict:
    """Branding for the message templates — same FOLLOWUP_* the bot uses."""
    try:
        from config import Config
        return {
            "name": Config.FOLLOWUP_AGENCY_NAME,
            "phone": Config.FOLLOWUP_PHONE,
            "website": Config.FOLLOWUP_WEBSITE,
        }
    except Exception:
        return {
            "name": (os.getenv("FOLLOWUP_AGENCY_NAME") or "Tri State Tags").strip(),
            "phone": (os.getenv("FOLLOWUP_PHONE") or "").strip(),
            "website": (os.getenv("FOLLOWUP_WEBSITE") or "tristatetags.com").strip(),
        }


def _email_copy_address() -> str:
    try:
        from config import Config
        return (getattr(Config, "FOLLOWUP_EMAIL_COPY", "") or "").strip()
    except Exception:
        return (os.getenv("FOLLOWUP_EMAIL_COPY") or "").strip()


def _mask_phone(p: str) -> str:
    digits = re.sub(r"\D", "", p or "")
    return ("…" + digits[-4:]) if len(digits) >= 4 else "…"


def _mask_email(e: str) -> str:
    e = (e or "").strip()
    if "@" not in e:
        return "…"
    user, _, domain = e.partition("@")
    return (user[:1] or "…") + "…@" + domain


def _client_name(lead: dict) -> str:
    lines = (lead.get("vehicle_details") or "").splitlines()
    return (lines[0] or "").strip() if lines else ""


def _party_contact(db, lead_id: str, party: str):
    """{"name","phone","email","reference_id"} for one party of one lead.

    Resolved fresh from the database on every send — the browser never chooses
    the destination address, only the party. Returns None when the lead is gone.
    """
    try:
        r = (
            db.client.table("leads")
            .select("id, reference_id, vehicle_details, phone_number, email, "
                    "telegram_username, user_id, group_id")
            .eq("id", str(lead_id)).limit(1).execute()
        )
        lead = (r.data or [None])[0]
    except Exception as e:
        logger.error("receipts board: lead lookup failed for %s: %s", lead_id, e)
        lead = None
    if not lead:
        return None
    ref = (lead.get("reference_id") or "").strip()

    if party == "client":
        return {
            "name": _client_name(lead) or "client",
            "phone": (lead.get("phone_number") or "").strip(),
            "email": (lead.get("email") or "").strip(),
            "reference_id": ref,
        }
    if party == "driver":
        drv = {}
        try:
            a = (
                db.client.table("lead_assignments")
                .select("lead_id, status, driver:drivers(driver_name, phone_number, email)")
                .eq("lead_id", str(lead_id)).eq("status", "accepted")
                .limit(1).execute()
            )
            drv = ((a.data or [{}])[0].get("driver") or {})
        except Exception as e:
            logger.warning("receipts board: driver lookup failed for %s: %s", lead_id, e)
        return {
            "name": (drv.get("driver_name") or "").strip() or "driver",
            "phone": (drv.get("phone_number") or "").strip(),
            "email": (drv.get("email") or "").strip(),
            "reference_id": ref,
        }
    if party == "issuer":
        # Issuers live in Telegram; no phone or email is stored for them, so the
        # buttons stay guarded until that day comes.
        return {
            "name": (lead.get("telegram_username") or "").strip() or "issuer",
            "phone": "",
            "email": "",
            "reference_id": ref,
        }
    # dispatcher — the team (group) the lead went to
    grp = {}
    try:
        gid = str(lead.get("group_id") or "")
        if gid:
            g = (
                db.client.table("groups")
                .select("id, group_name, group_telegram_id, supervisory_telegram_id")
                .eq("id", gid).limit(1).execute()
            )
            grp = (g.data or [{}])[0] or {}
    except Exception as e:
        logger.warning("receipts board: group lookup failed for %s: %s", lead_id, e)
    return {
        "name": (grp.get("group_name") or "").strip() or "dispatcher",
        "phone": "",
        "email": "",
        "reference_id": ref,
    }


# ── Voice: intent + answers ─────────────────────────────────────────────────
# Actions the page knows how to perform; anything else is answer-only.
VOICE_ACTIONS = (
    "set_view",       # args: {view: table|cards|sheet|chart|crm}
    "toggle_view",    # flips table <-> cards
    "set_theme",      # args: {theme: light|dark|midnight|matrix|sunset|ocean|monday|mono|bubblegum|auto}
    "game_mode",      # args: {mode: off|subtle|full}
    "play_tetris",    # opens the sprite layer's tetris minigame
    "celebrate",      # fires the goal.hit reaction
    "download_csv",
    "search",         # args: {query}
    "filter_status",  # args: {status}
    "filter_month",   # args: {month: "2026-05"}
    "refresh",
    "none",
)


def _money(price) -> float:
    try:
        return float(re.sub(r"[^0-9.]", "", str(price or "")) or 0)
    except Exception:
        return 0.0


def _voice_summary(rows: list) -> dict:
    """Compact aggregates the voice model (and the local fallback) answer from."""
    months, statuses, issuers, drivers = {}, {}, {}, {}
    total = total_rec = 0.0
    year_total = 0.0
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    for r in rows or []:
        amt = _money(r.get("price"))
        total += amt
        mk = str(r.get("created_at") or "")[:7] or "unknown"
        m = months.setdefault(mk, {"count": 0, "sum": 0.0, "receipt_sum": 0.0, "receipts": 0})
        m["count"] += 1
        m["sum"] += amt
        if r.get("has_receipt"):
            total_rec += amt
            m["receipt_sum"] += amt
            m["receipts"] += 1
        s = r.get("status") or "new"
        statuses[s] = statuses.get(s, 0) + 1
        iss = (r.get("issuer") or "").strip()
        if iss and iss != "—":
            issuers[iss] = issuers.get(iss, 0) + 1
        drv = (r.get("driver_name") or "").strip()
        if drv and drv != "—":
            drivers[drv] = drivers.get(drv, 0) + 1
        try:
            created = datetime.fromisoformat(str(r.get("created_at")).replace("Z", "+00:00"))
            if (now - created).days <= 365:
                year_total += amt
        except Exception:
            pass
    latest = (rows or [{}])[0]
    return {
        "total_leads": len(rows or []),
        "total_dollars": round(total, 2),
        "with_receipt_dollars": round(total_rec, 2),
        "missing_dollars": round(total - total_rec, 2),
        "last_365_days_dollars": round(year_total, 2),
        "by_month": {k: {kk: (round(vv, 2) if isinstance(vv, float) else vv)
                         for kk, vv in v.items()}
                     for k, v in sorted(months.items(), reverse=True)[:14]},
        "by_status": statuses,
        "top_issuers": sorted(issuers.items(), key=lambda x: -x[1])[:5],
        "top_drivers": sorted(drivers.items(), key=lambda x: -x[1])[:5],
        "latest_lead": {
            "client": latest.get("client_name"), "issuer": latest.get("issuer"),
            "price": latest.get("price"), "created_at": latest.get("created_at"),
            "status": latest.get("status"), "reference": latest.get("reference_id"),
        },
    }


VOICE_THEMES = ("light", "dark", "midnight", "matrix", "sunset", "ocean",
                "monday", "mono", "bubblegum")

# Every view and the many ways a person actually asks for it. ORDER MATTERS —
# the most specific pattern wins, so "excel sheet list" is never read as the
# plain table. Plurals and -ing forms are all matched: "charts", "charting"
# and "chart me" must behave identically to "chart".
_VIEW_PATTERNS = (
    ("sheet", r"excel|spread\s*sheet|\bsheets?\b|\bspreadsheets?\b"),
    ("chart", r"\bchart\w*|\bgraph\w*|diagram\w*|\bplots?\b|visuali[sz]\w*|"
              r"\bbars?\b|\bpie\b|\banalytics?\b|\bstats?\b|\bnumbers?\b|"
              r"\bfigures?\b|\btotals?\b"),
    ("crm",   r"\bcrm\b|pipeline\w*|kanban|deal ?board|\bdrag\b|\bstages?\b"),
    ("cards", r"\bcards?\b|\btiles?\b"),
    ("table", r"\btables?\b|row by column|column view|\bgrid\b|\brows?\b|"
              r"\blist\b|\bspreadsheet-less\b|normal view|default view"),
)

# Clause splitter for multi-intent utterances: "tell me the last client AND
# change the theme". Deliberately generous — a wrongly split clause simply
# fails to match and is dropped, while a missed split loses a whole command.
_CLAUSE_SPLIT = re.compile(
    r"\s+(?:and then|and also|and|then|also|plus|as well as|&)\s+|[,;]+")

# Small rotating pools so the assistant never sounds like a recording.
_SAY = {
    "sheet": ("Here's the spreadsheet.", "Spreadsheet view, coming up.",
              "Switching to the sheet."),
    "chart": ("Here are the numbers as charts.", "Charts it is.",
              "Pulling up the graphs."),
    "crm": ("CRM mode.", "Here's the pipeline.", "Switching to the deal board."),
    "cards": ("Card view.", "Here are the cards.", "Switching to cards."),
    "table": ("Back to the table.", "Row by column, coming up.",
              "Here's the table view."),
    "csv": ("Downloading the CSV.", "Exporting it now.", "CSV on its way."),
    "refresh": ("Refreshing.", "Pulling the latest.", "Reloading the board."),
    "toggle": ("Toggling the view.", "Flipping it over."),
    "celebrate": ("Let's go! 🎉", "Confetti incoming!", "Celebrating!"),
    "huh": ("I didn't catch that. You can ask for a view — table, cards, sheet, "
            "charts or CRM — a different theme, the CSV, or anything about the "
            "numbers.",
            "Not sure what you meant. Try \"charts\", \"another theme\", "
            "\"download the CSV\", or ask how much we made this month."),
}


def _pick(key: str) -> str:
    import random
    return random.choice(_SAY[key])


def _pick_other_theme(current: str) -> str:
    """A theme that is NOT the one they are looking at — what "I don't like
    this one" and "change the theme" both mean."""
    import random
    cur = (current or "").strip().lower()
    pool = [t for t in VOICE_THEMES if t != cur] or list(VOICE_THEMES)
    return random.choice(pool)


def _voice_answer(t: str, summary: dict):
    """A spoken answer for a question about the board, or None."""
    money = lambda v: f"${(v or 0):,.0f}"                                # noqa: E731
    latest = summary.get("latest_lead") or {}

    if re.search(r"(name|who).*(last|latest|recent|newest).*(client|customer)"
                 r"|(last|latest|recent|newest) (client|customer)", t):
        who = latest.get("client") or "unknown"
        return (f"The last client is {who}"
                + (f", {latest.get('price')}" if latest.get("price") else "")
                + (f", entered by {latest.get('issuer')}." if latest.get("issuer")
                   else "."))
    if re.search(r"(who|which).*(issuer|sent|entered).*(last|latest|recent)"
                 r"|(last|latest|recent).*(lead|transmission)", t):
        return (f"The most recent lead is {latest.get('client') or 'unknown'} "
                f"for {latest.get('price') or 'an unknown amount'}, entered by "
                f"{latest.get('issuer') or 'an unknown issuer'}.")
    if re.search(r"(top|best|most).*(driver)", t):
        top = (summary.get("top_drivers") or [])
        return (f"{top[0][0]} is top with {top[0][1]} leads." if top
                else "No driver has taken a lead yet.")
    if re.search(r"(top|best|most).*(issuer|agent|sender)", t):
        top = (summary.get("top_issuers") or [])
        return (f"{top[0][0]} leads with {top[0][1]} entries." if top
                else "No issuer activity yet.")
    # Small talk gets a small answer rather than a wall of instructions — but
    # ONLY when the greeting is the whole utterance. "hi, how much did we make"
    # is a question wearing a hello.
    if re.fullmatch(r"\s*(?:hi|hey|hello|yo|sup|good (?:morning|afternoon|evening))"
                    r"[\s,!.]*(?:there|everyone|team)?[\s,!.?]*", t):
        import random
        return random.choice(("Hey — what do you need?", "Hi. What can I do?",
                              "Hey there. Ask me for a view or a number."))
    if re.search(r"\bthank|\bthanks\b|\bcheers\b|\bnice one\b|\bappreciate", t):
        import random
        return random.choice(("Anytime.", "You got it.", "No problem."))
    if re.search(r"never ?mind|forget it|nothing|cancel that", t):
        return "No worries."
    if re.search(r"what can you do|help me|what do you do|your commands|"
                 r"how do (?:i|you) (?:use|work)", t):
        return ("I can switch views — table, cards, sheet, charts or CRM — change "
                "the theme, filter by month or status, download the CSV, start "
                "tetris, and answer questions about the money, the receipts, the "
                "drivers and the issuers. Say two things at once and I'll do both.")
    if re.search(r"how are we doing|how'?s business|how'?s it going|"
                 r"the summary|summar(?:y|ise|ize)|overview|how'?s the board", t):
        by = summary.get("by_status") or {}
        done = by.get("receipt_uploaded", 0) + by.get("paid", 0)
        return (f"{summary.get('total_leads', 0)} transmissions, "
                f"{money(summary.get('total_dollars'))} all in — "
                f"{done} receipted, {money(summary.get('missing_dollars'))} "
                "still owed.")
    # "how many are MISSING receipts" must not answer with the receipted count.
    if re.search(r"(how many|count|number of).*(missing|owed|outstanding|"
                 r"no receipt|without)", t):
        by = summary.get("by_status") or {}
        done = by.get("receipt_uploaded", 0) + by.get("paid", 0)
        return (f"{max(0, summary.get('total_leads', 0) - done)} transmissions "
                f"have no receipt yet, worth "
                f"{money(summary.get('missing_dollars'))}.")
    if re.search(r"(how many|count|number of).*(receipt)", t):
        by = summary.get("by_status") or {}
        return (f"{by.get('receipt_uploaded', 0) + by.get('paid', 0)} "
                "transmissions have a receipt uploaded.")
    if re.search(r"how many|count of|number of", t):
        return f"{summary.get('total_leads', 0)} transmissions on the board."
    if re.search(r"missing|owed|outstanding|no receipt|without.*receipt", t):
        return (f"{money(summary.get('missing_dollars'))} has no receipt "
                "against it yet.")
    if re.search(r"(this|current) month|month so far", t):
        mk = sorted((summary.get("by_month") or {}).keys(), reverse=True)
        if mk:
            m = summary["by_month"][mk[0]]
            return (f"{money(m.get('sum'))} this month across "
                    f"{m.get('count', 0)} transmissions, "
                    f"{money(m.get('receipt_sum'))} of it receipted.")
    if re.search(r"(year|365|twelve month|12 month)", t) and \
            re.search(r"how much|revenue|made|total|earn|sales", t):
        return (f"About {money(summary.get('last_365_days_dollars'))} in the last "
                f"twelve months, across {summary.get('total_leads', 0)} "
                "transmissions on the board.")
    if re.search(r"how much|revenue|made|total|earn|sales|money", t):
        return (f"{money(summary.get('total_dollars'))} across the board — "
                f"{money(summary.get('with_receipt_dollars'))} of it has a "
                "receipt uploaded.")
    return None


# Spoken names for the board's statuses, for "show me only the paid ones".
_STATUS_WORDS = (
    ("receipt_uploaded", r"paid|receipts? uploaded|receipted"),
    ("on_the_way", r"on the way|out for delivery|en route"),
    ("tag_printed", r"printed"),
    ("tag_emailed", r"emailed"),
    ("tag_issued", r"issued"),
    ("followup", r"follow ?up"),
    ("delivered", r"delivered"),
    ("new", r"new leads?|brand new|unassigned"),
)
_MONTHS = ("january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december")

# A question wants an ANSWER even when it names something actionable:
# "how much did we make" must never open the charts.
_QUESTION = re.compile(
    r"\bhow (?:much|many|are|is|'?s)\b|\bwhat'?s?\b|\bwho'?s?\b|\bwhich\b|"
    r"\bwhen\b|\btell me\b|\bname of\b|\?\s*$")

# An explicit "give me the file" beats every view that shares a word with it —
# "export to excel" is a download, not the spreadsheet view.
_DOWNLOAD = re.compile(
    r"(?:download|export|save|send me|email me)\b[^.]{0,40}?"
    r"\b(?:csv|excel|spread\s*sheet|spreadsheet|file|report|data)\b"
    # …and the words the other way round: "excel export", "spreadsheet download"
    r"|\b(?:csv|excel|spread\s*sheet|spreadsheet)\s+(?:export|download|file|dump)\b"
    r"|\bcsv\b|\bcee ess vee\b")


def _voice_clause(t: str, ctx: dict, summary: dict):
    """One clause -> (action, args, say) or None. `t` is already lowercased."""
    themed = bool(re.search(r"\btheme\b|\bcolou?rs?\b|\bskin\b|\bpalette\b", t))
    named = re.search(r"\b(" + "|".join(VOICE_THEMES) + r"|auto|default|system)\b", t)
    wants_other = bool(re.search(
        r"\b(?:change|switch|different|another|other|next|new|random|surprise|"
        r"anything else|something else)\b", t)) or bool(re.search(
        r"\b(?:don'?t|do not|dont) (?:like|want)\b|\bhate\b|\bugly\b|"
        r"\bnot? a fan\b|\btoo (?:bright|dark)\b", t))

    def theme_named(name):
        if name in ("default", "system", "auto"):
            return ("set_theme", {"theme": "auto"}, "Back to the system theme.")
        return ("set_theme", {"theme": name}, "Switching to " + name + ".")

    # ── A file request, FIRST: it shares its nouns with the sheet view ────
    if _DOWNLOAD.search(t):
        return ("download_csv", {}, _pick("csv"))

    # ── Themes, by name or by feeling ────────────────────────────────────
    if themed:
        if named:
            return theme_named(named.group(1))
        pick = _pick_other_theme(ctx.get("theme"))
        reason = ("Not a fan? Here's " if re.search(
            r"don'?t|dont|hate|ugly|bright|dark", t) else "Here's ")
        return ("set_theme", {"theme": pick}, reason + pick + " instead.")
    # "dark mode", "night mode", "light mode" — the word "theme" never appears.
    if not named:
        mode_word = re.search(r"\b(night|dark|day|light)\b\s*mode\b|"
                              r"\bgo (dark|light)\b|\bmake it (dark|light)\b", t)
        if mode_word:
            hit = next(g for g in mode_word.groups() if g)
            return theme_named("dark" if hit in ("night", "dark") else "light")

    # ── Views ────────────────────────────────────────────────────────────
    mentions_view = bool(re.search(r"\bviews?\b|\blayouts?\b|\bdisplays?\b", t))
    if (re.search(r"\btoggle\b|\bflip\b|\bswap\b", t)
            or (wants_other and mentions_view)) and \
            not re.search("|".join(p for _, p in _VIEW_PATTERNS), t):
        return ("toggle_view", {}, _pick("toggle"))
    for view, pattern in _VIEW_PATTERNS:
        if re.search(pattern, t):
            return ("set_view", {"view": view}, _pick(view))

    # ── Everything else ──────────────────────────────────────────────────
    if "tetris" in t or re.search(r"play (?:a |the )?game|mini ?game", t):
        return ("play_tetris", {},
                "Let's play! Arrow keys, or drag and tap on a phone.")
    if re.search(r"\bgame mode\b|\bsprites?\b|\bcharacters?\b|video game", t):
        mode = ("off" if re.search(r"\boff\b|kill|stop|quiet|hide", t)
                else "subtle" if re.search(r"subtle|calm|less|fewer", t) else "full")
        return ("game_mode", {"mode": mode},
                "Turning the characters off." if mode == "off"
                else "Game mode " + mode + ".")
    if re.search(r"celebrat|confetti|party|hooray", t):
        return ("celebrate", {}, _pick("celebrate"))
    if re.search(r"refresh|reload|update the board|latest data", t):
        return ("refresh", {}, _pick("refresh"))

    # Filters — "show me only the paid ones", "just august"
    if re.search(r"\bonly\b|\bjust\b|\bfilter\b", t):
        for status, pattern in _STATUS_WORDS:
            if re.search(pattern, t):
                return ("filter_status", {"status": status},
                        "Filtering to " + STATUS_LABELS.get(status, status) + ".")
    for i, month in enumerate(_MONTHS, start=1):
        if re.search(r"\b" + month + r"\b", t):
            from datetime import datetime, timezone
            ym = re.search(r"\b(20\d\d)\b", t)
            year = int(ym.group(1)) if ym else datetime.now(timezone.utc).year
            return ("filter_month", {"month": "%d-%02d" % (year, i)},
                    "Showing " + month.capitalize() + " " + str(year) + ".")

    m = re.search(r"(?:search|find|look ?for|filter)\s+"
                  r"(?:for\s+|by\s+|on\s+|to\s+)?(.+)", t)
    if m and len(m.group(1).strip()) > 1:
        q = m.group(1).strip().strip("\"'")
        return ("search", {"query": q}, "Searching for " + q + ".")

    # A theme named on its own ("matrix", "put it on mono") — LAST, so any view
    # or command sharing that word always wins first.
    if named:
        return theme_named(named.group(1))
    return None


def _voice_local(text: str, ctx: dict, summary: dict) -> dict:
    """The offline brain — and, since OPENAI_API_KEY is often unset on this
    service, the one that actually answers most of the time.

    Multi-intent by design: an utterance is split into clauses so "tell me the
    last client and change the theme" both answers AND acts."""
    whole = (text or "").lower().strip()
    clauses = [c.strip() for c in _CLAUSE_SPLIT.split(whole) if c and c.strip()]
    if not clauses:
        clauses = [whole]

    # Only one of these can be true of the board at a time, so a later one
    # REPLACES an earlier one ("cards then the sheet" ends on the sheet)
    # instead of being dropped as a duplicate.
    exclusive = ("set_view", "set_theme", "game_mode", "filter_status",
                 "filter_month")
    actions, says, seen = [], [], set()

    def add(action, args, line):
        if action in exclusive:
            for i, existing in enumerate(actions):
                if existing["action"] == action:
                    actions.pop(i)                  # keep the LAST word on it
                    break
        elif (action, json.dumps(args, sort_keys=True)) in seen:
            return                                  # the same deed twice: once is enough
        seen.add((action, json.dumps(args, sort_keys=True)))
        actions.append({"action": action, "args": args})
        says.append(line)

    for clause in clauses:
        # A question is answered even when it names something actionable
        # ("how much did we make" must not open the charts); anything else
        # prefers the command ("chart the revenue" charts).
        asked = bool(_QUESTION.search(clause))
        hit = None if asked else _voice_clause(clause, ctx, summary)
        if hit:
            add(*hit)
            continue
        answer = _voice_answer(clause, summary)
        if answer:
            says.append(answer)
            continue
        hit = hit or _voice_clause(clause, ctx, summary)
        if hit:
            add(*hit)

    # Nothing landed clause by clause — try the whole utterance once, since a
    # clumsy split can strand a command ("switch me over to, uh, charts").
    if not actions and not says:
        answer = _voice_answer(whole, summary)
        if answer:
            says.append(answer)
        else:
            hit = _voice_clause(whole, ctx, summary)
            if hit:
                add(*hit)

    # Only now, and only for the WHOLE utterance: a bare opinion ("I don't like
    # this one", "change it") is about the look. Per-clause this fired on
    # fragments a speech-to-text comma had stranded — "switch me over to, uh,
    # charts" flipped the palette on its way to the charts.
    if not actions and not says:
        if re.search(r"\b(?:change|different|another|other|next|new|random|"
                     r"surprise|something else)\b", whole) or re.search(
                     r"\b(?:don'?t|dont) (?:like|want)\b|\bhate\b|\bugly\b", whole):
            if not re.search(r"\bclient|driver|lead|receipt|issuer|dispatcher|"
                             r"status|price|tag\b", whole):
                pick = _pick_other_theme(ctx.get("theme"))
                add("set_theme", {"theme": pick},
                    f"No problem — trying {pick} instead.")
    if not says:
        says.append(_pick("huh"))
    return {"say": " ".join(says)[:600], "actions": actions}


def _voice_openai(text: str, ctx: dict, summary: dict):
    """Ask OpenAI for {say, actions[]}. None when unavailable — the local brain
    answers instead, so the mic never goes dead with the API down."""
    key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not key:
        return None
    try:
        from openai import OpenAI
        # max_retries=0 + a hard timeout, like utils/nl_router.py — a Flask
        # request must answer or fall back, never hang on retries.
        client = OpenAI(api_key=key, timeout=15, max_retries=0)
        model = ((os.getenv("OPENAI_ADMIN_MODEL") or "").strip()
                 or (os.getenv("OPENAI_MODEL") or "").strip()
                 or "gpt-4o-mini")
        system = (
            "You are the voice assistant for a temporary-tag back-office board "
            "(transmissions, receipts, drivers, issuers, dispatchers). Reply "
            "with ONE JSON object: {\"say\": string, \"actions\": "
            "[{\"action\": string, \"args\": object}]}. "
            "'say' is a short, natural spoken reply (1-2 sentences, no markdown) "
            "— vary your wording, never sound canned. "
            "An utterance may contain SEVERAL requests: put one entry in "
            "'actions' for EACH thing to do, in order, and answer any question "
            "in 'say'. Use an empty actions list for pure questions. "
            "Valid actions: " + ", ".join(a for a in VOICE_ACTIONS if a != "none")
            + ". View names: table (row by column), cards, sheet (excel/"
            "spreadsheet), chart (diagram/graphs), crm (pipeline/kanban). "
            "Themes: " + ", ".join(VOICE_THEMES) + ", auto. If they ask to "
            "change the theme, dislike the current one, or want 'another', pick "
            "a specific theme that is NOT the current one (given in ui.theme) "
            "and name your pick in 'say'. game_mode modes: off, subtle, full — "
            "'game mode'/'video game view' means full unless they say subtle or "
            "off. 'tetris' or 'play a game' means play_tetris. Answer money and "
            "count questions from DATA truthfully; amounts are USD."
        )
        user = json.dumps({"command": text, "ui": ctx, "data": summary})
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            response_format={"type": "json_object"},
            max_tokens=400,
            temperature=0.4,             # a little warmth; the schema keeps it honest
        )
        out = json.loads(resp.choices[0].message.content or "{}")
        actions = []
        raw = out.get("actions")
        if not isinstance(raw, list):                # tolerate the old single shape
            raw = [{"action": out.get("action"), "args": out.get("args")}]
        for item in raw[:4]:
            if not isinstance(item, dict):
                continue
            name = item.get("action")
            if name in VOICE_ACTIONS and name != "none":
                args = item.get("args")
                actions.append({"action": name,
                                "args": args if isinstance(args, dict) else {}})
        return {"say": str(out.get("say") or "")[:600], "actions": actions}
    except Exception as e:
        logger.warning("voice: OpenAI intent failed: %s", e)
        return None


BOARD_HTML = r"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Receipts &amp; Transmissions</title>
<link rel="stylesheet" href="/receipts/asset/themes.css">
<style>
 :root {
   --bg:#f4f5f7; --card:#fff; --ink:#172b4d; --muted:#6b778c; --line:#dfe1e6;
   --soft:#f8f9fb; --accent:#0065ff;
   --new:#8993a4; --followup:#ff991f; --issued:#00b8d9; --emailed:#36b37e;
   --printed:#8777d9; --otw:#0065ff; --del:#00875a; --paid:#00ca72;
   --ok-bg:#e3fcef; --ok-ink:#006644; --bad-bg:#ffebe6; --bad-ink:#bf2600;
   --z-sprites:20;
 }
 @media (prefers-color-scheme: dark) {
   :root:not([data-theme]) { --bg:#1d2125; --card:#22272b; --ink:#e6edf3; --muted:#9fadbc;
           --line:#2c333a; --soft:#1a1e22;
           --ok-bg:#133527; --ok-ink:#7ee2b8; --bad-bg:#42221f; --bad-ink:#ff9c8f; }
 }
 * { box-sizing:border-box; }
 html, body { height:100%; }
 body { margin:0; font:14px/1.45 -apple-system,system-ui,"Segoe UI",sans-serif;
        background:var(--bg); color:var(--ink);
        transition:background-color .25s, color .25s; }
 header { background:var(--card); border-bottom:1px solid var(--line);
          padding:12px 18px 10px; display:flex; gap:12px; align-items:center;
          flex-wrap:wrap; }
 h1 { font-size:18px; margin:0; font-weight:700; white-space:nowrap; }
 .sub { color:var(--muted); font-size:12px; }
 .grow { flex:1; }
 input[type=search] { padding:8px 12px; border:1px solid var(--line); border-radius:8px;
                      background:var(--bg); color:inherit; min-width:200px; }
 .tabs { display:flex; gap:6px; flex-wrap:wrap; }
 .tab { padding:6px 12px; border:1px solid var(--line); border-radius:20px;
        background:transparent; color:var(--muted); cursor:pointer; font-weight:600;
        font-size:12.5px; }
 .tab.on { background:var(--ink); color:var(--card); border-color:var(--ink); }
 .counts { color:var(--muted); font-size:12px; }
 .who, .hbtn { border:1px dashed var(--line); border-radius:20px; padding:6px 12px;
        background:transparent; color:var(--muted); cursor:pointer; font-size:13px;
        white-space:nowrap; }
 .hbtn { border-style:solid; }
 .hbtn:hover { color:var(--accent); border-color:var(--accent); }
 .seg { display:flex; border:1px solid var(--line); border-radius:9px; overflow:hidden; }
 .seg button { border:0; background:transparent; color:var(--muted); padding:7px 11px;
               cursor:pointer; font-weight:650; font-size:12.5px; border-right:1px solid var(--line); }
 .seg button:last-child { border-right:0; }
 .seg button.on { background:var(--accent); color:#fff; }
 #stats { display:flex; gap:10px; flex-wrap:wrap; padding:10px 18px 0; }
 .stat { background:var(--card); border:1px solid var(--line); border-radius:10px;
         padding:8px 14px; font-size:13px; color:var(--muted); }
 .stat b { color:var(--ink); font-size:15px; margin-left:6px; }
 .stat.warn b { color:var(--bad-ink); }
 #cfg { margin:10px 18px 0; padding:9px 14px; border-radius:8px; font-size:13px;
        background:var(--soft); border:1px solid var(--line); color:var(--muted);
        display:none; }
 .err { background:var(--bad-bg); color:var(--bad-ink); padding:10px 14px;
        border-radius:8px; margin:10px 18px; }
 main { padding:12px 18px 80px; }
 .view { display:none; }
 .view.on { display:block; }
 .wrap { overflow:auto; background:var(--card); border:1px solid var(--line);
         border-radius:10px; max-height:calc(100vh - 230px); }
 table { width:100%; min-width:1420px; border-collapse:collapse; }
 th { position:sticky; top:0; z-index:3; background:var(--card);
      text-align:left; font-size:11px; letter-spacing:.06em; text-transform:uppercase;
      color:var(--muted); padding:10px 12px; border-bottom:1px solid var(--line);
      white-space:nowrap; box-shadow:0 1px 0 var(--line); }
 td { padding:11px 12px; border-bottom:1px solid var(--line); vertical-align:top; }
 tr.row:hover { background:rgba(0,101,255,.05); }
 tr.mrow td { background:var(--soft); font-weight:750; cursor:pointer; padding:9px 12px;
              font-size:13px; letter-spacing:.02em; user-select:none; }
 tr.mrow .msum { color:var(--muted); font-weight:600; font-size:12px; margin-left:10px; }
 .idx { color:var(--muted); font-size:12px; font-weight:650; }
 .ref { font-family:ui-monospace,monospace; font-size:12px; color:var(--muted); }
 .cname { font-weight:650; }
 .carline { color:var(--muted); font-size:12px; }
 .exp { cursor:pointer; user-select:none; color:var(--muted); width:26px; }
 .pill { display:inline-block; padding:3px 10px; border-radius:20px; font-size:11.5px;
         font-weight:650; color:#fff; white-space:nowrap; }
 .s-new{background:var(--new)} .s-followup{background:var(--followup)}
 .s-tag_issued{background:var(--issued)} .s-tag_emailed{background:var(--emailed)}
 .s-tag_printed{background:var(--printed)} .s-on_the_way{background:var(--otw)}
 .s-delivered{background:var(--del)} .s-receipt_uploaded{background:var(--paid)}
 select.status { margin-top:5px; padding:5px 8px; border-radius:8px;
                 border:1px solid var(--line); background:var(--bg); color:inherit;
                 font-weight:600; max-width:150px; }
 .renew { display:inline-block; padding:3px 9px; border-radius:8px; font-size:12px;
          font-weight:700; border:1px solid var(--line); white-space:nowrap; }
 .renew.ok { color:var(--ok-ink); background:var(--ok-bg); border-color:transparent; }
 .renew.soon { color:#8a5b00; background:rgba(255,153,31,.18); border-color:transparent; }
 .renew.due { color:var(--bad-ink); background:var(--bad-bg); border-color:transparent; }
 .ins { display:inline-block; padding:3px 9px; border-radius:8px; font-size:11.5px;
        font-weight:700; white-space:nowrap; border:1px solid var(--line);
        color:var(--muted); }
 .ins.sent { color:var(--ok-ink); background:var(--ok-bg); border-color:transparent; }
 .ins.issued { color:#0b5c7a; background:rgba(0,184,217,.18); border-color:transparent; }
 .ins.pending { color:#8a5b00; background:rgba(255,153,31,.18); border-color:transparent; }
 .ins.failed { color:var(--bad-ink); background:var(--bad-bg); border-color:transparent; }
 .ins.none { opacity:.45; }
 /* The chip is the card once one exists — make that obvious. */
 a.ins { cursor:pointer; text-decoration:none; }
 a.ins:hover { filter:brightness(.94); text-decoration:none; box-shadow:0 0 0 1px var(--accent); }
 .insbox { margin-top:10px; border:1px solid var(--line); border-radius:10px;
           padding:10px 12px; background:var(--card); }
 .insbox h4 { margin:0 0 8px; font-size:11px; letter-spacing:.06em;
              text-transform:uppercase; color:var(--muted); }
 .thumb { width:72px; height:52px; object-fit:cover; border-radius:8px;
          border:1px solid var(--line); cursor:zoom-in; display:block; background:var(--soft); }
 .nothumb { width:72px; height:52px; border:1px dashed var(--line); border-radius:8px;
            color:var(--muted); font-size:11px; display:flex; align-items:center;
            justify-content:center; text-align:center; }
 .rinfo { margin-top:5px; font-size:12px; color:var(--muted); }
 .rinfo b { color:var(--ink); }
 .phone a { font-weight:650; white-space:nowrap; }
 .party { min-width:158px; font-size:12.5px; }
 .pname { font-weight:650; margin-bottom:2px; }
 .cl { color:var(--ink); margin:1px 0; white-space:nowrap; overflow:hidden;
       text-overflow:ellipsis; max-width:210px; }
 .cl .ic { display:inline-block; width:15px; color:var(--muted); }
 .cl.none { color:var(--muted); }
 .acts { margin-top:6px; display:flex; gap:5px; flex-wrap:wrap; }
 .act { border:1px solid var(--line); background:var(--soft); color:var(--ink);
        border-radius:7px; padding:3px 8px; font-size:12px; font-weight:600;
        cursor:pointer; }
 .act:hover { border-color:var(--accent); color:var(--accent); }
 .act.dim { opacity:.45; }
 .detail { background:var(--soft); }
 .detail dl { display:grid; grid-template-columns:max-content 1fr; gap:6px 16px; margin:0; }
 .detail dt { color:var(--muted); font-size:12px; }
 .detail dd { margin:0; min-width:0; overflow-wrap:anywhere; }
 .detail img { max-width:min(460px,100%); border-radius:8px; margin-top:10px;
               border:1px solid var(--line); cursor:zoom-in; }
 a { color:var(--otw); text-decoration:none; }
 a:hover { text-decoration:underline; }
 .none { color:var(--muted); }
 .saving { opacity:.5; }
 .overlay { position:fixed; inset:0; background:rgba(9,30,66,.55); z-index:50;
            display:flex; align-items:center; justify-content:center; padding:20px; }
 .overlay[hidden] { display:none; }
 .sheet-modal { background:var(--card); color:var(--ink); border-radius:12px;
          width:min(560px,100%); max-height:92vh; overflow:auto; padding:20px 22px;
          box-shadow:0 18px 50px rgba(0,0,0,.35); }
 .sheet-modal h2 { margin:0 0 4px; font-size:16px; }
 .c-to { color:var(--muted); font-size:13px; margin-bottom:12px; }
 .sheet-modal label { display:block; font-size:12px; color:var(--muted); margin:10px 0 4px; }
 .sheet-modal input, .sheet-modal textarea { width:100%; padding:9px 11px;
        border:1px solid var(--line);
        border-radius:8px; background:var(--bg); color:inherit; font:inherit; }
 .sheet-modal textarea { resize:vertical; }
 .c-actions { display:flex; gap:8px; align-items:center; margin-top:14px; }
 .c-actions .spacer { flex:1; }
 .btn { border-radius:8px; padding:8px 16px; font-weight:650; cursor:pointer;
        border:1px solid var(--line); background:var(--soft); color:var(--ink); }
 .btn.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
 .btn[disabled] { opacity:.55; cursor:default; }
 #c-result { font-size:13px; }
 #c-result.ok { color:var(--ok-ink); }
 #c-result.bad { color:var(--bad-ink); }
 #lightbox img { max-width:94vw; max-height:92vh; border-radius:10px; cursor:zoom-out;
                 background:#fff; }
 #toasts { position:fixed; right:16px; bottom:84px; z-index:60; display:flex;
           flex-direction:column; gap:8px; }
 .toast { padding:10px 14px; border-radius:8px; font-size:13px; font-weight:600;
          box-shadow:0 6px 18px rgba(0,0,0,.25); pointer-events:none; }
 /* While a tetris game is up, toasts move to the top so the touch controls
    underneath stay tappable. The sprite layer toggles the class. */
 body.krab-tetris-on #toasts { top:70px; bottom:auto; }
 /* The floating mic must never sit on top of an open modal sheet. The body
    class is set in JS (bulletproof); :has() is the belt-and-braces path. */
 body.krab-modal-open .vc-root, body.krab-modal-open #vc-root,
 body:has(.overlay:not([hidden])) .vc-root,
 body:has(.overlay:not([hidden])) #vc-root { display:none !important; }
 .toast.ok { background:var(--ok-bg); color:var(--ok-ink); }
 .toast.bad { background:var(--bad-bg); color:var(--bad-ink); }
 /* ── Phone cards ────────────────────────────────────────────────────────── */
 .mdivider { font-weight:750; color:var(--muted); padding:8px 4px 2px; cursor:pointer;
             user-select:none; font-size:13px; }
 .card { background:var(--card); border:1px solid var(--line); border-radius:12px;
         padding:12px 14px; }
 .c-top { display:flex; gap:10px; align-items:flex-start; justify-content:space-between; }
 .c-top .ref { margin-top:2px; }
 .c-mid { display:flex; gap:12px; margin-top:10px; align-items:flex-start; }
 .c-mid .thumb, .c-mid .nothumb { width:96px; height:72px; flex:0 0 auto; }
 .c-facts { flex:1; min-width:0; display:flex; flex-direction:column; gap:6px; }
 .c-facts select.status { margin-top:2px; align-self:flex-start; }
 .c-phone a { font-weight:650; }
 .c-parties { margin-top:10px; border-top:1px solid var(--line); }
 .prow { display:flex; align-items:center; gap:8px; padding:7px 0;
         border-bottom:1px solid var(--line); }
 .plabel { flex:0 0 78px; color:var(--muted); font-size:11px; letter-spacing:.05em;
           text-transform:uppercase; }
 .pwho { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis;
         white-space:nowrap; font-weight:600; font-size:13px; }
 .pacts { display:flex; gap:6px; flex:0 0 auto; }
 .c-more { margin-top:8px; border:0; background:transparent; color:var(--muted);
           font:inherit; font-size:13px; font-weight:650; cursor:pointer;
           padding:6px 0; text-align:left; }
 .c-detail { border-top:1px dashed var(--line); padding-top:10px; margin-top:2px;
             font-size:13px; }
 .c-detail img { max-width:100%; }
 .empty { padding:30px 10px; text-align:center; }
 #vw-sheet, #vw-chart, #vw-crm { min-height:300px; }
 footer { position:fixed; left:0; right:0; bottom:0; padding:6px 18px;
          font-size:11px; color:var(--muted); background:var(--bg); text-align:right;
          pointer-events:none; z-index:5; }
 @media (max-width:860px) {
   header { padding:12px 12px 10px; gap:8px; }
   h1 { font-size:16px; }
   .sub { display:none; }
   main { padding:10px 8px 90px; }
   #stats { padding:8px 8px 0; }
   .wrap { max-height:none; }
   .tabs { width:100%; order:3; overflow-x:auto; flex-wrap:nowrap;
           scrollbar-width:none; padding-bottom:2px; }
   .tab { flex:0 0 auto; }
   input[type=search] { flex:1; min-width:110px; font-size:16px; }
   select.status, .sheet-modal input, .sheet-modal textarea { font-size:16px; }
   .vc-input { font-size:16px !important; }   /* iOS zooms sub-16px inputs */
   .act { padding:8px 12px; }
   .overlay { padding:0; align-items:flex-end; }
   .sheet-modal { width:100%; max-height:88vh; border-radius:14px 14px 0 0; padding:16px; }
   #toasts { left:12px; right:12px; bottom:84px; }
   footer { display:none; }
 }
 @media (max-width:760px) { .hide-sm { display:none; } }
</style></head><body>
<header>
  <div>
    <h1>🧾 Receipts &amp; Transmissions</h1>
    <div class="sub">Every transmission, its receipt, every party — and where it has got to.</div>
  </div>
  <div class="tabs" id="tabs"></div>
  <span class="grow"></span>
  <input type="search" id="q" placeholder="Search ref, client, driver, phone…">
  <div class="seg" id="seg">
    <button data-view="table" title="Row by column">📋 Table</button>
    <button data-view="cards" title="Card view">🗂 Cards</button>
    <button data-view="sheet" title="Spreadsheet">📑 Sheet</button>
    <button data-view="chart" title="Charts">📊 Charts</button>
    <button data-view="crm" title="Pipeline">📌 CRM</button>
  </div>
  <button class="hbtn" id="csv" title="Download everything as a CSV file">⬇ CSV</button>
  <span id="themeMount"></span>
  <button class="hbtn" id="gamechip" title="Ambient game mode">🎮 off</button>
  <button class="who" id="who" title="Shown next to everything you change or send">👤 …</button>
  <span class="counts" id="counts"></span>
</header>
<div id="stats"></div>
<div id="cfg"></div>
<div id="err"></div>
<main>
  <div class="view" id="vw-table">
  <div class="wrap">
  <table>
    <thead><tr>
      <th class="exp"></th>
      <th>#</th>
      <th>Client</th>
      <th>Receipt</th>
      <th>Client phone</th>
      <th>Tags</th>
      <th>Client contact</th>
      <th>Driver</th>
      <th>Issuer</th>
      <th>Dispatcher</th>
      <th>Renewal</th>
      <th>Status</th>
      <th>Insurance</th>
      <th class="hide-sm">Updated</th>
    </tr></thead>
    <tbody id="rows"><tr><td colspan="14" class="none">Loading…</td></tr></tbody>
  </table>
  </div>
  </div>
  <div class="view" id="vw-cards"><div id="cards"><div class="none empty">Loading…</div></div></div>
  <div class="view" id="vw-sheet"></div>
  <div class="view" id="vw-chart"></div>
  <div class="view" id="vw-crm"></div>
</main>

<div class="overlay" id="compose" hidden>
  <div class="sheet-modal">
    <h2 id="c-title">Send</h2>
    <div class="c-to" id="c-to"></div>
    <label id="c-sublabel">Subject
      <input id="c-subject" autocomplete="off">
    </label>
    <label>Message
      <textarea id="c-msg" rows="9"></textarea>
    </label>
    <div class="c-actions">
      <span id="c-result"></span>
      <span class="spacer"></span>
      <button class="btn" id="c-cancel">Cancel</button>
      <button class="btn primary" id="c-send">Send</button>
    </div>
  </div>
</div>
<div class="overlay" id="lightbox" hidden><img id="lb-img" alt="receipt"></div>
<div id="toasts"></div>
<footer>refreshes every 30s</footer>

<script src="/receipts/asset/themes.js"></script>
<script src="/receipts/asset/sheet.js"></script>
<script src="/receipts/asset/charts.js"></script>
<script src="/receipts/asset/crm.js"></script>
<script src="/receipts/asset/voice.js"></script>
<script>
const STATUSES = __STATUSES__;
const LABELS = __LABELS__;
const AGENCY = __AGENCY__;
const API = "/receipts/api";
const IMG = "/receipts/receipt/";
let ALL = [], filter = "", q = "";
let CFG = {email: true, sms: "unknown"};   // refreshed from /receipts/api/sendconfig
let COMPOSE = null;                        // {row, party, channel}
let VIEW = localStorage.getItem("krab_view") || "table";
if (!["table","cards","sheet","chart","crm"].includes(VIEW)) VIEW = "table";
let COLLAPSED = {};
try { COLLAPSED = JSON.parse(localStorage.getItem("krab_mcollapse") || "{}") || {}; } catch (e) {}
let PREV_STATUS = null;                    // lead_id -> status, for bus diffing
const PARTY_KEYS = ["client", "driver", "issuer", "dispatcher"];
const MQ = window.matchMedia("(max-width: 860px)");

const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

function when(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return isNaN(d) ? "—" : d.toLocaleString([], {month:"short", day:"numeric",
                                               hour:"2-digit", minute:"2-digit"});
}
const digits = s => String(s || "").replace(/\D/g, "");
const telHref = p => { const d = digits(p); return d.length >= 10 ? "tel:+1" + d.slice(-10) : ""; };
const firstName = n => (String(n || "").trim().split(/\s+/)[0]) || "there";
const moneyNum = p => { const n = parseFloat(String(p || "").replace(/[^0-9.]/g, "")); return isNaN(n) ? 0 : n; };
const fmtMoney = n => "$" + Math.round(n).toLocaleString();
const monthKey = iso => (iso || "").slice(0, 7) || "unknown";
function monthLabel(key) {
  if (key === "unknown") return "UNDATED";
  const d = new Date(key + "-15T12:00:00Z");
  return isNaN(d) ? key : d.toLocaleString([], {month:"long", year:"numeric"}).toUpperCase();
}
const normStatus = s => (s === "paid" ? "receipt_uploaded" : (s || "new"));
const statusLabel = s => LABELS[normStatus(s)] || s;
// The view modules bridge through window.* — top-level const does not create
// window properties, so export the shared helpers explicitly.
window.esc = esc; window.when = when; window.moneyNum = moneyNum;
window.monthKey = monthKey; window.monthLabel = monthLabel; window.LABELS = LABELS;

// Renewal countdown. When the bot holds a real renewal clock for the lead
// (lead_renewals.renewal_due_at) that IS the number; otherwise 29 days from
// the day the tag was issued (created date when a tag was never dated).
function renewalDays(r) {
  if (r.renewal_due_at) {
    const due = new Date(r.renewal_due_at);
    if (!isNaN(due)) return Math.max(-99, Math.ceil((due.getTime() - Date.now()) / 86400000));
  }
  const base = r.issue_date || r.created_at;
  if (!base) return null;
  const d = new Date(base);
  if (isNaN(d)) return null;
  return 29 - Math.floor((Date.now() - d.getTime()) / 86400000);
}
function renewalChip(r) {
  const d = renewalDays(r);
  if (d == null) return '<span class="none">—</span>';
  if (d > 7) return `<span class="renew ok">${d} d</span>`;
  if (d > 0) return `<span class="renew soon">${d} d</span>`;
  return `<span class="renew due">${d === 0 ? "due" : Math.abs(d) + "d over"}</span>`;
}

// ── Event bus: the page narrates, optional layers (game mode) listen. ──────
function bus(type, payload) {
  try { window.dispatchEvent(new CustomEvent("krab", {detail: {type, payload: payload || {}}})); }
  catch (e) {}
}
const FORWARD_RANK = {new:0, followup:1, tag_issued:2, tag_emailed:3,
                      tag_printed:4, on_the_way:5, delivered:6, receipt_uploaded:7};
function emitDiffs(rows) {
  const next = {};
  rows.forEach(r => { next[r.lead_id] = normStatus(r.status); });
  if (PREV_STATUS) {
    rows.forEach(r => {
      const was = PREV_STATUS[r.lead_id], now = next[r.lead_id];
      if (was === undefined) { bus("lead.created", {id: r.lead_id}); return; }
      if (was === now) return;
      if (now === "receipt_uploaded") bus("deal.won", {id: r.lead_id, value: moneyNum(r.price)});
      else if (now === "on_the_way") bus("driver.on_the_way", {id: r.lead_id});
      else if ((FORWARD_RANK[now] || 0) > (FORWARD_RANK[was] || 0))
        bus("stage.advanced", {id: r.lead_id, label: statusLabel(now)});
    });
  }
  PREV_STATUS = next;
}

function whoAmI(ask) {
  let w = localStorage.getItem("krab_who") || "";
  if (!w && ask) {
    w = (prompt("Your name (shown next to what you change or send):") || "").trim();
    if (w) localStorage.setItem("krab_who", w);
  }
  document.getElementById("who").textContent = "👤 " + (localStorage.getItem("krab_who") || "who am I?");
  return localStorage.getItem("krab_who") || "";
}
document.getElementById("who").onclick = () => {
  const w = (prompt("Your name (shown next to what you change or send):",
                    localStorage.getItem("krab_who") || "") || "").trim();
  if (w) localStorage.setItem("krab_who", w);
  whoAmI(false);
};

function toast(text, ok) {
  const t = document.createElement("div");
  t.className = "toast " + (ok ? "ok" : "bad");
  t.textContent = text;
  document.getElementById("toasts").appendChild(t);
  setTimeout(() => t.remove(), 6500);
}

function tabs() {
  const el = document.getElementById("tabs");
  const counts = {};
  ALL.forEach(r => { const s = normStatus(r.status); counts[s] = (counts[s] || 0) + 1; });
  el.innerHTML = [["", "All"]].concat(STATUSES.map(s => [s, LABELS[s]]))
    .map(([v, label]) => {
      const n = v ? (counts[v] || 0) : ALL.length;
      if (v && !n && v !== "new") return "";     // quiet empty stops, keep the strip short
      return `<button class="tab ${filter === v ? "on" : ""}" data-f="${v}">`
           + `${esc(label)} <span class="counts">${n}</span></button>`;
    }).join("");
  el.querySelectorAll(".tab").forEach(b => b.onclick = () => {
    filter = b.dataset.f; draw();
  });
}

function visible() {
  const needle = q.trim().toLowerCase();
  return ALL.filter(r => (!filter || normStatus(r.status) === filter)
    && (!needle || JSON.stringify(r).toLowerCase().includes(needle)));
}

// Month folders — Monday-style: the month, then its rows, first to last day.
function monthGroups(rows) {
  const out = [];
  let cur = null;
  rows.forEach(r => {
    const key = monthKey(r.created_at);
    if (!cur || cur.key !== key) {
      cur = {key, label: monthLabel(key), rows: [], sumAll: 0, sumRec: 0, nRec: 0};
      out.push(cur);
    }
    cur.rows.push(r);
    const amt = moneyNum(r.price);
    cur.sumAll += amt;
    if (r.has_receipt) { cur.sumRec += amt; cur.nRec += 1; }
  });
  return out;
}

function renderStats(rows) {
  let sumAll = 0, sumRec = 0, nRec = 0;
  rows.forEach(r => { const a = moneyNum(r.price); sumAll += a;
                      if (r.has_receipt) { sumRec += a; nRec += 1; } });
  const nowKey = monthKey(new Date().toISOString());
  let mAll = 0, mRec = 0, mN = 0;
  rows.forEach(r => { if (monthKey(r.created_at) === nowKey) {
    const a = moneyNum(r.price); mAll += a; mN += 1; if (r.has_receipt) mRec += a; } });
  document.getElementById("stats").innerHTML = [
    `<span class="stat">🧾 With receipts<b>${fmtMoney(sumRec)}</b> <span class="counts">(${nRec})</span></span>`,
    `<span class="stat">💰 All<b>${fmtMoney(sumAll)}</b> <span class="counts">(${rows.length})</span></span>`,
    `<span class="stat warn">⚠ Missing<b>${fmtMoney(sumAll - sumRec)}</b> <span class="counts">(${rows.length - nRec})</span></span>`,
    `<span class="stat">📅 ${esc(monthLabel(nowKey))}<b>${fmtMoney(mAll)}</b> <span class="counts">(${mN} leads · ${fmtMoney(mRec)} receipted)</span></span>`,
  ].join("");
}

// Every party's contacts, in the one consistent shape the blocks render.
function contacts(r) {
  return {
    client: {
      label: "Client", name: r.client_name === "—" ? "" : r.client_name,
      tg: "", tgHref: "",
      phone: r.client_phone || "", email: r.email || "",
    },
    driver: {
      label: "Driver", name: r.driver_name === "—" ? "" : r.driver_name,
      tg: r.driver_tg_id ? "id " + r.driver_tg_id : "", tgHref: "",
      phone: r.driver_phone || "", email: r.driver_email || "",
    },
    issuer: {
      label: "Issuer",
      name: r.issuer_username ? "@" + r.issuer_username : (r.issuer === "—" ? "" : r.issuer),
      tg: r.issuer_tg_id ? "id " + r.issuer_tg_id : "",
      tgHref: r.issuer_username ? "https://t.me/" + r.issuer_username : "",
      phone: "", email: "",
    },
    dispatcher: {
      label: "Dispatcher", name: r.group_name === "—" ? "" : r.group_name,
      tg: r.dispatcher_tg_id ? "sup " + r.dispatcher_tg_id
          : (r.group_tg_id ? "grp " + r.group_tg_id : ""),
      tgHref: "",
      phone: "", email: "",
    },
  };
}

function contactLine(ic, val, href) {
  if (!val) return `<div class="cl none"><span class="ic">${ic}</span>—</div>`;
  const inner = href ? `<a href="${esc(href)}" target="_blank" rel="noopener">${esc(val)}</a>` : esc(val);
  return `<div class="cl" title="${esc(val)}"><span class="ic">${ic}</span>${inner}</div>`;
}

function actButton(r, party, ch, label) {
  const ok = ch === "email" ? CFG.email : CFG.sms;
  const title = ch === "email"
    ? (CFG.email ? "Send an email to the " + party : "Email sender not configured on this service")
    : (CFG.sms ? "Send an SMS to the " + party + " via " + CFG.sms : "SMS sender not configured on this service");
  return `<button class="act ${ok ? "" : "dim"}" data-id="${esc(r.lead_id)}"
    data-party="${party}" data-ch="${ch}" title="${title}">${label}</button>`;
}

function block(r, party) {
  const c = contacts(r)[party];
  const btns = [];
  if (c.email) btns.push(actButton(r, party, "email", "✉ Email"));
  if (c.phone) btns.push(actButton(r, party, "sms", "💬 SMS"));
  return `<div class="party">`
    + `<div class="pname">${c.name ? esc(c.name) : '<span class="none">—</span>'}</div>`
    + contactLine("✈", c.tg, c.tgHref)
    + contactLine("☎", c.phone, telHref(c.phone))
    + contactLine("✉", c.email, c.email ? "mailto:" + c.email : "")
    + (btns.length ? `<div class="acts">${btns.join("")}</div>` : "")
    + `</div>`;
}

function receiptCell(r) {
  const img = r.has_receipt
    ? `<img class="thumb" loading="lazy" src="${IMG + encodeURIComponent(r.lead_id)}"
         data-full="${IMG + encodeURIComponent(r.lead_id)}" alt="receipt"
         onerror="this.outerHTML='<div class=nothumb>no image</div>'">`
    : `<div class="nothumb">no receipt</div>`;
  const date = r.receipt_at ? when(r.receipt_at) : (r.has_receipt ? "on file" : "—");
  return `${img}<div class="rinfo"><b>${esc(r.price)}</b> · ${esc(date)}<br>${esc(statusLabel(r.status))}</div>`;
}

function statusBits(r) {
  const now = normStatus(r.status);
  const opts = STATUSES.map(s =>
    `<option value="${s}" ${now === s ? "selected" : ""}>${esc(LABELS[s])}</option>`
  ).join("");
  return {
    pill: `<span class="pill s-${esc(now)}">${esc(statusLabel(r.status))}</span>`,
    select: `<select class="status" data-id="${esc(r.lead_id)}">${opts}</select>`,
  };
}

const INS_CARD = "/receipts/insurance/";
const INS_LABEL = {
  none: "tag only", pending: "not issued yet", issued: "card",
  sent: "card", failed: "failed",
};

// A lead is tag-only or tag + insurance. Tag-only rows stay quiet; the insured
// ones say how far the card actually got, because a policy that was never
// emailed is the failure worth seeing and it was invisible on this board.
//
// Once a card exists the chip IS the card: it opens the issued FS-20 rather
// than merely reporting that one was sent. "Card sent" told you a thing had
// happened somewhere else and left you no way to look at it.
function insuranceChip(r) {
  const st = r.insurance_state || (r.has_insurance ? "pending" : "none");
  if (st === "none") return '<span class="ins none">—</span>';
  const label = INS_LABEL[st] || st;
  const viewable = st === "issued" || st === "sent";
  const tip = st === "sent"
    ? `Card emailed${r.insurance_sent_to ? " to " + r.insurance_sent_to : ""} — click to view`
    : (viewable ? "Issued but not emailed — click to view" : label);

  let html = viewable
    ? `<a class="ins ${esc(st)}" href="${INS_CARD + encodeURIComponent(r.lead_id)}"
          target="_blank" rel="noopener" title="${esc(tip)}">🛡 ${esc(label)}</a>`
    : `<span class="ins ${esc(st)}" title="${esc(tip)}">🛡 ${esc(label)}</span>`;
  if (r.insurance_policy) html += `<div class="ref">${esc(r.insurance_policy)}</div>`;
  return html;
}

function insuranceBlock(r) {
  if (!r.has_insurance) return "";
  const st = r.insurance_state || "pending";
  const row = (k, v) => (v ? `<dt>${k}</dt><dd>${v}</dd>` : "");
  const ref = v => `<span class="ref">${esc(v)}</span>`;
  return `<div class="insbox">
    <h4>🛡 Insurance</h4>
    <dl>
      <dt>State</dt><dd><span class="ins ${esc(st)}">${esc(INS_LABEL[st] || st)}</span></dd>
      ${row("Card", (st === "issued" || st === "sent")
            ? `<a href="${INS_CARD + encodeURIComponent(r.lead_id)}" target="_blank"
                  rel="noopener">open the issued card (PDF)</a>` : "")}
      ${row("Policy #", r.insurance_policy ? ref(r.insurance_policy) : "")}
      ${row("Card sent to", esc(r.insurance_sent_to || ""))}
      ${row("Card sent", r.insurance_sent_at ? esc(when(r.insurance_sent_at)) : "")}
      ${row("Driver licence", r.insurance_dl ? ref(r.insurance_dl) : "")}
      ${row("Portal login", r.portal_email
            ? ref(r.portal_email) + (r.portal_password ? " / " + ref(r.portal_password) : "")
            : "")}
      ${row("Problem", r.insurance_error
            ? `<span style="color:var(--bad-ink)">${esc(r.insurance_error)}</span>` : "")}
    </dl>
  </div>`;
}

function detailBody(r) {
  const c = contacts(r);
  const partyLines = PARTY_KEYS.map(p => {
    const cc = c[p];
    const bits = [cc.name, cc.tg, cc.phone, cc.email].filter(Boolean).map(esc).join(" · ");
    return `<dt>${cc.label}</dt><dd>${bits || "—"}</dd>`;
  }).join("");
  return `<dl>
      <dt>Reference</dt><dd class="ref">${esc(r.reference_id)}</dd>
      <dt>Car</dt><dd>${esc(r.car)} ${(r.tags || 1) > 1 ? `— <b>${esc(r.tags)} tags owed</b>` : ""}</dd>
      <dt>Price</dt><dd>${esc(r.price)}</dd>
      <dt>Delivery</dt><dd>${esc(r.delivery) || "—"}</dd>
      <dt>Notes</dt><dd>${esc(r.notes) || "—"}</dd>
      <dt>Entered by</dt><dd>${esc(r.issuer)}</dd>
      <dt>Created</dt><dd>${esc(when(r.created_at))}</dd>
      <dt>Tag issued</dt><dd>${esc(when(r.issue_date))}</dd>
      <dt>Receipt</dt><dd>${r.receipt_in_db ? "stored here (never expires)"
                            : (r.has_receipt ? "external link" : "not handed in")}</dd>
      ${partyLines}
    </dl>
    ${insuranceBlock(r)}
    ${r.has_receipt ? `<img loading="lazy" src="${IMG + encodeURIComponent(r.lead_id)}"
                         data-full="${IMG + encodeURIComponent(r.lead_id)}" alt="receipt">` : ""}`;
}

function rowHtml(r, idx) {
  const s = statusBits(r);
  const phone = r.client_phone
    ? `<a href="${esc(telHref(r.client_phone) || "#")}">${esc(r.client_phone)}</a>`
    : '<span class="none">—</span>';
  return `<tr class="row" data-id="${esc(r.lead_id)}">
    <td class="exp" data-x="${esc(r.lead_id)}">▸</td>
    <td class="idx">${idx}</td>
    <td><div class="cname">${esc(r.client_name)}</div>
        <div class="ref">${esc(r.reference_id)}</div>
        <div class="carline">${esc(r.car)}</div></td>
    <td>${receiptCell(r)}</td>
    <td class="phone">${phone}</td>
    <td>${(r.tags || 1) > 1 ? `<b title="one tag per car">${esc(r.tags)}×</b>` : ""}</td>
    <td>${block(r, "client")}</td>
    <td>${block(r, "driver")}</td>
    <td>${block(r, "issuer")}</td>
    <td>${block(r, "dispatcher")}</td>
    <td>${renewalChip(r)}</td>
    <td>${s.pill}<br>${s.select}</td>
    <td>${insuranceChip(r)}</td>
    <td class="hide-sm">${esc(when(r.status_updated_at))}<br>
        <span class="counts">${esc(r.status_updated_by || "")}</span></td>
  </tr>
  <tr class="detail" id="d-${esc(r.lead_id)}" hidden><td colspan="14">${detailBody(r)}</td></tr>`;
}

function monthRowHtml(g) {
  const closed = !!COLLAPSED[g.key];
  return `<tr class="mrow" data-mk="${esc(g.key)}"><td colspan="14">
    ${closed ? "📁" : "📂"} ${esc(g.label)}
    <span class="msum">${g.rows.length} lead${g.rows.length === 1 ? "" : "s"}
      · ${fmtMoney(g.sumAll)} total · ${fmtMoney(g.sumRec)} with receipts (${g.nRec})
      · ${fmtMoney(g.sumAll - g.sumRec)} missing</span>
    <span class="msum" style="float:right">${closed ? "▸" : "▾"}</span>
  </td></tr>`;
}

// One card per transmission on a phone — the same data, thumb-first.
function cardHtml(r, idx) {
  const c = contacts(r);
  const s = statusBits(r);
  const partyRow = p => {
    const cc = c[p];
    const btns = [];
    if (cc.email) btns.push(actButton(r, p, "email", "✉ Email"));
    if (cc.phone) btns.push(actButton(r, p, "sms", "💬 SMS"));
    return `<div class="prow">
      <span class="plabel">${cc.label}</span>
      <span class="pwho">${cc.name ? esc(cc.name) : '<span class="none">—</span>'}</span>
      <span class="pacts">${btns.join("")}</span>
    </div>`;
  };
  const img = r.has_receipt
    ? `<img class="thumb" loading="lazy" src="${IMG + encodeURIComponent(r.lead_id)}"
         data-full="${IMG + encodeURIComponent(r.lead_id)}" alt="receipt"
         onerror="this.outerHTML='<div class=nothumb>no image</div>'">`
    : `<div class="nothumb">no receipt</div>`;
  const date = r.receipt_at ? when(r.receipt_at) : (r.has_receipt ? "on file" : "—");
  const phone = r.client_phone
    ? `<a href="${esc(telHref(r.client_phone) || "#")}">☎ ${esc(r.client_phone)}</a>`
    : '<span class="none">☎ —</span>';
  return `<div class="card" data-id="${esc(r.lead_id)}">
    <div class="c-top">
      <div class="c-id">
        <div class="cname"><span class="idx">#${idx}</span> ${esc(r.client_name)}</div>
        <div class="ref">${esc(r.reference_id)} · ${esc(r.car)}${(r.tags || 1) > 1 ? ` · <b>${esc(r.tags)}× tags</b>` : ""}</div>
      </div>
      <div style="text-align:right">${s.pill}<div style="margin-top:5px">${renewalChip(r)}</div></div>
    </div>
    <div class="c-mid">
      ${img}
      <div class="c-facts">
        <div><b>${esc(r.price)}</b> · <span class="counts">${esc(date)}</span></div>
        <div class="c-phone">${phone}</div>
        ${s.select}
      </div>
    </div>
    <div class="c-parties">${PARTY_KEYS.map(partyRow).join("")}</div>
    <button class="exp c-more" data-x="${esc(r.lead_id)}">▸ details</button>
    <div class="detail c-detail" id="d-${esc(r.lead_id)}" hidden>${detailBody(r)}</div>
  </div>`;
}

function setView(v) {
  VIEW = v;
  localStorage.setItem("krab_view", v);
  document.querySelectorAll("#seg button").forEach(b =>
    b.classList.toggle("on", b.dataset.view === v));
  draw();
}

function draw() {
  tabs();
  renderStats(ALL);
  const rows = visible();
  document.getElementById("counts").textContent = `${rows.length} of ${ALL.length}`;
  document.querySelectorAll(".view").forEach(el =>
    el.classList.toggle("on", el.id === "vw-" + VIEW));
  const tb = document.getElementById("rows");
  const cards = document.getElementById("cards");
  tb.innerHTML = ""; cards.innerHTML = "";
  // Leaving a view tears down its document-level hooks and observers — the
  // CRM's outside-click closers and the charts' ResizeObserver must not keep
  // running against a hidden container forever.
  if (VIEW !== "crm") {
    const crmEl = document.getElementById("vw-crm");
    if (crmEl.__crmDocClose) {
      ["click", "scroll", "keydown"].forEach(t =>
        document.removeEventListener(t, crmEl.__crmDocClose, true));
      crmEl.__crmDocClose = null;
    }
    crmEl.innerHTML = "";
  }
  if (VIEW !== "chart") {
    const chEl = document.getElementById("vw-chart");
    if (chEl.__kcRO) { chEl.__kcRO.disconnect(); chEl.__kcRO = null; }
    chEl.__kcRows = null;
    chEl.innerHTML = "";
  }
  if (VIEW !== "sheet") document.getElementById("vw-sheet").innerHTML = "";

  if (VIEW === "table") {
    if (!rows.length) {
      tb.innerHTML = '<tr><td colspan="14" class="none">Nothing here yet.</td></tr>';
      return;
    }
    let idx = 0;
    const parts = [];
    monthGroups(rows).forEach(g => {
      parts.push(monthRowHtml(g));
      g.rows.forEach(r => {
        idx += 1;
        if (!COLLAPSED[g.key]) parts.push(rowHtml(r, idx));
      });
    });
    tb.innerHTML = parts.join("");
  } else if (VIEW === "cards") {
    if (!rows.length) {
      cards.innerHTML = '<div class="none empty">Nothing here yet.</div>';
      return;
    }
    let idx = 0;
    const parts = [];
    monthGroups(rows).forEach(g => {
      const closed = !!COLLAPSED[g.key];
      parts.push(`<div class="mdivider" data-mk="${esc(g.key)}">${closed ? "📁" : "📂"} ${esc(g.label)}
        · ${g.rows.length} · ${fmtMoney(g.sumAll)} (${fmtMoney(g.sumRec)} receipted) ${closed ? "▸" : "▾"}</div>`);
      g.rows.forEach(r => { idx += 1; if (!closed) parts.push(cardHtml(r, idx)); });
    });
    cards.innerHTML = parts.join("");
  } else if (VIEW === "sheet") {
    const el = document.getElementById("vw-sheet");
    if (typeof renderSheetView === "function") renderSheetView(rows, el);
    else el.innerHTML = '<div class="none empty">Sheet module not installed.</div>';
  } else if (VIEW === "chart") {
    const el = document.getElementById("vw-chart");
    if (typeof renderChartView === "function") renderChartView(rows, el);
    else el.innerHTML = '<div class="none empty">Charts module not installed.</div>';
  } else if (VIEW === "crm") {
    const el = document.getElementById("vw-crm");
    if (typeof renderCrmView === "function")
      renderCrmView(rows, el, {LABELS, STATUSES,
        onStatusChange: (id, next) => saveStatus(id, next, null),
        onOpen: id => { setView("table"); requestAnimationFrame(() => {
          const d = document.getElementById("d-" + id);
          if (d) { d.hidden = false; d.scrollIntoView({block: "center"}); } }); }});
    else el.innerHTML = '<div class="none empty">CRM module not installed.</div>';
  }
}

async function saveStatus(id, next, holder) {
  if (holder) holder.classList.add("saving");
  try {
    const res = await fetch(`${API}/transmissions/${encodeURIComponent(id)}/status`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({status: next, by: whoAmI(true)}),
    });
    if (!res.ok) throw new Error(await res.text());
    const row = ALL.find(r => r.lead_id === id);
    if (row) {
      const was = normStatus(row.status);
      row.status = next;
      row.status_updated_at = new Date().toISOString();
      row.status_updated_by = localStorage.getItem("krab_who") || "";
      if (next === "receipt_uploaded") bus("deal.won", {id, value: moneyNum(row.price)});
      else if (next === "on_the_way") bus("driver.on_the_way", {id});
      else if ((FORWARD_RANK[next] || 0) > (FORWARD_RANK[was] || 0))
        bus("stage.advanced", {id, label: statusLabel(next)});
      if (PREV_STATUS) PREV_STATUS[id] = normStatus(next);
    }
    draw();
  } catch (e) {
    document.getElementById("err").innerHTML =
      `<div class="err">Could not save that status: ${esc(e.message)}</div>`;
    if (holder) holder.classList.remove("saving");
    draw();          // the select must never keep showing a status that did not save
  }
}

// One set of listeners for every layout — rows, cards and month folders come
// and go, the container stays.
document.querySelector("main").addEventListener("click", e => {
  const mh = e.target.closest(".mrow, .mdivider");
  if (mh) {
    const k = mh.dataset.mk;
    COLLAPSED[k] = !COLLAPSED[k];
    try { localStorage.setItem("krab_mcollapse", JSON.stringify(COLLAPSED)); } catch (e2) {}
    draw();
    return;
  }
  const exp = e.target.closest(".exp");
  if (exp && exp.dataset.x) {
    const d = document.getElementById("d-" + exp.dataset.x);
    if (d) {
      d.hidden = !d.hidden;
      exp.textContent = exp.classList.contains("c-more")
        ? (d.hidden ? "▸ details" : "▾ details")
        : (d.hidden ? "▸" : "▾");
    }
    return;
  }
  const act = e.target.closest(".act");
  if (act) {
    const row = ALL.find(r => r.lead_id === act.dataset.id);
    if (row) openCompose(row, act.dataset.party, act.dataset.ch);
    return;
  }
  const img = e.target.closest("img[data-full]");
  if (img) {
    document.getElementById("lb-img").src = img.dataset.full;
    document.getElementById("lightbox").hidden = false;
  }
});
document.querySelector("main").addEventListener("change", e => {
  const sel = e.target.closest("select.status");
  if (!sel) return;
  saveStatus(sel.dataset.id, sel.value, sel.closest("tr") || sel.closest(".card"));
});

// ── Compose ────────────────────────────────────────────────────────────────
function statusLine(r) {
  return {
    new: "We have received your order and are getting it ready.",
    followup: "We are following up on your order — some details are still needed.",
    tag_issued: "Your temporary tag has been issued.",
    tag_emailed: "Your temporary tag has been emailed to you.",
    tag_printed: "Your temporary tag is printed and ready.",
    on_the_way: "Your temporary tag is on the way to you now.",
    delivered: "Your temporary tag has been delivered.",
    receipt_uploaded: "Payment received — thank you! Your transaction is complete.",
  }[normStatus(r.status)] || "Here is an update on your temporary tag.";
}

function prefill(r, party, channel) {
  if (party === "client") {
    if (channel === "sms")
      return {subject: "", message:
        `Hi ${firstName(r.client_name)}, it's ${AGENCY.name}. ${statusLine(r)} (ref ${r.reference_id}).`
        + (AGENCY.phone ? ` Questions? Call/text ${AGENCY.phone}.` : "")};
    return {
      subject: `Update on your temporary tag — ${r.reference_id}`,
      message: `Hi ${firstName(r.client_name)},

${statusLine(r)}

Reference: ${r.reference_id}
Vehicle: ${r.car}

Questions? Reply to this email` + (AGENCY.phone ? ` or call/text ${AGENCY.phone}` : "") + `.

Thank you,
${AGENCY.name}
${AGENCY.website}`,
    };
  }
  const who = localStorage.getItem("krab_who") || AGENCY.name;
  const line = `${r.reference_id} — ${r.client_name}, ${r.car}: status "${statusLabel(r.status)}".`;
  if (channel === "sms")
    return {subject: "", message: `From the receipts board (${who}): ${line}`};
  return {
    subject: `Transmission ${r.reference_id} — ${statusLabel(r.status)}`,
    message: `Hi,

${line}

Sent from the receipts board by ${who}.

${AGENCY.name}`,
  };
}

function pauseTetrisForModal() {
  // Arrow keys must never steer a hidden game behind an open sheet.
  try {
    if (window.krabTetris && window.krabTetris.active && window.krabTetris.active()
        && window.krabTetris.pause) window.krabTetris.pause(true);
  } catch (e) {}
}

// One watcher owns "is a modal up?": it hides the floating mic (which sits
// above the sheets) and pauses any game behind them. Attribute-driven, so it
// is correct no matter which code path opened or closed the overlay.
(function watchModals() {
  const overlays = ["compose", "lightbox"].map(id => document.getElementById(id))
                                          .filter(Boolean);
  const sync = () => {
    const open = overlays.some(o => !o.hidden);
    document.body.classList.toggle("krab-modal-open", open);
    if (open) pauseTetrisForModal();
  };
  const mo = new MutationObserver(sync);
  overlays.forEach(o => mo.observe(o, {attributes: true, attributeFilter: ["hidden"]}));
  sync();
})();

function openCompose(row, party, channel) {
  COMPOSE = {row, party, channel};
  const c = contacts(row)[party];
  const to = channel === "email" ? c.email : c.phone;
  if (!to) { toast(`No ${channel === "email" ? "email" : "phone number"} on file for the ${party}.`, false); return; }
  const t = prefill(row, party, channel);
  document.getElementById("c-title").textContent =
    (channel === "email" ? "✉ Email" : "💬 SMS") + " → " + c.label.toLowerCase();
  document.getElementById("c-to").textContent =
    `To ${c.name || party}: ${to}` + (channel === "sms" && CFG.sms ? `  (via ${CFG.sms})` : "");
  document.getElementById("c-sublabel").style.display = channel === "email" ? "" : "none";
  document.getElementById("c-subject").value = t.subject;
  document.getElementById("c-msg").value = t.message;
  const res = document.getElementById("c-result");
  res.textContent = ""; res.className = "";
  document.getElementById("c-send").disabled = false;
  document.getElementById("compose").hidden = false;
  document.getElementById("c-msg").focus();
}

document.getElementById("c-cancel").onclick = () => {
  document.getElementById("compose").hidden = true; COMPOSE = null;
};
document.getElementById("c-send").onclick = async () => {
  if (!COMPOSE) return;
  const {row, party, channel} = COMPOSE;
  const btn = document.getElementById("c-send");
  const res = document.getElementById("c-result");
  const message = document.getElementById("c-msg").value.trim();
  if (!message) { res.textContent = "Write a message first."; res.className = "bad"; return; }
  btn.disabled = true; btn.textContent = "Sending…";
  res.textContent = ""; res.className = "";
  try {
    const r = await fetch(`${API}/transmissions/${encodeURIComponent(row.lead_id)}/notify`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        party, channel, message,
        subject: document.getElementById("c-subject").value.trim(),
        by: whoAmI(true),
      }),
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
    res.textContent = `Sent ✓ via ${body.provider || channel} to ${body.to || "recipient"}`;
    res.className = "ok";
    toast(`${channel === "email" ? "Email" : "SMS"} sent to the ${party} (${row.reference_id})`, true);
    bus("task.completed", {id: row.lead_id});
    setTimeout(() => { document.getElementById("compose").hidden = true; COMPOSE = null; }, 1400);
  } catch (e) {
    res.textContent = e.message; res.className = "bad";
    toast(`Send failed: ${e.message}`, false);
  } finally {
    btn.disabled = false; btn.textContent = "Send";
  }
};

document.getElementById("lightbox").onclick = () => {
  document.getElementById("lightbox").hidden = true;
};
document.addEventListener("keydown", e => {
  if (e.key === "Escape") {
    document.getElementById("lightbox").hidden = true;
    document.getElementById("compose").hidden = true;
    COMPOSE = null;
  }
});

// ── CSV ────────────────────────────────────────────────────────────────────
function fallbackCsv(rows) {
  const cols = ["reference_id","client_name","client_phone","email","car","tags","price",
                "has_receipt","receipt_at","status","driver_name","group_name","issuer",
                "created_at","issue_date","delivery","notes","status_updated_by"];
  const cell = v => { v = String(v == null ? "" : v);
    // A leading = + - @ (or tab/CR) executes as a formula in Excel — neutralize.
    if (/^[=+\-@\t\r]/.test(v)) v = "'" + v;
    return /[",\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v; };
  return "﻿" + [cols.join(",")].concat(
    rows.map(r => cols.map(c => cell(r[c])).join(","))).join("\r\n");
}
function downloadCsv() {
  const csv = (typeof buildCsv === "function") ? buildCsv(ALL) : fallbackCsv(ALL);
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([csv], {type: "text/csv;charset=utf-8"}));
  a.download = "receipts-" + new Date().toISOString().slice(0, 10) + ".csv";
  document.body.appendChild(a); a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 500);
  toast("CSV downloaded.", true);
}
document.getElementById("csv").onclick = downloadCsv;

// ── Voice actions — everything the mic (or a typed command) can do. ────────
window.krabVoiceAction = function (action, args) {
  args = args || {};
  switch (action) {
    case "set_view": if (["table","cards","sheet","chart","crm"].includes(args.view)) setView(args.view); break;
    case "toggle_view": setView(VIEW === "table" ? "cards" : "table"); break;
    case "set_theme": {
      if (typeof applyTheme !== "function") break;
      const known = (typeof THEMES !== "undefined" && Array.isArray(THEMES))
        ? THEMES.map(t => t.id) : [];
      let want = String(args.theme || "").trim().toLowerCase();
      // The server normally names a concrete theme; these relative words are
      // honoured too so a model answering "next" is never a no-op.
      if (!want || /^(next|other|another|different|random|surprise|change)$/.test(want)) {
        const cur = localStorage.getItem("krab_theme") || "auto";
        const pool = (known.length ? known : ["light","dark","midnight","matrix",
          "sunset","ocean","monday","mono","bubblegum"]).filter(t => t !== cur);
        want = pool.length ? pool[Math.floor(Math.random() * pool.length)] : "dark";
      }
      applyTheme(want);
      break;
    }
    case "game_mode": {
      const m = ["off","subtle","full"].includes(args.mode) ? args.mode : "full";
      if (window.krabGame) window.krabGame.setMode(m);
      else localStorage.setItem("krab_game", m);
      updateGameChip();
      break;
    }
    case "play_tetris":
      if (window.krabGame && window.krabGame.playTetris) {
        if (window.krabGame.mode && window.krabGame.mode() === "off")
          window.krabGame.setMode("subtle");     // the game needs the layer awake
        window.krabGame.playTetris();
        updateGameChip();
      } else if (window.krabTetris && window.krabTetris.start) {
        window.krabTetris.start({});
      } else {
        toast("Tetris is not installed on this deployment.", false);
      }
      break;
    case "celebrate": bus("goal.hit", {}); break;
    case "download_csv": downloadCsv(); break;
    case "search": q = String(args.query || ""); document.getElementById("q").value = q; draw(); break;
    case "filter_status": filter = STATUSES.includes(args.status) ? args.status : ""; draw(); break;
    case "filter_month": {
      const k = String(args.month || "");
      const groups = monthGroups(ALL);
      if (!groups.some(g => g.key === k)) break;   // unknown month = no-op, not a blank board
      Object.keys(COLLAPSED).forEach(x => delete COLLAPSED[x]);
      groups.forEach(g => { if (g.key !== k) COLLAPSED[g.key] = true; });
      draw();
      break;
    }
    case "refresh": load(); break;
  }
};

function updateGameChip() {
  const m = (window.krabGame && window.krabGame.mode && window.krabGame.mode())
            || localStorage.getItem("krab_game") || "off";
  document.getElementById("gamechip").textContent = "🎮 " + m;
}
document.getElementById("gamechip").onclick = () => {
  const order = ["off", "subtle", "full"];
  const cur = (window.krabGame && window.krabGame.mode && window.krabGame.mode())
              || localStorage.getItem("krab_game") || "off";
  const next = order[(order.indexOf(cur) + 1) % order.length];
  window.krabVoiceAction("game_mode", {mode: next});
};

document.querySelectorAll("#seg button").forEach(b =>
  b.onclick = () => setView(b.dataset.view));

// ── Data ───────────────────────────────────────────────────────────────────
async function loadConfig() {
  try {
    const res = await fetch(`${API}/sendconfig`);
    if (!res.ok) return;
    CFG = await res.json();
    const notes = [];
    if (!CFG.email) notes.push("email (set RESEND_API_KEY + RESEND_FROM, or SENDGRID_API_KEY + SENDGRID_FROM)");
    if (!CFG.sms) notes.push("SMS (set GHL_API_KEY + GHL_LOCATION_ID for GoHighLevel, or TWILIO_*)");
    let text = notes.length
      ? "⚠ Sending not configured for " + notes.join(" and ") + " — buttons will explain when clicked."
      : "";
    if (CFG.status_column === false)
      text += (text ? "   " : "") + "⚠ Statuses cannot be saved yet — run "
            + "database/migration_lead_delivery_status.sql (and migration_lead_status_v2.sql) once.";
    const cfg = document.getElementById("cfg");
    cfg.textContent = text;
    cfg.style.display = text ? "block" : "none";
  } catch (e) { /* the board still works read-only */ }
}

async function load() {
  try {
    const res = await fetch(`${API}/transmissions?limit=500`);
    if (!res.ok) throw new Error(await res.text());
    ALL = await res.json();
    emitDiffs(ALL);
    bus("data.refreshed", {count: ALL.length});
    document.getElementById("err").innerHTML = "";
  } catch (e) {
    document.getElementById("err").innerHTML =
      `<div class="err">Could not load the board: ${esc(e.message)}</div>`;
    ALL = [];
  }
  draw();
}

document.getElementById("q").oninput = e => { q = e.target.value; draw(); };
// Canvas views (charts) and inline-styled views resolve their colors at render
// time — redraw whenever the theme attribute flips, however it was flipped.
new MutationObserver(() => draw()).observe(document.documentElement,
  {attributes: true, attributeFilter: ["data-theme"]});
whoAmI(false);
if (typeof initThemes === "function") initThemes(document.getElementById("themeMount"));
if (typeof applyTheme === "function") applyTheme(localStorage.getItem("krab_theme") || "auto");
if (typeof initVoice === "function") initVoice({
  endpoint: API + "/voice",
  getContext: () => ({view: VIEW, theme: localStorage.getItem("krab_theme") || "auto"}),
  onAction: (a, g) => window.krabVoiceAction(a, g),
});
updateGameChip();
setView(VIEW);
loadConfig();
load();
setInterval(() => {                     // the board is shared — keep it fresh,
  if (document.querySelector(".overlay:not([hidden])")) return;   // but never under a compose
  load();
}, 30000);
</script>
<script defer src="/receipts/asset/tetris.js"></script>
<script defer src="/receipts/game.js"></script>
</body></html>"""


def register(app, db_provider):
    """Attach the board and its endpoints to the dashboard app.

    `db_provider` is resolved on every request, not captured once: binding the
    client at registration time would leave the board talking to a stale handle if
    the dashboard ever rebuilds it (and would quietly ignore a swapped-in double).

    Every endpoint is ALSO mounted under /receipts/* so a single Vercel rewrite
    (tristatetags.com/receipts →  this service) carries the whole board —
    tristatetags.com/api/* already belongs to the checkout proxy and must not
    be fought over."""
    _resolve = db_provider if callable(db_provider) else (lambda: db_provider)

    @app.route("/receipts", methods=["GET"])
    def receipts_board():
        html = (BOARD_HTML
                .replace("__STATUSES__", json.dumps(list(STATUS_ORDER)))
                .replace("__LABELS__", json.dumps(STATUS_LABELS))
                .replace("__AGENCY__", json.dumps(_agency())))
        return Response(html, mimetype="text/html")

    @app.route("/receipts/asset/<name>", methods=["GET"])
    def receipts_asset(name):
        """View modules (sheet, charts, CRM, themes, voice) — optional: with
        receipts_assets.py absent, the board's core still works."""
        body = None
        try:
            import receipts_assets
            body = receipts_assets.ASSETS.get(name)
        except Exception:
            body = None
        if body is None:
            return jsonify({"error": "no such asset"}), 404
        mime = "text/css" if name.endswith(".css") else "application/javascript"
        return Response(body, mimetype=mime,
                        headers={"Cache-Control": "public, max-age=300"})

    @app.route("/receipts/game.js", methods=["GET"])
    def receipts_game_js():
        """The ambient sprite layer — deliberately removable: delete
        receipts_game.py and this serves an empty stub, nothing else changes."""
        try:
            import receipts_game
            return Response(receipts_game.GAME_JS, mimetype="application/javascript",
                            headers={"Cache-Control": "public, max-age=300"})
        except Exception:
            return Response("/* game layer not installed */",
                            mimetype="application/javascript")

    @app.route("/receipts/api/transmissions", methods=["GET"])
    @app.route("/api/transmissions", methods=["GET"])
    def api_transmissions():
        try:
            raw = request.args.get("limit", "300")
            limit = int(raw) if str(raw).isdigit() else 300
            return jsonify(_resolve().get_transmissions(
                limit=limit,
                status=(request.args.get("status") or "").strip(),
                search=(request.args.get("q") or "").strip(),
            ))
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/receipts/api/transmissions/<lead_id>/status", methods=["POST"])
    @app.route("/api/transmissions/<lead_id>/status", methods=["POST"])
    def api_set_transmission_status(lead_id):
        body = request.get_json(silent=True) or {}
        status = (body.get("status") or request.args.get("status") or "").strip()
        if status not in ACCEPTED_STATUSES:
            return jsonify({"error": f"status must be one of {list(STATUS_ORDER)}"}), 400
        who = (body.get("by") or "").strip()
        if not _resolve().set_lead_status(lead_id, status, who):
            return jsonify({"error": (
                "could not save — if this keeps happening, check that "
                "database/migration_lead_delivery_status.sql and "
                "migration_lead_status_v2.sql have been run"
            )}), 500
        return jsonify({"ok": True, "lead_id": lead_id, "status": status})

    @app.route("/receipts/api/transmissions/<lead_id>/notify", methods=["POST"])
    @app.route("/api/transmissions/<lead_id>/notify", methods=["POST"])
    def api_notify_party(lead_id):
        """Send an email or SMS to one party of one transmission.

        The browser names the party; the ADDRESS is resolved from the database
        here, so the endpoint can never be pointed at an arbitrary recipient."""
        body = request.get_json(silent=True) or {}
        party = (body.get("party") or "").strip().lower()
        channel = (body.get("channel") or "").strip().lower()
        if party not in PARTIES:
            return jsonify({"error": f"party must be one of {list(PARTIES)}"}), 400
        if channel not in ("email", "sms"):
            return jsonify({"error": "channel must be email or sms"}), 400
        message = (body.get("message") or "").strip()
        if not message:
            return jsonify({"error": "message is required"}), 400
        who = (body.get("by") or "").strip()[:64]

        key = (str(lead_id), party, channel)
        now = time.time()
        if now - _recent_sends.get(key, 0) < _MIN_SECONDS_BETWEEN_SENDS:
            return jsonify({"error": "Just sent — give it a few seconds before retrying."}), 429

        contact = _party_contact(_resolve(), lead_id, party)
        if contact is None:
            return jsonify({"error": "lead not found"}), 404

        if channel == "email":
            to = (contact.get("email") or "").strip()
            if not to:
                return jsonify({"error": f"No email on file for the {party}."}), 400
            subject = (body.get("subject") or "").strip() \
                or f"Update on your transmission — {contact.get('reference_id') or lead_id}"
            from utils.client_outreach import send_client_email
            ok, err = send_client_email(to, subject, message, copy_to=_email_copy_address())
            provider, shown = "resend/sendgrid", _mask_email(to)
        else:
            to = (contact.get("phone") or "").strip()
            if not to:
                return jsonify({"error": f"No phone number on file for the {party}."}), 400
            from utils.client_outreach import send_client_sms, sms_configured
            from utils.ghl_client import ghl_configured, send_ghl_sms
            if ghl_configured():
                ok, err = send_ghl_sms(to, message)
                provider = "gohighlevel"
            elif sms_configured():
                ok, err = send_client_sms(to, message)
                provider = "twilio"
            else:
                return jsonify({"error": (
                    "SMS is not configured — set GHL_API_KEY + GHL_LOCATION_ID "
                    "(GoHighLevel), or TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / "
                    "TWILIO_FROM_NUMBER."
                )}), 503
            shown = _mask_phone(to)

        logger.info("receipts board: %s to %s for lead %s by %s -> %s%s",
                    channel, party, lead_id, who or "board",
                    "ok" if ok else "FAILED", "" if ok else f" ({err})")
        if not ok:
            return jsonify({"error": err or "send failed"}), 502
        _recent_sends[key] = now
        return jsonify({"ok": True, "party": party, "channel": channel,
                        "provider": provider, "to": shown})

    @app.route("/receipts/api/voice", methods=["POST"])
    def receipts_voice():
        """The mic's brain: one short command in, {say, action, args} out.

        OpenAI when configured (it can both answer questions from the board's
        aggregates and pick a UI action); a deterministic local parser when not,
        so the mic never goes dead."""
        body = request.get_json(silent=True) or {}
        text = (body.get("text") or "").strip()[:500]
        if not text:
            return jsonify({"error": "text is required"}), 400
        now = time.time()
        ip = (request.headers.get("X-Forwarded-For") or
              request.remote_addr or "?").split(",")[0].strip()
        if now - _voice_last_by_ip.get(ip, 0) < _VOICE_MIN_GAP_SEC:
            return jsonify({"error": "one command at a time"}), 429
        while _voice_window and now - _voice_window[0] > 60:
            _voice_window.pop(0)
        if len(_voice_window) >= _VOICE_GLOBAL_PER_MIN:
            return jsonify({"error": "the voice line is busy — try again shortly"}), 429
        _voice_last_by_ip[ip] = now
        _voice_window.append(now)
        ctx = {"view": str(body.get("view") or "")[:20],
               "theme": str(body.get("theme") or "")[:20]}
        summary = _voice_summary_cache["value"]
        if summary is None or now - _voice_summary_cache["at"] > 30:
            try:
                rows = _resolve().get_transmissions(limit=1000)
            except Exception as e:
                logger.warning("voice: could not load rows: %s", e)
                rows = []
            summary = _voice_summary(rows)
            _voice_summary_cache.update(at=now, value=summary)

        local = _voice_local(text, ctx, summary)
        result = _voice_openai(text, ctx, summary)
        if result is None:
            result = local
        elif not result.get("actions") and local.get("actions"):
            # The model answered but did not act on a command the deterministic
            # parser is sure about ("download the csv") — do the thing anyway.
            result["actions"] = local["actions"]
        if not (result.get("say") or "").strip():
            result["say"] = local.get("say") or ""

        actions = result.get("actions") or []
        # `action`/`args` stay in the payload for anything still reading the
        # single-intent shape; `actions` is what the page executes.
        return jsonify({
            "say": result.get("say") or "",
            "action": actions[0]["action"] if actions else "none",
            "args": actions[0]["args"] if actions else {},
            "actions": actions,
        })

    @app.route("/receipts/api/sendconfig", methods=["GET"])
    def receipts_send_config():
        """Which senders this deployment can actually use — booleans only, so the
        page can label its buttons honestly. No secrets leave the process."""
        email_ok, sms = False, None
        try:
            from utils.client_outreach import _sendgrid_config, sms_configured
            from utils.resend_client import get_resend_client, get_resend_from_address
            email_ok = bool(get_resend_client() and get_resend_from_address()) \
                or bool(_sendgrid_config())
            from utils.ghl_client import ghl_configured
            if ghl_configured():
                sms = "gohighlevel"
            elif sms_configured():
                sms = "twilio"
        except Exception as e:
            logger.warning("receipts board: sendconfig probe failed: %s", e)
        # Whether statuses can persist at all — the migration adds the column,
        # and a board that quietly cannot save is how this shipped broken once.
        status_col = True
        try:
            _resolve().client.table("leads").select("delivery_status").limit(1).execute()
        except Exception:
            status_col = False
        return jsonify({"email": email_ok, "sms": sms, "status_column": status_col})

    @app.route("/receipts/insurance/<lead_id>", methods=["GET"])
    def receipts_insurance_card(lead_id):
        """The issued FS-20 card, rebuilt for viewing.

        The card is emailed and never stored, so there is no file to serve —
        but everything printed on it survives on the lead, so it is rebuilt from
        exactly that. View only: no email, no portal, and no new policy number.
        """
        try:
            r = (
                _resolve().client.table("leads")
                .select("id, reference_id, vehicle_details, delivery_details, extra_info, "
                        "driver_license_id, insurance_card_policy_number, "
                        "insurance_card_sent_at, issue_date, created_at")
                .eq("id", str(lead_id)).limit(1).execute()
            )
            lead = (r.data or [None])[0]
        except Exception as e:
            logger.error("receipts board: insurance lead read failed for %s: %s", lead_id, e)
            # The exception can carry query shape; the served page gets fixed text.
            return jsonify({"error": "could not read the lead"}), 500
        if not lead:
            return jsonify({"error": "lead not found"}), 404

        try:
            import insurance_card_view
        except Exception as e:
            logger.error("insurance card view unavailable: %s", e)
            return jsonify({"error": "card rendering is unavailable"}), 503

        pdf, err = insurance_card_view.build_card_pdf_for_lead(lead)
        if not pdf:
            return jsonify({"error": err or "no card"}), 404

        ref = (lead.get("reference_id") or lead_id)
        safe = "".join(ch for ch in str(ref) if ch.isalnum() or ch in "-_") or "card"
        return Response(pdf, mimetype="application/pdf", headers={
            "Content-Disposition": f'inline; filename="insurance-card-{safe}.pdf"',
            "Cache-Control": "private, max-age=300",
            "X-Content-Type-Options": "nosniff",
        })

    @app.route("/receipts/receipt/<lead_id>", methods=["GET"])
    def receipts_receipt_image(lead_id):
        """The receipt for the board's thumbnails — database first (never
        expires). Anything still external is resolved by the canonical
        /api/receipts/image view (Telegram re-signing included), and the bytes
        it finds are mirrored INTO receipt_files — so every receipt makes the
        slow external trip at most once, then serves from the row forever."""
        got = None
        try:
            got = _resolve().get_receipt_file(lead_id)
        except Exception as e:
            logger.warning("receipts board: stored receipt read failed for %s: %s", lead_id, e)
        if got:
            safe = RECEIPT_MIME.get((got.get("content_type") or "").lower(), "image/jpeg")
            return Response(got["data"], mimetype=safe, headers={
                "Cache-Control": "private, max-age=86400",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'",
            })
        resolver = app.view_functions.get("api_receipt_image")
        if resolver is None:
            return jsonify({"error": "no receipt stored for this lead"}), 404
        resp = resolver(lead_id)
        try:
            mime = (getattr(resp, "mimetype", "") or "").lower()
            if getattr(resp, "status_code", 0) == 200 and mime in RECEIPT_MIME:
                data = resp.get_data()
                if data and len(data) <= 8_000_000:
                    _resolve().save_receipt_file(
                        lead_id, data=data, content_type=RECEIPT_MIME[mime],
                        source="board")
        except Exception as e:
            logger.warning("receipts board: could not mirror receipt %s: %s", lead_id, e)
        return resp
