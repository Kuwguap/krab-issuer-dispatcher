"""Tests for NY driver license ID requirement."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils import parse_lead as pl  # noqa: E402


class TestNyDriverLicenseId(unittest.TestCase):
    def test_has_driver_license_id(self) -> None:
        self.assertTrue(pl.has_driver_license_id({"driver_license_id": "123456789"}))
        self.assertFalse(pl.has_driver_license_id({"driver_license_id": "-"}))
        self.assertFalse(pl.has_driver_license_id({}))

    def test_ny_needs_driver_license_id(self) -> None:
        lead = {"driver_license_id": ""}
        self.assertTrue(pl.ny_needs_driver_license_id(lead, "NY"))
        self.assertFalse(pl.ny_needs_driver_license_id(lead, "NJ"))
        lead["driver_license_id"] = "A1234567"
        self.assertFalse(pl.ny_needs_driver_license_id(lead, "NY"))


if __name__ == "__main__":
    unittest.main()
