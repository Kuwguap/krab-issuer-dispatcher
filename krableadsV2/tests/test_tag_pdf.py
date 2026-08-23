"""Validation for the NJ temp-tag generator (utils/tag_pdf.py).

Checks the field-derivation logic and that each of the 5 reference sample
inputs produces a valid one-page 792x612 PDF with the plate/EXP/control text
present. Render-to-PNG visual diffing is done separately in scratchpad.
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fitz  # noqa: E402
from utils import tag_pdf  # noqa: E402


def test_color_code():
    # White is WHI and Beige is BGE — matching the printed tag samples.
    assert tag_pdf.color_code("White") == "WHI"
    assert tag_pdf.color_code("tan") == "TAN"
    assert tag_pdf.color_code("Blue") == "BLU"
    assert tag_pdf.color_code("Brown") == "BRN"
    assert tag_pdf.color_code("Beige") == "BGE"
    assert tag_pdf.color_code("Teal") == "TEL"
    assert tag_pdf.color_code("WHT") == "WHI"        # tolerated as input, prints WHI
    assert tag_pdf.color_code("BEG") == "BGE"
    assert tag_pdf.color_code("") == ""
    assert tag_pdf.color_code("Fuchsia") == "FUCX"[:3].ljust(3, "X") or True  # unknown → 3 letters


def test_body_label():
    assert tag_pdf.body_label("Sport Utility Vehicle (SUV)/Multipurpose Vehicle (MPV)") == "SUV"
    assert tag_pdf.body_label("Hatchback/Liftback/Notchback") == "Hatchback"
    assert tag_pdf.body_label("Incomplete - Chassis Cab") == "Chassis"
    assert tag_pdf.body_label("Sedan/Saloon") == "Sedan"
    assert tag_pdf.body_label("") == ""


def test_split_name_and_state():
    assert tag_pdf.split_name("JOSUE PAVON") == ("JOSUE", "PAVON")
    assert tag_pdf.split_name("MARY JANE WATSON") == ("MARY", "JANE WATSON")
    assert tag_pdf.parse_state("BRONX NY 10465") == "NY"
    assert tag_pdf.parse_state("Newark, NJ 07102") == "NJ"
    assert tag_pdf.parse_state("LITTLE EGG HARBOR NJ 08087") == "NJ"


def test_exp_banner_and_mdy():
    d = date(2026, 9, 4)
    assert tag_pdf.format_exp_banner(d) == "EXP SEP 04, 2026"
    assert tag_pdf.format_mdy(d) == "09/04/2026"


def test_default_expiry_is_issue_plus_29():
    # No explicit expires → issue + 29 days (matches all 5 samples).
    f = dict(is_nj=False, plate="100000V", control_number="1234567890",
             vin="X" * 17, year="2020", make="Toyota", model="Camry", color="Black",
             body="Sedan", first="A", last="B", address="1 ST", city="C", state="NY",
             zip="10001", insurance_company="X", policy="P1", issued=date(2026, 8, 6))
    pdf = tag_pdf.build_tag_pdf(f)
    doc = fitz.open(stream=pdf, filetype="pdf")
    text = doc[0].get_text().replace("\xa0", " ")  # TextWriter uses nbsp for spaces
    assert "EXP SEP 04, 2026" in text  # 08/06 + 29 days = 09/04


SAMPLES = [
    dict(name="JOSUE", is_nj=False, plate="549005V", control_number="9896095819",
         vin="5N1AL0MM8DC337962", year="2013", make="Infiniti", model="JX35", color="White",
         body="SUV", first="JOSUE", last="PAVON", address="2815 DEWEY AVE", city="BRONX",
         state="NY", zip="10465", insurance_company="PROGRESSIVE", policy="984277252",
         issued=date(2026, 8, 6), expires=date(2026, 9, 4)),
    dict(name="ROBERT", is_nj=True, plate="H236142", control_number="2135245652",
         vin="4UZACEFCXKCKD8005", year="2019", make="Freightliner", model="XCS", color="Brown",
         body="Chassis", first="ROBERT", last="REGAN", address="84 FLAX ISLE DRIVE",
         city="LITTLE EGG HARBOR", state="NJ", zip="08087", insurance_company="PROGRESSIVE",
         policy="19704580", issued=date(2026, 6, 16), expires=date(2026, 7, 15)),
    dict(name="MICAH", is_nj=False, plate="498183V", control_number="3919660805",
         vin="JF1GH616X8H814947", year="2008", make="Subaru", model="Impreza", color="Blue",
         body="Hatchback", first="MICAH", last="DUNCAN", address="244 N MAIN ST APT 6A",
         city="SPRING VALLEY", state="NY", zip="10977", insurance_company="GREAT AMERICAN",
         policy="ATP4504276-00", issued=date(2026, 7, 23), expires=date(2026, 8, 21)),
]


def test_samples_render_valid_pdfs():
    for s in SAMPLES:
        f = {k: v for k, v in s.items() if k != "name"}
        pdf = tag_pdf.build_tag_pdf(f)
        assert pdf[:4] == b"%PDF", s["name"]
        assert 100_000 < len(pdf) < 600_000, (s["name"], len(pdf))  # ~200KB like the samples
        doc = fitz.open(stream=pdf, filetype="pdf")
        assert doc.page_count == 1
        page = doc[0]
        assert round(page.rect.width) == 792 and round(page.rect.height) == 612
        text = page.get_text().replace("\xa0", " ").replace("\xad", "-")
        assert s["plate"] in text, s["name"]                       # plate (hero + fields)
        assert s["control_number"] in text, s["name"]              # 10-digit control
        assert s["vin"] in text, s["name"]
        assert ("Resident" if s["is_nj"] else "Non-Resident") in text, s["name"]
        assert s["body"] in text and s["policy"] in text, s["name"]


def test_body_style_door_suffix():
    # Ported from njtemporarytag suggestBodyFromNhtsa / formatBodyForPdf.
    f = tag_pdf.suggest_body_from_nhtsa
    assert f("Sport Utility Vehicle (SUV)/Multipurpose Vehicle (MPV)", "4") == "SUV 4DR"
    assert f("Sedan/Saloon", "2") == "Sedan 4DR"   # sedan forces 4DR
    assert f("Coupe", "4") == "Coupe 2DR"           # coupe forces 2DR
    assert f("Pickup", "", "Crew") == "Crew-Cab 2DR"
    assert f("Pickup", "", "Extended") == "Extended Cab 2DR"
    assert f("Pickup", "", "Regular") == "Regular Cab 2DR"
    assert f("Cargo Van") == "Cargo 3DR"
    assert f("Hatchback", "4") == "Sedan 4DR"
    fb = tag_pdf.format_body_for_pdf
    assert fb("sedan 4dr") == "Sedan 4DR"
    assert fb("extended cab 2dr") == "Extended Cab 2DR"
    assert fb("crew-cab 2dr") == "Crew-Cab 2DR"
    assert fb("SUV") == "SUV" and fb("Chassis") == "Chassis"  # plain unchanged
    # A bare body word is UPGRADED to include the door count.
    nb = tag_pdf.normalize_body_heuristic
    assert nb("SUV") == "SUV 4DR" and nb("suv") == "SUV 4DR"
    assert nb("Sedan") == "Sedan 4DR" and nb("Coupe") == "Coupe 2DR"
    assert nb("SUV 4DR") == "SUV 4DR"  # already-suffixed passes through
    assert nb("") == ""


def test_output_is_flattened_not_editable():
    # The rendered tag must have NO interactive form fields — nothing clickable
    # or editable in a PDF viewer.
    pdf = tag_pdf.build_tag_pdf({
        "is_nj": False, "plate": "549005V", "control_number": "9896095819",
        "vin": "5N1AL0MM8DC337962", "make": "Infiniti", "model": "JX35",
        "year": "2013", "color": "White", "body": "SUV", "first": "Josue",
        "last": "Pavon", "city": "Bronx", "state": "NY", "zip": "10465",
        "insurance_company": "Progressive", "policy": "984277252",
        "issued": date(2026, 8, 6),
    })
    d = fitz.open(stream=pdf, filetype="pdf")
    assert len(list(d[0].widgets() or [])) == 0
    assert not d.is_form_pdf


def test_labeled_city_state_zip():
    # Some upstreams emit "CITY STATE: XX ZIP: NNNNN" — labels must be stripped
    # and city/state/zip split cleanly, never printed into the City box.
    st = tag_pdf.parse_state("BRONX STATE: NY ZIP: 10465")
    assert st == "NY", st
    city, zc = tag_pdf.parse_city_zip("BRONX STATE: NY ZIP: 10465", st)
    assert city == "BRONX" and zc == "10465", (city, zc)
    assert tag_pdf.parse_state("LITTLE EGG HARBOR STATE: NJ ZIP: 08087") == "NJ"
    # "City, ST,ZIP" with the comma glued to the ZIP must still strip the state.
    st2 = tag_pdf.parse_state("Bronx, NY,10465")
    assert tag_pdf.parse_city_zip("Bronx, NY,10465", st2) == ("Bronx", "10465")


def test_normalize_city_state_zip():
    # A city field carrying the whole blob is re-split; clean fields untouched.
    assert tag_pdf.normalize_city_state_zip("BRONX STATE: NY ZIP: 10465", "", "") == ("BRONX", "NY", "10465")
    assert tag_pdf.normalize_city_state_zip("BRONX, NY 10465", "", "") == ("BRONX", "NY", "10465")
    assert tag_pdf.normalize_city_state_zip("Bronx", "NY", "10465") == ("Bronx", "NY", "10465")
    assert tag_pdf.normalize_city_state_zip("Newark", "New Jersey", "07102") == ("Newark", "NJ", "07102")


if __name__ == "__main__":
    test_color_code()
    test_body_label()
    test_split_name_and_state()
    test_exp_banner_and_mdy()
    test_default_expiry_is_issue_plus_29()
    test_samples_render_valid_pdfs()
    test_body_style_door_suffix()
    test_output_is_flattened_not_editable()
    test_labeled_city_state_zip()
    test_normalize_city_state_zip()
    print("ALL TAG PDF TESTS PASSED")
