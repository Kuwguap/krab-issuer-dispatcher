"""OpenAI sales agent + JSON slot extraction."""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from openai import OpenAI

from config import REQUIRED_SLOTS, Config

logger = logging.getLogger(__name__)

SLOT_EXTRACT_PROMPT = """Extract insurance lead fields from the conversation snippet below.
Return ONLY a JSON object with these string keys (use "" if unknown — never invent):
name, address, city_state_zip, delivery_address, delivery_city_state_zip,
vin, year_make_model, color, current_insurance, policy_number, delivery_time, phone, email

Rules:
- vin: exactly 17 alphanumeric characters or ""
- phone: only if clearly a phone number
- email: must look like an email or ""
"""


def _client() -> Optional[OpenAI]:
    if not Config.OPENAI_API_KEY:
        return None
    return OpenAI(api_key=Config.OPENAI_API_KEY, max_retries=0)


def _parse_json(text: str) -> Optional[dict[str, Any]]:
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```\w*\s*", "", t)
        t = re.sub(r"\s*```\s*$", "", t).strip()
    try:
        data = json.loads(t)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", t)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


def _regex_slots(text: str) -> dict[str, str]:
    slots: dict[str, str] = {}
    email_m = re.search(r"[\w.+-]+@[\w.-]+\.\w+", text)
    if email_m:
        slots["email"] = email_m.group(0).lower()
    phone_m = re.search(r"\+?1?\s*\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}", text)
    if phone_m:
        slots["phone"] = phone_m.group(0).strip()
    vin_m = re.search(r"(?<![A-HJ-NPR-Z0-9])[A-HJ-NPR-Z0-9]{17}(?![A-HJ-NPR-Z0-9])", text.upper())
    if vin_m:
        slots["vin"] = vin_m.group(0)
    return slots


def extract_slots(user_text: str, existing: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    merged.update({k: v for k, v in _regex_slots(user_text).items() if v})

    client = _client()
    if not client:
        return merged

    prompt = (
        SLOT_EXTRACT_PROMPT
        + "\n\nCurrent slots:\n"
        + json.dumps({k: merged.get(k, "") for k in REQUIRED_SLOTS})
        + "\n\nLatest user message:\n"
        + user_text[:4000]
    )
    try:
        resp = client.chat.completions.create(
            model=Config.OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.0,
            max_tokens=800,
        )
        raw = (resp.choices[0].message.content or "").strip()
        data = _parse_json(raw)
        if data:
            for k in REQUIRED_SLOTS:
                v = (data.get(k) or "").strip()
                if v:
                    merged[k] = v
    except Exception as e:
        logger.warning("Slot extraction failed: %s", e)
    return merged


def slots_complete(slots: dict[str, Any]) -> bool:
    return all((slots.get(k) or "").strip() for k in REQUIRED_SLOTS)


def missing_slots(slots: dict[str, Any]) -> list[str]:
    return [k for k in REQUIRED_SLOTS if not (slots.get(k) or "").strip()]


def generate_reply(history: list[dict[str, str]], user_text: str, slots: dict[str, Any]) -> str:
    client = _client()
    missing = missing_slots(slots)
    if not client:
        if missing:
            return (
                "Thanks! I still need: "
                + ", ".join(m.replace("_", " ") for m in missing[:3])
                + ". Please send those details."
            )
        return "Thanks! We have your info and will follow up shortly."

    system = Config.FB_BOT_SYSTEM_PROMPT
    if missing:
        system += f"\n\nStill missing fields: {', '.join(missing)}. Ask for one or two at a time."

    messages: list[dict[str, str]] = [{"role": "system", "content": system}]
    messages.extend(history[-12:])
    messages.append({"role": "user", "content": user_text})

    try:
        resp = client.chat.completions.create(
            model=Config.OPENAI_MODEL,
            messages=messages,
            temperature=0.7,
            max_tokens=500,
        )
        return (resp.choices[0].message.content or "").strip() or "Thanks for your message!"
    except Exception as e:
        logger.warning("OpenAI reply failed: %s", e)
        return "Thanks! Could you resend that? I'm having a brief technical issue."


def build_forward_payload(slots: dict[str, Any], psid: str) -> dict[str, Any]:
    return {
        "name": slots.get("name", ""),
        "address": slots.get("address", ""),
        "city_state_zip": slots.get("city_state_zip", ""),
        "delivery_address": slots.get("delivery_address", ""),
        "delivery_city_state_zip": slots.get("delivery_city_state_zip", ""),
        "vin": slots.get("vin", ""),
        "year_make_model": slots.get("year_make_model", ""),
        "color": slots.get("color", ""),
        "current_insurance": slots.get("current_insurance", ""),
        "policy_number": slots.get("policy_number", ""),
        "delivery_time": slots.get("delivery_time", ""),
        "phone": slots.get("phone", ""),
        "email": slots.get("email", ""),
        "fb_psid": psid,
    }
