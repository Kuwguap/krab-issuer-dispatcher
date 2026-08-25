r"""Talking to the bot the way you would talk to a person.

The operator's ask: "activate proper command accepting, like a chat bot. Instead
of saying 'driver all' I can say something like 'I'd like to select all drivers'.
Make it very fluid for all phases and sections and processes."

954 phrasings were run against the live code before any of this was written and
588 were not understood. The failures were five shapes, not a thousand:

  * conversational preamble    "I'd like to select all drivers"
  * quantifier-first order     "all drivers" (only "driver all" worked)
  * a punctuation separator    "driver: Susan" — how a phone transcribes a pause
  * a glued-on tail            "driver Susan, send it" hunted for a driver
                               literally called "Susan, send it"
  * a trailing courtesy        "black please" stored the word "please"

THE INVARIANT, and the reason this is shippable on a live bot:

    _classify_review_command is strict(text) or fluent(normalised(text)).
    The fluent pass may only ever UPGRADE a ("NONE", None).
    It can never overrule a verdict the strict pass already reached.

So the second half of this file matters more than the first. A layer that
understands everything but occasionally files "Same Day Delivery" as a command is
worse than the rigid bot it replaces, because the operator cannot see it happen.

Run:  venv\Scripts\python.exe -m pytest tests/test_fluent_commands.py -q
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


def kind(text, **kw):
    return bot._classify_review_command(text, **kw)[0]


def payload(text, **kw):
    return bot._classify_review_command(text, **kw)[1]


class TheWayPeopleActuallyAskTest(unittest.TestCase):

    ALL_DRIVERS = [
        "driver all", "all drivers", "all the drivers", "all of the drivers",
        "every driver", "I'd like to select all drivers",
        "can you pick all drivers please", "send it to all drivers",
        "send it to all the drivers", "notify all drivers",
        "please select all drivers", "let's use everyone", "everyone",
        "everybody", "blast it to everybody", "ok all drivers",
        "um, all drivers", "just send it to everyone thanks",
    ]

    ONE_DRIVER = [
        "driver Susan", "driver: Susan", "driver, Susan", "driver - Susan",
        "Driver. Susan.", "please driver Susan", "can you select driver Susan",
        "I'd like driver Susan", "i want driver susan", "ok driver Susan",
        "yeah driver susan", "um driver susan", "DRIVER SUSAN",
        "change the driver to Susan", "could you set the driver to Susan",
        "driver Susan please", "driver Susan, thanks", "driver Susan.",
    ]

    def test_all_drivers_however_it_is_said(self):
        for p in self.ALL_DRIVERS:
            with self.subTest(said=p):
                self.assertEqual(kind(p), "SELECT_DRIVER", p)
                self.assertTrue(bot._ALL_SELECT_RE.match(payload(p) or ""),
                                f"{p!r} did not resolve to ALL")

    def test_one_named_driver_however_it_is_said(self):
        for p in self.ONE_DRIVER:
            with self.subTest(said=p):
                self.assertEqual(kind(p), "SELECT_DRIVER", p)
                self.assertIn("susan", (payload(p) or "").lower(), p)

    # Known limitation, deliberately not asserted: a name with a glued-on tail and
    # no comma to split at — "driver Susan for this one" — still finds nobody.
    # Trimming a trailing clause without a separator needs the split that ships
    # behind KRAB_FLUENCY_SUBMIT; guessing at it here would steal real values.

    def test_the_dispatcher(self):
        for p in ("dispatcher HighKage", "dispatcher: HighKage",
                  "please dispatcher HighKage", "I'd like to select dispatcher HighKage",
                  "can you set the dispatcher to HighKage",
                  "change the dispatcher to HighKage", "all dispatchers"):
            with self.subTest(said=p):
                self.assertEqual(kind(p), "SELECT_GROUP", p)

    def test_the_source(self):
        for p in ("source Instagram", "source: Instagram",
                  "this one came from Instagram", "they found us on Instagram",
                  "please set the source to Instagram"):
            with self.subTest(said=p):
                self.assertEqual(kind(p), "SELECT_SOURCE", p)

    def test_a_glued_on_tail_does_not_poison_the_name(self):
        """The capture runs to end of line and was then compared, whole, against
        the driver name — so one extra word found nobody."""
        drivers = [{"id": "d2", "driver_name": "Susan"},
                   {"id": "d1", "driver_name": "Kita"}]
        for p, want in (("Susan, send it", "Susan"), ("Kita, she's closest", "Kita"),
                        ("Susan.", "Susan"), ("Susan, thanks", "Susan")):
            with self.subTest(payload=p):
                got = bot._resolve_pick_name(p, drivers, "driver_name")
                self.assertEqual((got or {}).get("driver_name"), want)


class TrailingCourtesyIsNotPartOfTheValueTest(unittest.TestCase):

    def test_it_is_shaved_where_it_is_never_meaningful(self):
        for ek, v, want in (("col", "black please", "black"),
                            ("col", "white thanks", "white"),
                            ("fn", "Susan please", "Susan"),
                            ("ins", "Progressive thanks", "Progressive"),
                            ("vin", "1N4AL3AP0HC166043 please", "1N4AL3AP0HC166043")):
            with self.subTest(field=ek, value=v):
                self.assertEqual(bot._clean_inline_value(ek, v), want)

    def test_a_note_keeps_its_thanks(self):
        """The exclusions are the design, not an oversight."""
        for ek in ("issuer", "driver", "xtra"):
            with self.subTest(field=ek):
                self.assertEqual(bot._clean_inline_value(ek, "call ahead please"),
                                 "call ahead please")

    def test_an_address_keeps_its_oklahoma(self):
        """"OK" is a US state abbreviation and is in the courtesy vocabulary."""
        for ek in ("addr", "csz", "daddr", "dcsz"):
            with self.subTest(field=ek):
                self.assertEqual(bot._clean_inline_value(ek, "Tulsa OK"), "Tulsa OK")


class NothingLegitimateGetsStolenTest(unittest.TestCase):
    """The half that matters more. Every entry here is a real value a real
    operator types, drawn from the 43-item battery the design produced."""

    NOT_COMMANDS = [
        "Will Smith", "Ryan Driver", "Acme Group", "Tulsa OK",
        "Fort Lee, NJ 07024", "$1,500", "bob@x.com", "never mind that",
        "tell the driver the dispatch is running late",
        "all drivers are running late", "tell all drivers I said hi",
        "the drivers all called already",
    ]

    def test_they_never_actually_change_a_selection(self):
        """Asserted on BEHAVIOUR, not on classification. "the drivers all called
        already" opens with the noun and so classifies as a pick; what matters is
        that the prose guard then refuses it and the sentence stays a value."""
        drivers = [{"id": "d1", "driver_name": "Kita"},
                   {"id": "d2", "driver_name": "Susan"}]
        groups = [{"id": "g1", "group_name": "HighKage"}]
        for t in self.NOT_COMMANDS:
            with self.subTest(text=t):
                k, p = bot._classify_review_command(t, vin_pending=False)
                if k not in ("SELECT_DRIVER", "SELECT_GROUP", "SELECT_SOURCE"):
                    continue
                pool = groups if k == "SELECT_GROUP" else drivers
                key = "group_name" if k == "SELECT_GROUP" else "driver_name"
                self.assertIsNone(bot._resolve_pick_name(p, pool, key),
                                  f"{t!r} resolved to a real pick")
                self.assertFalse(bot._ALL_SELECT_RE.match(p or ""),
                                 f"{t!r} resolved to ALL")

    def test_a_note_is_still_a_note_on_both_paths(self):
        """The single-selection path has always known "driver note" is a note.
        The MULTI path did not, and widening the head gate makes that path
        reachable far more often."""
        for t in ("driver note call ahead",
                  "driver note call the dispatcher first",
                  "driver note tell dispatch we are late"):
            with self.subTest(text=t):
                self.assertEqual(kind(t), "FIELD_EDITS", t)
        self.assertIsNone(
            bot._parse_multi_select_line("driver note call the dispatcher first"))

    def test_prose_that_opens_with_a_noun_is_recognised_as_prose(self):
        """"driver needs to ring the bell twice" is a note. It used to open a
        picker over the top of it and the note was lost."""
        for t in ("driver needs to ring the bell twice",
                  "the driver said dispatch was late",
                  "driver has to call first"):
            with self.subTest(text=t):
                self.assertTrue(bot._payload_is_prose(
                    (payload(t) or t)), t)

    def test_a_client_named_like_a_filler_word_survives(self):
        """_FIELD_LEAD_FILLERS was grown for fluency. If the "is this value
        entirely filler" check had grown with it, these clients would be
        silently cleared — with a green Updated toast."""
        for name in ("Will", "Mark", "Just", "Guy", "Quick", "Still", "Wait",
                     "Hope", "Grace"):
            with self.subTest(name=name):
                self.assertEqual(bot._clean_inline_value("fn", name), name)

    def test_a_value_made_only_of_filler_is_still_discarded(self):
        for v in ("the", "to the", "a", "is"):
            with self.subTest(value=v):
                self.assertEqual(bot._clean_inline_value("fn", v), "")

    def test_the_separator_repair_only_touches_known_labels(self):
        """Anchoring the repair to a LABEL is the whole safety story: Lee, 1 and
        a are not labels, so these are untouched."""
        for t in ("Fort Lee, NJ 07024", "$1,500", "a@b.com", "3.5 hours"):
            with self.subTest(text=t):
                self.assertEqual(bot._norm_command_text(t), t)

    def test_a_price_is_not_an_address(self):
        """"150 plus toll" has a digit and was filed as the REGISTRATION address,
        then mirrored into the delivery address."""
        for v in ("150 plus toll", "150 dollars", "200 bucks", "150 flat"):
            with self.subTest(value=v):
                self.assertEqual(bot._structured_value_ek(v), "price")


class TheVinVerbsOnlyFireWhenAskedTest(unittest.TestCase):
    r"""_VIN_KEEP_RE searches for a bare \b(keep|same)\b ANYWHERE in the line —
    the loosest recogniser in the file. With no DMV question on screen it claimed
    ordinary notes."""

    LOOSE = ["Same Day Delivery", "keep the gate code handy",
             "keep the driver the same", "same address as before"]

    def test_they_are_silent_with_no_question_up(self):
        for t in self.LOOSE:
            with self.subTest(text=t):
                self.assertFalse(kind(t, vin_pending=False).startswith("VIN"), t)

    def test_they_still_work_when_the_question_is_up(self):
        self.assertEqual(kind("keep it", vin_pending=True), "VIN_KEEP")
        self.assertEqual(kind("use the new one", vin_pending=True), "VIN_USE")


class TheFluentPassCanOnlyUpgradeTest(unittest.TestCase):
    """The invariant. If pass 2 can ever overrule pass 1, nothing that works
    today is safe tomorrow."""

    SAMPLES = [
        "driver Susan", "price 150", "submit", "add insurance", "run vin",
        "driver note call ahead", "color black", "name John Doe",
        "phone 551-301-3737", "dispatcher HighKage", "source Instagram",
        "Same Day Delivery", "Will Smith", "$1,500", "all drivers",
        "I'd like to select all drivers", "the driver said dispatch was late",
    ]

    def test_a_strict_verdict_is_never_overruled(self):
        for t in self.SAMPLES:
            with self.subTest(text=t):
                strict = bot._classify_review_command_once(t, vin_pending=False)
                both = bot._classify_review_command(t, vin_pending=False)
                if strict[0] != "NONE":
                    self.assertEqual(both, strict,
                                     f"the fluent pass overruled strict on {t!r}")

    def test_the_normaliser_is_pure_and_total(self):
        for t in self.SAMPLES + ["", "   ", "x" * 500, "a\nb"]:
            with self.subTest(text=t[:30]):
                out = bot._norm_command_text(t)
                self.assertIsInstance(out, str)
                if t.strip():
                    self.assertTrue(out.strip(), f"{t!r} normalised to nothing")

    def test_it_bails_on_a_paste_rather_than_mangling_it(self):
        paste = "CHARLES JONES\n9 hibiscus Lane\nVIN: 1N4AL3AP0HC166043"
        self.assertEqual(bot._norm_command_text(paste), paste)
        long = "please " * 80
        self.assertEqual(bot._norm_command_text(long), long.strip())

    def test_it_can_be_switched_off_entirely(self):
        """A live bot needs an off switch that does not need a deploy."""
        with mock.patch.dict(os.environ, {"KRAB_FLUENCY": "0"}):
            self.assertEqual(bot._norm_command_text("I'd like to select all drivers"),
                             "I'd like to select all drivers")
            self.assertEqual(kind("all drivers"), "NONE")

    def test_the_normaliser_never_produces_a_stored_value(self):
        """Values are always sliced from the RAW text."""
        card = {}
        bot._apply_inline_review_text(card, "name Will Smith")
        self.assertEqual(card.get("name"), "Will Smith")


if __name__ == "__main__":
    unittest.main()
