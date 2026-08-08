"""Unit tests for external lead message parser."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.external_lead_parser import (  # noqa: E402
    SAMPLE_MESSAGE,
    parse_external_lead_message,
    parse_external_lead_fields,
)


class TestExternalLeadParser(unittest.TestCase):
    def test_parse_sample_message(self) -> None:
        state, errors = parse_external_lead_message(SAMPLE_MESSAGE)
        self.assertEqual(errors, [])
        assert state is not None
        self.assertEqual(state["name"], "Zebin Fang Fang")
        self.assertEqual(state["pending_phone_number"], "2138622301")
        self.assertEqual(state["pending_price"], "$150.00")
        self.assertEqual(state["vin"], "SCA665C56HUX86704")
        self.assertEqual(state["car"], "2017 ROLLS-ROYCE Wraith")
        self.assertEqual(state["color"], "black")
        self.assertEqual(state["insurance_company"], "AC Insurance")
        self.assertEqual(state["insurance_policy_number"], "279-06071-913")
        self.assertEqual(state["email"], "zebinfang1002@gmail.com")
        self.assertEqual(state["external_order_id"], "bf6923ca")
        self.assertIn("28 brookside rd", state["address"].lower())
        self.assertIn("02169", state["city_state_zip"])
        self.assertIn("28 brookside rd", state["delivery_address"].lower())

    def test_vehicle_details_eleven_lines(self) -> None:
        state, _ = parse_external_lead_message(SAMPLE_MESSAGE)
        assert state is not None
        lines = state["vehicle_details"].splitlines()
        self.assertEqual(len(lines), 11)
        self.assertEqual(lines[0], "Zebin Fang Fang")
        self.assertEqual(lines[5], "SCA665C56HUX86704")

    def test_same_address_applied_to_both(self) -> None:
        fields = {
            "name": "Test User",
            "phone": "7322418338",
            "price": "$100.00",
            "vin": "SCA665C56HUX86704",
            "vehicle": "2017 Honda Civic, red",
            "registration address": "543 Garden Place Keyport NJ 07735",
        }
        state, errors = parse_external_lead_fields(fields)
        self.assertEqual(errors, [])
        assert state is not None
        self.assertEqual(state["delivery_address"], state["address"])
        self.assertEqual(state["delivery_city_state_zip"], state["city_state_zip"])

    def test_missing_required_fields(self) -> None:
        state, errors = parse_external_lead_fields({"name": "Only Name"})
        self.assertIsNone(state)
        self.assertTrue(any("Phone" in e for e in errors))
        self.assertTrue(any("Price" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
