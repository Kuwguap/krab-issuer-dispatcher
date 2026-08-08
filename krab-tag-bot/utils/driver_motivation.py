"""Rotating motivational lines for drivers — loads driver_motivation.json from repo root."""
import json
import logging
import random
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

_DEFAULT = [
    "Safe miles, happy clients — keep rolling. 🚗",
    "Upload on time, stack wins every time. 🧾",
]

_quotes: Optional[List[str]] = None


def _load() -> List[str]:
    global _quotes
    if _quotes is not None:
        return _quotes
    path = Path(__file__).resolve().parent.parent / "driver_motivation.json"
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list) and data:
                _quotes = [str(q).strip() for q in data if q]
            else:
                _quotes = _DEFAULT
        else:
            _quotes = _DEFAULT
    except Exception as e:
        logger.warning("Could not load driver_motivation.json: %s", e)
        _quotes = _DEFAULT
    return _quotes or _DEFAULT


def get_random_driver_quote() -> str:
    """Return a random driver-focused quote from driver_motivation.json."""
    qs = _load()
    return random.choice(qs) if qs else _DEFAULT[0]
