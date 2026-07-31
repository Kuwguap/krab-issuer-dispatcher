"""Unit tests for the read-path merge: persisted send-time fields win, the
Issuer lead join only fills blanks, receipts stay join-only."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.api import _merge_lead_fields  # noqa: E402

META = {
    "lead_client_name": "Lead Name",
    "price": "999",
    "lead_client_phone": "+15550001111",
    "lead_client_email": "lead@x.com",
    "receipt_image_url": "https://r/img.jpg",
    "receipt_price": "180",
}


class TestMerge(unittest.TestCase):
    def test_persisted_wins_over_join(self) -> None:
        row = {
            "client_name": "Local Name",
            "price": "150",
            "client_phone": "+19998887777",
            "client_email": "local@x.com",
        }
        _merge_lead_fields(row, META)
        self.assertEqual(row["lead_client_name"], "Local Name")
        self.assertEqual(row["price"], "150")
        self.assertEqual(row["lead_client_phone"], "+19998887777")
        self.assertEqual(row["lead_client_email"], "local@x.com")

    def test_join_fills_gaps_only(self) -> None:
        row = {"client_name": "Local Name", "price": None, "client_phone": "", "client_email": "  "}
        _merge_lead_fields(row, META)
        self.assertEqual(row["lead_client_name"], "Local Name")
        self.assertEqual(row["price"], "999")
        self.assertEqual(row["lead_client_phone"], "+15550001111")
        self.assertEqual(row["lead_client_email"], "lead@x.com")

    def test_receipts_always_join_only(self) -> None:
        row = {"client_name": "Local"}
        _merge_lead_fields(row, META)
        self.assertEqual(row["receipt_image_url"], "https://r/img.jpg")
        self.assertEqual(row["receipt_price"], "180")

    def test_no_supabase_surfaces_persisted(self) -> None:
        row = {
            "client_name": "Local Name",
            "price": "150",
            "client_phone": "+19998887777",
            "client_email": "local@x.com",
        }
        _merge_lead_fields(row, None)
        self.assertEqual(row["lead_client_name"], "Local Name")
        self.assertEqual(row["price"], "150")
        self.assertEqual(row["lead_client_phone"], "+19998887777")
        self.assertEqual(row["lead_client_email"], "local@x.com")
        self.assertIsNone(row["receipt_image_url"])
        self.assertIsNone(row["receipt_price"])

    def test_legacy_all_null_row_behaves_like_before(self) -> None:
        row = {"client_name": None, "price": None, "client_phone": None, "client_email": None}
        _merge_lead_fields(row, META)
        self.assertEqual(row["lead_client_name"], "Lead Name")
        self.assertEqual(row["price"], "999")
        row2: dict = {}
        _merge_lead_fields(row2, None)
        self.assertIsNone(row2["lead_client_name"])
        self.assertIsNone(row2["price"])


if __name__ == "__main__":
    unittest.main()
