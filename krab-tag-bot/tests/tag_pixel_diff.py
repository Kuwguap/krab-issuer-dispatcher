"""Pixel/text parity harness: render build_tag_pdf() for known reference orders
and diff against the real reference tags in Telegram Desktop.

Run:  venv/Scripts/python.exe tests/tag_pixel_diff.py
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fitz  # PyMuPDF
from utils import tag_pdf

REF_DIR = r"C:/Users/tatia/Downloads/Telegram Desktop"

SAMPLES = {
    "JOSUE PAVON": {
        "is_nj": False,
        "plate": "549005V", "control_number": "9896095819",
        "vin": "5N1AL0MM8DC337962", "year": "2013", "make": "Infiniti",
        "model": "JX35", "color": "White", "body": "SUV",
        "first": "Josue", "last": "Pavon",
        "address": "2815 Dewey Ave", "city": "Bronx", "state": "NY", "zip": "10465",
        "insurance_company": "Progressive", "policy": "984277252",
        "issued": date(2026, 8, 6), "expires": date(2026, 9, 4),
    },
    "ROBERT REGAN": {
        "is_nj": True,
        "plate": "H236142", "control_number": "2135245652",
        "vin": "4UZACEFCXKCKD8005", "year": "2019", "make": "Freightliner",
        "model": "XCS", "color": "Brown", "body": "Chassis",
        "first": "Robert", "last": "Regan",
        "address": "84 Flax Isle Drive", "city": "Little Egg Harbor", "state": "NJ", "zip": "08087",
        "insurance_company": "Progressive", "policy": "19704580",
        "issued": date(2026, 6, 16), "expires": date(2026, 7, 15),
    },
}

DPI = 150


def raster(doc_or_bytes):
    doc = doc_or_bytes if isinstance(doc_or_bytes, fitz.Document) else fitz.open(stream=doc_or_bytes, filetype="pdf")
    pg = doc[0]
    pm = pg.get_pixmap(matrix=fitz.Matrix(DPI / 72, DPI / 72), colorspace=fitz.csGRAY)
    return pm.samples, pm.width, pm.height


def mean_abs_diff(a, b):
    n = min(len(a), len(b))
    if n == 0:
        return 255.0
    total = 0
    for i in range(0, n, 7):  # sample every 7th byte — fast, representative
        total += abs(a[i] - b[i])
    return total / (n / 7)


def hero_spans(pdf_bytes):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf") if isinstance(pdf_bytes, (bytes, bytearray)) else pdf_bytes
    out = {}
    for b in doc[0].get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            for s in l["spans"]:
                if s["size"] >= 15 and s["text"].strip():
                    # PyMuPDF reports TextWriter spaces as U+00A0 on read-back;
                    # normalize so the comparison reflects drawn content.
                    txt = s["text"].strip().replace("\xa0", " ")
                    out[round(s["size"])] = (txt, round(s["origin"][0], 1), round(s["origin"][1], 1), s["font"])
    return out


def main():
    fail = 0
    for name, fields in SAMPLES.items():
        ref_path = os.path.join(REF_DIR, name + ".pdf")
        if not os.path.exists(ref_path):
            print(f"SKIP {name}: reference not found")
            continue
        mine = tag_pdf.build_tag_pdf(fields)
        ra, rw, rh = raster(fitz.open(ref_path))
        ma, mw, mh = raster(mine)
        print(f"==== {name}")
        print(f"  raster ref={rw}x{rh} mine={mw}x{mh}")
        if (rw, rh) != (mw, mh):
            print("  SIZE MISMATCH"); fail += 1; continue
        d = mean_abs_diff(ra, ma)
        print(f"  mean abs pixel diff (0-255): {d:.3f}")
        # hero text parity (plate 180, exp 60, control 20)
        rh_s, mh_s = hero_spans(fitz.open(ref_path)), hero_spans(mine)
        for size in (180, 60, 20):
            r = rh_s.get(size); m = mh_s.get(size)
            ok = r and m and r[0] == m[0] and abs(r[1] - m[1]) < 3 and abs(r[2] - m[2]) < 3
            print(f"    hero@{size}: ref={r} mine={m} {'OK' if ok else 'DRIFT'}")
            if not ok:
                fail += 1
        if d > 8.0:
            print(f"  PIXEL DIFF TOO HIGH ({d:.3f} > 8)"); fail += 1
    print("\n" + ("ALL PARITY CHECKS PASSED" if fail == 0 else f"{fail} PARITY ISSUE(S)"))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
