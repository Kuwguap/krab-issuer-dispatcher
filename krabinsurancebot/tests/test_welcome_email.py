"""Tests for purchase welcome email with portal login."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils import resend_client as rc  # noqa: E402


class TestWelcomeEmail(unittest.TestCase):
    def test_portal_login_included_when_requested(self) -> None:
        subject, body = rc.build_purchase_welcome_email(
            rc.PurchaseWelcomeEmailInput(
                first_name="Carlos",
                policy_number="ABP6329663932",
                effective_date_label="July 5, 2026",
                vehicle_line="2015 NISSAN Altima — Black",
                portal_email="DinnerTableTeam@gmail.com",
                portal_password="Temp#A9",
                include_portal_login=True,
            )
        )
        self.assertEqual(subject, "Your policy is active — ABP6329663932")
        self.assertIn("Portal Login:", body)
        self.assertIn("Email: DinnerTableTeam@gmail.com", body)
        self.assertIn("Password: Temp#A9", body)
        self.assertIn("Website: TriStateCoverage.com/login", body)
        self.assertIn("Hi Carlos,", body)
        self.assertIn("www.TriStateCoverage.com", body)

    def test_pdf_only_omits_portal_login(self) -> None:
        _subject, body = rc.build_purchase_welcome_email(
            rc.PurchaseWelcomeEmailInput(
                first_name="Carlos",
                policy_number="ABP6329663932",
                effective_date_label="July 5, 2026",
                vehicle_line="2015 NISSAN Altima — Black",
                portal_email="DinnerTableTeam@gmail.com",
                portal_password="",
            )
        )
        self.assertNotIn("Portal Login:", body)
        self.assertNotIn("Password:", body)


if __name__ == "__main__":
    unittest.main()
