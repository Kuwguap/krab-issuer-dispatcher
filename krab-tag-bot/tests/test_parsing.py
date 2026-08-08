"""Parsing tests for the shared bot/endpoint parser (labeled + unlabeled)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.pop("SUPABASE_URL", None)
os.environ.pop("SUPABASE_KEY", None)

import parsing  # noqa: E402


def test_unlabeled_freeform():
    msg = (
        "Josue Pavon\n3474794095\n5N1AL0MM8DC337962\n2013\n"
        "White Infiniti JX35\n2815 Dewey Ave\nBronx, NY,10465\n"
        "Progressive\nPolicy: 9896095819"
    )
    p = parsing.parse_details(msg)
    assert p["name"] == "Josue Pavon"
    assert p["phone"] == "3474794095"
    assert p["vin"] == "5N1AL0MM8DC337962"
    assert p["year"] == "2013"
    assert p["color"].lower() == "white"
    assert p["make"] == "Infiniti" and p["model"] == "JX35"
    assert p["address"] == "2815 Dewey Ave"
    assert p["city"] == "Bronx" and p["state"] == "NY" and p["zip"] == "10465"
    assert p["insurance_company"] == "Progressive"
    assert p["policy"] == "9896095819"


def test_labeled_still_works():
    p = parsing.parse_details(
        "Name: Jane Doe\nCity: Bronx  State: NY  Zip: 10465\nInsurance: GEICO\nPolicy #: 123"
    )
    assert p["name"] == "Jane Doe"
    assert (p["city"], p["state"], p["zip"]) == ("Bronx", "NY", "10465")
    assert p["insurance_company"] == "GEICO" and p["policy"] == "123"


def test_mixed_labeled_and_unlabeled():
    p = parsing.parse_details(
        "Name: Bob Ray\n(347) 623-9061\n1HGCM82633A004352\n"
        "Black Honda Accord\n12 Oak St\nNewark NJ 07102"
    )
    assert p["name"] == "Bob Ray" and p["phone"] == "3476239061"
    assert p["make"] == "Honda" and p["model"] == "Accord"
    assert p["city"] == "Newark" and p["state"] == "NJ" and p["zip"] == "07102"


def test_labeled_wins_over_inference():
    # An explicit label overrides a same-typed unlabeled line.
    p = parsing.parse_details("Name: Real Name\nSome Other Person")
    assert p["name"] == "Real Name"


if __name__ == "__main__":
    test_unlabeled_freeform()
    test_labeled_still_works()
    test_mixed_labeled_and_unlabeled()
    test_labeled_wins_over_inference()
    print("ALL PARSING TESTS PASSED")
