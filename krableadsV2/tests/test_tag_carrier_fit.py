r"""A long carrier name must not print over the policy number.

Reported: "if the insurance company name is long it forces it into a small space,
for instance 'Privilege Underwriters Reciprocal Exchange' … make sure it doesn't
touch the adjacent field which is the policy number".

Measured on the real template: the `ins` box has 84.15pt of room and `policy`
starts 8.51pt to its right on the SAME baseline. _draw_field shrank proportionally
with a 4.0pt floor, so the instant the size clamped the width stopped fitting —
there is no safe band — and it then drew with no clip. The 42-character example ran
20pt past its own box and printed over the first three digits of the policy number.

These tests RENDER the PDF and measure the spans, because that is the only way to
know. Run:  venv\Scripts\python.exe -m pytest tests/test_tag_carrier_fit.py -q
"""
import datetime
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy-key")

import fitz  # noqa: E402
from utils import tag_pdf  # noqa: E402

POLICY = "PAIP1234567890"

# Real carriers whose names outgrow the box. Every one of these used to overflow.
LONG_NAMES = [
    "Privilege Underwriters Reciprocal Exchange",
    "New Jersey Manufacturers Insurance Company",
    "Progressive Garden State Insurance Company",
    "Palisades Safety and Insurance Association",
    "Government Employees Insurance Company",
    "Plymouth Rock Assurance Corporation",
    "State Farm Mutual Automobile Insurance Company",
    "Liberty Mutual Fire Insurance Company",
    "The Hanover American Insurance Company",
    "American Reliable Insurance Company",
    "Farmers Insurance Exchange",
    "Erie Insurance Exchange",
    "Selective Way Insurance Company",
]


def _render(carrier, policy=POLICY, is_nj=True):
    return tag_pdf.build_tag_pdf({
        "is_nj": is_nj, "plate": "H209861", "control_number": "3406302948",
        "vin": "WDDZF4JB4HA036041", "year": "2017", "make": "MERZ", "model": "E300",
        "color": "WHI", "body": "4DSD", "first": "JOHN", "last": "DAMIAN",
        "address": "325 PROSPECT ST", "city": "PERTH AMBOY", "state": "NJ",
        "zip": "08861", "insurance_company": carrier, "policy": policy,
        "issued": datetime.date(2026, 8, 25), "expires": datetime.date(2026, 9, 24),
    })


def _spans(pdf_bytes):
    doc = fitz.open("pdf", pdf_bytes)
    try:
        out = []
        for blk in doc[0].get_text("dict")["blocks"]:
            for line in blk.get("lines", []):
                for sp in line["spans"]:
                    out.append((sp["text"], sp["bbox"], sp["size"]))
        return out
    finally:
        doc.close()


def _ins_and_policy(pdf_bytes):
    """The carrier span and the policy span, found by where they sit on the page."""
    spans = _spans(pdf_bytes)
    policy = next((s for s in spans if POLICY in s[0]), None)
    carrier = next((s for s in spans
                    if 255 < s[1][0] < 300 and 578 < s[1][1] < 592 and POLICY not in s[0]),
                   None)
    return carrier, policy


class TheCarrierNeverTouchesThePolicyTest(unittest.TestCase):

    def test_the_reported_name(self):
        carrier, policy = _ins_and_policy(_render("Privilege Underwriters Reciprocal Exchange"))
        self.assertIsNotNone(carrier, "the carrier was not drawn at all")
        self.assertIsNotNone(policy)
        self.assertLessEqual(
            carrier[1][2], policy[1][0],
            f"{carrier[0]!r} runs {carrier[1][2] - policy[1][0]:.1f}pt into the policy number")

    def test_every_long_carrier(self):
        for name in LONG_NAMES:
            with self.subTest(carrier=name):
                carrier, policy = _ins_and_policy(_render(name))
                self.assertIsNotNone(carrier, name)
                self.assertLessEqual(carrier[1][2], policy[1][0], f"{name} -> {carrier[0]!r}")

    def test_it_stays_inside_its_own_box_too(self):
        """The box ends at x=345.16 — overflowing it is what reached the neighbour."""
        for name in LONG_NAMES:
            with self.subTest(carrier=name):
                carrier, _ = _ins_and_policy(_render(name))
                self.assertLessEqual(carrier[1][2], 345.16 + 0.5, f"{name} -> {carrier[0]!r}")

    def test_the_non_resident_template_too(self):
        carrier, policy = _ins_and_policy(
            _render("Privilege Underwriters Reciprocal Exchange", is_nj=False))
        self.assertLessEqual(carrier[1][2], policy[1][0])

    def test_a_long_policy_number_is_not_pushed_around(self):
        carrier, policy = _ins_and_policy(
            _render("New Jersey Manufacturers Insurance Company", policy=POLICY))
        self.assertIn(POLICY, policy[0], "the policy number must print in full")


