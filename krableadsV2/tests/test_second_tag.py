r"""One client, two cars, two tags — and exactly one of everything else.

The operator's words: "Result = 2 tags sent to dispatch group. 1 client
1 transaction 1 phone number 1 price and 1 reference number 1 driver 1 dispatcher
MOST IMPORTANTLY 1 RECEIPT".

Every test here guards a failure that this codebase has actually shipped, or that
the audit found waiting to happen:

  * a button whose callback_data no registered pattern matched — the tap does
    nothing, with no reply and no log. PH1_EDIT_MENU_CB_PATTERN's
    ``ph1edit_[a-z]+`` cannot match a digit, so every car-2 button was dead
    before this feature existed;
  * ``_phase1_from_stored_lead`` force-writing the FIRST VIN found anywhere in
    ``vehicle_details`` into car 1's slot — which is why car 2 must never be
    appended to that blob;
  * ``mark_instant_pdf_delivered`` stamping a PAID lead delivered after the first
    of two tags, so the sweep never comes back for the second;
  * the colour palette hardcoding car 1, so picking a colour for the 2nd Tag
    repainted the first car.

Run:  venv\Scripts\python.exe -m pytest tests/test_second_tag.py -q
"""
import asyncio
import itertools
import json
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy-key")
# The two-car split must work with no AI at all: the account has been out of
# credits, and a deterministic split is testable besides.
os.environ.pop("OPENAI_API_KEY", None)

import utils.database as udb  # noqa: E402

if "bot" not in sys.modules:
    udb.Database = mock.MagicMock()
import bot  # noqa: E402


# The operator's real message, verbatim.
CHARLES_PASTE = """Client Charles
Phone 845-423-9476

CHARLES JONES
9 hibiscus Lane Monticello New York 13701
2017 Nissan Altima
VIN: 1N4AL3AP0HC166043
Geico
0407306000
Color grey

CHARLES G JONES
11530 Mango terrace drive apt.102 Seffner Florida 33584
2010 Toyota Camry
VIN: 4T1BF3EK6AU051219
Progressive
982658176
Color grey

Delivery time now 1 hour to 9 hibiscus Lane Monticello New York 13701
Phone 845-423-9476"""

CAR2 = {
    "name": "CHARLES G JONES",
    "address": "11530 Mango terrace drive apt.102",
    "city_state_zip": "Seffner Florida 33584",
    "vin": "4T1BF3EK6AU051219",
    "car": "2010 Toyota Camry",
    "color": "Grey",
    "insurance_company": "Progressive",
    "insurance_policy_number": "982658176",
}


def one_car_lead(**over):
    lead = {
        "id": "lead-1",
        "reference_id": "ABC12345",
        "price": "$150",
        "phone_number": "845-423-9476",
        "vehicle_details": "\n".join([
            "CHARLES JONES", "9 hibiscus Lane", "Monticello New York 13701",
            "9 hibiscus Lane", "Monticello New York 13701",
            "1N4AL3AP0HC166043", "2017 Nissan Altima", "Grey",
            "Geico", "0407306000", "now 1 hour",
        ]),
        "extra_info": "now 1 hour",
    }
    lead.update(over)
    return lead


def two_car_lead(car2=None, **over):
    return one_car_lead(extra_vehicles=[dict(car2 or CAR2)], **over)


def fake_plates():
    """The real allocator's contract (utils/database.py): NJ -> H######,
    anything else -> ######V, plus a random control number."""
    seq = itertools.count(1)

    def alloc(is_nj):
        n = next(seq)
        return {"plate": f"H{n:06d}" if is_nj else f"{n:06d}V",
                "control_number": f"{n:010d}"}
    return alloc


