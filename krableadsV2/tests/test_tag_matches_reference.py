r"""The generated tag lands on the same grid as the authoritative ones.

Two real tags produced by the authoritative system were measured span by span:
ED CASTILLO and CAIN CHERNICE, both non-resident (######V). Their text BASELINES
are identical to two decimal places across the pair, so the layout is a fixed
grid and only the values differ.

THE GRID IS PINNED HERE, NOT THE DOCUMENTS. The originals carry real customers'
names, addresses and VINs and do not belong in a repository; every number below
was read off both of them and they agree, which is the part worth keeping.

Two measurement traps this file exists to remember:

  * Compare BASELINES, never bounding boxes. A bbox top moves with the tallest
    glyph in the span, so "452897V" and "552947V" appear 23pt apart in an
    identical layout. Every early "difference" was really a font metric.
  * The widget rects in the templates are NOT the grid. Four fields whose widget
    tops span 0.64pt all print on one baseline, 472.50; the insurance company
    and policy number share baseline 587.50 while their widget mid-Ys sit 0.62pt
    apart, so no single vertical-centring rule can ever place both. The rects
    keep their real job — bounding the shrink-to-fit — and the values are
    anchored to the measured grid.

Run:  venv\Scripts\python.exe -m pytest tests/test_tag_matches_reference.py -q
"""
import datetime as dt
import io
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

# ED CASTILLO's own data, so the expected geometry below is directly comparable
# with the authoritative document it was measured from.
REFERENCE_CASE = dict(
    is_nj=False, plate="452897V", control_number="1404035439",
    vin="4S4BP61C786356675", year="2008", make="Subaru", model="Outback",
    color="TAN", body="SUV", first="ED", last="CASTILLO",
    address="98 ACADEMY ST", city="POUGHKEEPSIE", state="NY", zip="12601",
    insurance_company="AMERICAN ROAD INS CO", policy="ARI767703832",
    issued=dt.date(2026, 5, 24), expires=dt.date(2026, 6, 22),
)

# text -> (x, baseline y, size). Measured off BOTH authoritative tags.
EXPECTED = {
    "New Jersey":                     (186.55, 88.29, 88.00),
    "452897V":                        (30.00, 238.00, 180.00),
    "EXP JUN 22, 2026":               (117.00, 309.00, 60.00),
    "30 Day Non-Resident Temporary Plate": (119.88, 339.52, 24.00),
    "1404035439":                     (338.00, 376.00, 20.00),
    "TEMPORARY VEHICLE REGISTRATION": (34.93, 436.45, 16.00),
    "DEALER COPY":                    (484.91, 436.45, 16.00),
    "2008 Subaru Outback,TAN":        (24.80, 390.50, 11.00),
    "98 ACADEMY ST":                  (119.10, 522.00, 7.00),
    "POUGHKEEPSIE":                   (262.70, 523.00, 7.00),
    "12601":                          (408.00, 523.00, 7.00),
    "SUV":                            (260.00, 498.00, 7.00),
    "Outback":                        (185.10, 498.50, 7.00),
    "2008":                           (35.60, 498.70, 7.00),
    "AMERICAN ROAD INS CO":           (260.50, 587.50, 7.00),
    "ARI767703832":                   (353.00, 587.50, 7.00),
    "ED":                             (36.00, 524.00, 12.76),
    "CASTILLO":                       (36.00, 533.00, 12.76),
}

# 0.05pt is 1/8 of one dot at 600 dpi. The residual is 0.033pt at worst, and it
# lives in how PyMuPDF emits glyph positioning rather than in our coordinates.
TOL = 0.05


def spans(pdf_bytes):
    """[(text, x, baseline_y, size)] — baselines, for the reason in the docstring."""
    d = fitz.open("pdf", pdf_bytes)
    out = []
    for blk in d[0].get_text("rawdict")["blocks"]:
        for line in blk.get("lines", []):
            for s in line.get("spans", []):
                ch = s.get("chars", [])
                t = "".join(c["c"] for c in ch).strip()
                if t:
                    out.append((t, round(ch[0]["origin"][0], 3),
                                round(ch[0]["origin"][1], 3), round(s["size"], 2)))
    d.close()
    return out


class ItLandsOnTheReferenceGridTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.spans = spans(tag_pdf.build_tag_pdf(dict(REFERENCE_CASE)))
        cls.by_text = {}
        for t, x, y, sz in cls.spans:
            cls.by_text.setdefault(t, []).append((x, y, sz))

    def test_every_measured_field(self):
        for text, (wx, wy, wsz) in EXPECTED.items():
            with self.subTest(field=text):
                got = self.by_text.get(text)
                self.assertTrue(got, f"{text!r} is not on the tag at all")
                x, y, sz = min(got, key=lambda g: abs(g[1] - wy))
                self.assertAlmostEqual(x, wx, delta=TOL, msg=f"{text!r} x")
                self.assertAlmostEqual(y, wy, delta=TOL, msg=f"{text!r} baseline")
                self.assertAlmostEqual(sz, wsz, delta=0.01, msg=f"{text!r} size")

    def test_the_header_and_dealer_copy_share_a_baseline(self):
        """The shipped template artwork had the header 3pt above its own
        row-mate. They are printed side by side; they must sit on one line."""
        h = self.by_text["TEMPORARY VEHICLE REGISTRATION"][0]
        d = self.by_text["DEALER COPY"][0]
        self.assertAlmostEqual(h[1], d[1], delta=0.01)

    def test_the_insurance_row_shares_a_baseline(self):
        """Their widget mid-Ys are 0.62pt apart, so centring in the rect could
        never place both — this is what proves the grid is real."""
        ins = self.by_text["AMERICAN ROAD INS CO"][0]
        pol = self.by_text["ARI767703832"][0]
        self.assertAlmostEqual(ins[1], pol[1], delta=0.01)
        self.assertAlmostEqual(ins[1], 587.50, delta=TOL)


class ASpaceIsASpaceTest(unittest.TestCase):
    r"""MuPDF builds /ToUnicode by reverse-mapping the font's cmap, and Arial
    reaches the space glyph from both U+0020 and U+00A0 — the higher one won. So
    every space on every tag we have ever issued extracted, and copy-pasted, as a
    non-breaking space, and a VIN copied off a tag did not match a VIN search."""

    def test_no_non_breaking_spaces_anywhere(self):
        for is_nj in (False, True):
            case = dict(REFERENCE_CASE, is_nj=is_nj)
            with self.subTest(nj=is_nj):
                for t, *_ in spans(tag_pdf.build_tag_pdf(case)):
                    self.assertNotIn(" ", t, f"non-breaking space in {t!r}")

    def test_no_soft_hyphens_either(self):
        """Arial reaches the hyphen glyph from U+002D and U+00AD the same way."""
        case = dict(REFERENCE_CASE, last="RODRIGUEZ-SANTIAGO")
        for t, *_ in spans(tag_pdf.build_tag_pdf(case)):
            self.assertNotIn("­", t, f"soft hyphen in {t!r}")

    def test_the_address_extracts_verbatim(self):
        got = [t for t, *_ in spans(tag_pdf.build_tag_pdf(dict(REFERENCE_CASE)))]
        self.assertIn("98 ACADEMY ST", got)
        self.assertIn("AMERICAN ROAD INS CO", got)


