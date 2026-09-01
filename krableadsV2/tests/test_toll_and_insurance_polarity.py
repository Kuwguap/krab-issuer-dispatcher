r"""Three recognisers that read the operator backwards.

Found while measuring how many natural phrasings the bot understands: 954 were
tested against the running code and 588 were not understood. These three were not
"not understood" — they were understood as the OPPOSITE, which is worse, because
the card then looks correct.

    price 150 no toll          ->  "$150 + toll"   the client is billed for the
    price 150 without toll     ->  "$150 + toll"   toll they just declined
    price 150 minus the toll   ->  "$150 + toll"

    they don't need insurance  ->  insurance ON    a policy issued and billed to
    don't add insurance        ->  insurance ON    someone who said no

    insurer "none" / "na"      ->  already insured  so the car that most needs
    insurer "null" / "unknown" ->  already insured  coverage never gets a policy

The insurance one is a class of slip worth naming: ``\bn't\b`` can never match
inside "don't", because there is no word boundary between the apostrophe and the
t. The negation branch had to put the apostrophe INSIDE the alternation.

Run:  venv\Scripts\python.exe -m pytest tests/test_toll_and_insurance_polarity.py -q
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
os.environ.pop("OPENAI_API_KEY", None)

import utils.database as udb  # noqa: E402

if "bot" not in sys.modules:
    udb.Database = mock.MagicMock()
import bot  # noqa: E402


class NamingTheTollIsNotConsentingToItTest(unittest.TestCase):
    """"150 no toll" mentions the toll in order to refuse it. The only test used
    to be whether the word appeared at all."""

    WANTED = ["150 plus toll", "150 with toll", "150 and toll", "150 toll",
              "150 + toll", "150 including toll", "150 plus tolls"]
    REFUSED = ["150 no toll", "150 without toll", "150 minus the toll",
               "150 no tolls", "150 w/o toll", "150 wo toll",
               "150 toll not included", "150 excluding toll", "150 except toll"]

    def test_a_toll_that_was_asked_for_is_added(self):
        for p in self.WANTED:
            with self.subTest(price=p):
                self.assertEqual(bot._clean_inline_value("price", p), "$150 + toll")

    def test_a_toll_that_was_refused_is_not(self):
        for p in self.REFUSED:
            with self.subTest(price=p):
                self.assertEqual(bot._clean_inline_value("price", p), "$150")

    def test_no_toll_mentioned_at_all(self):
        self.assertEqual(bot._clean_inline_value("price", "150"), "$150")
        self.assertEqual(bot._clean_inline_value("price", "$150"), "$150")

    def test_the_predicate_directly(self):
        self.assertTrue(bot._price_has_toll("$150 + toll"))
        self.assertFalse(bot._price_has_toll("$150"))
        self.assertFalse(bot._price_has_toll("150 no toll"))

    def test_a_note_about_tolls_is_still_a_note(self):
        """"no toll road access" is a delivery instruction, not a price."""
        self.assertEqual(bot._clean_inline_value("driver", "no toll road access"),
                         "no toll road access")


class InsuranceMeansWhatTheOperatorSaidTest(unittest.TestCase):

    ON = ["add insurance", "insurance on", "they want insurance",
          "he needs insurance", "she wants insurance", "turn the insurance on",
          "put insurance on it", "include coverage", "issue insurance",
          "add the insurance", "get him insurance", "insurance please"]

    OFF = ["no insurance", "they don't need insurance", "he doesn't need insurance",
           "don't add insurance", "turn the insurance off", "take insurance off",
           "no need for insurance", "he already has insurance",
           "they already have insurance", "they're covered", "he's covered",
           "she is covered", "they are covered", "already insured",
           "skip insurance", "without insurance", "remove insurance",
           "cancel the insurance", "insurance off"]

    NEITHER = ["we need insurance info from him", "no insurance company on file",
               "insurance GEICO", "insurance", "send the insurance paperwork",
               "what's their insurance carrier", "insurance card number 12345",
               "covered parking available", "Same Day Delivery"]

    def test_asking_for_it(self):
        for p in self.ON:
            with self.subTest(said=p):
                self.assertIs(bot._insurance_intent(p), True)

    def test_refusing_it(self):
        for p in self.OFF:
            with self.subTest(said=p):
                self.assertIs(bot._insurance_intent(p), False)

    def test_merely_talking_about_it(self):
        """A remark is neither an on nor an off. "we need insurance info from him"
        is a note about a missing document; it used to switch the policy on."""
        for p in self.NEITHER:
            with self.subTest(said=p):
                self.assertIsNone(bot._insurance_intent(p))

    def test_the_contraction_that_started_this(self):
        r"""``\bn't\b`` cannot match inside "don't" — there is no word boundary
        between the apostrophe and the t."""
        for p in ("they don't need insurance", "he doesn't need insurance",
                  "we won't need insurance", "she can't get insurance",
                  "don't add insurance"):
            with self.subTest(said=p):
                self.assertIs(bot._insurance_intent(p), False)

    def test_a_refusal_containing_an_affirmation(self):
        """"do NOT add insurance" contains "add insurance". Negation has to be
        tested before affirmation."""
        self.assertIs(bot._insurance_intent("do not add insurance"), False)
        self.assertIs(bot._insurance_intent("please do not add insurance"), False)


class OneDefinitionOfNoInsurerTest(unittest.TestCase):
    """The hand-written tuple compared case-SENSITIVELY and listed five spellings,
    so a car whose insurer read "none" counted as insured and never got a policy."""

    def _lead(self, insurer, policy="-"):
        return {"vehicle_details": "\n".join(["N"] * 8 + [insurer, policy, "-"])}

    def test_every_way_of_writing_nothing(self):
        for v in ("-", "—", "N/A", "n/a", "NA", "na", "None", "none", "NONE",
                  "null", "unknown", "", "  "):
            with self.subTest(insurer=v):
                self.assertFalse(bot._lead_already_insured(self._lead(v)),
                                 f"{v!r} is not an insurer")

    def test_a_real_insurer_still_counts(self):
        """With their policy number beside it. Both halves, or the tag has a box
        it cannot fill."""
        for v in ("Geico", "Progressive", "State Farm", "Allstate"):
            with self.subTest(insurer=v):
                self.assertTrue(bot._lead_already_insured(self._lead(v, "POL-9")))

    def test_a_real_insurer_without_a_policy_number_does_not(self):
        for v in ("Geico", "Progressive", "State Farm", "Allstate"):
            with self.subTest(insurer=v):
                self.assertFalse(bot._lead_already_insured(self._lead(v)),
                                 "a carrier with no policy number is not cover "
                                 "we can print")

    def test_it_agrees_with_the_per_car_rule(self):
        """Two definitions of "blank" drifting apart is how one car gets coverage
        and its twin does not."""
        for v in ("-", "N/A", "none", "null", "unknown", ""):
            with self.subTest(value=v):
                self.assertEqual(
                    bot._lead_already_insured(self._lead(v)),
                    not bot._vehicle_needs_coverage(
                        {"insurance_company": v, "insurance_policy_number": v}))


if __name__ == "__main__":
    unittest.main()
