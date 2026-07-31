"""Unit tests for send-time local client-field capture (independent registration)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.client_capture import (  # noqa: E402
    capture_client_fields,
    extract_client_email,
    extract_client_name,
    extract_client_phone,
    extract_price,
    normalize_us_phone,
)


class TestClientName(unittest.TestCase):
    def test_plain_filename(self) -> None:
        self.assertEqual(extract_client_name("LESIE M PINTOR.pdf"), "Lesie M Pintor")

    def test_reference_token_stripped(self) -> None:
        self.assertEqual(extract_client_name("JOHN DOE AB12CD34.pdf"), "John Doe")

    def test_underscores_and_counter(self) -> None:
        self.assertEqual(extract_client_name("john_doe (2).pdf"), "John Doe")

    def test_eight_letter_surname_preserved(self) -> None:
        self.assertEqual(extract_client_name("CASTILLO M RIVERA.pdf"), "Castillo M Rivera")

    def test_tag_ref_filename_falls_back_to_notes(self) -> None:
        self.assertEqual(
            extract_client_name("TAG_ABC12345.pdf", "Jane Roe\n555-123-4567"),
            "Jane Roe",
        )

    def test_garbage_filename_no_notes(self) -> None:
        self.assertIsNone(extract_client_name("scan_20260731.pdf"))

    def test_notes_first_line_with_digits_rejected(self) -> None:
        self.assertIsNone(extract_client_name("scan.pdf", "12119 195th St"))


class TestPhone(unittest.TestCase):
    def test_formats_normalize(self) -> None:
        for raw in ("(973) 555-1234", "973.555.1234", "+1 973 555 1234"):
            self.assertEqual(extract_client_phone(f"call {raw} now"), "+19735551234", raw)

    def test_contiguous_non_serial_area_code(self) -> None:
        self.assertEqual(extract_client_phone("5515551234"), "+15515551234")

    def test_vin_not_matched(self) -> None:
        self.assertIsNone(extract_client_phone("VIN 1HGCM82633A123456"))

    def test_nj_serial_contiguous_skipped(self) -> None:
        # Contiguous 10-digit runs starting with 9 are NJ tag serials, not
        # phones — documented trade-off (punctuated 9xx phones still match).
        self.assertIsNone(extract_client_phone("9123456789"))
        self.assertIsNone(extract_client_phone("9735551234"))

    def test_normalize_helper(self) -> None:
        self.assertEqual(normalize_us_phone("1 (973) 555-1234"), "+19735551234")
        self.assertIsNone(normalize_us_phone("12345"))


class TestEmailAndPrice(unittest.TestCase):
    def test_email_first_match(self) -> None:
        self.assertEqual(extract_client_email("a@b.com then c@d.com"), "a@b.com")
        self.assertIsNone(extract_client_email("no email here"))

    def test_price_first_dollar_wins(self) -> None:
        self.assertEqual(extract_price("$180 cash, $50 deposit"), "180")

    def test_price_with_comma_and_cents(self) -> None:
        self.assertEqual(extract_price("total $1,250.50"), "1250.50")

    def test_price_labeled_fallback(self) -> None:
        self.assertEqual(extract_price("price: 200"), "200")

    def test_no_price(self) -> None:
        self.assertIsNone(extract_price("no numbers with dollars"))


class TestCapture(unittest.TestCase):
    def test_full_capture(self) -> None:
        got = capture_client_fields(
            "LESIE M PINTOR.pdf",
            "Lesie M Pintor\n28 brookside rd\n(973) 555-1234\nlesie@x.com\n$150.00",
            "Some Driver",
            "driver@fleet.com",
        )
        self.assertEqual(got["client_name"], "Lesie M Pintor")
        self.assertEqual(got["price"], "150.00")
        self.assertEqual(got["client_phone"], "+19735551234")
        self.assertEqual(got["client_email"], "lesie@x.com")

    def test_driver_email_never_used_as_client_email(self) -> None:
        got = capture_client_fields("JOHN DOE.pdf", "no email in notes", "Some Driver", "driver@fleet.com")
        self.assertIsNone(got["client_email"])

    def test_email_client_path_uses_recipient(self) -> None:
        got = capture_client_fields(
            "JOHN DOE.pdf", "notes", "Client 📧 buyer@x.com", "buyer@x.com"
        )
        self.assertEqual(got["client_email"], "buyer@x.com")

    def test_missing_everything(self) -> None:
        got = capture_client_fields("scan_1.pdf", "", None, None)
        self.assertEqual(
            got,
            {"client_name": None, "price": None, "client_phone": None, "client_email": None},
        )


if __name__ == "__main__":
    unittest.main()
