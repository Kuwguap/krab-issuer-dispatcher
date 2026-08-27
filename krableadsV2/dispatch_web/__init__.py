"""dispatch_web — web mirror of the krableadsV2 dispatch bot, mounted at /dispatch.

Importing this package IS the wiring: view modules attach their routes to
core.bp at import time, so import once, then hand register(app) the host app.
"""
import importlib
import logging
import pkgutil

from .core import bp, fmt_ts, get_db, register, require_login  # noqa: F401

logger = logging.getLogger(__name__)

# Discover view modules instead of hard-coding names: whatever a builder drops
# into this package gets its routes attached without editing this file. Core is
# already imported above; underscore names are helpers that opt out. One broken
# view must not take the whole host service down with it (admin_dashboard also
# serves tristatetags.com/backend) — log the traceback loudly (ERROR level
# reaches Sentry) and keep the rest of the board alive; the broken module's
# pages 404 until it imports clean.
for _mod in pkgutil.iter_modules(__path__):
    if _mod.name == "core" or _mod.name.startswith("_"):
        continue
    try:
        importlib.import_module("." + _mod.name, __name__)
    except Exception:
        logger.exception(
            "dispatch_web: view module %r failed to import; its pages are dark",
            _mod.name,
        )

__all__ = ["bp", "get_db", "require_login", "register", "fmt_ts"]
