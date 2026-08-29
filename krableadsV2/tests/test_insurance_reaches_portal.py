r"""All insurance is TriState Coverage's, so all of it belongs on their board.

tristatecoverage.com/receipts is driven by the `profiles` table and stitches a
policy on by `policies.user_id`, so a client only appears there if the bot has
called POST /api/integrations/clients for them. The NJ branch never did — it
built its card through the barcode app and returned before the portal call —
so NJ policies, the bulk of them, existed only in Telegram and in the client's
inbox.

Run:  venv\Scripts\python.exe -m pytest tests/test_insurance_reaches_portal.py -q
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy-key")

import bot  # noqa: E402
from utils import tristatecoverage_api as tsc  # noqa: E402

SRC = (ROOT / "bot.py").read_text(encoding="utf-8")


def _issuer_src() -> str:
    body = SRC.split("async def _build_and_send_insurance_card", 1)[1]
    return body.split("\nasync def ", 1)[0]


class TheNjBranchRegistersTooTest(unittest.TestCase):
    """The whole point: NJ used to return before the portal was ever called."""

    def test_the_nj_branch_calls_the_portal_before_reporting_success(self):
        nj = _issuer_src().split('if card_state == "NJ":', 1)[1]
        nj = nj.split("if not Config.is_portal_integration_configured()", 1)[0]
        self.assertIn("_register_policy_with_portal", nj)
        # …and it does so BEFORE the success return.
        self.assertLess(nj.index("_register_policy_with_portal"),
                        nj.index("return (\n            True,"))

    def test_a_portal_outage_does_not_fail_a_card_already_delivered(self):
        """The NJ client has the card in hand; the portal is best effort."""
        nj = _issuer_src().split('if card_state == "NJ":', 1)[1]
        nj = nj.split("if not Config.is_portal_integration_configured()", 1)[0]
        self.assertIn("if not nj_portal_ok:", nj)
        self.assertIn("logger.warning", nj)
        # The success return is NOT conditional on the portal.
        self.assertIn("return (\n            True,", nj)

    def test_the_vehicle_label_exists_before_the_state_split(self):
        """It is sent by both states now, so it must be built above the branch."""
        issuer = _issuer_src()
        self.assertLess(issuer.index("vehicle_name_api = "),
                        issuer.index('if card_state == "NJ":'))


class ThePayloadMatchesWhatThePortalAcceptsTest(unittest.TestCase):

    def _payload(self, **over):
        args = dict(email="a@b.com", password="Temp#A9", name="magnolia diaz",
                    phone="551-301-3737", vehicle_name="2006 Scion xB",
                    vin="JTLKT324364094480", policy_number="ABP6300173880",
                    effective_iso="2026-08-29", expiration_iso="2026-09-28",
                    annual_premium=250.0, model_year="2006",
                    vehicle_make="Scion", vehicle_model="xB",
                    policy_address="3125 Park Ave Apt 11D, Bronx, NY 10451")
        args.update(over)
        return bot._portal_payload_for_lead(**args)

    def test_it_sends_modelYear_not_vehicleYear(self):
        """The portal's schema silently DROPS unknown keys — vehicleYear was
        how the year went missing before."""
        p = self._payload()
        self.assertEqual("2006", p["modelYear"])
        self.assertNotIn("vehicleYear", p)

    def test_it_sends_the_address_the_board_falls_back_to(self):
        """/receipts has no address field of its own for API-made members; it
        reads the vehicle's policy_address."""
        self.assertEqual("3125 Park Ave Apt 11D, Bronx, NY 10451",
                         self._payload()["policyAddress"])

    def test_the_name_is_upper_and_the_portal_sends_no_second_email(self):
        p = self._payload()
        self.assertEqual("MAGNOLIA DIAZ", p["name"])
        self.assertIs(True, p["skipWelcomeEmail"])

    def test_empty_values_are_dropped_not_sent_blank(self):
        p = self._payload(model_year=None, vehicle_make=None, policy_address=None)
        for k in ("modelYear", "vehicleMake", "policyAddress"):
            self.assertNotIn(k, p)

    def test_a_missing_phone_still_satisfies_the_schema(self):
        """phone is required, 7-40 chars — an empty one would 400 the call."""
        self.assertEqual("+1 000 000 0000", self._payload(phone="")["phone"])


class AnOkThatDidNotRecordThePolicyIsNotSuccessTest(unittest.TestCase):
    """The portal answers 200 with a `warning` when it skipped the policy —
    a duplicate policy number, or dates it could not read. The member then
    sits on the board with nothing against them."""

    def test_the_warning_is_surfaced(self):
        r = tsc.CreatePortalClientResult(
            True, 200, payload={"ok": True, "warning": "policy_number in use"})
        self.assertEqual("policy_number in use", r.warning)
        self.assertFalse(r.policy_registered)

    def test_a_clean_success_registers(self):
        r = tsc.CreatePortalClientResult(True, 200, payload={"ok": True})
        self.assertIsNone(r.warning)
        self.assertTrue(r.policy_registered)

    def test_a_failure_is_never_registered(self):
        self.assertFalse(tsc.CreatePortalClientResult(False, 400).policy_registered)

    def test_a_non_dict_payload_does_not_explode(self):
        for junk in (None, "text", 5, []):
            r = tsc.CreatePortalClientResult(True, 200, payload=junk)
            self.assertIsNone(r.warning)


class TheHelperRefusesQuietlyWhenUnconfiguredTest(unittest.IsolatedAsyncioTestCase):

    async def test_no_key_means_a_note_not_a_crash(self):
        with mock.patch.object(bot.Config, "is_portal_integration_configured",
                               staticmethod(lambda: False)):
            ok, note = await bot._register_policy_with_portal({}, None, why="NJ")
        self.assertFalse(ok)
        self.assertIn("INTEGRATIONS_API_KEY", note)

    async def test_a_warning_response_reports_the_policy_missing(self):
        res = tsc.CreatePortalClientResult(
            True, 200, payload={"ok": True, "warning": "policy_number in use"})
        with mock.patch.object(bot.Config, "is_portal_integration_configured",
                               staticmethod(lambda: True)), \
                mock.patch.object(tsc, "create_portal_client", lambda p, b: res):
            ok, note = await bot._register_policy_with_portal({}, None, why="NJ")
        self.assertFalse(ok)
        self.assertIn("did not record the policy", note)

    async def test_a_clean_call_reports_success(self):
        res = tsc.CreatePortalClientResult(True, 200, payload={"ok": True})
        with mock.patch.object(bot.Config, "is_portal_integration_configured",
                               staticmethod(lambda: True)), \
                mock.patch.object(tsc, "create_portal_client", lambda p, b: res):
            ok, note = await bot._register_policy_with_portal({}, None, why="NY")
        self.assertTrue(ok)
        self.assertIsNone(note)

    async def test_a_network_error_is_a_note_not_an_exception(self):
        def boom(p, b):
            raise RuntimeError("connection reset")
        with mock.patch.object(bot.Config, "is_portal_integration_configured",
                               staticmethod(lambda: True)), \
                mock.patch.object(tsc, "create_portal_client", boom):
            ok, note = await bot._register_policy_with_portal({}, None, why="NJ")
        self.assertFalse(ok)
        self.assertIn("connection reset", note)


if __name__ == "__main__":
    unittest.main()
