"""Checkout document images for external (website) leads.

tristatetags.com customers can upload a driver's license, insurance card and
VIN photo. Those arrive here as public HTTPS URLs (Supabase storage or the
quicktags server). We do two things with them:

1. Attach them to the lead as ``phase1_attached_files`` entries so the
   accepting group receives every document (Telegram accepts public HTTPS
   URLs anywhere a file_id is accepted — no download/re-upload needed).
2. Best-effort AI gap-fill: read the images with the same Phase 1 vision
   extractor the bot uses and fill in fields the customer left blank
   (VIN, insurance company, policy number, email, driver-license ID).
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

from utils import ai_vision

logger = logging.getLogger(__name__)

MAX_FILES_PER_LEAD = 10
_DOWNLOAD_MAX_BYTES = 12 * 1024 * 1024
_DOWNLOAD_TIMEOUT = 25

# Positional lines (0-based) in the 17-line vision output that map to fields
# the website form may leave blank. Labeled lines (Email:, DriverLicenseID:)
# are matched by prefix instead because their position drifts less reliably.
_VIN_LINE = 5
_INSURANCE_LINE = 8
_POLICY_LINE = 9

_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$", re.IGNORECASE)


def normalize_ingest_files(files: Any) -> List[Dict[str, str]]:
    """Turn raw ``[{"url":..., "label":...}]`` payloads into attachment entries.

    Output shape matches what ``_forward_phase1_attached_files_to_targets``
    already sends: ``{"type": "photo"|"document", "file_id": <https url>,
    "caption": <label>}``. PDFs must go out as documents — Telegram's
    sendPhoto rejects them.
    """
    out: List[Dict[str, str]] = []
    if not isinstance(files, list):
        return out
    seen: set = set()
    for f in files:
        if len(out) >= MAX_FILES_PER_LEAD:
            break
        if isinstance(f, str):
            f = {"url": f}
        if not isinstance(f, dict):
            continue
        url = str(f.get("url") or f.get("file_url") or "").strip()
        if not url or url in seen:
            continue
        try:
            parsed = urlparse(url)
        except Exception:
            continue
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            continue
        seen.add(url)
        is_pdf = parsed.path.lower().endswith(".pdf")
        entry: Dict[str, str] = {
            "type": "document" if is_pdf else "photo",
            "file_id": url,
        }
        label = str(f.get("label") or f.get("caption") or "").strip()
        if label:
            entry["caption"] = label[:200]
        out.append(entry)
    return out


def _download_media_part(url: str) -> Optional[Tuple[bytes, str]]:
    """Fetch one attachment URL and return (image_bytes, mime) for vision.

    PDFs are rendered to a first-page PNG. Oversized or unreadable files are
    skipped — attachment delivery does not depend on this succeeding.
    """
    try:
        r = requests.get(url, timeout=_DOWNLOAD_TIMEOUT, stream=True)
        if not r.ok:
            logger.warning("Ingest file download failed (%s): %s", r.status_code, url[:120])
            return None
        chunks: List[bytes] = []
        total = 0
        for chunk in r.iter_content(chunk_size=65536):
            total += len(chunk)
            if total > _DOWNLOAD_MAX_BYTES:
                logger.warning("Ingest file too large, skipping AI read: %s", url[:120])
                return None
            chunks.append(chunk)
        blob = b"".join(chunks)
        if not blob:
            return None
        mime = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if mime == "application/pdf" or url.lower().split("?")[0].endswith(".pdf") or blob[:5] == b"%PDF-":
            png = ai_vision.pdf_first_page_to_png_bytes(blob)
            return (png, "image/png") if png else None
        if not mime.startswith("image/"):
            mime = "image/jpeg"
        return blob, mime
    except Exception as e:
        logger.warning("Ingest file download error for %s: %s", url[:120], e)
        return None


def _parse_vision_lines(raw_text: str) -> Dict[str, str]:
    """Pull the gap-fillable fields out of the 17-line vision output."""
    found: Dict[str, str] = {}
    lines = [l.strip() for l in (raw_text or "").splitlines() if l.strip()]

    def _clean(val: str) -> str:
        v = (val or "").strip()
        return "" if v in ("-", "–", "—", "N/A", "n/a") else v

    if len(lines) > _VIN_LINE:
        vin = _clean(lines[_VIN_LINE]).replace(" ", "").upper()
        if _VIN_RE.fullmatch(vin):
            found["vin"] = vin
    if len(lines) > _INSURANCE_LINE:
        ins = _clean(lines[_INSURANCE_LINE])
        if ins and ":" not in ins:
            found["insurance_company"] = ins
    if len(lines) > _POLICY_LINE:
        pol = _clean(lines[_POLICY_LINE])
        if pol and ":" not in pol:
            found["insurance_policy_number"] = pol

    for line in lines:
        low = line.lower()
        if low.startswith("email:"):
            email = _clean(line.split(":", 1)[1])
            if email and "@" in email and "email" not in found:
                found["email"] = email
        elif low.startswith("driverlicenseid:") or low.startswith("driver license id:"):
            dl = _clean(line.split(":", 1)[1])
            if dl:
                try:
                    dl = ai_vision.normalize_driver_license_id(dl) or dl
                except Exception:
                    pass
                if dl and "driver_license_id" not in found:
                    found["driver_license_id"] = dl
    # A stray VIN anywhere in the output beats no VIN at all.
    if "vin" not in found:
        for line in lines:
            token = _clean(line).replace(" ", "").upper()
            if _VIN_RE.fullmatch(token):
                found["vin"] = token
                break
    return found


def extract_fields_from_files(
    attachments: List[Dict[str, str]],
    wanted_keys: Optional[List[str]] = None,
) -> Dict[str, str]:
    """Read the attached document URLs with AI vision and return extracted
    field values (canonical keys: vin, insurance_company,
    insurance_policy_number, email, driver_license_id).

    Best-effort: any failure (download, quota, unparsable output) returns
    whatever was extracted so far — never raises.
    """
    from config import Config

    if not attachments or not Config.is_ai_vision_configured():
        return {}
    parts: List[Tuple[bytes, str]] = []
    for entry in attachments:
        url = str(entry.get("file_id") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            continue
        part = _download_media_part(url)
        if part:
            parts.append(part)
    if not parts:
        return {}
    try:
        raw = ai_vision.extract_structured_from_media_parts(parts)
    except ai_vision.AIVisionQuotaError:
        logger.warning("AI vision quota exceeded while reading checkout documents")
        return {}
    except Exception as e:
        logger.warning("AI vision read of checkout documents failed: %s", e)
        return {}
    if not raw:
        return {}
    found = _parse_vision_lines(raw)
    if wanted_keys is not None:
        found = {k: v for k, v in found.items() if k in wanted_keys}
    if found:
        logger.info("Checkout documents AI gap-fill extracted: %s", sorted(found.keys()))
    return found