class ThePasteBecomesTwoCarsTest(unittest.TestCase):
    """The headline: paste the job, get two cars, with nothing crossed over."""

    def test_two_blocks_come_out_of_one_paste(self):
        blocks, shared = bot._split_vehicle_blocks(CHARLES_PASTE)
        self.assertEqual(len(blocks), 2)
        self.assertIn("1N4AL3AP0HC166043", blocks[0])
        self.assertNotIn("4T1BF3EK6AU051219", blocks[0])
        self.assertIn("4T1BF3EK6AU051219", blocks[1])
        self.assertNotIn("1N4AL3AP0HC166043", blocks[1])
        # The delivery and phone belong to the job, not to either car.
        self.assertIn("845-423-9476", shared)
        self.assertIn("Delivery time", shared)

    def test_every_field_of_car_two(self):
        state = {}
        _car1_text, added = bot._apply_multi_vehicle_paste(state, CHARLES_PASTE)
        self.assertEqual(added, 1)
        got = bot._extra_vehicles(state)[0]
        for key, want in CAR2.items():
            self.assertEqual(got.get(key), want, f"car 2 {key}")

    def test_the_policy_number_is_not_mistaken_for_a_phone(self):
        """This codebase's value classifier reads "0407306000" as a phone number,
        so the policy has to be taken positionally — the digits after the
        insurer — never by classifying the line."""
        v = bot._fields_from_vehicle_block(
            "CHARLES JONES\n9 hibiscus Lane Monticello New York 13701\n"
            "2017 Nissan Altima\nVIN: 1N4AL3AP0HC166043\nGeico\n0407306000\nColor grey")
        self.assertEqual(v["insurance_policy_number"], "0407306000")
        self.assertEqual(v["insurance_company"], "GEICO")
        self.assertEqual(v["vin"], "1N4AL3AP0HC166043")

    def test_the_delivery_address_is_not_swallowed_into_a_car(self):
        state = {}
        bot._apply_multi_vehicle_paste(state, CHARLES_PASTE)
        for v in bot._extra_vehicles(state):
            self.assertNotIn("845-423-9476", json.dumps(v))
            self.assertNotIn("Delivery", json.dumps(v))

    def test_a_one_car_paste_is_left_completely_alone(self):
        text = "CHARLES JONES\n9 hibiscus Lane Monticello New York 13701\nVIN: 1N4AL3AP0HC166043"
        state = {}
        self.assertEqual(bot._apply_multi_vehicle_paste(state, text), (text, 0))
        self.assertEqual(bot._extra_vehicles(state), [])
        self.assertIsNone(bot._split_vehicle_blocks(text))

    def test_a_seventeen_digit_number_is_not_a_vin(self):
        """A 17-digit account or policy number would otherwise invent a car."""
        self.assertEqual(bot._all_vins_17("policy 12345678901234567"), [])
        self.assertEqual(bot._all_vins_17("VIN 1N4AL3AP0HC166043"), ["1N4AL3AP0HC166043"])


class EachCarGetsItsOwnTagTest(unittest.TestCase):
    """Two PDFs, and each built from its OWN car's details."""

    def setUp(self):
        self.alloc = mock.patch.object(bot.db, "allocate_temp_plate", fake_plates())
        self.upd = mock.patch.object(bot.db, "update_lead", return_value=True)
        self.alloc.start()
        self.upd.start()
        self.addCleanup(self.alloc.stop)
        self.addCleanup(self.upd.stop)

    def test_a_single_car_lead_still_builds_exactly_one_tag(self):
        self.assertEqual(bot._lead_vehicle_indices(one_car_lead()), [1])
        self.assertEqual(bot._vehicle_count(one_car_lead()), 1)

    def test_a_two_car_lead_builds_two(self):
        self.assertEqual(bot._lead_vehicle_indices(two_car_lead()), [1, 2])

    def test_neither_car_borrows_the_others_details(self):
        lead = two_car_lead()
        one = asyncio.run(bot._tag_fields_from_lead(lead, vehicle=1))
        two = asyncio.run(bot._tag_fields_from_lead(lead, vehicle=2))
        self.assertEqual((one["first"], one["last"]), ("CHARLES", "JONES"))
        self.assertEqual((two["first"], two["last"]), ("CHARLES", "G JONES"))
        self.assertEqual(one["vin"], "1N4AL3AP0HC166043")
        self.assertEqual(two["vin"], "4T1BF3EK6AU051219")
        self.assertEqual((one["make"], one["model"]), ("Nissan", "Altima"))
        self.assertEqual((two["make"], two["model"]), ("Toyota", "Camry"))
        self.assertEqual(one["insurance_company"], "Geico")
        self.assertEqual(two["insurance_company"], "Progressive")
        self.assertNotEqual(one["plate"], two["plate"])
        self.assertNotEqual(one["control_number"], two["control_number"])

    def test_the_plate_format_follows_each_cars_own_state(self):
        """Car 1 is New York and car 2 is New Jersey. Reusing car 1's state would
        print a non-NJ plate on the NJ template, or the reverse — silently, and on
        a legal document."""
        lead = two_car_lead(dict(CAR2, city_state_zip="Newark New Jersey 07102"))
        one = asyncio.run(bot._tag_fields_from_lead(lead, vehicle=1))
        two = asyncio.run(bot._tag_fields_from_lead(lead, vehicle=2))
        self.assertEqual((one["state"], one["is_nj"]), ("NY", False))
        self.assertEqual((two["state"], two["is_nj"]), ("NJ", True))
        self.assertRegex(one["plate"], r"^\d{6}V$")
        self.assertRegex(two["plate"], r"^H\d{6}$")

    def test_car_two_keeps_its_plate_so_a_resend_is_identical(self):
        lead = two_car_lead(dict(CAR2, plate="477040V", tag_control_number="1234567890"))
        f = asyncio.run(bot._tag_fields_from_lead(lead, vehicle=2))
        self.assertEqual(f["plate"], "477040V")
        self.assertEqual(f["control_number"], "1234567890")

    def test_a_renewal_mints_a_fresh_plate_for_car_two_as_well(self):
        """A renewal used to refresh car 1 only, so the second car's tag quietly
        expired — and it recurs every month."""
        lead = two_car_lead(dict(CAR2, plate="477040V", tag_control_number="1234567890"))
        f = asyncio.run(bot._tag_fields_from_lead(lead, vehicle=2, renewal=True))
        self.assertNotEqual(f["plate"], "477040V")

    def test_car_two_is_never_appended_to_the_vehicle_details_blob(self):
        """``_phase1_from_stored_lead`` force-writes the first VIN found anywhere
        in that blob into car 1's slot, so an appended block could print car 2's
        VIN on car 1's tag."""
        lead = two_car_lead()
        self.assertNotIn("4T1BF3EK6AU051219", lead["vehicle_details"])
        self.assertEqual(len(lead["vehicle_details"].splitlines()), 11)
        p1 = bot._phase1_from_stored_lead(lead)
        self.assertEqual(p1["vin"], "1N4AL3AP0HC166043")


