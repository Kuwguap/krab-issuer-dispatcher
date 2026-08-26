"""Error reporting, with the customers left out of it.

This bot handles the most sensitive data the business touches: legal names, home
addresses, VINs, driver licence numbers, insurance policy numbers, one-time
phone links and portal passwords. Sentry is a third party. None of that may
reach it, and "we set send_default_pii=False" is not enough on its own —
personal data arrives here inside exception messages, inside local variables,
inside breadcrumb text and inside URLs.

So the scrubbing is the point of this module and the SDK setup is the easy half.
Three layers, because any one of them can be defeated on its own:

  1. the SDK is told to send no PII and no local variables;
  2. every event passes through _scrub(), which walks the whole structure and
     redacts by KEY NAME and by PATTERN — a VIN, a licence plate or a policy
     number is redacted wherever it appears, even inside a free-text message;
  3. anything the scrubber cannot understand is dropped rather than guessed at.

Off unless SENTRY_DSN is set, so a developer machine and the test suite report
nothing anywhere.
"""
import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

_REDACTED = "[redacted]"

# Keys whose VALUE is personal, wherever they appear in an event. Matched as a
# substring of the lower-cased key, so "client_phone_number" and "phone" both go.
_SENSITIVE_KEYS = (
    "name", "address", "city_state_zip", "csz", "zip", "phone", "email",
    "vin", "plate", "tag_control", "control_number", "driver_license", "dl",
    "policy", "insurance_card", "portal_password", "password", "secret",
    "token", "api_key", "apikey", "authorization", "encrypted_link",
    "onetimesecret", "client", "owner", "first", "last", "vehicle_details",
    "delivery", "extra_info", "receipt", "dob", "ssn",
)

