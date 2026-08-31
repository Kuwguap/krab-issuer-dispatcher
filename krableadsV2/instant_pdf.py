"""$100 instant PDF — pay, and the tag goes straight to the chosen driver.

The normal route waits for a dispatch team to accept a lead. This one skips that:
the payment IS the approval. It lives here, on the dashboard, because the dashboard
is already tristatetags.com/backend and already holds the database every side reads.

No Stripe SDK: the two calls we need are plain REST, and `requests` is already a
dependency — adding a package would mean a redeploy that could take the bot with it.

Nothing hangs, by design. Every step is a column on the lead:

    requested_at -> session_id -> paid_at -> delivered_at

The webhook only ever writes `paid_at`. The bot polls for paid-and-undelivered and
stamps `delivered_at` once the tag is actually in the driver's chat. A crash between
the two delays a tag; it cannot lose one, and it cannot take the money without
eventually delivering.
"""
import hashlib
import hmac
import logging
import os
import time

import requests
from flask import jsonify, render_template_string, request

logger = logging.getLogger(__name__)

INSTANT_PDF_CENTS = int(os.getenv("INSTANT_PDF_CENTS") or "10000")   # $100.00
_STRIPE_API = "https://api.stripe.com/v1/checkout/sessions"
# Stripe's own tolerance for replayed webhooks.
_WEBHOOK_TOLERANCE_S = 300


def _stripe_key() -> str:
    return (os.getenv("STRIPE_SECRET_KEY") or "").strip()


def _admin_key() -> str:
    return (os.getenv("INTEGRATIONS_API_KEY") or os.getenv("ADMIN_API_KEY") or "").strip()


def _telegram_deep_link() -> str:
    """Where a driver goes when the payment page is done with them.

    A Stripe redirect owns the tab, so window.close() is refused -- the browser
    only lets a page close what it opened. Sending them back to the bot's chat
    is the thing that actually works on a phone.
    """
    handle = (os.getenv("TELEGRAM_BOT_USERNAME") or "KrabDispatchBot").strip().lstrip("@")
    return f"https://t.me/{handle}" if handle else "https://t.me"


_PAYMENTS_READY = {"at": 0.0, "ok": None, "why": ""}


def payments_ready(db_client, *, ttl: float = 60.0) -> tuple:
    """(ok, why) — can this database record a payment at all?

    The whole instant-tag chain lives in columns created by
    database/migration_instant_pdf.sql: the checkout stamps
    instant_pdf_session_id, the webhook stamps instant_pdf_paid_at, and the
    bot's sweep finds the lead by exactly those two. On a database without
    them PostgREST answers 42703 to every one of those writes and reads, so
    the money is taken, the webhook 500s (and Stripe retries it forever), and
    the tag is never delivered to anybody.

    Checked here because this endpoint is the single place money starts, and
    refusing to sell is the only honest answer when delivery is impossible.
    Cached for a minute: it is one HEAD-shaped select, but it is on the hot path.
    """
    import time as _t
    now = _t.monotonic()
    if _PAYMENTS_READY["ok"] is not None and (now - _PAYMENTS_READY["at"]) < ttl:
        return _PAYMENTS_READY["ok"], _PAYMENTS_READY["why"]
    ok, why = True, ""
    try:
        db_client.table("leads").select(
            "instant_pdf_paid_at, instant_pdf_delivered_at, "
            "instant_pdf_driver_id, instant_pdf_session_id"
        ).limit(1).execute()
    except Exception as e:
        msg = str(e)
        if "42703" in msg or "does not exist" in msg:
            ok = False
            why = ("This database has not run database/migration_instant_pdf.sql, "
                   "so a payment cannot be recorded or the tag delivered.")
        else:
            logger.warning("payments_ready check failed: %s", e)
    _PAYMENTS_READY.update({"at": now, "ok": ok, "why": why})
    return ok, why


def _public_base() -> str:
    """Where Stripe sends the driver back to.

    The admin's own origin by default, NOT the tristatetags.com/backend proxy:
    that is a Vercel rewrite on a site this repo does not deploy, and a
    deployment without it answers every /backend/* path with the marketing
    site's own 404 -- which is exactly what a paying driver saw.
    """
    return (os.getenv("RECEIPT_PORTAL_BASE")
            or "https://krab-issuer-admin.onrender.com").strip().rstrip("/")