class NoButtonGoesUnansweredTest(unittest.TestCase):
    """The repeated failure in this codebase: a keyboard emits callback_data that
    no registered pattern matches, so the tap silently does nothing."""

    CARD = {"name": "CHARLES JONES", "extra_vehicles": [dict(CAR2)]}

    def _callbacks(self, markup):
        return [b.callback_data for row in markup.inline_keyboard for b in row]

    def test_the_add_car_button_is_on_the_review_card(self):
        cbs = self._callbacks(bot._build_review_keyboard_with_selections({}))
        self.assertIn(bot.PH1_ADD_CAR_CB, cbs)

    def test_the_add_car_button_is_on_the_edit_picker_too(self):
        cbs = self._callbacks(bot._phase1_edit_fields_keyboard({}))
        self.assertIn(bot.PH1_ADD_CAR_CB, cbs)

    def test_every_button_on_every_screen_matches_a_registered_pattern(self):
        screens = {
            "review card": bot._build_review_keyboard_with_selections(self.CARD),
            "edit picker": bot._phase1_edit_fields_keyboard(self.CARD),
            "2nd Tag picker": bot._vehicle_edit_fields_keyboard(self.CARD, 2),
        }
        for name, markup in screens.items():
            for cb in self._callbacks(markup):
                with self.subTest(screen=name, callback=cb):
                    self.assertTrue(
                        re.match(bot.PH1_REVIEW_CB_PATTERN, cb),
                        f"{cb} reaches no handler via PH1_REVIEW_CB_PATTERN")

    def test_every_per_car_field_button_survives_the_edit_menu_state(self):
        """PH1_EDIT_MENU_CB_PATTERN was ``ph1edit_[a-z]+`` — anchored, and unable
        to match a digit. In STATE_AI_EDIT_MENU that handler runs FIRST, so every
        one of these was a dead button."""
        for cb in self._callbacks(bot._vehicle_edit_fields_keyboard(self.CARD, 2)):
            with self.subTest(callback=cb):
                self.assertTrue(re.match(bot.PH1_EDIT_MENU_CB_PATTERN, cb), cb)

    def test_the_old_pattern_really_would_have_dropped_them(self):
        self.assertIsNone(re.match(r"^(ph1_back|ph1_accept|ph1edit_[a-z]+)$", "ph1edit_v2vin"))

    def test_car_one_buttons_still_match_both_patterns(self):
        for cb in self._callbacks(bot._phase1_edit_fields_keyboard({})):
            with self.subTest(callback=cb):
                self.assertTrue(re.match(bot.PH1_REVIEW_CB_PATTERN, cb), cb)

    def test_every_per_car_edit_key_resolves_to_a_prompt(self):
        """Both edit entry points silently return on an unrecognised key."""
        for base in bot.VEHICLE_EDIT_KEYS:
            ek = bot._vehicle_edit_key(2, base)
            with self.subTest(edit_key=ek):
                self.assertTrue(bot._is_known_edit_key(ek))
                self.assertIn("2nd Tag", bot._edit_prompt_label(ek))
                self.assertEqual(bot._edit_key_base(ek), base)

    def test_a_car_one_key_is_not_read_as_a_car_key(self):
        for ek in ("vin", "col", "addr", "price", "phone", "email"):
            self.assertIsNone(bot._vehicle_edit_key_parts(ek), ek)


