"""AI-powered receipt verification using OpenAI GPT vision."""
import logging
import base64
import json
from config import Config

logger = logging.getLogger(__name__)


def verify_receipt_against_addresses(
    image_bytes: bytes,
    expected_addresses: list[dict],
    mime_type: str = "image/jpeg",
) -> dict:
    """
    Analyze a receipt/delivery photo and check which expected addresses are present.

    expected_addresses: list of {"driver_name": ..., "address": ..., "driver_id": ...}

    Returns:
        {
            "found": [{"driver_name": ..., "address": ...}, ...],
            "missing": [{"driver_name": ..., "address": ..., "driver_id": ...}, ...],
            "raw_text": "...",
            "summary": "...",
        }
    """
    if not Config.OPENAI_API_KEY:
        return {"found": [], "missing": expected_addresses, "raw_text": "", "summary": "AI not configured."}

    try:
        from openai import OpenAI
        client = OpenAI(api_key=Config.OPENAI_API_KEY)

        b64 = base64.b64encode(image_bytes).decode("utf-8")
        addr_list = "\n".join(
            f"- {a['driver_name']}: {a['address']}"
            for a in expected_addresses
        )

        response = client.chat.completions.create(
            model=Config.OPENAI_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are verifying a paper/document delivery receipt. "
                        "The image shows a receipt or proof of delivery. "
                        "Extract all addresses and names visible in the image. "
                        "Then compare against the expected list below and report which are found and which are missing."
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"Expected deliveries:\n{addr_list}\n\n"
                                "Look at this receipt image. For each expected address, "
                                "tell me if it appears in the receipt. "
                                "Reply in JSON: {\"found\": [list of driver names found], "
                                "\"missing\": [list of driver names not found], "
                                "\"extracted_text\": \"brief summary of what you see\"}"
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime_type};base64,{b64}"},
                        },
                    ],
                },
            ],
            max_tokens=1000,
        )

        raw = response.choices[0].message.content or ""
        try:
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0]
            parsed = json.loads(cleaned)
        except (json.JSONDecodeError, IndexError):
            parsed = {"found": [], "missing": [], "extracted_text": raw}

        found_names = set(n.lower().strip() for n in parsed.get("found", []))
        addr_by_name = {a["driver_name"].lower().strip(): a for a in expected_addresses}

        found = [addr_by_name[n] for n in found_names if n in addr_by_name]
        missing = [a for a in expected_addresses if a["driver_name"].lower().strip() not in found_names]

        return {
            "found": found,
            "missing": missing,
            "raw_text": parsed.get("extracted_text", raw),
            "summary": f"Found {len(found)}/{len(expected_addresses)} deliveries in receipt.",
        }
    except Exception as e:
        logger.error("AI receipt verification failed: %s", e)
        return {
            "found": [],
            "missing": expected_addresses,
            "raw_text": "",
            "summary": f"AI verification error: {e}",
        }
