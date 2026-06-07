"""OpenAI extraction for driver interview questionnaire."""
from __future__ import annotations

import base64
import json
import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

INTERVIEW_FIELD_KEYS = [
    "full_name",
    "work_commitment",
    "phone_number",
    "email",
    "mailing_address",
    "drivers_license_id",
    "telegram_username",
    "emergency_contact",
    "referral",
    "payment_method",
    "profession_skill",
    "telegram_id",
]

# Fields shown on the web interview form (used to validate AI parse results).
FORM_EXTRACT_KEYS = [
    "full_name",
    "work_commitment",
    "phone_number",
    "email",
    "mailing_address",
    "telegram_username",
    "emergency_contact",
    "referral",
    "payment_method",
    "profession_skill",
]

INTERVIEW_JSON_PROMPT = """You extract driver interview / onboarding information from pasted text or an image.

Return ONLY a JSON object (no markdown) with exactly these string keys:
- full_name — first and last name together
- work_commitment — years to work together: 1, 2, 3, Forever, or "I Don't want to work I just need money"
- phone_number — phone or mobile number
- email — email address
- mailing_address — full mailing address for test batch of tag papers (may be multiple lines joined with ", ")
- telegram_username — with or without @
- emergency_contact — family or emergency contact name and phone
- referral — who referred them, or empty string
- payment_method — Zelle, Cashapp, Venmo, or PayPal
- profession_skill — talents / what they are good at
- telegram_id — numeric Telegram user id if visible, else empty string

Use "" for any missing or unreadable field. Do not invent data.

If the image is a driver's license, fill full_name and mailing_address from the card when visible."""

_LABEL_TO_KEY = {
    "fullname": "full_name",
    "full name": "full_name",
    "workcommitment": "work_commitment",
    "work commitment": "work_commitment",
    "phone": "phone_number",
    "phonenumber": "phone_number",
    "phone number": "phone_number",
    "email": "email",
    "mailingaddress": "mailing_address",
    "mailing address": "mailing_address",
    "driverlicenseid": "drivers_license_id",
    "telegramusername": "telegram_username",
    "telegram username": "telegram_username",
    "emergencycontact": "emergency_contact",
    "emergency contact": "emergency_contact",
    "family emergency contact": "emergency_contact",
    "referral": "referral",
    "paymentmethod": "payment_method",
    "payment method": "payment_method",
    "payment choice": "payment_method",
    "payment preference": "payment_method",
    "professionskill": "profession_skill",
    "profession skill": "profession_skill",
    "profession skills": "profession_skill",
    "telegramid": "telegram_id",
    "telegram id": "telegram_id",
}


class AIVisionQuotaError(Exception):
    pass


def split_full_name(full: str) -> tuple[str, str]:
    parts = [p for p in (full or "").strip().split() if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0][:64], ""
    first = parts[0][:64]
    last = " ".join(parts[1:])[:128]
    return first, last


def _empty_interview_dict() -> dict[str, str]:
    return {k: "" for k in INTERVIEW_FIELD_KEYS}


def filled_interview_keys(data: dict[str, Any]) -> list[str]:
    """Non-empty form field keys from an extracted/normalized dict."""
    return [k for k in FORM_EXTRACT_KEYS if (str(data.get(k) or "").strip())]


def _clean_value(v: Any) -> str:
    s = (str(v) if v is not None else "").strip()
    if s.lower() in ("-", "—", "–", "none", "n/a", "na", "null", "unknown"):
        return ""
    return s


def _normalize_label(label: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (label or "").lower()).strip()


def parse_interview_structured(raw: str) -> dict[str, str]:
    """Parse JSON or legacy line-based AI output into field dict."""
    out = _empty_interview_dict()
    if not raw or not str(raw).strip():
        return out

    text = str(raw).strip()
    if text.startswith("{") or text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                merged = dict(parsed)
                if "fields" in parsed and isinstance(parsed["fields"], dict):
                    merged = parsed["fields"]
                for k, v in merged.items():
                    key = k if k in INTERVIEW_FIELD_KEYS else None
                    if not key:
                        norm = re.sub(r"[^a-z0-9]", "", str(k).lower())
                        key = _LABEL_TO_KEY.get(norm)
                    if key:
                        out[key] = _clean_value(v)
                return out
        except json.JSONDecodeError:
            pass

    fence = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text)
    if fence:
        try:
            parsed = json.loads(fence.group(1))
            if isinstance(parsed, dict):
                return parse_interview_structured(json.dumps(parsed))
        except json.JSONDecodeError:
            pass

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        line = re.sub(r"^\d{1,2}[\).\s]+", "", line).strip()
        line = re.sub(r"^\*\*|\*\*$", "", line)
        sep = ":" if ":" in line else ("-" if " - " in line else None)
        if not sep:
            continue
        if sep == ":":
            label, _, val = line.partition(":")
        else:
            label, _, val = line.partition(" - ")
        key = _LABEL_TO_KEY.get(_normalize_label(label.replace(" ", "")))
        if not key:
            key = _LABEL_TO_KEY.get(_normalize_label(label))
        if not key:
            continue
        out[key] = _clean_value(val)
    return out