class TypingLandsOnTheRightCarTest(unittest.TestCase):
    """Everything in the edit path routes by CONTENT, so a bare "Progressive" or
    "grey" typed at the 2nd Tag's prompt would have been filed against car 1."""

    def card(self):
        return {"name": "CHARLES JONES", "color": "Black", "vin": "1N4AL3AP0HC166043",
                "insurance_company": "Geico",
                "extra_vehicles": [bot._blank_vehicle()]}

    def test_a_colour_typed_at_the_second_cars_prompt(self):
        c = self.card()
        handled, _ = asyncio.run(bot._place_text_at_field_prompt(c, "v2col", "grey"))
        self.assertTrue(handled)
        self.assertEqual(bot._extra_vehicles(c)[0]["color"], "Grey")
        self.assertEqual(c["color"], "Black", "car 1's colour must not move")

    def test_an_insurer_typed_at_the_second_cars_prompt(self):
        c = self.card()
        asyncio.run(bot._place_text_at_field_prompt(c, "v2ins", "Progressive"))
        self.assertEqual(bot._extra_vehicles(c)[0]["insurance_company"], "Progressive")
        self.assertEqual(c["insurance_company"], "Geico")

    def test_a_vin_typed_at_the_second_cars_prompt(self):
        c = self.card()
        asyncio.run(bot._place_text_at_field_prompt(c, "v2vin", "4T1BF3EK6AU051219"))
        self.assertEqual(bot._extra_vehicles(c)[0]["vin"], "4T1BF3EK6AU051219")
        self.assertEqual(c["vin"], "1N4AL3AP0HC166043")

    def test_one_line_fills_both_of_the_second_cars_address_rows(self):
        c = self.card()
        bot._apply_vehicle_edit(
            c, "v2addr", "11530 Mango terrace drive apt.102 Seffner Florida 33584")
        v = bot._extra_vehicles(c)[0]
        self.assertEqual(v["address"], "11530 Mango terrace drive apt.102")
        self.assertEqual(v["city_state_zip"], "Seffner Florida 33584")

    def test_first_and_last_name_build_one_stored_name(self):
        c = self.card()
        bot._apply_vehicle_edit(c, "v2fn", "CHARLES")
        bot._apply_vehicle_edit(c, "v2ln", "G JONES")
        self.assertEqual(bot._extra_vehicles(c)[0]["name"], "CHARLES G JONES")

    def test_editing_the_last_name_alone_keeps_the_first(self):
        c = self.card()
        bot._apply_vehicle_edit(c, "v2fn", "CHARLES")
        bot._apply_vehicle_edit(c, "v2ln", "JONES")
        bot._apply_vehicle_edit(c, "v2ln", "SMITH")
        self.assertEqual(bot._extra_vehicles(c)[0]["name"], "CHARLES SMITH")

    def test_a_minus_clears_a_second_car_field(self):
        c = self.card()
        bot._apply_vehicle_edit(c, "v2car", "2010 Toyota Camry")
        bot._apply_vehicle_edit(c, "v2car", "-")
        self.assertEqual(bot._extra_vehicles(c)[0]["car"], "")

    def test_a_car_one_key_is_not_diverted_to_a_car(self):
        c = self.card()
        self.assertFalse(bot._apply_vehicle_edit(c, "col", "grey"))
        self.assertEqual(bot._extra_vehicles(c)[0]["color"], "")

    def test_a_stale_button_for_a_removed_car_does_not_raise(self):
        c = {"extra_vehicles": []}
        self.assertFalse(bot._apply_vehicle_edit(c, "v2vin", "4T1BF3EK6AU051219"))


