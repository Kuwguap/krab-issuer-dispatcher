"""Tests for insured name formatting on insurance cards."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils import insurance_card as ic  # noqa: E402


class TestInsuranceCardNames(unittest.TestCase):
    def test_format_insured_fs20_name_keeps_full_name(self) -> None:
        full = "JOSEPH C MCRAE JR"
        self.assertEqual(ic.format_insured_fs20_name(full), full)

    def test_format_insured_fs20_name_strips_commas_not_initials(self) -> None:
        self.assertEqual(
            ic.format_insured_fs20_name("MCRAE, JOSEPH C JR"),
            "MCRAE JOSEPH C JR",
        )

    def test_format_insured_fs20_name_normalizes_whitespace(self) -> None:
        self.assertEqual(
            ic.format_insured_fs20_name("  MARIA   ELENA   GARCIA  "),
            "MARIA ELENA GARCIA",
        )


if __name__ == "__main__":
    unittest.main()
