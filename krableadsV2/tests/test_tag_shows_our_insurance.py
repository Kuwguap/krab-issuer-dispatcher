r"""A tag the bot insures itself must print that insurance.

Asked for: "when adding insurance the bot creates insurance and the created
insurance data is supposed to fill the plate insurance company and policy, NJ
and non NJ both ... National Specialty Ins ... use the policy number from the
generated ins as the policy number on the tag".

The tag is built and SENT before the insurance rides along after it, so the
insurer box and the policy box were empty on exactly the cars the bot was
about to cover. The number is minted locally in both states (the same ABP63
series), so it is settled before the tag is drawn and the issue reuses it --
one number on the tag, on the card, and in the portal.

Run:  venv\Scripts\python.exe -m pytest tests/test_tag_shows_our_insurance.py -q
"""
import asyncio
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
from utils import tag_pdf as _tag_pdf  # noqa: E402

SRC = (ROOT / "bot.py").read_text(encoding="utf-8")

# name / addr / csz / dl-addr / dl-csz / vin / car / colour / insurer / policy / notes
VD_UNINSURED = ("Magnolia Diaz\n3125 Park Ave\nBronx, NY 10451\n-\n-\n"
                "JTLKT324364094480\n2006 Scion xB\nGrey\n-\n-\n-")
VD_HAS_GEICO = VD_UNINSURED.replace("\nGrey\n-\n-\n", "\nGrey\nGeico\nPOL-9\n")


def _lead(**over):
    lead = {"id": "11111111-2222-3333-4444-555555555555",
            "reference_id": "OCLMKA8I", "vehicle_details": VD_UNINSURED,
            "delivery_details": "3125 Park Ave\nBronx, NY 10451",
            "plate": "T123456", "tag_control_number": "C00001",
            "wants_insurance": True}
    lead.update(over)
    return lead


def _fields(lead, vehicle=1, db=None):
    db = db or mock.MagicMock()
    with mock.patch.object(bot, "db", db), \
            mock.patch.object(_tag_pdf, "decode_vin_for_tag", lambda v: None):
        return asyncio.run(bot._tag_fields_from_lead(lead, vehicle=vehicle)), db


class OurInsuranceReachesTheTagTest(unittest.TestCase):

    def test_an_uninsured_car_prints_our_carrier_and_policy(self):
        fields, _ = _fields(_lead())
        self.assertEqual("National Specialty Ins", fields["insurance_company"])
        self.assertRegex(fields["policy"], r"^ABP63\d{8}$")

    def test_the_short_name_is_what_the_operator_asked_for(self):
        self.assertEqual("National Specialty Ins", bot.TAG_INSURER_NAME)

    def test_the_number_is_persisted_so_the_card_can_reuse_it(self):
        lead = _lead()
        fields, db = _fields(lead)
        written = [c.args[1] for c in db.update_lead.call_args_list
                   if "insurance_card_policy_number" in c.args[1]]
        self.assertTrue(written, "the policy number must be saved on the lead")
        self.assertEqual(fields["policy"], written[0]["insurance_card_policy_number"])
        self.assertEqual(fields["policy"], lead["insurance_card_policy_number"])

    def test_an_already_issued_policy_is_reused_not_reminted(self):
        lead = _lead(insurance_card_policy_number="ABP6300000001")
        fields, db = _fields(lead)
        self.assertEqual("ABP6300000001", fields["policy"])
        self.assertFalse([c for c in db.update_lead.call_args_list
                          if "insurance_card_policy_number" in c.args[1]],
                         "a settled number must not be minted again")

    def test_the_tag_is_stable_across_re_sends(self):
        lead = _lead()
        first, _ = _fields(lead)
        second, _ = _fields(lead)
        self.assertEqual(first["policy"], second["policy"])


class SomebodyElsesInsuranceIsLeftAloneTest(unittest.TestCase):

    def test_a_client_with_geico_keeps_geico(self):
        fields, db = _fields(_lead(vehicle_details=VD_HAS_GEICO))
        self.assertEqual("Geico", fields["insurance_company"])
        self.assertEqual("POL-9", fields["policy"])
        self.assertFalse([c for c in db.update_lead.call_args_list
                          if "insurance_card_policy_number" in c.args[1]])

    def test_a_lead_that_did_not_ask_for_insurance_is_untouched(self):
        fields, _ = _fields(_lead(wants_insurance=False))
        self.assertEqual("", fields["insurance_company"])
        self.assertEqual("", fields["policy"])

    def test_a_failure_to_settle_never_breaks_the_tag(self):
        db = mock.MagicMock()
        db.update_lead.side_effect = RuntimeError("supabase down")
        fields, _ = _fields(_lead(), db=db)
        self.assertIn("plate", fields)              # the tag still built
        self.assertEqual("", fields["insurance_company"])


class BothStatesUseTheSameNumberTest(unittest.TestCase):

    def test_nj_reuses_the_number_the_tag_printed(self):
        issuer = SRC.split("async def _build_and_send_insurance_card", 1)[1]
        issuer = issuer.split("\nasync def ", 1)[0]
        nj = issuer.split('if card_state == "NJ":', 1)[1]
        self.assertIn('lead.get("insurance_card_policy_number")', nj)
        self.assertIn("or nj.generate_nj_policy_number()", nj)

    def test_ny_already_reused_it(self):
        issuer = SRC.split("async def _build_and_send_insurance_card", 1)[1]
        issuer = issuer.split("\nasync def ", 1)[0]
        self.assertIn('policy_number = (lead.get("insurance_card_policy_number") or "").strip()',
                      issuer)

    def test_both_states_mint_the_same_series(self):
        from utils import insurance_card as ic
        from utils import nj_card_api as nj
        self.assertRegex(ic.generate_policy_number(), r"^ABP63\d{8}$")
        self.assertRegex(nj.generate_nj_policy_number(), r"^ABP63\d{8}$")


if __name__ == "__main__":
    unittest.main()
