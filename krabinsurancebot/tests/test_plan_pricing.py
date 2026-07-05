"""Unit tests for plan term pricing."""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot import PLAN_MONTH_VALUES, format_plan_price, plan_term_premium  # noqa: E402
from utils import insurance_card as ic  # noqa: E402


class TestPlanPricing(unittest.TestCase):
    def test_plan_month_values(self) -> None:
        self.assertEqual(PLAN_MONTH_VALUES, (1, 6, 12))

    def test_plan_term_premium_map(self) -> None:
        self.assertEqual(plan_term_premium(1), 100.0)
        self.assertEqual(plan_term_premium(6), 500.0)
        self.assertEqual(plan_term_premium(12), 900.0)
        self.assertEqual(plan_term_premium(99), 100.0)

    def test_format_plan_price(self) -> None:
        self.assertEqual(format_plan_price(6), "$500")
        self.assertEqual(format_plan_price(1, state="NJ"), "$100")

    def test_expiration_matches_plan_months(self) -> None:
        start = date(2026, 7, 5)
        for months in (1, 6, 12):
            end = ic.expiration_for_plan(start, months=months)
            self.assertGreater(end, start)


if __name__ == "__main__":
    unittest.main()
