"""OpenAI extraction for driver interview questionnaire (12 fields)."""
from __future__ import annotations

import base64
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

INTERVIEW_STRUCTURE_PROMPT = """You are extracting driver onboarding / interview information from an image, screenshot, or pasted text.

STRICT RULES:
- Output ONLY a plain text block with exactly 12 lines. One line per field—nothing else on that line.
- If a value is missing or unreadable, put a single dash "-" after the colon.
- FullName: first and last name together (e.g. John Doe).
- Phone: only numbers explicitly labelled as phone/cell/mobile/contact.
- Email: one valid email or "-".
- Telegram username: with or without @ prefix.
- Telegram ID: numeric Telegram user id if visible, else "-".

Order and labels (exactly one value per line):
1) FullName: <full legal name, first and last>
2) WorkCommitment: <hours per week, availability, schedule commitment>
3) Phone: <phone number>
4) Email: <email address>
5) MailingAddress: <full mailing address>
6) DriverLicenseID: <driver license number>
7) TelegramUsername: <telegram @username>
8) EmergencyContact: <name and phone of emergency contact>
9) Referral: <who referred them, or ->
10) PaymentMethod: <CashApp/Venmo/Zelle/etc>
11) ProfessionSkill: <job skills / profession>
12) TelegramID: <numeric telegram id if stated>

If the image is a driver's license, fill FullName, MailingAddress, and DriverLicenseID from the card; use "-" for fields not visible on the license.

Output nothing else—no explanation, no markdown, no line numbers. Only these 12 lines."""

_LABEL_TO_KEY = {
    "fullname": "full_name",
    "workcommitment": "work_commitment",
    "phone": "phone_number",
    "email": "email",
    "mailingaddress": "mailing_address",
    "driverlicenseid": "drivers_license_id",
    "telegramusername": "telegram_username",
    "emergencycontact": "emergency_contact",
    "referral": "referral",
    "paymentmethod": "payment_method",
    "professionskill": "profession_skill",
    "telegramid": "telegram_id",
}


class AIVisionQuotaError(Exception):
    pass


def split_full_name(full: str) -> tuple[str, str]:
    """Return (first, last) from a single full-name string."""
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


def parse_interview_structured(raw: str) -> dict[str, str]:
    """Parse 12-line AI output into field dict."""
    out = _empty_interview_dict()
    if not raw or not str(raw).strip():
        return out
    for line in str(raw).splitlines():
        line = line.strip()
        if not line:
            continue
        line = re.sub(r"^\d{1,2}[\).\s]+", "", line).strip()
        if ":" not in line:
            continue
        label, _, val = line.partition(":")
        key = _LABEL_TO_KEY.get(re.sub(r"[^a-z0-9]", "", label.lower()))
        if not key:
            continue
        v = val.strip()
        if v in ("-", "—", "–", "none", "n/a", "na"):
            v = ""
        out[key] = v
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
        v = data.get(k)
        out[k] = (str(v).strip() if v is not None else "") or ""
    if out.get("telegram_username") and not out["telegram_username"].startswith("@"):
        if re.match(r"^[A-Za-z0-9_]{3,}$", out["telegram_username"]):
            out["telegram_username"] = "@" + out["telegram_username"]
    out["first_name"] = _derive_first_name(out)
    return out


def _call_openai_text(prompt: str, user_text: str) -> str:
    from config import Config

    if not Config.OPENAI_API_KEY:
        raise ValueError("OPENAI_API_KEY is not configured")
    from openai import OpenAI

    client = OpenAI(api_key=Config.OPENAI_API_KEY, max_retries=0)
    model = Config.OPENAI_VISION_MODEL or "gpt-4o"
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_text},
        ],
        max_tokens=1200,
    )
    return (response.choices[0].message.content or "").strip()


def _call_openai_vision(image_bytes: bytes, mime_type: str = "image/jpeg") -> str:
    from config import Config

    if not Config.OPENAI_API_KEY:
        raise ValueError("OPENAI_API_KEY is not configured")
    from openai import OpenAI

    b64 = base64.standard_b64encode(image_bytes).decode("ascii")
    data_url = f"data:{mime_type};base64,{b64}"
    client = OpenAI(api_key=Config.OPENAI_API_KEY, max_retries=0)
    model = Config.OPENAI_VISION_MODEL or "gpt-4o"
    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": INTERVIEW_STRUCTURE_PROMPT},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
        max_tokens=1200,
    )
    return (response.choices[0].message.content or "").strip()


def extract_interview_from_text(text: str) -> dict[str, str]:
    """Structure freeform interview paste via OpenAI."""
    if not text or not text.strip():
        return normalize_interview_data({})
    try:
        raw = _call_openai_text(
            INTERVIEW_STRUCTURE_PROMPT,
            f"Extract the 12 fields from this interview information:\n\n{text.strip()}",
        )
    except Exception as e:
        err = str(e).lower()
        if "429" in err or "quota" in err or "insufficient" in err:
            raise AIVisionQuotaError(str(e)) from e
        logger.error("extract_interview_from_text: %s", e)
        raise
    return normalize_interview_data(parse_interview_structured(raw))


def extract_interview_from_image(image_bytes: bytes, mime_type: str = "image/jpeg") -> dict[str, str]:
    """Extract interview fields from a photo/screenshot."""
    if not image_bytes:
        return normalize_interview_data({})
    try:
        raw = _call_openai_vision(image_bytes, mime_type)
    except Exception as e:
        err = str(e).lower()
        if "429" in err or "quota" in err or "insufficient" in err:
            raise AIVisionQuotaError(str(e)) from e
        logger.error("extract_interview_from_image: %s", e)
        raise
    return normalize_interview_data(parse_interview_structured(raw))
