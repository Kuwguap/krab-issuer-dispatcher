"""One address given → it is used for both registration and delivery.

The helper existed and the typed/edit paths called it, but the AI extraction merge did
not — so a photo of a registration, or a dictated lead, left the delivery line as "-"
and the driver got no address.

Run:  venv\\Scripts\\python.exe -m pytest tests/test_address_mirroring.py -q
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

import utils.database as udb  # noqa: E402

if "bot" not in sys.modules:
    udb.Database = mock.MagicMock()
import bot  # noqa: E402


def ai_block(reg="-", reg_csz="-", deliv="-", deliv_csz="-"):
    """The 11-line extraction an image or dictation produces."""
    return "\n".join(["John Damian", reg, reg_csz, deliv, deliv_csz,
                      "1HGCM82633A004352", "2019 Honda Accord", "Blue", "-", "-", "-"])


class ExtractionMirrorsTest(unittest.TestCase):

    def test_registration_only_fills_delivery(self):
        d = {}
        bot._merge_phase1_adjust(d, ai_block(reg="123 Main St", reg_csz="Newark NJ 07102"))
        self.assertEqual(d.get("delivery_address"), "123 Main St")
        self.assertEqual(d.get("delivery_city_state_zip"), "Newark NJ 07102")

    def test_a_given_delivery_address_is_never_overwritten(self):
        d = {}
        bot._merge_phase1_adjust(d, ai_block(
            reg="123 Main St", reg_csz="Newark NJ 07102",
            deliv="88 Ocean Ave", deliv_csz="Fort Lee NJ 07024"))
        self.assertEqual(d.get("address"), "123 Main St")
        self.assertEqual(d.get("delivery_address"), "88 Ocean Ave")

    def test_delivery_only_fills_registration(self):
        """The mirror works both ways, as it already did for typed input."""
        d = {}
        bot._merge_phase1_adjust(d, ai_block(deliv="88 Ocean Ave",
                                             deliv_csz="Fort Lee NJ 07024"))
        self.assertEqual(d.get("address"), "88 Ocean Ave")
        self.assertEqual(d.get("city_state_zip"), "Fort Lee NJ 07024")


class TypedPathsStillMirrorTest(unittest.TestCase):

    def test_inline_edit_mirrors(self):
        d = {}
        bot._apply_inline_review_text(d, "address 123 Main St, Newark NJ 07102")
        self.assertEqual(d.get("delivery_address"), "123 Main St")
        self.assertEqual(d.get("delivery_city_state_zip"), "Newark NJ 07102")

    def test_a_separate_delivery_edit_wins(self):
        d = {}
        bot._apply_inline_review_text(d, "address 123 Main St, Newark NJ 07102")
        bot._apply_inline_review_text(d, "delivery address 88 Ocean Ave, Fort Lee NJ 07024")
        self.assertEqual(d.get("address"), "123 Main St")
        self.assertEqual(d.get("delivery_address"), "88 Ocean Ave")


class SubmitTimeNetTest(unittest.TestCase):
    """Whatever route the lead took, it must not dispatch with a blank delivery."""

    def test_helper_fills_an_empty_delivery(self):
        d = {"address": "12 Oak Rd", "city_state_zip": "Jersey City NJ 07305",
             "delivery_address": "-", "delivery_city_state_zip": "-"}
        bot._apply_single_address_as_both(d)
        self.assertEqual(d.get("delivery_address"), "12 Oak Rd")
        self.assertEqual(d.get("delivery_city_state_zip"), "Jersey City NJ 07305")

    def test_submit_runs_the_mirror(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        submit = src.split("async def _submit_lead_from_review", 1)[1][:600]
        self.assertIn("_apply_single_address_as_both", submit,
                      "submit must mirror as a last resort")

    def test_nothing_to_mirror_is_harmless(self):
        d = {"address": "-", "city_state_zip": "-",
             "delivery_address": "-", "delivery_city_state_zip": "-"}
        bot._apply_single_address_as_both(d)
        self.assertEqual(d.get("delivery_address"), "-")


if __name__ == "__main__":
    unittest.main()