# Patterns that identify a person or their vehicle wherever they occur, including
# inside an exception message that was built by string formatting.
_PATTERNS = (
    # A 17-character VIN. Checked first: it is the most distinctive.
    (re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b"), "[vin]"),
    # NJ temp plates, both templates.
    (re.compile(r"\b(?:H\d{6}|\d{6}V)\b"), "[plate]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[email]"),
    # Phone numbers, loosely, in the shapes this business receives them.
    (re.compile(r"\+?1?[\s\-.(]*\d{3}[)\s\-.]*\d{3}[\s\-.]*\d{4}\b"), "[phone]"),
    # A US street address opening — number then words then a street word.
    (re.compile(r"\b\d{1,6}\s+[\w.'-]+(?:\s+[\w.'-]+){0,3}\s+"
                r"(?:st|street|ave|avenue|rd|road|blvd|dr|drive|ln|lane|way|ct|"
                r"court|pl|place|ter|terrace|cir|circle|pkwy|hwy)\b\.?",
                re.IGNORECASE), "[address]"),
    (re.compile(r"\b\d{5}(?:-\d{4})?\b"), "[zip]"),
)

# A sensitive key and its value, as they appear once a dict has been turned into
# a string — which is how a lead usually reaches an exception message. Covers
# Python's repr ('k': 'v'), JSON ("k": "v") and keyword form (k=v).
_KV_IN_TEXT_RE = re.compile(
    r"(['\"]?(?:\w*(?:" + "|".join(_SENSITIVE_KEYS) + r")\w*)['\"]?\s*[:=]\s*)"
    r"(?:'[^']*'|\"[^\"]*\"|[^,;}\)\s][^,;}\)]*)",
    re.IGNORECASE,
)

# Sentry's own scaffolding. These describe WHERE the code failed, never WHO it
# failed for, and they are matched exactly rather than by substring — "filename"
# contains "name", and redacting it removes the stack trace, which is the one
# thing an error report exists to carry.
_STRUCTURAL_KEYS = frozenset({
    "filename", "abs_path", "module", "function", "lineno", "colno", "type",
    "pre_context", "context_line", "post_context", "in_app", "package",
    "platform", "environment", "release", "level", "logger", "transaction",
    "event_id", "timestamp", "sdk",
})
# NOT in that list, deliberately: a bare "name" key in THIS codebase is the
# client's name (lead["name"], state_data["name"]). Sentry's own sdk.name would
# be redacted along with it, and losing the string "sentry.python" costs
# nothing next to leaking a customer.

_MAX_DEPTH = 12


def _scrub_text(value: str, known: tuple = ()) -> str:
    """Redact a free-text string.

    ``known`` is the set of values this event carries under a sensitive key
    somewhere else — so a name that also appears in a formatted message is
    caught, even though nothing about the message itself says it is a name.
    """
    out = value
    # Known values FIRST. These came from a sensitive key elsewhere in this same
    # event, so they are certainties, and a fuzzy pattern running first can eat
    # part of one and leave the rest readable -- a licence number surfaced as
    # "J1234[phone]" exactly that way.
    for value_seen in known:
        if value_seen and len(value_seen) >= 3:
            out = out.replace(value_seen, _REDACTED)
    # A stringified dict still names its fields; use them.
    out = _KV_IN_TEXT_RE.sub(lambda m: m.group(1) + _REDACTED, out)
    for pattern, replacement in _PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def _collect_sensitive_values(obj: Any, into: set, depth: int = 0) -> None:
    """Every value sitting under a sensitive key, anywhere in the event."""
    if depth > _MAX_DEPTH:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            if _is_sensitive_key(k) and isinstance(v, str) and 3 <= len(v) <= 120:
                into.add(v)
            else:
                _collect_sensitive_values(v, into, depth + 1)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _collect_sensitive_values(v, into, depth + 1)


def _is_sensitive_key(key: Any) -> bool:
    k = str(key).lower()
    if k in _STRUCTURAL_KEYS:
        return False
    return any(s in k for s in _SENSITIVE_KEYS)


def _scrub(obj: Any, depth: int = 0, known: tuple = ()) -> Any:
    """Walk anything and redact personal data by key name and by pattern.

    Unknown types are stringified and then pattern-scrubbed rather than passed
    through: a custom object's repr is exactly where a lead ends up.
    """
    if depth > _MAX_DEPTH:
        return _REDACTED
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    if isinstance(obj, str):
        return _scrub_text(obj, known)
    if isinstance(obj, dict):
        return {
            k: (_REDACTED if _is_sensitive_key(k) else _scrub(v, depth + 1, known))
            for k, v in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [_scrub(v, depth + 1, known) for v in obj]
    try:
        return _scrub_text(str(obj), known)
    except Exception:
        return _REDACTED


def _before_send(event, hint):
    """Last gate before anything leaves this process."""
    try:
        seen: set = set()
        _collect_sensitive_values(event, seen)
        return _scrub(event, known=tuple(sorted(seen, key=len, reverse=True)))
    except Exception:
        # A scrubber that raises must not become a leak. Drop the event.
        logger.warning("sentry: scrub failed, dropping the event", exc_info=True)
        return None


def _before_breadcrumb(crumb, hint):
    try:
        return _scrub(crumb)
    except Exception:
        return None


def init_sentry(component: str) -> bool:
    """Start error reporting for one service. True if it is on.

    ``component`` separates the bot worker from the web dashboard in Sentry, so
    an issue says which process it came from.

    A no-op with no SENTRY_DSN, which is the default on a developer machine and
    in the test suite.
    """
    dsn = (os.getenv("SENTRY_DSN") or "").strip()
    if not dsn:
        return False
    try:
        import sentry_sdk
    except ImportError:
        logger.warning("SENTRY_DSN is set but sentry-sdk is not installed")
        return False

    # Render exposes the deployed commit; the release must match what the CI
    # workflow creates or an agent reading the issue gets the wrong source.
    commit = (os.getenv("SENTRY_RELEASE")
              or os.getenv("RENDER_GIT_COMMIT")
              or "")[:40]
    try:
        sentry_sdk.init(
            dsn=dsn,
            release=f"krableads@{commit}" if commit else None,
            environment=os.getenv("SENTRY_ENVIRONMENT", "production"),
            # NOT negotiable on this codebase — see the module docstring.
            send_default_pii=False,
            include_local_variables=False,
            max_request_body_size="never",
            attach_stacktrace=True,
            before_send=_before_send,
            before_breadcrumb=_before_breadcrumb,
            traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0")),
            # A telegram bot retries; a burst of the same failure should not
            # cost the whole quota.
            sample_rate=float(os.getenv("SENTRY_SAMPLE_RATE", "1.0")),
        )
        sentry_sdk.set_tag("component", component)
        logger.info("sentry: reporting as %s, release %s", component, commit or "unset")
        return True
    except Exception:
        logger.warning("sentry: init failed — continuing without it", exc_info=True)
        return False


def capture(exc: BaseException, **tags) -> None:
    """Report an exception that was handled, so it is not invisible.

    Silent excepts are how this codebase has hidden failures before; this makes
    one visible without changing the behaviour around it.
    """
    if not (os.getenv("SENTRY_DSN") or "").strip():
        return
    try:
        import sentry_sdk
        with sentry_sdk.push_scope() as scope:
            for k, v in tags.items():
                scope.set_tag(k, str(v)[:200])
            sentry_sdk.capture_exception(exc)
    except Exception:
        pass
