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


def _public_base() -> str:
    return (os.getenv("RECEIPT_PORTAL_BASE")
            or "https://tristatetags.com/backend").strip().rstrip("/")


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
<title>Payment received</title>
<style>
 body{font:16px/1.5 -apple-system,system-ui,sans-serif;margin:0;padding:32px;
      background:#f6f7f9;color:#111;display:flex;justify-content:center}
 @media (prefers-color-scheme:dark){body{background:#111;color:#eee}}
 .card{background:#fff;border-radius:14px;padding:26px;max-width:32rem;
       box-shadow:0 1px 3px rgba(0,0,0,.15)}
 @media (prefers-color-scheme:dark){.card{background:#1c1c1e}}
 h1{font-size:1.3rem;margin:0 0 .5rem}
 .ok{color:#0a7;font-weight:650}
 .ref{font-family:ui-monospace,monospace}
</style></head><body>
<div class="card">
  <h1>✅ Payment received</h1>
  <p class="ok">The tag is on its way to the driver now.</p>
  <p>Reference <span class="ref">{{ reference_id or "—" }}</span>. It arrives in
     their Telegram chat within a minute — no dispatch approval needed.</p>
  <p>You can close this page.</p>
</div></body></html>"""


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

    @app.route("/api/instant/checkout", methods=["POST"])
    def api_instant_checkout():
        """The bot asks for a pay link for one lead and one driver."""
        if not _authorised():
            return jsonify({"error": "unauthorized"}), 401
        key = _stripe_key()
        if not key:
            return jsonify({"error": "STRIPE_SECRET_KEY is not configured"}), 503

        body = request.get_json(silent=True) or {}
        lead_id = str(body.get("lead_id") or "").strip()
        driver_id = str(body.get("driver_id") or "").strip()
        reference_id = str(body.get("reference_id") or "").strip()
        if not lead_id or not driver_id:
            return jsonify({"error": "lead_id and driver_id are required"}), 400

        base = _public_base()
        form = {
            "mode": "payment",
            "success_url": f"{base}/instant/success?session_id={{CHECKOUT_SESSION_ID}}",
            "cancel_url": f"{base}/instant/cancelled",
            "line_items[0][quantity]": "1",
            "line_items[0][price_data][currency]": "usd",
            "line_items[0][price_data][unit_amount]": str(INSTANT_PDF_CENTS),
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
                         "Idempotency-Key": f"instant:{lead_id}:{driver_id}"},
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
                "instant_pdf_amount_cents": INSTANT_PDF_CENTS,
            }).eq("id", lead_id).execute()
        except Exception as e:
            # The link is valid and the webhook can still find the lead by metadata,
            # so this is worth shouting about but not worth refusing the sale.
            logger.error("instant checkout: could not stamp lead %s: %s", lead_id, e)
        return jsonify({"ok": True, "url": url, "session_id": session_id,
                        "amount_cents": INSTANT_PDF_CENTS})

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
            _resolve().client.table("leads").update({
                "instant_pdf_paid_at": "now()",
                "instant_pdf_session_id": obj.get("id"),
            }).eq("id", lead_id).is_("instant_pdf_paid_at", "null").execute()
            logger.info("instant pdf PAID for lead %s (session %s)", lead_id, obj.get("id"))
        except Exception as e:
            # A 500 makes Stripe retry, which is what we want when the write failed.
            logger.error("stripe webhook: could not mark %s paid: %s", lead_id, e)
            return jsonify({"error": "could not record the payment"}), 500
        return jsonify({"ok": True, "lead_id": lead_id})

    @app.route("/instant/success", methods=["GET"])
    def instant_success():
        ref = ""
        sid = (request.args.get("session_id") or "").strip()
        if sid:
            try:
                r = (_resolve().client.table("leads").select("reference_id")
                     .eq("instant_pdf_session_id", sid).limit(1).execute())
                ref = ((r.data or [{}])[0].get("reference_id") or "").strip()
            except Exception:
                ref = ""
        return render_template_string(_SUCCESS_PAGE, reference_id=ref)

    @app.route("/instant/cancelled", methods=["GET"])
    def instant_cancelled():
        return render_template_string(
            _SUCCESS_PAGE.replace("✅ Payment received", "Payment cancelled")
            .replace("The tag is on its way to the driver now.",
                     "Nothing was charged.")
            .replace("It arrives in their Telegram chat within a minute — no "
                     "dispatch approval needed.",
                     "Send the lead the usual way, or ask for the link again."),
            reference_id="")

    logger.info("Instant-PDF endpoints mounted (amount: %d cents)", INSTANT_PDF_CENTS)
