"""An email typed into an address is an email (lead 1K3AX0XS).

1K3AX0XS came from a tristatetags.com checkout with an empty email and the
customer's email typed at the end of the address:
    "113, East 84th Street, New York, NY, 10028, United States, Josue_jeanette@icloud.com"
It rode into every driver offer inside the delivery details, and its lone "_"
broke the offer's Markdown, so the lead reached no drivers.

On a NEW website/API ingest the email now moves into the email field (when
that is empty) and always leaves the address text. An address with no "@" is
untouched; an email already on the lead wins; "Apt 2D" survives.

Pure parsing plus a mocked ingest -- no network, no database.

Run:  venv/Scripts/python.exe -m pytest tests/test_address_email.py -q
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy-key")

from utils import lead_ingest  # noqa: E402
from utils.external_lead_parser import (  # noqa: E402
    PAID_COVERAGE_LINE,
    SAMPLE_MESSAGE,
    move_address_emails_to_email,
    parse_external_lead_message,
    pull_emails_from_address,
)

EMAIL = "Josue_jeanette@icloud.com"
ADDRESS = "113, East 84th Street, New York, NY, 10028, United States"
TYPED = ADDRESS + ", " + EMAIL


def _message(delivery=TYPED, registration=ADDRESS, email=None, extra=()):
    """The website's New Lead text (server/krableads-ingest.js). No
    "Delivery email" line when the checkout email was left empty."""
    lines = ["\U0001f195 New Lead", "Order #f85e6052", "Customer: Josue Pavon",
             "Phone: 2125550143"]
    if email is not None:
        lines.append("Delivery email: " + email)
    lines.append("Delivery method: Email Delivery")
    if registration is not None:
        lines.append("Registration address: " + registration)
    if delivery is not None:
        lines.append("Delivery address: " + delivery)
    lines += ["VIN: JTLKT324364094480", "Vehicle: 2006 Scion xB, grey"]
    lines += list(extra)
    lines += ["Service: 30-Day NY Temp Tag", "Price: $150.00"]
    return "\n".join(lines)


def _fields(**over):
    fields = {"name": "Josue Pavon", "phone": "2125550143", "price": "$150.00",
              "vin": "JTLKT324364094480", "vehicle": "2006 Scion xB, grey"}
    fields.update(over)
    return fields


class IngestDB:
    """create_lead only: an ingest writes one NEW row and touches nothing else."""

    def __init__(self):
        self.payloads = []

    def create_lead(self, payload):
        self.payloads.append(dict(payload))
        return {"id": "lead-1", "reference_id": payload.get("reference_id")}

    def get_all_groups(self):
        return [{"id": "g1", "group_name": "HighKage", "is_active": True}]

    def auto_close_followups_matching_lead(self, lead):
        return []


def _ingest(**kwargs):
    idb = IngestDB()
    ots = mock.MagicMock()
    ots.return_value.encrypt_phone.return_value = None
    ots.return_value.last_error = "not configured"
    with mock.patch.object(lead_ingest, "resolve_api_lead_user_sync",
                           lambda _db, _u: (7, "tristatetag", None)), \
            mock.patch.object(lead_ingest, "OneTimeSecret", ots), \
            mock.patch("utils.ledger.is_configured", lambda: False):
        result, errors = lead_ingest.ingest_external_lead(idb, **kwargs)
    return idb, result, errors


def _payload(**kwargs):
    idb, result, errors = _ingest(**kwargs)
    if errors:
        raise AssertionError(errors)
    assert result is not None
    return idb.payloads[0]


# ------------------------------------------------------- the 1K3AX0XS shape

class OneK3AX0XSShapeTest(unittest.TestCase):

    def test_the_email_moves_and_the_address_ends_at_united_states(self):
        payload = _payload(message=_message())
        self.assertEqual(EMAIL, payload["email"])
        self.assertEqual(ADDRESS, payload["delivery_details"])
        self.assertTrue(payload["delivery_details"].endswith("United States"))
        lines = payload["vehicle_details"].splitlines()
        self.assertEqual(11, len(lines))
        self.assertEqual(ADDRESS, lines[1])
        self.assertEqual(ADDRESS, lines[3])
        self.assertNotIn("@", payload["vehicle_details"])

    def test_the_underscore_that_broke_the_driver_offer_is_gone(self):
        payload = _payload(message=_message())
        for key in ("delivery_details", "vehicle_details", "extra_info"):
            with self.subTest(key=key):
                self.assertNotIn("_", payload[key])
                self.assertNotIn("@", payload[key])

    def test_in_both_addresses(self):
        payload = _payload(message=_message(delivery=TYPED, registration=TYPED))
        self.assertEqual(EMAIL, payload["email"])
        lines = payload["vehicle_details"].splitlines()
        self.assertEqual(ADDRESS, lines[1])
        self.assertEqual(ADDRESS, lines[3])

    def test_in_the_registration_address_only(self):
        payload = _payload(message=_message(delivery=None, registration=TYPED))
        self.assertEqual(EMAIL, payload["email"])
        self.assertEqual(ADDRESS, payload["delivery_details"])

    def test_a_fields_body_too(self):
        payload = _payload(fields=_fields(**{"Delivery Address": TYPED,
                                             "registration_address": ADDRESS,
                                             "email": ""}))
        self.assertEqual(EMAIL, payload["email"])
        self.assertEqual(ADDRESS, payload["delivery_details"])

    def test_the_paid_coverage_line_still_arms(self):
        payload = _payload(message=_message(extra=[PAID_COVERAGE_LINE]))
        self.assertIs(True, payload["wants_insurance"])
        self.assertEqual(EMAIL, payload["email"])
        self.assertEqual(ADDRESS, payload["delivery_details"])

    def test_documents_are_not_asked_for_an_email_the_address_gave(self):
        extract = mock.MagicMock(return_value={})
        with mock.patch.object(lead_ingest, "normalize_ingest_files", lambda f: list(f)), \
                mock.patch.object(lead_ingest, "extract_fields_from_files", extract):
            payload = _payload(message=_message(),
                               files=[{"url": "https://x.test/dl.jpg",
                                       "label": "Drivers License"}])
        self.assertNotIn("email", extract.call_args.kwargs["wanted_keys"])
        self.assertEqual(EMAIL, payload["email"])
        self.assertEqual(ADDRESS, payload["delivery_details"])


# --------------------------------------------------- no "@", no change at all

class AnAddressWithNoAtIsUntouchedTest(unittest.TestCase):

    def test_the_text_comes_back_exactly(self):
        for text in ("28 brookside rd quincy MA 02169",
                     "  113, East 84th Street ,, Apt 2D  ",
                     "5 Main St\nKeyport NJ 07735",
                     "5 Main St / Apt 2D - rear, Keyport NJ 07735",
                     "5 Main St,\nKeyport NJ 07735",
                     ""):
            with self.subTest(text=text):
                self.assertEqual((text, []), pull_emails_from_address(text))
        self.assertEqual(("", []), pull_emails_from_address(None))

    def test_a_stray_at_is_not_an_email(self):
        for text in ("Unit @ rear, 5 Main St, Keyport NJ 07735",
                     "c/o x@localhost, 5 Main St, Keyport NJ 07735",
                     "5 Main St @ Broadway, New York NY 10001",
                     "5 Main St, a@b.c0m, Keyport NJ 07735"):
            with self.subTest(text=text):
                self.assertEqual((text, []), pull_emails_from_address(text))
                fields = _fields(**{"registration address": text})
                self.assertEqual(fields, move_address_emails_to_email(fields))

    def test_the_sample_message_ingests_exactly_as_before(self):
        state, errors = parse_external_lead_message(SAMPLE_MESSAGE)
        self.assertEqual([], errors)
        payload = _payload(message=SAMPLE_MESSAGE)
        for key in ("vehicle_details", "delivery_details", "email", "extra_info"):
            with self.subTest(key=key):
                self.assertEqual(state[key], payload[key])


# ------------------------------------------------ an email on the lead wins

class AnEmailAlreadySetWinsTest(unittest.TestCase):

    def test_a_different_delivery_email_is_kept(self):
        payload = _payload(message=_message(email="josue.real@gmail.com"))
        self.assertEqual("josue.real@gmail.com", payload["email"])
        self.assertEqual(ADDRESS, payload["delivery_details"])

    def test_the_same_email_is_kept_and_the_address_still_cleaned(self):
        payload = _payload(message=_message(email=EMAIL))
        self.assertEqual(EMAIL, payload["email"])
        self.assertEqual(ADDRESS, payload["delivery_details"])

    def test_in_a_fields_body(self):
        fields = _fields(**{"Delivery email": "josue.real@gmail.com",
                            "registration address": TYPED})
        moved = move_address_emails_to_email(fields)
        self.assertEqual(ADDRESS, moved["registration address"])
        self.assertNotIn("email", moved)
        payload = _payload(fields=fields)
        self.assertEqual("josue.real@gmail.com", payload["email"])
        self.assertEqual(ADDRESS, payload["delivery_details"])

    def test_a_placeholder_is_not_an_email_and_gives_way(self):
        """Otherwise the typed email is cut from the address AND dropped."""
        for over in ({"email": "-"}, {"Delivery email": "N/A"},
                     {"email": "none", "delivery_email": "-"}):
            with self.subTest(over=over):
                payload = _payload(fields=_fields(**{"delivery address": TYPED}, **over))
                self.assertEqual(EMAIL, payload["email"])
                self.assertEqual(ADDRESS, payload["delivery_details"])

    def test_an_existing_email_with_an_at_is_never_replaced(self):
        moved = move_address_emails_to_email(
            _fields(**{"delivery address": TYPED, "email": "x@localhost"}))
        self.assertEqual("x@localhost", moved["email"])
        self.assertEqual(ADDRESS, moved["delivery address"])


# ------------------------------------------------------- "Apt 2D" survives

class TheApartmentSurvivesTest(unittest.TestCase):

    def test_an_apartment_beside_the_email(self):
        cases = (
            ("113 East 84th Street, Apt 2D, " + EMAIL + ", New York, NY 10028",
             "113 East 84th Street, Apt 2D, New York, NY 10028"),
            ("113 East 84th Street Apt 2D " + EMAIL,
             "113 East 84th Street Apt 2D"),
            ("113 East 84th Street, Apt 2D, New York NY 10028, " + EMAIL,
             "113 East 84th Street, Apt 2D, New York NY 10028"),
        )
        for text, want in cases:
            with self.subTest(text=text):
                self.assertEqual((want, [EMAIL]), pull_emails_from_address(text))

    def test_through_the_ingest(self):
        typed = "113 East 84th Street, Apt 2D, New York NY 10028, " + EMAIL
        payload = _payload(message=_message(delivery=typed, registration=typed))
        self.assertEqual(EMAIL, payload["email"])
        self.assertIn("Apt 2D", payload["vehicle_details"].splitlines()[1])
        self.assertIn("Apt 2D", payload["delivery_details"])
        self.assertNotIn("@", payload["vehicle_details"])

    def test_an_apartment_or_line_2_field(self):
        for key in ("apartment", "Apt #", "Address line 2", "address2",
                    "delivery_apartment", "Unit"):
            with self.subTest(key=key):
                moved = move_address_emails_to_email(
                    _fields(**{"registration address": ADDRESS,
                               key: "Apt 2D " + EMAIL}))
                self.assertEqual("Apt 2D", moved[key])
                self.assertEqual(EMAIL, moved["email"])
        moved = move_address_emails_to_email(
            _fields(**{"registration address": ADDRESS, "Address line 2": EMAIL}))
        self.assertEqual("", moved["Address line 2"])
        self.assertEqual(EMAIL, moved["email"])
        self.assertEqual(ADDRESS, moved["registration address"])


# ------------------------------------------------------- how it is cut out

class TheJoinGoesWithTheEmailTest(unittest.TestCase):

    def test_shapes(self):
        cases = (
            (TYPED, ADDRESS),
            (TYPED + ".", ADDRESS),
            (EMAIL + ", " + ADDRESS, ADDRESS),
            (ADDRESS + ", Email: " + EMAIL, ADDRESS),
            (ADDRESS + ", e-mail - " + EMAIL, ADDRESS),
            (ADDRESS + " (" + EMAIL + ")", ADDRESS),
            (ADDRESS + " <" + EMAIL + ">", ADDRESS),
            ("5 Main St josue@icloud.com Keyport NJ 07735", "5 Main St Keyport NJ 07735"),
            ("5 Main St\n" + EMAIL + "\nKeyport NJ 07735", "5 Main St\nKeyport NJ 07735"),
            ("  " + TYPED + "  ", ADDRESS),
            # No dangling comma, bracket or separator left behind.
            (ADDRESS + ",\n" + EMAIL, ADDRESS),
            (ADDRESS + ", \r\n" + EMAIL, ADDRESS),
            ("5 Main St,\n" + EMAIL + "\nKeyport NJ 07735", "5 Main St\nKeyport NJ 07735"),
            (ADDRESS + " (email " + EMAIL + ")", ADDRESS),
            (ADDRESS + " (Email: " + EMAIL + ")", ADDRESS),
            (ADDRESS + " / " + EMAIL, ADDRESS),
            (ADDRESS + " - " + EMAIL, ADDRESS),
        )
        for text, want in cases:
            with self.subTest(text=text):
                kept, emails = pull_emails_from_address(text)
                self.assertEqual(want, kept)
                self.assertEqual(1, len(emails))

    def test_two_emails_the_first_becomes_the_email(self):
        text = "a.one@icloud.com, 5 Main St, Keyport NJ 07735, b.two@gmail.com"
        self.assertEqual(("5 Main St, Keyport NJ 07735",
                          ["a.one@icloud.com", "b.two@gmail.com"]),
                         pull_emails_from_address(text))
        moved = move_address_emails_to_email(_fields(**{"delivery address": text}))
        self.assertEqual("a.one@icloud.com", moved["email"])

    def test_the_delivery_address_is_looked_at_first(self):
        moved = move_address_emails_to_email(_fields(**{
            "registration address": ADDRESS + ", reg@icloud.com",
            "delivery address": ADDRESS + ", del@icloud.com"}))
        self.assertEqual("del@icloud.com", moved["email"])
        self.assertEqual(ADDRESS, moved["registration address"])
        self.assertEqual(ADDRESS, moved["delivery address"])

    def test_the_input_is_never_changed(self):
        fields = _fields(**{"delivery address": TYPED})
        before = dict(fields)
        move_address_emails_to_email(fields)
        self.assertEqual(before, fields)
        self.assertEqual({}, move_address_emails_to_email(None))


# ------------------------------------- an address that was only an email

class AnEmailOnlyAddressTest(unittest.TestCase):

    def test_the_other_address_stands_in(self):
        payload = _payload(message=_message(registration=EMAIL, delivery=ADDRESS))
        self.assertEqual(EMAIL, payload["email"])
        self.assertEqual(ADDRESS, payload["vehicle_details"].splitlines()[1])
        self.assertNotIn("@", payload["vehicle_details"])

    def test_with_no_address_left_the_lead_is_still_taken(self):
        """It was accepted before; refusing a paid order now would push it
        off the bot onto the website's legacy fallback."""
        idb, result, errors = _ingest(message=_message(registration=EMAIL, delivery=EMAIL))
        self.assertEqual([], errors)
        self.assertIsNotNone(result)
        payload = idb.payloads[0]
        self.assertEqual(EMAIL, payload["email"])
        self.assertEqual("-", payload["delivery_details"])
        self.assertNotIn("@", payload["vehicle_details"])


# ------------------------------------------------------------- the scope

class OnlyTheIngestMovesItTest(unittest.TestCase):
    """The shared parse_* functions are unchanged: Dispatch Web's paste box
    (dispatch_web/newlead.py) and every other caller see what they saw before.
    Only utils/lead_ingest.py -- new website/API leads -- moves the email.
    Relax this pin deliberately if those paths should clean addresses too."""

    def test_the_shared_parser_is_unchanged(self):
        state, errors = parse_external_lead_message(_message())
        self.assertEqual([], errors)
        self.assertIsNone(state["email"])
        self.assertEqual(TYPED, state["delivery_address"])


if __name__ == "__main__":
    unittest.main()