class ItStaysReadableTest(unittest.TestCase):
    """Fitting is easy; fitting and still being recognisable is the point."""

    def test_it_never_shrinks_below_legibility(self):
        for name in LONG_NAMES:
            with self.subTest(carrier=name):
                carrier, _ = _ins_and_policy(_render(name))
                self.assertGreaterEqual(carrier[2], tag_pdf._MIN_FIELD_PT, f"{name}")

    def test_the_first_word_always_survives(self):
        """Whatever else goes, the name has to start with the right word."""
        for name in LONG_NAMES:
            with self.subTest(carrier=name):
                carrier, _ = _ins_and_policy(_render(name))
                first = name.split()[0].upper()
                self.assertTrue(
                    carrier[0].startswith(first[:4]),
                    f"{name} became {carrier[0]!r}")

    def test_a_short_name_is_left_completely_alone(self):
        for name in ("GEICO", "NJM", "Allstate", "State Farm"):
            with self.subTest(carrier=name):
                carrier, _ = _ins_and_policy(_render(name))
                # The PDF text layer hands back a non-breaking space between words.
                drawn = " ".join(carrier[0].replace(" ", " ").split())
                self.assertEqual(name.upper(), drawn)
                self.assertAlmostEqual(7.0, carrier[2], places=1)


class TheShortenerItselfTest(unittest.TestCase):

    def setUp(self):
        self.font = fitz.Font("helv")

    def _short(self, name, avail=84.15, size=7.0):
        return tag_pdf.shorten_carrier(name, avail, size, self.font)

    def test_it_uses_the_house_abbreviations(self):
        """The insurance card already prints INS.CO. — the tag should match."""
        got = self._short("Liberty Mutual Fire Insurance Company")
        self.assertIn("INS", got)
        self.assertNotIn("INSURANCE", got)

    def test_it_initialises_a_multi_word_tail(self):
        got = self._short("Privilege Underwriters Reciprocal Exchange", avail=60.0)
        self.assertTrue(got.startswith("PRIVILEGE"), got)

    def test_it_never_leaves_a_lone_letter_for_one_word(self):
        """"RECIP E" for EXCHANGE says nothing — drop the word instead."""
        for name in LONG_NAMES:
            with self.subTest(carrier=name):
                got = self._short(name, avail=70.0)
                self.assertFalse(
                    any(len(w) == 1 and w.isalpha() for w in got.split()[1:2]),
                    got)

    def test_an_impossible_box_still_produces_something(self):
        got = self._short("Privilege Underwriters Reciprocal Exchange", avail=14.0)
        self.assertTrue(got)
        self.assertLessEqual(self.font.text_length(got, 7.0), 14.0 + 0.01)

    def test_a_box_too_small_for_anything_gives_up_cleanly(self):
        self.assertEqual("", self._short("Privilege Underwriters", avail=0.5))

    def test_empty_in_empty_out(self):
        for v in ("", "   ", None):
            with self.subTest(v=v):
                self.assertEqual("", self._short(v))

    def test_it_is_deterministic(self):
        """No network, no model — the tag prints whether or not the AI is reachable."""
        a = self._short("Privilege Underwriters Reciprocal Exchange")
        b = self._short("Privilege Underwriters Reciprocal Exchange")
        self.assertEqual(a, b)


class NoFieldMayLeaveItsBoxTest(unittest.TestCase):
    """The clamp is general — it protects every field, not just the carrier."""

    def test_the_trimmer_always_fits(self):
        font = fitz.Font("helv")
        for text in ("A" * 200, "WORD " * 40, "X"):
            for avail in (10.0, 40.0, 84.15):
                with self.subTest(len=len(text), avail=avail):
                    got = tag_pdf._fit_by_trimming(text, avail, 7.0, font)
                    self.assertLessEqual(font.text_length(got, 7.0), avail + 0.01)

    def test_a_very_long_owner_name_does_not_escape(self):
        pdf = tag_pdf.build_tag_pdf({
            "is_nj": True, "plate": "H209861", "control_number": "3406302948",
            "vin": "WDDZF4JB4HA036041", "year": "2017", "make": "MERZ", "model": "E300",
            "color": "WHI", "body": "4DSD",
            "first": "BARTHOLOMEW MAXIMILIAN", "last": "VON HAPSBURG-LOTHRINGEN",
            "address": "1234 EXTRAORDINARILY LONG BOULEVARD APARTMENT 5678",
            "city": "PERTH AMBOY", "state": "NJ", "zip": "08861",
            "insurance_company": "GEICO", "policy": POLICY,
            "issued": datetime.date(2026, 8, 25), "expires": datetime.date(2026, 9, 24),
        })
        # Nothing on the row may reach the right edge of the form area.
        for text, bbox, _ in _spans(pdf):
            if 578 < bbox[1] < 592:
                self.assertLess(bbox[2], 445.0, f"{text!r} escaped to x={bbox[2]:.1f}")


if __name__ == "__main__":
    unittest.main()
