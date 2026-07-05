"""Unit tests for insurance lead resolution from PDF / notes / Supabase."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot import insurance_lead as il  # noqa: E402


SAMPLE_CLIENT_DETAILS = """JOHN SMITH
123 MAIN ST
NEWARK NJ 07102
123 MAIN ST
NEWARK NJ 07102
1HGCM82633A123456
2018 HONDA ACCORD
BLACK
PROGRESSIVE
POL123
"""


class TestInsuranceLead(unittest.TestCase):
    def test_parse_client_details_phase1(self) -> None:
        lead = il.parse_client_details_text(SAMPLE_CLIENT_DETAILS)
        self.assertIsNotNone(lead)
        lines = (lead or {})["vehicle_details"].splitlines()
        self.assertEqual(lines[0], "JOHN SMITH")
        self.assertEqual(lines[1], "123 MAIN ST")
        self.assertEqual(lines[2], "NEWARK NJ 07102")
        self.assertEqual(lines[5], "1HGCM82633A123456")

    def test_merge_prefers_pdf_over_supabase(self) -> None:
        supabase = {
            "vehicle_details": "DENAO C\n503 GLENMORE AVE 1ST FLR\nBROOKLYN NEW YORK 11207\n\n\nWRONGVIN123456789\n",
            "delivery_details": "",
            "extra_info": "",
        }
        pdf_fields = {
            "first": "John",
            "last": "Smith",
            "address": "123 Main St",
            "city": "Newark",
            "state": "NJ",
            "zip": "07102",
            "vin1": "1HGCM82633A123456",
            "year": "2018",
            "make1": "HONDA",
            "model1": "ACCORD",
            "color": "BLACK",
        }
        pdf_lead = il._lead_from_temp_tag_fields(pdf_fields)
        self.assertIsNotNone(pdf_lead)

        merged = il.merge_insurance_lead(
            supabase_lead=supabase,
            pdf_bytes=None,
            client_details=None,
            file_name=None,
        )
        # Without PDF bytes, supabase is only source — baseline wrong name.
        self.assertEqual(merged["vehicle_details"].splitlines()[0], "DENAO C")

        # Simulate PDF parse by injecting through client_details (same insured data).
        merged2 = il.merge_insurance_lead(
            supabase_lead=supabase,
            pdf_bytes=None,
            client_details=SAMPLE_CLIENT_DETAILS,
            file_name="JOHN SMITH AB12CD34.pdf",
        )
        self.assertEqual(merged2["vehicle_details"].splitlines()[0], "JOHN SMITH")
        self.assertIn("client_details", merged2["_merge_sources"])

    def test_name_from_filename_strips_reference_token(self) -> None:
        self.assertEqual(
            il._name_from_filename("jane doe Xy12Ab34.pdf"),
            "JANE DOE",
        )

    def test_lead_from_temp_tag_fields(self) -> None:
        lead = il._lead_from_temp_tag_fields(
            {
                "first": "MARIA",
                "last": "GARCIA",
                "address": "9 OAK AVE",
                "city": "JERSEY CITY",
                "state": "NJ",
                "zip": "07302",
                "vin1": "5FNRL6H74KB123456",
            }
        )
        self.assertIsNotNone(lead)
        vd = (lead or {})["vehicle_details"].splitlines()
        self.assertEqual(vd[0], "MARIA GARCIA")
        self.assertEqual(vd[1], "9 OAK AVE")
        self.assertIn("JERSEY CITY", vd[2])


if __name__ == "__main__":
    unittest.main()
