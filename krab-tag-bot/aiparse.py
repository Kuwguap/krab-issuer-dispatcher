"""AI parsing — the SAME engine as krableadsV2 (build-copied ai_vision.py).

Runs OpenAI vision on photos/PDFs and OpenAI text extraction on typed text,
maps krableadsV2's 17-line structured output to the tag fields tagcore expects,
and falls back to the deterministic label parser when the AI isn't configured
or an image can't be read.
"""
from __future__ import annotations

import importlib.util
import logging
import os
import re
from typing import List, Optional, Tuple

import tagcore
from config import Config
from parsing import parse_details

logger = logging.getLogger(__name__)
_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_ai_vision():
    local = os.path.join(_HERE, "taggen", "ai_vision.py")
    if os.path.exists(local):
        import sys
        if _HERE not in sys.path:
            sys.path.insert(0, _HERE)
        from taggen import ai_vision  # type: ignore
        return ai_vision
    src = os.path.abspath(os.path.join(_HERE, "..", "krableadsV2", "utils", "ai_vision.py"))
    if not os.path.exists(src):
        return None
    try:
        spec = importlib.util.spec_from_file_location("krab_ai_vision_src", src)
        mod = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(mod)
        return mod
    except Exception as e:
        logger.warning("ai_vision unavailable: %s", e)
        return None


_AV = _load_ai_vision()


def available() -> bool:
    return _AV is not None and Config.is_ai_vision_configured()


def _fill(dst: dict, src: dict) -> None:
    for k, v in (src or {}).items():
        if v and not dst.get(k):
            dst[k] = v


def _map_17line(raw: str) -> dict:
    """krableadsV2's 17-line structured output → tag fields.

    Lines 1-11 are positional (name/address/csz/delivery.../vin/car/color/
    insurance/policy/extra); lines 12-17 are labeled (Phone/Price/notes/Email/DL).
    """
    if not raw:
        return {}
    lines = [re.sub(r"^\d{1,2}\)\s*", "", l.strip()).strip() for l in raw.splitlines() if l.strip()]

    def L(i: int) -> str:
        return lines[i] if i < len(lines) else ""

    color = L(7)
    if _AV and hasattr(_AV, "normalize_phase1_color"):
        try:
            color = _AV.normalize_phase1_color(color) or color
        except Exception:
            pass
    out = {
        "name": L(0),
        "address": L(1),
        "city_state_zip": L(2),
        "vin": L(5),
        "color": color,
        "insurance_company": L(8),
        "policy": L(9),
    }
    car = L(6)
    if car and car not in ("-", "—"):
        try:
            y, mk, md = tagcore.tag_pdf.parse_car_line(car)
            out["year"], out["make"], out["model"] = y, mk, md
        except Exception:
            pass
    for line in lines[11:]:
        low = line.lower()
        if low.startswith("phone:"):
            out["phone"] = line.split(":", 1)[1].strip()
        elif low.startswith("email:"):
            val = line.split(":", 1)[1].strip()
            if _AV and hasattr(_AV, "normalize_email"):
                val = _AV.normalize_email(val) or val
            out["email"] = val
    # drop placeholder dashes
    return {k: v for k, v in out.items() if v and str(v).strip() not in ("-", "—", "N/A", "n/a")}


def extract(text: str = "", media_parts: Optional[List[Tuple[bytes, str]]] = None) -> dict:
    """Return tag-ready fields from typed text and/or media (photos/PDF pages)."""
    fields: dict = {}
    if media_parts and available():
        try:
            raw = _AV.extract_structured_from_media_parts(media_parts)
            _fill(fields, _map_17line(raw))
        except Exception as e:
            logger.warning("media AI extraction failed: %s", e)
    if text and text.strip():
        if available():
            try:
                raw = _AV.extract_structured_from_text(text)
                _fill(fields, _map_17line(raw))
            except Exception as e:
                logger.warning("text AI extraction failed: %s", e)
        # Deterministic label/freeform parser fills any remaining blanks.
        _fill(fields, parse_details(text))
    return fields