class TheOwnerNameIsFittedPerLineTest(unittest.TestCase):
    """12.76pt on the reference tags, not the 12.0 the widget declares, and each
    line sized on its own — "ED" and "CASTILLO" are both 12.76."""

    def test_a_name_that_fits_keeps_the_reference_size(self):
        for n in ("ED", "CASTILLO", "CAIN", "SMITH", "MARIA"):
            with self.subTest(name=n):
                self.assertAlmostEqual(
                    tag_pdf.fit_owner_name(n, tag_pdf._load_font("regular")),
                    12.76, delta=0.001)

    def test_a_name_too_wide_shrinks_instead_of_spilling(self):
        font = tag_pdf._load_font("regular")
        for n in ("RODRIGUEZ-SANTIAGO", "VANDERBILT-WHITTINGTON",
                  "GLOBAL TRANSPORT LLC"):
            with self.subTest(name=n):
                size = tag_pdf.fit_owner_name(n, font)
                self.assertLess(size, 12.76)
                self.assertLessEqual(font.text_length(n, size), 80.0 + 0.01)

    def test_it_never_shrinks_below_legibility(self):
        font = tag_pdf._load_font("regular")
        self.assertGreaterEqual(tag_pdf.fit_owner_name("W" * 60, font), 5.5)

    def test_each_line_is_sized_independently(self):
        """A short first name beside a long surname keeps its own size."""
        case = dict(REFERENCE_CASE, first="ED", last="RODRIGUEZ-SANTIAGO")
        by = {t: (x, y, s) for t, x, y, s in spans(tag_pdf.build_tag_pdf(case))}
        self.assertAlmostEqual(by["ED"][2], 12.76, delta=0.01)
        self.assertLess(by["RODRIGUEZ-SANTIAGO"][2], 12.76)


class TheResidentTemplateUsesTheSameGridTest(unittest.TestCase):
    """There is no authoritative RESIDENT tag to measure, so this asserts only
    what can be checked: the two templates carry identical widget geometry, so
    the grid measured on one is the grid of the other."""

    def test_the_two_templates_agree_on_every_anchored_field(self):
        keys = {}
        for name in ("NONNJ.pdf", "NJ.pdf"):
            d = fitz.open(str(ROOT / "assets" / name))
            keys[name] = {(w.field_name, round(w.rect.x0, 1)) for w in d[0].widgets()}
            d.close()
        anchors = set(tag_pdf._FIELD_ANCHOR)
        self.assertEqual(anchors - keys["NONNJ.pdf"], set())
        self.assertEqual(anchors - keys["NJ.pdf"], set(),
                         "an anchor that misses NJ silently falls back to the rect")

    def test_a_resident_tag_lands_on_the_same_rows(self):
        case = dict(REFERENCE_CASE, is_nj=True, plate="H209861", state="NJ",
                    city="NEWARK", zip="07102")
        got = spans(tag_pdf.build_tag_pdf(case))

        def row(text, near):
            """The occurrence nearest a given baseline — the plate and the VIN
            each appear more than once on a tag."""
            hits = [(x, y, s) for t, x, y, s in got if t == text]
            self.assertTrue(hits, f"{text!r} is not on the tag")
            return min(hits, key=lambda h: abs(h[1] - near))

        self.assertAlmostEqual(row("H209861", 238.00)[1], 238.00, delta=TOL)
        self.assertAlmostEqual(row("H209861", 472.60)[1], 472.60, delta=TOL)
        self.assertAlmostEqual(row("1404035439", 376.00)[1], 376.00, delta=TOL)
        self.assertAlmostEqual(row("NEWARK", 523.00)[1], 523.00, delta=TOL)
        self.assertAlmostEqual(row("ED", 524.00)[1], 524.00, delta=TOL)


class TheTemplateArtworkIsCorrectedTest(unittest.TestCase):
    """Two text matrices in the shipped templates are a revision behind the
    authoritative tags. Patched in memory per render, so the .pdf assets stay
    byte-identical to what the operator's other tools read."""

    def test_the_assets_are_not_rewritten(self):
        before = (ROOT / "assets" / "NONNJ.pdf").read_bytes()
        tag_pdf.build_tag_pdf(dict(REFERENCE_CASE))
        self.assertEqual((ROOT / "assets" / "NONNJ.pdf").read_bytes(), before)

    def test_the_patch_is_a_no_op_on_an_unrecognised_template(self):
        """A future re-export must degrade quietly, not corrupt."""
        doc = fitz.open()
        doc.new_page(width=792, height=612)
        tag_pdf._patch_template_artwork(doc)     # must not raise
        doc.close()


if __name__ == "__main__":
    unittest.main()
