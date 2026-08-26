r"""Nothing about a customer reaches Sentry.

This bot handles the most sensitive data the business touches — legal names, home
addresses, VINs, driver licence numbers, insurance policies, one-time phone links
and portal passwords. Sentry is a third party, and an AI agent reading the issues
is a fourth. So this file is not a nicety; it is the control that makes sending
errors off-site acceptable at all.

`send_default_pii=False` alone does NOT do it. Personal data arrives inside
exception MESSAGES, inside breadcrumb text, inside URLs, and inside dicts that
were stringified long before the SDK saw them:

    KeyError: lead {'name': 'CHARLES G JONES', 'driver_license_id': 'D1234567'}

Key-based redaction cannot see those keys — they are text by then. Hence the
in-text key/value pass and the cross-reference pass, both exercised below.

WHAT IS NOT COVERED, stated rather than implied: a name typed straight into a
message with no structured counterpart anywhere in the same event cannot be
recognised — "Could not send tag for CHARLES JONES" is indistinguishable from
prose without already knowing the name. The last test in this file is the
mitigation: it fails when new code formats a lead field into a log message.

Run:  venv\Scripts\python.exe -m pytest tests/test_observability.py -q
"""
import json
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from utils import observability as obs  # noqa: E402

# One real lead, in the shapes it actually reaches an error report.
NAME = "CHARLES G JONES"
VIN = "4T1BF3EK6AU051219"
PHONE = "845-423-9476"
EMAIL = "charles@example.com"
ADDRESS = "11530 Mango terrace drive apt.102"
CSZ = "Seffner Florida 33584"
DL = "D1234567"
POLICY = "982658176"
PLATE = "477040V"
PASSWORD = "Temp#A9"

MUST_NEVER_APPEAR = [NAME, "CHARLES", "JONES", VIN, PHONE, EMAIL, ADDRESS,
                     "Mango terrace", "Seffner", DL, POLICY, PLATE, PASSWORD,
                     "33584"]

# Debugging context that MUST survive — a scrubber that redacts everything is
# just an off switch with extra steps.
MUST_SURVIVE = ["lead-abc-123", "ABC12345", "KeyError", "$150", "bot.py"]


def event_with_everything():
    return {
        "message": f"Could not send tag for {NAME}, VIN {VIN}, plate {PLATE}",
        "exception": {"values": [{
            "type": "KeyError",
            "value": (f"lead {{'name': '{NAME}', 'address': '{ADDRESS}', "
                      f"'city_state_zip': '{CSZ}', 'phone_number': '{PHONE}', "
                      f"'email': '{EMAIL}', 'driver_license_id': '{DL}', "
                      f"'insurance_policy_number': '{POLICY}'}}"),
            "stacktrace": {"frames": [{"filename": "bot.py", "lineno": 9412,
                                       "function": "_build_and_send_tag_pdf"}]},
        }]},
        "extra": {
            "lead_id": "lead-abc-123",
            "reference_id": "ABC12345",
            "price": "$150",
            "client_name": NAME,
            "vehicle_details": f"{NAME}\n{ADDRESS}\n{VIN}",
            "portal_password": PASSWORD,
            "driver_license_id": DL,
        },
        "breadcrumbs": [
            {"message": f"emailing {NAME} at {EMAIL}"},
            {"message": f"allocated plate {PLATE} for {VIN}"},
        ],
        "request": {"url": f"https://x/api/receipts/link/abc?vin={VIN}&phone={PHONE}",
                    "headers": {"Authorization": "Bearer sk-live-secret"}},
        "user": {"id": 555, "username": "issuer", "email": EMAIL},
    }


def sent(event):
    """What would actually leave the process, as text."""
    out = obs._before_send(event, None)
    return json.dumps(out) if out is not None else ""


