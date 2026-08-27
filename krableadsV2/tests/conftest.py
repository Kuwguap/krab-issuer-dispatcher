"""The test suite reports to nobody.

Several tests build the real Application by running ``bot.main()``, which calls
``init_sentry("bot")``, and ``admin_dashboard.py`` calls ``init_sentry`` at
module scope. On a developer machine that happens to have SENTRY_DSN exported --
which is exactly the machine someone debugging Sentry is sitting at -- test-run
errors would be transmitted to the live project, mixed in with production issues
and spending the quota on fixtures.

CI already sets SENTRY_DSN to '' explicitly. This does the same for everyone
else, so the guarantee does not depend on remembering.

Set at MODULE scope, not in a fixture: pytest imports conftest before it
collects test modules, and a module-scope ``init_sentry`` call fires on import.
A session fixture would run after collection, which is already too late.
"""
import os

# Before any test module -- and therefore before any bot import -- is loaded.
os.environ["SENTRY_DSN"] = ""

import pytest


@pytest.fixture(autouse=True)
def _closed_nl_breaker():
    """Every test starts with the router's circuit breaker CLOSED.

    The breaker is module-global RAM: one suite's quota test would otherwise
    leave it open for 300s of wall-clock, turning every later test that touches
    classify() into a vacuous pass.
    """
    try:
        from utils import nl_router
        nl_router._breaker_reset()
    except Exception:
        pass
    yield


# The chat layer (the model reading every message first) is OFF for the suite.
# Two reasons: the ~34 existing parser suites pin the DETERMINISTIC ladder,
# which is the layer's fallback contract and must stay tested as such; and
# config.py's load_dotenv re-supplies a real OPENAI_API_KEY from .env on dev
# machines even after test modules pop it -- with the layer on by default,
# every e2e suite would make real OpenAI calls locally. The layer's own tests
# flip this back on with a mocked classify.
os.environ["KRAB_CHAT_LAYER"] = "0"
