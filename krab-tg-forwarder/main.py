"""Krab Telegram Forwarder — POST /leads -> DM to Telegram."""
from __future__ import annotations

import logging
import secrets
import sys
from contextlib import asynccontextmanager
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from config import Config
from formatter import format_lead_message
from store import init_db, log_delivery
from telegram_client import send_telegram_message

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    try:
        Config.validate(require_telegram=False)
    except ValueError as e:
        logger.warning("Startup config: %s (test mode may still work)", e)
    yield


app = FastAPI(title="Krab Telegram Forwarder", lifespan=lifespan)


class LeadIn(BaseModel):
    name: str = ""
    address: str = ""
    city_state_zip: str = ""
    delivery_address: str = ""
    delivery_city_state_zip: str = ""
    vin: str = ""
    year_make_model: str = ""
    color: str = ""
    current_insurance: str = ""
    policy_number: str = ""
    delivery_time: str = ""
    phone: str = ""
    email: str = ""
    fb_psid: str = Field(default="", description="Facebook Page-Scoped ID")


def _check_api_key(x_forwarder_key: Optional[str]) -> None:
    expected = Config.FORWARDER_API_KEY
    if not expected:
        raise HTTPException(503, "FORWARDER_API_KEY not configured")
    if not x_forwarder_key or not secrets.compare_digest(x_forwarder_key, expected):
        raise HTTPException(401, "Invalid X-Forwarder-Key")


def _new_reference_id() -> str:
    return f"FB-{secrets.token_hex(4).upper()}"


@app.get("/health")
def health():
    return {"ok": True, "service": "krab-tg-forwarder"}


@app.post("/test")
def test_lead(payload: LeadIn, x_forwarder_key: Optional[str] = Header(None)):
    """Validate payload and return reference_id without sending Telegram."""
    _check_api_key(x_forwarder_key)
    ref = _new_reference_id()
    body = payload.model_dump()
    log_delivery(ref, body, status="test", error=None)
    return {"ok": True, "reference_id": ref, "dry_run": True}


@app.post("/leads")
def forward_lead(payload: LeadIn, x_forwarder_key: Optional[str] = Header(None)):
    _check_api_key(x_forwarder_key)
    Config.validate(require_telegram=True)

    ref = _new_reference_id()
    body = payload.model_dump()
    text = format_lead_message(body, reference_id=ref)
    ok, err = send_telegram_message(text)
    if not ok:
        log_delivery(ref, body, status="failed", error=err)
        raise HTTPException(502, err or "Telegram send failed")

    log_delivery(ref, body, status="sent", error=None)
    return {"ok": True, "reference_id": ref}


def run() -> None:
    try:
        Config.validate(require_telegram=False)
    except ValueError as e:
        logger.error("%s", e)
        sys.exit(1)
    init_db()
    uvicorn.run(app, host="0.0.0.0", port=Config.PORT)


if __name__ == "__main__":
    run()
