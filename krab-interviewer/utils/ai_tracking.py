"""OpenAI vision extraction of USPS tracking number and destination ZIP from receipt photos."""
from __future__ import annotations

import base64
import json
import logging
import re
from typing import Any, Dict

logger = logging.getLogger(__name__)

_TRACKING_PROMPT = """You are reading a USPS shipping receipt or label photo.

Extract ONLY:
1) tracking_number — the USPS tracking number (usually 20–22 digits, may have spaces)
2) zip — the 5-digit destination ZIP code (or ZIP+4, return first 5 digits only)

Reply with JSON only, no markdown:
{"tracking_number": "...", "zip": "..."}

Use empty string "" if a field is not visible or unreadable."""


class AITrackingQuotaError(Exception):
    pass


def _empty_result() -> Dict[str, str]:
    return {"tracking_number": "", "zip": ""}


def _normalize_tracking(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    return digits if len(digits) >= 15 else ""


def _normalize_zip(raw: str) -> str:
    m = re.search(r"\b(\d{5})\b", raw or "")
    return m.group(1) if m else ""


def _parse_json_response(raw: str) -> Dict[str, str]:
    out = _empty_result()
    if not raw or not str(raw).strip():
        return out
    cleaned = str(raw).strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        tn = _normalize_tracking(cleaned)
        if tn:
            out["tracking_number"] = tn
        return out
    if isinstance(data, dict):
        out["tracking_number"] = _normalize_tracking(str(data.get("tracking_number") or ""))
        out["zip"] = _normalize_zip(str(data.get("zip") or ""))
    return out


def extract_tracking_zip(image_bytes: bytes, mime_type: str = "image/jpeg") -> Dict[str, str]:
    """Return {tracking_number, zip} from a receipt image."""
    from config import Config

    if not image_bytes:
        return _empty_result()
    if not Config.OPENAI_API_KEY:
        logger.warning("OPENAI_API_KEY not configured for tracking extraction")
        return _empty_result()

    try:
        from openai import OpenAI

        b64 = base64.standard_b64encode(image_bytes).decode("ascii")
        data_url = f"data:{mime_type};base64,{b64}"
        client = OpenAI(api_key=Config.OPENAI_API_KEY, max_retries=0)
        model = Config.OPENAI_VISION_MODEL or "gpt-4o"
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _TRACKING_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Extract tracking number and destination ZIP from this receipt."},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                },
            ],
            max_tokens=300,
        )
        raw = (response.choices[0].message.content or "").strip()
        return _parse_json_response(raw)
    except Exception as e:
        err = str(e).lower()
        if "429" in err or "quota" in err or "insufficient" in err:
            raise AITrackingQuotaError(str(e)) from e
        logger.error("extract_tracking_zip failed: %s", e)
        return _empty_result()


def extract_tracking_from_text(text: str) -> Dict[str, str]:
    """Pull tracking number from plain text (fallback when user types or AI fails)."""
    out = _empty_result()
    if not text or not text.strip():
        return out
    m = re.search(r"\b(\d{15,30})\b", text.replace(" ", ""))
    if m:
        out["tracking_number"] = m.group(1)
    zm = re.search(r"\b(\d{5})(?:-\d{4})?\b", text)
    if zm:
        out["zip"] = zm.group(1)
    return out
