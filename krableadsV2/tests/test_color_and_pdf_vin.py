"""Colour picker on the review card, and reading a VIN out of a PDF.

Two fixes:
  * the Color field is now tap/type/speak/photo, with a palette of inline buttons;
  * a PDF's VIN comes from its TEXT LAYER (exact) instead of being read off a
    150-DPI page render, which regularly missed or mangled 17 small characters.

Run:  venv\\Scripts\\python.exe -m pytest tests/test_color_and_pdf_vin.py -q
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy-key")

import utils.database as udb  # noqa: E402

if "bot" not in sys.modules:
    udb.Database = mock.MagicMock()
import bot  # noqa: E402
from utils import ai_vision  # noqa: E402


class ColorPaletteTest(unittest.TestCase):
    def test_prompt_text_is_the_requested_wording(self):
        self.assertEqual(
            bot._PH1_COLOR_PROMPT,
            "✏️ type, speak, click, or upload a picture to read the color",
        )

    def test_palette_offers_a_long_list_including_the_named_ones(self):
        labels = [b.text for row in bot._color_picker_keyboard().inline_keyboard for b in row]
        joined = " | ".join(labels)
        for wanted in ("Red", "Blue - Dark", "Blue - Light", "White", "Gray"):
            self.assertIn(wanted, joined)
        self.assertGreaterEqual(len(labels), 20, "palette should be a long list")

    def test_every_button_carries_a_usable_callback(self):
        for row in bot._color_picker_keyboard().inline_keyboard:
            for b in row:
                if b.callback_data == "edit_cancel":
                    continue
                self.assertTrue(b.callback_data.startswith(bot.PH1_COLOR_CB))
                # Telegram rejects callback_data over 64 bytes
                self.assertLessEqual(len(b.callback_data.encode()), 64)
                colour = b.callback_data[len(bot.PH1_COLOR_CB):]
                self.assertIn(colour, bot._PH1_COLORS)

    def test_cancel_button_present(self):
        datas = [b.callback_data for row in bot._color_picker_keyboard().inline_keyboard for b in row]
        self.assertIn("edit_cancel", datas)


# The operator's official name -> 3-letter DMV code list. Buttons never show the
# code; the tag PDF needs it.
OFFICIAL_CODES = {
    "Black": "BLK", "White": "WHI", "Gray": "GRY", "Silver": "SLV", "Red": "RED",
    "Blue": "BLU", "Green": "GRN", "Brown": "BRN", "Beige": "BGE", "Gold": "GLD",
    "Orange": "ORG", "Yellow": "YLW", "Purple": "PUR", "Tan": "TAN", "Cream": "CRM",
    "Maroon": "MRN", "Navy": "NVY", "Bronze": "BRZ", "Copper": "CPR", "Teal": "TEL",
}


class ColorCodeForPdfTest(unittest.TestCase):
    """Names on the buttons, codes on the tag."""

    def test_official_codes_are_exact(self):
        from utils import tag_pdf
        wrong = {n: tag_pdf.color_code(n) for n, want in OFFICIAL_CODES.items()
                 if tag_pdf.color_code(n) != want}
        self.assertEqual({}, wrong)

    def test_every_button_resolves_to_a_three_letter_code(self):
        from utils import tag_pdf
        bad = [c for c in bot._PH1_COLORS if len(tag_pdf.color_code(c) or "") != 3]
        self.assertEqual([], bad, "every picker colour must print on the tag")

    def test_newly_requested_colours_are_on_the_palette(self):
        for c in ("Brown", "Bronze", "Beige", "Amethyst", "Gold",
                  "Cream", "Copper", "Gray", "Green"):
            self.assertIn(c, bot._PH1_COLORS, c)

    def test_shade_variants_share_the_base_code(self):
        from utils import tag_pdf
        self.assertEqual(tag_pdf.color_code("Blue - Dark"), "BLU")
        self.assertEqual(tag_pdf.color_code("Blue - Light"), "BLU")
        self.assertEqual(tag_pdf.color_code("Green - Dark"), "GRN")

    def test_buttons_never_show_the_code(self):
        labels = [b.text for row in bot._color_picker_keyboard().inline_keyboard for b in row]
        for label, code in [(l, c) for l in labels for c in OFFICIAL_CODES.values()]:
            self.assertNotIn(f" {code}", label, f"{label} leaks the DMV code")

    def test_the_other_spelling_is_tolerated_as_input(self):
        """Anything already stored as WHT/BEG still prints the right code."""
        from utils import tag_pdf
        self.assertEqual(tag_pdf.color_code("WHT"), "WHI")
        self.assertEqual(tag_pdf.color_code("BEG"), "BGE")


class SpokenColorTest(unittest.TestCase):
    """A dictated colour is a sentence; the field must get just the colour."""

    def test_strips_spoken_lead_in(self):
        cases = {
            "the color is dark blue": "Dark Blue",
            "it's white": "White",
            "color red": "Red",
            "the car's colour is light blue": "Light Blue",
            "silver": "Silver",
        }
        for said, want in cases.items():
            self.assertEqual(bot._clean_spoken_color(said), want, said)

    def test_empty_input_returns_empty(self):
        self.assertEqual(bot._clean_spoken_color(""), "")


class EditPromptCleaningTest(unittest.TestCase):
    """The Edit-button path must normalize like the inline path does."""

    def test_price_typed_at_the_prompt_gets_its_dollar_sign(self):
        # "150" stored raw was rejected later by _is_valid_pending_price ("$" required)
        self.assertEqual(bot._clean_inline_value("price", "150"), "$150")
        self.assertTrue(bot._is_valid_pending_price("$150"))
        self.assertFalse(bot._is_valid_pending_price("150"))


class PdfVinTest(unittest.TestCase):
    """VIN straight from the PDF text layer."""

    def test_vin_check_digit(self):
        self.assertTrue(ai_vision.vin_check_digit_ok("3N1AB8CV2MY298179"))
        self.assertFalse(ai_vision.vin_check_digit_ok("3N1AB8CV9MY298179"))
        self.assertFalse(ai_vision.vin_check_digit_ok("TOOSHORT"))

    def test_vin_never_contains_i_o_or_q(self):
        # a 17-char token with I/O/Q is not a VIN and must not match
        self.assertIsNone(ai_vision.vin_from_text("QQQQQQQQQQQQQQQQQ"))
        self.assertIsNone(ai_vision.vin_from_text("IIIIIIIIIIIIIIIII"))

    def test_label_wins_over_other_17_char_tokens(self):
        text = ("ABCDEFGH1234567JK  some other token\n"
                "VIN: 3N1AB8CV2MY298179\n")
        self.assertEqual(ai_vision.vin_from_text(text), "3N1AB8CV2MY298179")

    def test_checksum_valid_candidate_preferred(self):
        text = "ZZZZZZZZZZZZZZZZZ and 3N1AB8CV2MY298179 somewhere"
        self.assertEqual(ai_vision.vin_from_text(text), "3N1AB8CV2MY298179")

    def test_no_text_returns_none(self):
        self.assertIsNone(ai_vision.vin_from_text(""))
        self.assertIsNone(ai_vision.vin_from_pdf(b""))

    def test_real_pdfs_in_the_repo(self):
        """The actual documents that exposed the bug (skipped if absent)."""
        expected = {
            "Policy #_ 2035252790.pdf": "3N1AB8CV2MY298179",
            "State of New Jersey Temporary Evidence of Insurance (2).pdf": "WBAJA7C59JG909541",
        }
        repo_root = ROOT.parent
        checked = 0
        for fname, want_vin in expected.items():
            path = repo_root / fname
            if not path.exists():
                continue
            checked += 1
            got = ai_vision.vin_from_pdf(path.read_bytes())
            self.assertEqual(got, want_vin, fname)
            self.assertTrue(ai_vision.vin_check_digit_ok(got), fname)
        if not checked:
            self.skipTest("sample PDFs not present")

    def test_render_dpi_raised_for_small_text(self):
        self.assertGreaterEqual(ai_vision.PDF_RENDER_DPI, 200)


class ColorFromImageTest(unittest.TestCase):
    def test_unsure_answer_is_not_written(self):
        with mock.patch.object(ai_vision, "_parse_json_from_model",
                               mock.MagicMock(return_value={"color": ""})):
            with mock.patch("openai.OpenAI", mock.MagicMock()):
                self.assertIsNone(ai_vision.read_color_from_image(b"", "image/jpeg"))

    def test_no_bytes_returns_none(self):
        self.assertIsNone(ai_vision.read_color_from_image(b"", "image/jpeg"))


if __name__ == "__main__":
    unittest.main()