class TheSecondCarsVinIsValidatedTest(unittest.TestCase):
    """``_clean_vin_and_car`` is the only thing enforcing "17 alphanumerics", and
    it only ever looked at car 1."""

    def test_a_vin_typed_into_the_car_field_is_rescued(self):
        v = {"vin": "", "car": "4T1BF3EK6AU051219 2010 Toyota Camry"}
        bot._clean_vehicle_vin(v)
        self.assertEqual(v["vin"], "4T1BF3EK6AU051219")
        self.assertEqual(v["car"], "2010 Toyota Camry")

    def test_every_edit_normalises_the_second_cars_vin(self):
        card = {"name": "X", "vin": "1N4AL3AP0HC166043", "car": "2017 Nissan Altima",
                "extra_vehicles": [dict(CAR2, vin="4t1bf3ek6au051219", color="grey")]}
        bot._clean_vin_and_car(card)
        v = bot._extra_vehicles(card)[0]
        self.assertEqual(v["vin"], "4T1BF3EK6AU051219")
        self.assertEqual(v["color"], "Grey")

    def test_a_short_vin_is_kept_visible_rather_than_silently_accepted(self):
        v = {"vin": "4T1BF3EK6AU05121", "car": ""}
        bot._clean_vehicle_vin(v)
        self.assertEqual(v["vin"], "4T1BF3EK6AU05121")
        self.assertIn("not 17", bot._extra_vehicles_submit_block({"extra_vehicles": [
            dict(CAR2, vin="4T1BF3EK6AU05121")]}))


class SubmitRefusesAnIncompleteCarTest(unittest.TestCase):
    """Named in one message rather than pushed through the missing-field prompt
    queue, which has a history of asking for values already on the card."""

    def test_a_complete_lead_is_allowed(self):
        self.assertEqual(bot._extra_vehicles_submit_block(
            {"vin": "1N4AL3AP0HC166043", "extra_vehicles": [dict(CAR2)]}), "")

    def test_a_lead_with_no_extra_cars_is_untouched(self):
        self.assertEqual(bot._extra_vehicles_submit_block({}), "")

    def test_the_message_names_the_car_and_the_field(self):
        out = bot._extra_vehicles_submit_block(
            {"extra_vehicles": [dict(CAR2, vin="", city_state_zip="")]})
        self.assertIn("2nd Tag", out)
        self.assertIn("VIN", out)
        self.assertIn("registration city, state, ZIP", out)

    def test_the_third_car_is_named_correctly(self):
        out = bot._extra_vehicles_submit_block(
            {"extra_vehicles": [dict(CAR2), dict(CAR2, vin="", name="")]})
        self.assertIn("3rd Tag", out)

    def test_the_same_vin_twice_is_refused(self):
        out = bot._extra_vehicles_submit_block(
            {"vin": "4T1BF3EK6AU051219", "extra_vehicles": [dict(CAR2)]})
        self.assertIn("one car, one tag", out)

    def test_an_untouched_blank_car_is_dropped_not_blocked(self):
        """Tapping ➕ Add Car and changing your mind must not block the lead."""
        card = {"extra_vehicles": [bot._blank_vehicle()]}
        bot._prune_empty_extra_vehicles(card)
        self.assertEqual(bot._extra_vehicles(card), [])
        self.assertEqual(bot._extra_vehicles_submit_block(card), "")


class InsuranceFollowsTheMissingFieldTest(unittest.TestCase):
    """The operator's rule: "if insurance is missing it means it needs tristate
    coverage for that"."""

    def test_a_car_that_arrived_insured_needs_nothing(self):
        self.assertFalse(bot._vehicle_needs_coverage(CAR2))
        self.assertFalse(bot._vehicle_needs_coverage(
            {"insurance_company": "Geico", "insurance_policy_number": ""}))

    def test_a_car_with_no_insurer_needs_coverage(self):
        self.assertTrue(bot._vehicle_needs_coverage(bot._blank_vehicle()))

    def test_placeholders_count_as_missing(self):
        for placeholder in ("-", "—", "N/A", "n/a", "none", "NONE", "unknown"):
            with self.subTest(placeholder=placeholder):
                self.assertTrue(bot._vehicle_needs_coverage(
                    {"insurance_company": placeholder,
                     "insurance_policy_number": placeholder}))

    def test_the_charles_jones_job_issues_no_policy_at_all(self):
        """Both cars came in with their own insurer."""
        state = {}
        bot._apply_multi_vehicle_paste(state, CHARLES_PASTE)
        for v in bot._extra_vehicles(state):
            self.assertFalse(bot._vehicle_needs_coverage(v))

    def test_each_car_is_insured_from_its_own_details(self):
        """A synthetic single-car lead is what keeps detect_card_state and the
        card builder — both of which read positional lines — off car 1."""
        lead = two_car_lead()
        synth = bot._synthetic_lead_for_vehicle(lead, 2)
        lines = synth["vehicle_details"].splitlines()
        self.assertEqual(lines[0], "CHARLES G JONES")
        self.assertEqual(lines[2], "Seffner Florida 33584")
        self.assertEqual(lines[5], "4T1BF3EK6AU051219")
        self.assertNotIn("1N4AL3AP0HC166043", synth["vehicle_details"])
        self.assertEqual(bot._extra_vehicles(synth), [], "must not recurse")


