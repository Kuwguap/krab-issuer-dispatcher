"""
Detect phone numbers in text and replace with OneTimeSecret links.
Uses regex only (no random numbers); each number is stored in OTS and replaced by its real link.
"""
import re
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Match phone numbers in any common format (US and international).
# US: +1 (732) 534-2659, +17325342659, 732-534-2659, (732)534-2659, 732 534 2659, 732.534.2659
# Avoids VIN (17 alphanumeric) and long digit-only strings by requiring 3-3-4 structure or +prefix.
PHONE_PATTERN = re.compile(
    r"(?:\+1[-.\s()]*)?\(?[2-9]\d{2}\)?[-.\s]*\d{3}[-.\s]*\d{4}\b"  # US: any separators
    r"|"
    r"\+1\d{10}\b"  # +1 followed by 10 digits, no separators
    r"|"
    r"\+\d{1,4}[-.\s]?(?:\d{1,4}[-.\s]?){2,6}\d{1,4}\b"  # international +code
)


def strip_phone_patterns(text: str) -> str:
    """Remove all phone number patterns from text. Use to clean VIN/car fields before display."""
    if not text or not str(text).strip():
        return text or ""
    return PHONE_PATTERN.sub("", str(text)).strip()


def _normalize_phone_for_cache(match: str) -> str:
    """Normalize to digits (and leading +) for deduplication."""
    digits = re.sub(r"\D", "", match)
    if match.strip().startswith("+"):
        return "+" + digits
    return digits


def replace_phones_with_ots_links(text: str, ots) -> str:
    """
    Find all phone numbers in text, store each in OneTimeSecret, replace with link.
    Same number appearing twice gets the same link (no duplicate OTS).
    """
    if not text or not text.strip():
        return text
    cache: dict[str, Optional[str]] = {}  # normalized_phone -> link
    result = []
    last_end = 0
    for m in PHONE_PATTERN.finditer(text):
        chunk = text[last_end : m.start()]
        result.append(chunk)
        raw = m.group(0)
        key = _normalize_phone_for_cache(raw)
        if key not in cache:
            link = ots.share_secret(raw)
            cache[key] = link
        link = cache[key]
        if link:
            # Wrap in spaces so the link never glues to VIN/car/address (keeps structure readable)
            result.append(" ")
            result.append(link)
            result.append(" ")
        else:
            result.append(" [phone – use link above] ")
        last_end = m.end()
    result.append(text[last_end:])
    # Normalize multiple spaces to single space so we don't get "  " between lines
    return re.sub(r" {2,}", " ", "".join(result))
