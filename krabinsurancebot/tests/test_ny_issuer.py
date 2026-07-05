"""NY FS-20 issuer constants."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import Config  # noqa: E402
from utils import insurance_card as ic  # noqa: E402


class TestNyIssuer(unittest.TestCase):
    def test_config_issuer_fixed_to_707(self) -> None:
        self.assertEqual(Config.NY_CARRIER_NAME, "707 National Specialty Insurance Company")
        self.assertEqual(Config.NY_AGENCY_NAME, "Serviced by AIPSO-SAIP")
        self.assertEqual(
            Config.NY_AGENCY_ADDRESS_LINES,
            ("PO Box 6400", "Providence, RI 02940-6200"),
        )
        self.assertNotIn("AMERICAN ROAD", Config.NY_CARRIER_NAME.upper())

    def test_card_issuer_defaults_match(self) -> None:
        issuer = ic.CardIssuer()
        self.assertEqual(issuer.carrier_name, "707 National Specialty Insurance Company")
        self.assertEqual(issuer.agency_name, "Serviced by AIPSO-SAIP")
        self.assertEqual(issuer.agency_address_lines[0], "PO Box 6400")


if __name__ == "__main__":
    unittest.main()