def _derive_first_name(data: dict[str, str]) -> str:
    full = (data.get("full_name") or "").strip()
    if full:
        first, _ = split_full_name(full)
        if first:
            return first
    un = (data.get("telegram_username") or "").strip().lstrip("@")
    if un:
        return un.split()[0][:64]
    ec = (data.get("emergency_contact") or "").strip()
    if ec:
        return ec.split()[0][:64]
    return ""


def normalize_interview_data(data: dict[str, Any]) -> dict[str, str]:
    out = _empty_interview_dict()
    for k in INTERVIEW_FIELD_KEYS:
        out[k] = _clean_value(data.get(k))
    if out.get("telegram_username") and not out["telegram_username"].startswith("@"):
        if re.match(r"^[A-Za-z0-9_]{3,32}$", out["telegram_username"].lstrip("@")):
            out["telegram_username"] = "@" + out["telegram_username"].lstrip("@")
    out["first_name"] = _derive_first_name(out)
    return out


def _openai_client():
    from config import Config
    from openai import OpenAI

    if not Config.OPENAI_API_KEY:
        raise ValueError("OPENAI_API_KEY is not configured")
    return OpenAI(api_key=Config.OPENAI_API_KEY, max_retries=0), Config.OPENAI_VISION_MODEL or "gpt-4o"


def _raise_if_quota(e: Exception) -> None:
    err = str(e).lower()
    if "429" in err or "quota" in err or "insufficient" in err:
        raise AIVisionQuotaError(str(e)) from e


def _extract_json_via_openai_text(user_text: str) -> dict[str, str]:
    client, model = _openai_client()
    response = client.chat.completions.create(
        model=model,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": INTERVIEW_JSON_PROMPT},
            {"role": "user", "content": user_text},
        ],
        max_tokens=1200,
    )
    raw = (response.choices[0].message.content or "").strip()
    return normalize_interview_data(parse_interview_structured(raw))


def _extract_json_via_openai_vision(image_bytes: bytes, mime_type: str = "image/jpeg") -> dict[str, str]:
    client, model = _openai_client()
    b64 = base64.standard_b64encode(image_bytes).decode("ascii")
    if mime_type.lower() in ("image/heic", "image/heif"):
        mime_type = "image/jpeg"
    data_url = f"data:{mime_type};base64,{b64}"
    response = client.chat.completions.create(
        model=model,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": INTERVIEW_JSON_PROMPT},
                    {"type": "image_url", "image_url": {"url": data_url, "detail": "high"}},
                ],
            }
        ],
        max_tokens=1200,
    )
    raw = (response.choices[0].message.content or "").strip()
    return normalize_interview_data(parse_interview_structured(raw))


def extract_interview_from_text(text: str) -> dict[str, str]:
    if not text or not text.strip():
        return normalize_interview_data({})
    try:
        return _extract_json_via_openai_text(
            f"Extract interview fields from this text:\n\n{text.strip()}"
        )
    except AIVisionQuotaError:
        raise
    except Exception as e:
        _raise_if_quota(e)
        logger.error("extract_interview_from_text: %s", e, exc_info=True)
        raise


def extract_interview_from_image(image_bytes: bytes, mime_type: str = "image/jpeg") -> dict[str, str]:
    if not image_bytes:
        return normalize_interview_data({})
    try:
        return _extract_json_via_openai_vision(image_bytes, mime_type)
    except AIVisionQuotaError:
        raise
    except Exception as e:
        _raise_if_quota(e)
        logger.error("extract_interview_from_image: %s", e, exc_info=True)
        raise