class ThePaidInstantTagCannotHalfDeliverTest(unittest.TestCase):
    """``mark_instant_pdf_delivered`` stamps the lead and the retry sweep filters
    on that stamp. "The first of two arrived" has to read as failure."""

    def _run(self, per_car_counts):
        sent = []

        async def fake_send(context, lead, chats, **kw):
            return per_car_counts[kw.get("vehicle", 1) - 1]

        async def go():
            with mock.patch.object(bot, "_build_and_send_tag_pdf", side_effect=fake_send):
                return await bot._send_all_tag_pdfs(None, two_car_lead(), [1])
        counts = asyncio.run(go())
        return counts, sent

    def test_both_tags_arriving_is_a_success(self):
        counts, _ = self._run([1, 1])
        self.assertEqual(counts, [1, 1])
        self.assertTrue(all(counts))

    def test_only_the_second_tag_failing_is_a_failure(self):
        counts, _ = self._run([1, 0])
        self.assertFalse(all(counts), "a half-delivered PAID lead must not be marked done")

    def test_only_the_first_tag_failing_is_a_failure(self):
        counts, _ = self._run([0, 1])
        self.assertFalse(all(counts))

    def test_a_single_car_lead_makes_exactly_one_call(self):
        calls = []

        async def fake_send(context, lead, chats, **kw):
            calls.append(kw.get("vehicle", 1))
            return 1

        async def go():
            with mock.patch.object(bot, "_build_and_send_tag_pdf", side_effect=fake_send):
                return await bot._send_all_tag_pdfs(None, one_car_lead(), [1])
        self.assertEqual(asyncio.run(go()), [1])
        self.assertEqual(calls, [1])

    def test_insurance_is_offered_once_not_once_per_car(self):
        """The hand-off is idempotent per LEAD, so N calls means N-1 no-ops — and
        a car needing coverage would silently never get a policy."""
        flags = []

        async def fake_send(context, lead, chats, **kw):
            flags.append(kw.get("ride_insurance"))
            return 1

        async def go():
            with mock.patch.object(bot, "_build_and_send_tag_pdf", side_effect=fake_send):
                await bot._send_all_tag_pdfs(None, two_car_lead(), [1])
        asyncio.run(go())
        self.assertEqual(flags.count(True), 1, f"expected exactly one, got {flags}")


class OneOfEverythingElseTest(unittest.TestCase):
    """1 client, 1 transaction, 1 phone, 1 price, 1 reference, 1 receipt."""

    def test_one_lead_row_means_one_reference_and_one_receipt(self):
        lead = two_car_lead()
        self.assertEqual(lead["reference_id"], "ABC12345")
        self.assertEqual(lead["price"], "$150")
        self.assertEqual(lead["phone_number"], "845-423-9476")
        # Receipts key off the lead id, so two cars cannot become two receipts.
        self.assertEqual(bot.receipt_portal_url(lead["id"]),
                         bot.receipt_portal_url(one_car_lead()["id"]))

    def test_the_dispatch_group_reads_both_cars(self):
        html = bot._format_group_lead_message_html(
            "ABC12345", bot._phase1_from_stored_lead(two_car_lead()),
            "https://link", None, None, "")
        self.assertIn("2nd Tag", html)
        self.assertIn("4T1BF3EK6AU051219", html)
        self.assertIn("Progressive", html)
        self.assertIn("Seffner Florida 33584", html)
        # And car 1 is still all there.
        self.assertIn("1N4AL3AP0HC166043", html)
        self.assertIn("Geico", html)

    def test_the_driver_is_told_how_many_tags_to_expect(self):
        self.assertEqual(bot._multi_tag_notice_lines(one_car_lead()), [])
        notice = bot._multi_tag_notice_lines(two_car_lead())
        self.assertEqual(len(notice), 1)
        self.assertIn("2 TAGS", notice[0])

    def test_another_tag_same_client_does_not_clone_the_extra_cars(self):
        """That button deliberately mints a SEPARATE lead for a separate
        transaction; carrying the extra cars over would issue their tags twice."""
        p1 = bot._phase1_from_stored_lead(two_car_lead())
        self.assertEqual(bot._extra_vehicles(p1), [dict(CAR2)])
        src = Path(bot.__file__).read_text(encoding="utf-8")
        self.assertIn("p1.pop(EXTRA_VEHICLES_KEY, None)", src)


