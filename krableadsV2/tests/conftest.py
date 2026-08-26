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
