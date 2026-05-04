"""
Motivation quotes and message templates for intelligent flow (Pro Mode).
Loads motivation.json; provides CORE / PSYCHOLOGY / AGGRESSIVE / BONUS messages.
"""
import json
import logging
import random
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# Default quotes if file missing
_DEFAULT_QUOTES = [
    "Post more. Close more. Earn more. ⚡",
    "Hustle harder. Get paid faster. 🚀",
    "Small actions → Massive income 📊",
]

_quotes: Optional[List[str]] = None


def _load_quotes() -> List[str]:
    global _quotes
    if _quotes is not None:
        return _quotes
    path = Path(__file__).resolve().parent.parent / "motivation.json"
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list) and data:
                _quotes = [str(q).strip() for q in data if q]
            else:
                _quotes = _DEFAULT_QUOTES
        else:
            _quotes = _DEFAULT_QUOTES
    except Exception as e:
        logger.warning("Could not load motivation.json: %s", e)
        _quotes = _DEFAULT_QUOTES
    return _quotes or _DEFAULT_QUOTES


def get_random_quote() -> str:
    """Return a random quote from motivation.json."""
    quotes = _load_quotes()
    return random.choice(quotes) if quotes else _DEFAULT_QUOTES[0]


# ——— Message type templates (Pro Mode) ———
def core_after_submission() -> str:
    """After client submission: success + random quote."""
    return (
        "✅ **Client received successfully!**\n\n"
        f"⚡️ {get_random_quote()}"
    )


def morning_psychology() -> str:
    """Morning auto push – psychology."""
    return (
        "🌅 **Morning Boost**\n\n"
        f"{get_random_quote()}"
    )


def evening_aggressive() -> str:
    """Evening push – aggressive."""
    return (
        "🔥 **Evening Push**\n\n"
        f"{get_random_quote()}"
    )


def no_clients_24h_aggressive() -> str:
    """No clients for 24h – aggressive nudge."""
    return (
        "⚡ **You’ve got this**\n\n"
        f"{get_random_quote()}"
    )


def top_performer_bonus() -> str:
    """Top performer – bonus message."""
    return (
        "👑 **Top performer**\n\n"
        f"{get_random_quote()}"
    )