def verify_stripe_signature(payload: bytes, header: str, secret: str,
                            now: float | None = None) -> bool:
    """Stripe's `t=…,v1=…` scheme, by hand.

    Signed with the timestamp prepended, so a captured body cannot be replayed
    later, and compared in constant time. Without this check anyone who knows the
    URL could mark any lead paid."""
    if not header or not secret:
        return False
    parts = dict(
        p.split("=", 1) for p in str(header).split(",") if "=" in p
    )
    ts, sig = parts.get("t", ""), parts.get("v1", "")
    if not ts or not sig:
        return False
    try:
        age = abs((now if now is not None else time.time()) - int(ts))
    except (TypeError, ValueError):
        return False
    if age > _WEBHOOK_TOLERANCE_S:
        return False
    expected = hmac.new(
        secret.encode("utf-8"),
        f"{ts}.".encode("utf-8") + payload,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, sig)


_SUCCESS_PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ heading }}</title>
<style>
 :root{color-scheme:light dark}
 body{font:16px/1.55 -apple-system,system-ui,Segoe UI,Roboto,sans-serif;margin:0;
      padding:28px 20px;background:#f4f6f8;color:#12161c;
      display:flex;justify-content:center;align-items:flex-start}
 @media (prefers-color-scheme:dark){body{background:#0f1115;color:#e8eaed}}
 .card{background:#fff;border-radius:18px;padding:30px 26px;max-width:30rem;width:100%;
       box-shadow:0 6px 24px rgba(16,24,40,.10);text-align:center}
 @media (prefers-color-scheme:dark){.card{background:#171a21}}
 .tick{width:74px;height:74px;border-radius:50%;margin:2px auto 18px;
       background:#e7f7ef;color:#0a7a4f;font-size:38px;line-height:74px}
 @media (prefers-color-scheme:dark){.tick{background:#12301f;color:#5fd08a}}
 h1{font-size:1.45rem;margin:0 0 .35rem;letter-spacing:-.01em}
 .lead{color:#0a7a4f;font-weight:650;margin:0 0 18px}
 @media (prefers-color-scheme:dark){.lead{color:#5fd08a}}
 dl{margin:0 0 20px;text-align:left;background:#f6f8fa;border-radius:12px;padding:14px 16px}
 @media (prefers-color-scheme:dark){dl{background:#11141a}}
 .row{display:flex;justify-content:space-between;gap:14px;padding:5px 0}
 .k{color:#6b7280}  .v{font-weight:600;text-align:right;word-break:break-word}
 .ref{font-family:ui-monospace,SFMono-Regular,monospace}
 .back{display:block;padding:15px 20px;border-radius:12px;background:#2f6df6;
       color:#fff;text-decoration:none;font-weight:650}
 .tiny{color:#6b7280;font-size:.87rem;margin:14px 0 0}
 ul{text-align:left;margin:0 0 20px;padding-left:20px;color:#374151}
 @media (prefers-color-scheme:dark){ul{color:#aab2c0}.k,.tiny{color:#8b93a7}}
 li{margin:4px 0}
</style></head><body>
<div class="card">
  <div class="tick">{{ tick }}</div>
  <h1>{{ heading }}</h1>
  <p class="lead">{{ lead }}</p>
  {% if reference_id %}
  <dl>
    <div class="row"><span class="k">Reference</span>
                     <span class="v ref">{{ reference_id }}</span></div>
    {% if amount %}<div class="row"><span class="k">Paid</span>
                     <span class="v">{{ amount }}</span></div>{% endif %}
  </dl>
  {% endif %}
  {% if steps %}<ul>{% for s in steps %}<li>{{ s }}</li>{% endfor %}</ul>{% endif %}
  <a class="back" href="{{ tg_deep }}">Back to Telegram</a>
  <p class="tiny">This page returns you to the chat on its own.</p>
</div>
<script>
  // Straight back to the chat. window.close() only works on a tab the page
  // itself opened, which a Stripe redirect is not, so the deep link is the one
  // that actually lands -- and the button is there for a browser that blocks it.
  setTimeout(function () {
    try { window.location.replace({{ tg_deep|tojson }}); } catch (e) {}
    setTimeout(function () { try { window.close(); } catch (e) {} }, 900);
  }, 2500);
</script>
</body></html>"""


def register(app, db_provider):
    """Mount the instant-PDF endpoints.

    `db_provider` is resolved per request rather than captured, so a rebuilt client
    is picked up and a test double actually takes effect."""
    _resolve = db_provider if callable(db_provider) else (lambda: db_provider)

    def _authorised() -> bool:
        supplied = (request.headers.get("Authorization") or "").strip()
        if supplied.lower().startswith("bearer "):
            supplied = supplied[7:].strip()
        expected = _admin_key()
        return bool(expected and supplied and hmac.compare_digest(supplied, expected))

    def _auth_error() -> str:
        """Which side is wrong — without ever echoing a key.

        A bare "unauthorized" is indistinguishable between "this server has no
        key at all" and "the two keys differ", and those need opposite fixes.
        Drivers only ever saw "Payment page unavailable", so this cost real
        dispatches while nobody could tell what to change.
        """
        if not _admin_key():
            return ("INTEGRATIONS_API_KEY is not set on the admin dashboard "
                    "(krab-issuer-admin) — set it to the same value the bot uses.")
        supplied = (request.headers.get("Authorization") or "").strip()
        if not supplied:
            return "No Authorization header was sent."
        return ("The caller's INTEGRATIONS_API_KEY does not match the admin "
                "dashboard's — set the same value on both Render services.")

    @app.route("/api/instant/checkout", methods=["POST"])
    def api_instant_checkout():
        """The bot asks for a pay link for one lead and one driver."""
        if not _authorised():
            return jsonify({"error": "unauthorized", "detail": _auth_error()}), 401
        key = _stripe_key()
        if not key:
            return jsonify({"error": "STRIPE_SECRET_KEY is not configured"}), 503

        body = request.get_json(silent=True) or {}
        lead_id = str(body.get("lead_id") or "").strip()
        driver_id = str(body.get("driver_id") or "").strip()
        reference_id = str(body.get("reference_id") or "").strip()
        if not lead_id or not driver_id:
            return jsonify({"error": "lead_id and driver_id are required"}), 400
        # Instant Tag sends a per-lead amount (driver_amount = price - $50);
        # absent or nonsense falls back to the flat price. Bounded so a bug can
        # neither charge a cent nor a fortune.
        try:
            amount_cents = int(body.get("amount_cents") or 0)
        except (TypeError, ValueError):
            amount_cents = 0
        if not (100 <= amount_cents <= 1_000_000):
            amount_cents = INSTANT_PDF_CENTS
        # A paid lead is done: a second driver must not be able to pay for it.
        # Type-strict on purpose — an unreadable answer means "unknown", and
        # unknown must sell (the webhook's null-claim still ensures one winner).
        try:
            r = (_resolve().client.table("leads").select("instant_pdf_paid_at")
                 .eq("id", lead_id).limit(1).execute())
            rows = r.data if isinstance(getattr(r, "data", None), list) else []
            row0 = rows[0] if rows else None
            if isinstance(row0, dict) and str(row0.get("instant_pdf_paid_at") or "").strip():
                return jsonify({"error": "already paid"}), 409
        except Exception as e:
            logger.warning("instant checkout: paid-check failed for %s: %s", lead_id, e)

        # Refuse to sell what cannot be delivered. Without the instant_pdf
        # columns the webhook cannot record this payment and the sweep can never
        # find it -- the driver pays and no tag ever arrives.
        _ready, _why = payments_ready(_resolve().client)
        if not _ready:
            logger.error("instant checkout REFUSED for %s: %s", lead_id, _why)
            return jsonify({"error": "payments are not configured", "detail": _why}), 503

        base = _public_base()
        form = {
            "mode": "payment",
            "success_url": f"{base}/instant/success?session_id={{CHECKOUT_SESSION_ID}}",
            "cancel_url": f"{base}/instant/cancelled",
            "line_items[0][quantity]": "1",
            "line_items[0][price_data][currency]": "usd",
            "line_items[0][price_data][unit_amount]": str(amount_cents),
            "line_items[0][price_data][product_data][name]": "Instant temp tag PDF",
            "line_items[0][price_data][product_data][description]":
                f"Straight to the driver, no dispatch wait. Ref {reference_id or lead_id}",
            # On the session so the webhook needs no lookup table of its own.
            "metadata[lead_id]": lead_id,
            "metadata[driver_id]": driver_id,
            "metadata[kind]": "instant_pdf",
            "client_reference_id": lead_id,
        }
        try:
            resp = requests.post(
                _STRIPE_API, data=form, timeout=20,
                auth=(key, ""),
                headers={"Stripe-Version": "2024-06-20",
                         # Same lead asked twice in a row reuses the session rather
                         # than opening a second one it could pay twice.
                         # Amount is part of the key: a re-ask after the price
                         # moved must open a NEW session, not reuse the old total.
                         "Idempotency-Key": f"instant:{lead_id}:{driver_id}:{amount_cents}"},
            )
        except requests.RequestException as e:
            logger.error("instant checkout: Stripe unreachable: %s", e)
            return jsonify({"error": f"Stripe unreachable: {e}"}), 502

        data = resp.json() if resp.content else {}
        if not resp.ok:
            msg = ((data.get("error") or {}).get("message")) or f"Stripe {resp.status_code}"
            logger.error("instant checkout failed for %s: %s", lead_id, msg)
            return jsonify({"error": msg}), 502

        session_id, url = data.get("id"), data.get("url")
        if not url:
            return jsonify({"error": "Stripe returned no checkout url"}), 502
        try:
            _resolve().client.table("leads").update({
                "instant_pdf_requested_at": "now()",
                "instant_pdf_session_id": session_id,
                "instant_pdf_driver_id": driver_id,
                "instant_pdf_amount_cents": amount_cents,
            }).eq("id", lead_id).execute()
        except Exception as e:
            # The link is valid and the webhook can still find the lead by metadata,
            # so this is worth shouting about but not worth refusing the sale.
            logger.error("instant checkout: could not stamp lead %s: %s", lead_id, e)
        return jsonify({"ok": True, "url": url, "session_id": session_id,
                        "amount_cents": amount_cents})

    @app.route("/api/stripe/webhook", methods=["POST"])
    def api_stripe_webhook():
        """Stripe telling us the money arrived. The ONLY thing that sets paid_at."""
        secret = (os.getenv("STRIPE_WEBHOOK_SECRET") or "").strip()
        raw = request.get_data()
        if not verify_stripe_signature(raw, request.headers.get("Stripe-Signature", ""), secret):
            logger.warning("stripe webhook: bad signature, ignored")
            return jsonify({"error": "bad signature"}), 400

        event = request.get_json(silent=True) or {}
        kind = event.get("type") or ""
        obj = ((event.get("data") or {}).get("object")) or {}
        if kind not in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
            return jsonify({"ok": True, "ignored": kind})     # 2xx or Stripe retries forever

        meta = obj.get("metadata") or {}
        lead_id = str(meta.get("lead_id") or obj.get("client_reference_id") or "").strip()
        if not lead_id:
            logger.error("stripe webhook: %s with no lead_id", kind)
            return jsonify({"ok": True, "ignored": "no lead_id"})
        if (obj.get("payment_status") or "") not in ("paid", "no_payment_required"):
            return jsonify({"ok": True, "ignored": "not paid yet"})

        try:
            # Only stamp paid_at, and only once — the bot delivers off the back of
            # it. Stripe retries until it gets a 2xx, so this must be idempotent.
            paid_update = {
                "instant_pdf_paid_at": "now()",
                "instant_pdf_session_id": obj.get("id"),
            }
            # The driver who PAID gets the tag. Several drivers can hold links
            # (the all-drivers broadcast); the checkout stamp holds whoever asked
            # LAST, which is not necessarily who paid.
            if str(meta.get("driver_id") or "").strip():
                paid_update["instant_pdf_driver_id"] = str(meta.get("driver_id")).strip()
            _resolve().client.table("leads").update(paid_update).eq(
                "id", lead_id).is_("instant_pdf_paid_at", "null").execute()
            logger.info("instant pdf PAID for lead %s (session %s)", lead_id, obj.get("id"))
        except Exception as e:
            # A 500 makes Stripe retry, which is what we want when the write failed.
            logger.error("stripe webhook: could not mark %s paid: %s", lead_id, e)
            return jsonify({"error": "could not record the payment"}), 500
        return jsonify({"ok": True, "lead_id": lead_id})

    @app.route("/instant/success", methods=["GET"])
    def instant_success():
        """Thank the driver, tell them what happens next, send them back."""
        ref, amount = "", ""
        sid = (request.args.get("session_id") or "").strip()
        if sid:
            try:
                r = (_resolve().client.table("leads")
                     .select("reference_id, instant_pdf_amount_cents")
                     .eq("instant_pdf_session_id", sid).limit(1).execute())
                row = (r.data or [{}])[0]
                ref = (row.get("reference_id") or "").strip()
                cents = row.get("instant_pdf_amount_cents")
                if cents:
                    amount = f"${int(cents) // 100}"
            except Exception:
                ref, amount = "", ""
        return render_template_string(
            _SUCCESS_PAGE,
            tick="✓",
            heading="Thank you — payment received",
            lead="Your deposit is in. The job is yours.",
            reference_id=ref, amount=amount,
            steps=[
                "The temp tag arrives in your Telegram chat in under a minute.",
                "The client's full address and phone come with it.",
                "Collect the cash on delivery and upload the receipt.",
            ],
            tg_deep=_telegram_deep_link())

    @app.route("/instant/cancelled", methods=["GET"])
    def instant_cancelled():
        return render_template_string(
            _SUCCESS_PAGE,
            tick="×",
            heading="Payment cancelled",
            lead="Nothing was charged.",
            reference_id="", amount="",
            steps=["The job is still open — the offer is in your chat.",
                   "Tap the deposit link again whenever you are ready."],
            tg_deep=_telegram_deep_link())

    logger.info("Instant-PDF endpoints mounted (amount: %d cents)", INSTANT_PDF_CENTS)