class NoCustomerDataLeavesTheProcessTest(unittest.TestCase):

    def setUp(self):
        self.blob = sent(event_with_everything())

    def test_nothing_identifying_survives(self):
        for secret in MUST_NEVER_APPEAR:
            with self.subTest(value=secret):
                self.assertNotIn(secret, self.blob)

    def test_the_debugging_context_does_survive(self):
        """Redacting everything would make the reports useless, which is its own
        kind of failure — the agent reading them could not fix anything."""
        for keep in MUST_SURVIVE:
            with self.subTest(value=keep):
                self.assertIn(keep, self.blob)

    def test_a_stringified_dict_is_caught(self):
        """The common shape: the dict became a string before the SDK saw it, so
        key-based redaction is blind to it."""
        blob = sent({"exception": {"values": [{"value":
            f"KeyError: {{'name': '{NAME}', 'vin': '{VIN}', 'policy': '{POLICY}'}}"}]}})
        for s in (NAME, VIN, POLICY):
            self.assertNotIn(s, blob)

    def test_a_name_repeated_in_free_text_is_caught_by_cross_reference(self):
        """The name appears under a key AND inside a formatted message; finding
        it once is enough to redact it everywhere in the same event."""
        blob = sent({"extra": {"client_name": NAME},
                     "message": f"retrying delivery for {NAME} now"})
        self.assertNotIn(NAME, blob)
        self.assertNotIn("CHARLES", blob)

    def test_json_and_keyword_shapes_too(self):
        for text in (f'{{"vin": "{VIN}", "phone": "{PHONE}"}}',
                     f"vin={VIN} phone={PHONE}",
                     f"'driver_license_id': '{DL}'"):
            with self.subTest(shape=text[:24]):
                blob = sent({"message": text})
                for s in (VIN, PHONE, DL):
                    self.assertNotIn(s, blob)

    def test_secrets_and_headers(self):
        blob = sent({"request": {"headers": {"Authorization": "Bearer sk-live-x"}},
                     "extra": {"api_key": "sk-live-y", "onetimesecret_token": "tok-z"}})
        for s in ("sk-live-x", "sk-live-y", "tok-z"):
            self.assertNotIn(s, blob)

    def test_a_scrubber_that_raises_drops_the_event(self):
        """Failing open would be a leak. It must fail closed."""
        with mock.patch.object(obs, "_scrub", side_effect=RuntimeError("boom")):
            self.assertIsNone(obs._before_send({"message": NAME}, None))

    def test_breadcrumbs_go_through_it_too(self):
        """A breadcrumb is scrubbed twice: once on its own as it is recorded, and
        again inside the event that carries it. The standalone pass catches
        everything patterned; a bare NAME has no structured counterpart to
        cross-reference against until the event is assembled, which is the
        documented gap the last test in this file guards."""
        crumb = obs._before_breadcrumb({"message": f"{NAME} {VIN} {PHONE}"}, None)
        blob = json.dumps(crumb)
        for s in (VIN, PHONE):
            self.assertNotIn(s, blob)

    def test_a_breadcrumb_is_scrubbed_again_inside_its_event(self):
        """Which is where the cross-reference closes the gap above."""
        blob = sent({"extra": {"client_name": NAME},
                     "breadcrumbs": [{"message": f"emailing {NAME}"}]})
        self.assertNotIn(NAME, blob)

    def test_the_stack_trace_is_never_redacted(self):
        """"filename" contains "name". Redacting it removes the one thing the
        report exists to carry — and the one thing an agent can act on."""
        blob = sent({"exception": {"values": [{"stacktrace": {"frames": [
            {"filename": "bot.py", "abs_path": "/app/bot.py", "lineno": 9412,
             "function": "_build_and_send_tag_pdf", "module": "bot"}]}}]}})
        for keep in ("bot.py", "9412", "_build_and_send_tag_pdf"):
            self.assertIn(keep, blob)

    def test_but_a_bare_name_key_is_still_the_client(self):
        """The exclusion list must not reach "name" itself — in this codebase
        lead["name"] IS the customer."""
        self.assertNotIn(NAME, sent({"extra": {"name": NAME}}))

    def test_deeply_nested_data_is_still_reached(self):
        deep = {"a": {"b": {"c": {"d": {"e": {"vin": VIN, "name": NAME}}}}}}
        self.assertNotIn(VIN, sent(deep))


