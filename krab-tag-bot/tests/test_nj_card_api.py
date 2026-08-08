"""Unit tests for NJ insurance card API client."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils import nj_card_api as nj  # noqa: E402


class TestNjCardApi(unittest.TestCase):
    def test_generate_nj_policy_number_format(self) -> None:
        for _ in range(50):
            n = nj.generate_nj_policy_number()
            self.assertEqual(len(n), 13)
            self.assertTrue(n.startswith("ABP63"))
            self.assertTrue(n[5:].isdigit())

    def test_build_nj_email_payload_camel_case(self) -> None:
        payload = nj.build_nj_email_payload(
            policy_number="ABP6300173880",
            effective_mm_dd_yyyy="05/26/2026",
            expiration_mm_dd_yyyy="06/26/2026",
            vehicle_year="2018",
            vehicle_make="BMW",
            vehicle_model="530",
            vin="4S4BP61C786356675",
            insured_name_upper="JOSEPH C MCRAE JR",
            insured_address_lines=["18 WOODSHORE E", "KEYPORT, NJ 07735"],
            email="client@example.com",
            first_name="JOSEPH",
            phone="+15551234567",
            annual_premium=1200.0,
        )

        self.assertEqual(payload["policyNumber"], "ABP6300173880")
        self.assertEqual(payload["effectiveMmDdYyyy"], "05/26/2026")
        self.assertEqual(payload["expirationMmDdYyyy"], "06/26/2026")
        self.assertEqual(payload["vehicleYear"], "2018")
        self.assertEqual(payload["vehicleMake"], "BMW")
        self.assertEqual(payload["vehicleModel"], "530")
        self.assertEqual(payload["vin"], "4S4BP61C786356675")
        self.assertEqual(payload["insuredNameUpper"], "JOSEPH C MCRAE JR")
        self.assertEqual(payload["insuredAddressLines"], ["18 WOODSHORE E", "KEYPORT, NJ 07735"])
        self.assertEqual(payload["email"], "client@example.com")
        self.assertEqual(payload["firstName"], "JOSEPH")
        self.assertEqual(payload["phone"], "+15551234567")
        self.assertEqual(payload["annualPremium"], 1200.0)
        self.assertIs(payload["skipPortalAccount"], False)

    @patch("utils.nj_card_api.requests.post")
    def test_send_nj_insurance_email_posts_json(self, mock_post) -> None:
        class FakeConfig:
            BARCODE_APP_BASE_URL = "https://barcode.example.com"

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "ok": True,
            "email": "client@example.com",
            "policyNumber": "ABP6300173880",
        }
        mock_post.return_value = mock_resp

        payload = nj.build_nj_email_payload(
            policy_number="ABP6300173880",
            effective_mm_dd_yyyy="05/26/2026",
            expiration_mm_dd_yyyy="06/26/2026",
            vehicle_year="2018",
            vehicle_make="BMW",
            vehicle_model="530",
            vin="4S4BP61C786356675",
            insured_name_upper="JOSEPH C MCRAE JR",
            insured_address_lines=["18 WOODSHORE E", "KEYPORT, NJ 07735"],
            email="client@example.com",
        )

        import types

        fake_config_module = types.SimpleNamespace(Config=FakeConfig)
        with patch.dict(sys.modules, {"config": fake_config_module}):
            result = nj.send_nj_insurance_email(payload)

        self.assertTrue(result.ok)
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], "https://barcode.example.com/api/insurance-card-nj/email")
        self.assertEqual(kwargs["headers"]["Content-Type"], "application/json")
        body = kwargs["json"]
        self.assertEqual(body["policyNumber"], "ABP6300173880")
        self.assertEqual(body["effectiveMmDdYyyy"], "05/26/2026")
        self.assertNotIn("policy_number", body)


if __name__ == "__main__":
    unittest.main()