class ASingleCarLeadIsUnchangedTest(unittest.TestCase):
    """"So everything stays same" — the review card, the blob and the group post
    for a one-car lead must be exactly what they were."""

    CARD = {
        "name": "CHARLES JONES", "address": "9 hibiscus Lane",
        "city_state_zip": "Monticello New York 13701",
        "vin": "1N4AL3AP0HC166043", "car": "2017 Nissan Altima", "color": "Grey",
        "insurance_company": "Geico", "insurance_policy_number": "0407306000",
        "pending_phone_number": "845-423-9476", "pending_price": "$150",
    }

    def test_the_card_gains_nothing(self):
        out = bot._format_phase1_field_lines(self.CARD)
        self.assertEqual(len(out.splitlines()), 18)
        self.assertNotIn("Tag", out)
        self.assertEqual(out, bot._format_phase1_field_lines(
            dict(self.CARD, extra_vehicles=[])))

    def test_the_extra_block_is_empty_for_one_car(self):
        self.assertEqual(bot._format_all_extra_vehicle_lines(self.CARD), "")
        self.assertEqual(bot._format_all_extra_vehicle_lines({}), "")

    def test_the_card_gains_the_operators_exact_block_for_two(self):
        out = bot._format_phase1_field_lines(dict(self.CARD, extra_vehicles=[dict(CAR2)]))
        for label in ("🚘 2nd Tag", "👤First name: CHARLES", "👤Last name: G JONES",
                      "🏠Registration address: 11530 Mango terrace drive apt.102",
                      "🏠Registration city, state, ZIP: Seffner Florida 33584",
                      "🔢VIN: 4T1BF3EK6AU051219", "🚘Car: 2010 Toyota Camry",
                      "🎨Color: Grey", "🛡Insurance company: Progressive",
                      "🛡Insurance policy #: 982658176"):
            self.assertIn(label, out)

    def test_the_group_post_for_one_car_has_no_extra_block(self):
        html = bot._format_group_lead_message_html(
            "ABC12345", bot._phase1_from_stored_lead(one_car_lead()),
            "https://link", None, None, "")
        self.assertNotIn("Tag</b>", html)