class ItIsOffUnlessTurnedOnTest(unittest.TestCase):
    """A developer machine and CI must report nothing anywhere."""

    def test_no_dsn_means_no_reporting(self):
        with mock.patch.dict(os.environ, {"SENTRY_DSN": ""}, clear=False):
            self.assertFalse(obs.init_sentry("test"))

    def test_capture_is_a_no_op_without_a_dsn(self):
        with mock.patch.dict(os.environ, {"SENTRY_DSN": ""}, clear=False):
            obs.capture(ValueError("boom"), lead="x")     # must not raise

    def test_it_never_takes_the_process_down(self):
        """Error reporting that crashes the bot is worse than none."""
        with mock.patch.dict(os.environ, {"SENTRY_DSN": "https://x@y/1"}), \
             mock.patch.dict(sys.modules, {"sentry_sdk": None}):
            self.assertFalse(obs.init_sentry("test"))


class TheReleaseMatchesWhatCiCreatesTest(unittest.TestCase):
    """If these two strings disagree, every event lands under a release carrying
    no source — and the agent reading the issue sees the wrong code, which is
    worse than seeing none."""

    def test_the_version_format_is_identical(self):
        src = (ROOT / "utils" / "observability.py").read_text(encoding="utf-8")
        wf = (ROOT.parent / ".github" / "workflows" / "sentry-release.yml").read_text(
            encoding="utf-8")
        self.assertIn('f"krableads@{commit}"', src)
        self.assertIn("version: krableads@", wf)

    def test_the_release_only_happens_after_ci_passes(self):
        wf = (ROOT.parent / ".github" / "workflows" / "sentry-release.yml").read_text(
            encoding="utf-8")
        self.assertIn("workflow_run", wf)
        self.assertIn("conclusion == 'success'", wf)

    def test_ci_pins_the_python_that_production_runs(self):
        ci = (ROOT.parent / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("python-version: '3.11'", ci)

    def test_ci_cannot_report_into_the_live_project(self):
        ci = (ROOT.parent / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("SENTRY_DSN: ''", ci)


class NewCodeDoesNotFormatLeadFieldsIntoLogsTest(unittest.TestCase):
    r"""The one gap the scrubber cannot close: a bare name in a message with no
    structured counterpart. So the mitigation is here instead — this fails when
    someone writes logger.info("... %s", lead["name"]) or an f-string of one.

    If it fires on a legitimate line, log the reference_id or the lead id
    instead; both are useful in an issue and neither identifies anybody.
    """

    # Fields that identify a person or their vehicle. reference_id, lead id and
    # price are deliberately absent — those are what you SHOULD log.
    RISKY = ("name", "address", "city_state_zip", "phone_number", "email",
             "driver_license_id", "vin", "insurance_policy_number",
             "vehicle_details", "portal_password")

    def test_no_log_line_formats_one(self):
        offenders = []
        for path in (ROOT / "bot.py", ROOT / "admin_dashboard.py"):
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                s = line.strip()
                if not re.search(r"\b(?:logger\.\w+|logging\.\w+)\(", s):
                    continue
                for field in self.RISKY:
                    # lead["name"] / lead.get("name") / {name} inside an f-string
                    if re.search(r'''[\[(]\s*["']%s["']\s*[\])]''' % field, s) or \
                       re.search(r"\{[^{}]*\b%s\b[^{}]*\}" % field, s):
                        offenders.append(f"{path.name}:{n}  {s[:96]}")
                        break
        self.assertEqual(offenders, [], "\n".join(
            ["a lead field is being formatted into a log line — log the "
             "reference_id or lead id instead:"] + offenders))


if __name__ == "__main__":
    unittest.main()
