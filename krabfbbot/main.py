"""Krab Facebook Sales Bot — Messenger webhook + OpenAI intake."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import sys
from contextlib import asynccontextmanager
from typing import Any, Optional

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from agent import build_forward_payload, extract_slots, generate_reply, slots_complete
from config import Config
from forwarder import forward_lead, queue_if_failed, retry_pending
from messenger import send_message
from state import get_conversation, init_db, save_conversation

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


def _verify_signature(payload: bytes, signature_header: Optional[str]) -> bool:
    secret = Config.META_APP_SECRET
    if not secret:
        return True
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = signature_header.split("=", 1)[1]
    digest = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, expected)


async def handle_incoming_message(item: dict[str, Any]) -> None:
    sender = (item.get("sender") or {}).get("id")
    if not sender:
        return
    msg = item.get("message") or {}
    if msg.get("is_echo"):
        return
    text = (msg.get("text") or "").strip()
    if not text:
        send_message(sender, "Please send a text message with your details.")
        return

    conv = get_conversation(sender)
    slots = dict(conv.get("slots") or {})
    history: list[dict[str, str]] = list(conv.get("history") or [])

    history.append({"role": "user", "content": text})
    slots = extract_slots(text, slots)

    if slots_complete(slots) and conv.get("status") != "completed":
        payload = build_forward_payload(slots, sender)
        ok, ref, err = forward_lead(payload)
        if ok and ref:
            reply = (
                f"You're all set! Reference: {ref}\n\n"
                "Our team has your info and will follow up shortly with your insurance documents."
            )
            save_conversation(
                sender,
                slots=slots,
                history=history + [{"role": "assistant", "content": reply}],
                status="completed",
                reference_id=ref,
            )
        else:
            queue_if_failed(sender, payload, err)
            reply = (
                "Thanks — we have all your details! "
                "A team member will follow up shortly. "
                "(Reference pending — saved locally.)"
            )
            save_conversation(
                sender,
                slots=slots,
                history=history + [{"role": "assistant", "content": reply}],
                status="pending_forward",
            )
    else:
        reply = generate_reply(history, text, slots)
        save_conversation(
            sender,
            slots=slots,
            history=history + [{"role": "assistant", "content": reply}],
            status=conv.get("status") or "active",
            reference_id=conv.get("reference_id"),
        )

    send_message(sender, reply)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler.add_job(
        retry_pending,
        "interval",
        seconds=Config.RETRY_INTERVAL_SECONDS,
        id="retry_forwards",
    )
    scheduler.start()
    logger.info("Krab FB Sales Bot started")
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="Krab Facebook Sales Bot", lifespan=lifespan)


@app.get("/health")
def health():
    return {"ok": True, "service": "krabfbbot"}


@app.get("/webhook")
def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    token = Config.META_VERIFY_TOKEN
    if hub_mode == "subscribe" and hub_verify_token == token:
        return PlainTextResponse(hub_challenge or "")
    raise HTTPException(403, "Verification failed")


@app.post("/webhook")
async def meta_webhook(request: Request):
    raw = await request.body()
    sig = request.headers.get("X-Hub-Signature-256")
    if not _verify_signature(raw, sig):
        raise HTTPException(403, "Invalid signature")

    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid JSON")

    if body.get("object") == "page":
        for entry in body.get("entry", []):
            for item in entry.get("messaging", []):
                await handle_incoming_message(item)
    return {"ok": True}


@app.post("/admin/replay")
async def admin_replay(payload: dict):
    """Debug: simulate a Messenger message without Meta."""
    psid = (payload.get("psid") or "test_psid").strip()
    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text required")
    await handle_incoming_message(
        {"sender": {"id": psid}, "message": {"text": text}}
    )
    conv = get_conversation(psid)
    return {"ok": True, "slots": conv.get("slots"), "status": conv.get("status")}


def run() -> None:
    try:
        Config.validate()
    except ValueError as e:
        logger.error("%s", e)
        sys.exit(1)
    init_db()
    uvicorn.run(app, host="0.0.0.0", port=Config.PORT)


if __name__ == "__main__":
    run()
