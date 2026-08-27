"""Pins for dispatch_web/README.md — the two claims that were exactly wrong.

1) The migration sentence used to say `create_lead` REFUSES without
   `migration_lead_api_ingest.sql`. It does the opposite: both ingest columns
   are in utils/database.py's optional-write set, so create_lead retries with
   them dropped, the save SUCCEEDS, the success flash shows, and the lead sits
   undispatched forever. These tests pin the README's corrected wording AND
   the code behavior it now describes — if someone ever removes the ingest
   columns from the optional set (making create_lead actually refuse), the
   behavior test fails and forces a README revisit, and vice versa.

2) The "Running locally" fence used to put the PowerShell alternative as an
   inline `# comment` after the cmd `set` line. cmd has no `#` comments — the
   comment became PART of the stored password (login then fails, no hint) —
   and in PowerShell `set` is Set-Variable, which no child process inherits
   (every route 503s). The fence must keep the two shells on separate, bare
   lines.

This file deliberately does NOT import dispatch_web (no blueprint under test),
and it does not touch the process-shared utils.database module either: the
behavior test executes utils/database.py as a private module copy, so it works
identically whether a sibling suite has already MagicMock-patched
utils.database.Database or this file runs alone.
"""
import importlib.util
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Before ANY repo import/exec: config.py runs load_dotenv at import time, and
# load_dotenv never overrides existing env — setdefault wins over a real .env
# on the dev box and fills the gap on bare CI. Nothing here dials anything
# (Database is built via __new__, never __init__), the dummies are hygiene.
os.environ["SENTRY_DSN"] = ""
os.environ.setdefault("SUPABASE_URL", "https://readmetest-dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "readmetest-dummy-supabase-key")

_README = _REPO_ROOT / "dispatch_web" / "README.md"

INGEST_COLUMNS = ("ingest_dispatch_pending", "external_order_id")


def _readme_text():
    assert _README.is_file(), "dispatch_web/README.md missing at %s" % _README
    return _README.read_text(encoding="utf-8")


def _load_real_database_module():
    """utils/database.py as a fresh private module — immune to the sibling
    suites' load-bearing `utils.database.Database = MagicMock(...)` patch
    (they patch the shared module attribute; this copy is never shared)."""
    spec = importlib.util.spec_from_file_location(
        "utils_database_readme_pin", _REPO_ROOT / "utils" / "database.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------- 1) the migration sentence


def test_readme_no_longer_claims_create_lead_refuses():
    text = _readme_text()
    assert "refuses without it" not in text, (
        "README still carries the backwards claim that create_lead refuses "
        "without migration_lead_api_ingest.sql — it silently succeeds"
    )
    assert not re.search(r"`?create_lead`?[^.\n]{0,60}\brefuses\b", text), (
        "README still says create_lead refuses; the real failure mode is a "
        "silent success with the ingest columns dropped"
    )
    norm = re.sub(r"\s+", " ", text).lower()
    for phrase in (
        "does **not** refuse",
        "the save succeeds",
        "never dispatched",
        "verify the migration ran",
    ):
        assert phrase in norm, (
            "README's migration sentence lost the corrected behavior "
            "(%r not found): without the migration the save succeeds, the "
            "flash shows, and the lead never dispatches" % phrase
        )


def test_ingest_columns_really_are_optional_write_keys():
    """The mechanism behind the README sentence: both columns the migration
    adds are in the optional set create_lead drops-and-retries on. If this
    fails, create_lead's behavior changed — update the README's 'Running
    locally' migration paragraph in the same commit."""
    dbmod = _load_real_database_module()
    for col in INGEST_COLUMNS:
        assert col in dbmod._OPTIONAL_LEADS_WRITE_KEYS, (
            "%s is no longer in _OPTIONAL_LEADS_WRITE_KEYS — create_lead "
            "would now genuinely refuse on an un-migrated DB, which is the "
            "OPPOSITE of what dispatch_web/README.md documents" % col
        )


class _UnmigratedLeadsClient:
    """PostgREST double for a DB that never ran migration_lead_api_ingest.sql:
    an insert carrying an ingest column raises the PGRST204 the live server
    sends (naming the column); a clean insert succeeds."""

    def __init__(self):
        self.attempts = []  # payload snapshot per insert (create_lead mutates in place)
        self._current = None

    def table(self, name):
        assert name == "leads", "unexpected table %r" % name
        return self

    def insert(self, payload):
        self.attempts.append(dict(payload))
        self._current = payload
        return self

    def execute(self):
        for col in INGEST_COLUMNS:
            if col in self._current:
                raise Exception(
                    "{'code': 'PGRST204', 'details': None, 'hint': None, "
                    "'message': \"Could not find the '%s' column of 'leads' "
                    "in the schema cache\"}" % col
                )
        row = dict(self._current)
        row["id"] = "lead-unmigrated-001"
        return SimpleNamespace(data=[row])


def test_create_lead_succeeds_silently_on_unmigrated_db():
    """End-to-end through the REAL create_lead: on an un-migrated DB the save
    returns a lead (so /dispatch/new flashes success) while the dispatch flag
    was silently dropped — the exact trap the README now warns about."""
    dbmod = _load_real_database_module()
    db = dbmod.Database.__new__(dbmod.Database)  # skip __init__: no real client
    db.client = _UnmigratedLeadsClient()
    db._tables_checked = True
    db._tables_exist = True
    db._error_logged = False

    lead = db.create_lead(
        {
            "vehicle_details": "DANA WHITLOCK\n12 MAIN ST\n" + "\n".join(["-"] * 9),
            "reference_id": "REF00001",
            "ingest_dispatch_pending": True,  # what the bot's 10s poll needs
            "external_order_id": "REF00001",
        }
    )

    assert lead is not None and lead.get("id"), (
        "create_lead returned None on the un-migrated DB — it now refuses, "
        "so dispatch_web/README.md's silent-success warning (and newlead.py's "
        "'missing migration' failure banner) describe the wrong world"
    )
    final = db.client.attempts[-1]
    for col in INGEST_COLUMNS:
        assert col not in final, (
            "%s survived to the successful insert — the fake never accepted "
            "it, so create_lead must have dropped it; test double broken" % col
        )
    # The trap itself: the row that got saved carries no dispatch flag, so
    # process_pending_api_lead_dispatches never sees it. Saved ≠ dispatched.
    assert "ingest_dispatch_pending" not in lead or not lead["ingest_dispatch_pending"]


# ------------------------------------------- 2) the "Running locally" fence


def _running_locally_fence():
    text = _readme_text()
    m = re.search(r"^## Running locally\s*$(.*?)(?:^## |\Z)", text, re.M | re.S)
    assert m, "README lost its '## Running locally' section"
    fence = re.search(r"```[^\n]*\n(.*?)```", m.group(1), re.S)
    assert fence, "'Running locally' section lost its fenced command block"
    return fence.group(1).splitlines()


def test_running_locally_has_both_shell_lines_bare():
    lines = [ln.strip() for ln in _running_locally_fence()]
    assert "set DISPATCH_WEB_PASSWORD=letmein" in lines, (
        "cmd line must be exactly 'set DISPATCH_WEB_PASSWORD=letmein' with "
        "nothing after the value — cmd has no comments; anything trailing is "
        "stored inside the password"
    )
    assert '$env:DISPATCH_WEB_PASSWORD="letmein"' in lines, (
        "PowerShell needs its own $env: line — its `set` is Set-Variable, "
        "which the child python never inherits (every route 503s)"
    )


def test_running_locally_no_inline_comment_on_cmd_set_lines():
    for ln in _running_locally_fence():
        stripped = ln.strip()
        if not stripped.lower().startswith("set "):
            continue  # comment/blank/python lines — not cmd env assignments
        assert "#" not in stripped, (
            "cmd `set` line carries an inline # comment; cmd stores it "
            "INSIDE the value (login fails with 'Wrong password.', no hint): "
            "%r" % stripped
        )
        assert "$env:" not in stripped, (
            "cmd and PowerShell forms share a line again — split them: %r"
            % stripped
        )