class TheColumnIsDeclaredTest(unittest.TestCase):
    """Code has shipped here reading columns whose migration did not exist."""

    def test_the_migration_declares_the_column(self):
        sql = (ROOT / "database" / "migration_extra_vehicles.sql").read_text(encoding="utf-8")
        self.assertIn("extra_vehicles", sql)
        self.assertIn("add column if not exists", sql.lower())

    def test_the_write_degrades_instead_of_refusing_the_whole_lead(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_real_db_ev", ROOT / "utils" / "database.py")
        real = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(real)
        self.assertIn("extra_vehicles", real._OPTIONAL_LEADS_WRITE_KEYS)

    def test_the_issuer_is_warned_when_the_extra_cars_are_dropped(self):
        """Degrading quietly would send one tag while reporting success."""
        src = Path(bot.__file__).read_text(encoding="utf-8")
        self.assertIn("migration_extra_vehicles.sql", src)

    def test_a_lead_read_back_without_the_column_reads_as_one_car(self):
        for raw in (None, "", "null", "not json", {}, 7):
            with self.subTest(raw=raw):
                self.assertEqual(bot._extra_vehicles({"extra_vehicles": raw}), [])
                self.assertEqual(bot._lead_vehicle_indices({"extra_vehicles": raw}), [1])

    def test_a_json_string_column_still_reads(self):
        self.assertEqual(
            bot._extra_vehicles({"extra_vehicles": json.dumps([CAR2])}), [dict(CAR2)])


class OrdinalsReadCorrectlyTest(unittest.TestCase):
    def test_them(self):
        for n, want in ((2, "2nd Tag"), (3, "3rd Tag"), (4, "4th Tag"), (11, "11th Tag"),
                        (12, "12th Tag"), (13, "13th Tag"), (21, "21st Tag"),
                        (22, "22nd Tag"), (23, "23rd Tag")):
            self.assertEqual(bot._ordinal_tag_label(n), want)


class EveryCreateSiteCarriesTheExtraCarsTest(unittest.TestCase):
    """There are FIVE db.create_lead calls. Only one is the review card's Submit —
    the others are reached whenever the phone or price still has to be asked for,
    and each would have dropped the extra cars silently and issued one tag."""

    def test_no_create_lead_call_is_left_unwired(self):
        src = Path(bot.__file__).read_text(encoding="utf-8")
        creates = src.count("db.create_lead(")
        wired = src.count("_attach_extra_vehicles_for_create(")
        # One helper definition + one call per create site.
        self.assertEqual(wired, creates + 1,
                         f"{creates} create_lead sites but only {wired - 1} wired")

    def test_the_helper_mints_a_plate_per_car_from_its_own_state(self):
        with mock.patch.object(bot.db, "allocate_temp_plate", fake_plates()):
            payload = asyncio.run(bot._attach_extra_vehicles_for_create(
                {}, {"extra_vehicles": [dict(CAR2),
                                        dict(CAR2, city_state_zip="Newark New Jersey 07102",
                                             vin="1HGCM82633A004352")]}))
        got = payload["extra_vehicles"]
        self.assertEqual(len(got), 2)
        self.assertRegex(got[0]["plate"], r"^\d{6}V$", "Florida is not NJ")
        self.assertRegex(got[1]["plate"], r"^H\d{6}$", "New Jersey gets an H plate")
        self.assertNotEqual(got[0]["tag_control_number"], got[1]["tag_control_number"])

    def test_the_helper_leaves_a_one_car_payload_untouched(self):
        self.assertEqual(asyncio.run(bot._attach_extra_vehicles_for_create({}, {})), {})

    def test_an_already_plated_car_keeps_its_plate(self):
        with mock.patch.object(bot.db, "allocate_temp_plate", fake_plates()):
            payload = asyncio.run(bot._attach_extra_vehicles_for_create(
                {}, {"extra_vehicles": [dict(CAR2, plate="477040V",
                                             tag_control_number="1234567890")]}))
        self.assertEqual(payload["extra_vehicles"][0]["plate"], "477040V")

    def test_a_dropped_column_is_reported_not_swallowed(self):
        said = []

        class Msg:
            async def reply_text(self, text, **kw):
                said.append(text)

        asyncio.run(bot._warn_if_extra_vehicles_were_dropped(
            Msg(), {"extra_vehicles": [dict(CAR2)]}, {"id": "lead-1"}))
        self.assertEqual(len(said), 1)
        self.assertIn("migration_extra_vehicles.sql", said[0])
        self.assertIn("only one tag", said[0])

    def test_nothing_is_said_when_they_saved_fine(self):
        said = []

        class Msg:
            async def reply_text(self, text, **kw):
                said.append(text)

        asyncio.run(bot._warn_if_extra_vehicles_were_dropped(
            Msg(), {"extra_vehicles": [dict(CAR2)]},
            {"id": "lead-1", "extra_vehicles": [dict(CAR2)]}))
        self.assertEqual(said, [])

    def test_a_one_car_lead_never_triggers_the_warning(self):
        said = []

        class Msg:
            async def reply_text(self, text, **kw):
                said.append(text)

        asyncio.run(bot._warn_if_extra_vehicles_were_dropped(Msg(), {}, {"id": "l"}))
        self.assertEqual(said, [])


class TheBoardShowsEveryCarTest(unittest.TestCase):
    """A row reading as ONE car when two tags are owed is how a second tag goes
    undelivered with nobody noticing."""

    def test_the_query_asks_for_the_column(self):
        src = (ROOT / "admin_dashboard.py").read_text(encoding="utf-8")
        get_tx = src[src.index("def get_transmissions("):]
        get_tx = get_tx[:get_tx.index("lead_ids = ")]
        self.assertIn("extra_vehicles", get_tx)

    def test_the_lean_fallback_does_not_ask_for_it(self):
        """That path exists for a database behind on migrations; asking for the
        column it is working around would defeat it."""
        src = (ROOT / "admin_dashboard.py").read_text(encoding="utf-8")
        lean = src[src.index("retrying lean"):]
        lean = lean[:lean.index("lead_ids = ")]
        self.assertNotIn("extra_vehicles", lean)

    def test_the_board_has_a_tags_column_and_matching_colspans(self):
        page = (ROOT / "receipts_page.py").read_text(encoding="utf-8")
        self.assertIn("<th>Tags</th>", page)
        self.assertIn("r.tags", page)
        # A stale colspan visibly breaks the table.
        self.assertNotIn('colspan="10"', page)


if __name__ == "__main__":
    unittest.main()
