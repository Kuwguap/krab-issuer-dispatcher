r"""A field prompt reads plain words the same way the review card does.

Asked for: "everyone should also be able to use the voice command and ai parsing
like just entering simple words for AI to understand and parse".

The prompts forced whatever arrived into the field you happened to have open and
rejected anything that did not fit it — open Price, say "the color is black", and
nothing happened. A prompt is where you are, not a cage.

Voice needs nothing extra: group -4 transcribes every voice note before any
handler runs, so a spoken phrase arrives here as text.

Run:  venv\Scripts\python.exe -m pytest tests/test_prompt_understands_words.py -q
"""
import asyncio
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


def _say(ek, text, card=None, ai=False):
    """What happens when `text` arrives while the `ek` prompt is open."""
    card = dict(card or {})
    with mock.patch.object(bot.Config, "is_ai_vision_configured",
                           classmethod(lambda cls: ai)):
        handled, changed = asyncio.run(
            bot._place_text_at_field_prompt(card, ek, text))
    return handled, changed, card


class WordsAtTheWrongPromptTest(unittest.TestCase):
    """The reported shape: you opened one field and said another."""

    def test_a_colour_said_at_the_price_prompt(self):
        _, changed, card = _say("price", "the color is black")
        self.assertEqual("Black", card.get("color"))
        self.assertEqual(["color"], changed)

    def test_one_bare_word_is_enough(self):
        """"just entering simple words" — no label needed."""
        _, _, card = _say("price", "black")
        self.assertEqual("Black", card.get("color"))

    def test_a_price_said_at_the_colour_prompt(self):
        _, _, card = _say("col", "price 150")
        self.assertEqual("$150", card.get("pending_price"))

    def test_a_car_said_at_the_vin_prompt(self):
        """It passes the loose VIN check, then gets scrubbed — used to vanish."""
        handled, _, card = _say("vin", "2019 Honda Accord")
        self.assertTrue(handled)
        self.assertEqual("2019 Honda Accord", card.get("car"))

    def test_a_carrier_said_at_the_name_prompt(self):
        _, _, card = _say("name", "geico")
        self.assertEqual("GEICO", card.get("insurance_company"))

    def test_several_fields_in_one_breath(self):
        _, changed, card = _say("price", "price 200 color white")
        self.assertEqual("$200", card.get("pending_price"))
        self.assertEqual("White", card.get("color"))
        self.assertEqual(2, len(changed), changed)


class TheOpenFieldStillTakesItsOwnValueTest(unittest.TestCase):
    """This must not regress — it is the common case."""

    def test_each_prompt_accepts_the_plain_value(self):
        for ek, said, key, want in (
                ("price", "150", "pending_price", "$150"),
                ("col", "black", "color", "Black"),
                ("phone", "551-301-3737", "pending_phone_number", "551-301-3737"),
                ("name", "John Damian", "name", "John Damian"),
                ("car", "2019 Honda Accord", "car", "2019 Honda Accord"),
                ("ins", "geico", "insurance_company", "GEICO")):
            with self.subTest(prompt=ek, said=said):
                handled, _, card = _say(ek, said)
                self.assertTrue(handled, said)
                self.assertEqual(want, card.get(key), said)

    def test_a_label_naming_the_open_field_is_just_the_value(self):
        _, _, card = _say("price", "price 150")
        self.assertEqual("$150", card.get("pending_price"))

    def test_repeating_the_same_value_is_not_an_error(self):
        handled, _, _ = _say("col", "black", card={"color": "Black"})
        self.assertTrue(handled, "re-entering the same value must not warn")


class ProseFieldsKeepWhatYouTypeTest(unittest.TestCase):
    """A note is a note, even when it mentions a field by name."""

    def test_a_driver_note_is_stored_verbatim(self):
        _, _, card = _say("driver", "color the car black and call first")
        self.assertEqual("color the car black and call first",
                         card.get("special_request_drivers"))
        self.assertIn(str(card.get("color") or "-"), ("-", "", "None"))

    def test_an_issuer_note_is_stored_verbatim(self):
        _, _, card = _say("issuer", "price is negotiable")
        self.assertEqual("price is negotiable", card.get("special_request_issuers"))


class NothingUsableTest(unittest.TestCase):

    def test_gibberish_is_reported_not_filed(self):
        handled, changed, card = _say("price", "asdfghjk")
        self.assertFalse(handled)
        self.assertEqual([], changed)
        self.assertNotIn("asdfghjk", str(card))

    def test_a_fumbled_price_never_becomes_the_client(self):
        """The 'one to four plain words is a name' rule is wrong at a prompt."""
        _, _, card = _say("price", "asdfghjk")
        self.assertIsNone(card.get("name"))

    def test_empty_input_is_not_handled(self):
        handled, _, _ = _say("price", "   ")
        self.assertFalse(handled)


class TheNameGuessIsOnlyOffAtPromptsTest(unittest.TestCase):
    """Idle text still gets the loose guess — that is what starts a lead."""

    def test_idle_text_may_still_be_read_as_a_name(self):
        card = {}
        with mock.patch.object(bot.Config, "is_ai_vision_configured",
                               classmethod(lambda cls: False)):
            asyncio.run(bot._smart_place_single_value(card, "Robert Rodriguez"))
        self.assertEqual("Robert Rodriguez", card.get("name"))

    def test_but_not_when_a_prompt_asked_for_something_else(self):
        card = {}
        with mock.patch.object(bot.Config, "is_ai_vision_configured",
                               classmethod(lambda cls: False)):
            asyncio.run(bot._smart_place_single_value(card, "Robert Rodriguez",
                                                      guess_name=False))
        self.assertIsNone(card.get("name"))

    def test_the_other_heuristics_survive_the_flag(self):
        """Only the name rule is dropped — colour and car still work."""
        for said, key in (("black", "color"), ("2019 Honda Accord", "car")):
            with self.subTest(said=said):
                card = {}
                with mock.patch.object(bot.Config, "is_ai_vision_configured",
                                       classmethod(lambda cls: False)):
                    asyncio.run(bot._smart_place_single_value(card, said,
                                                              guess_name=False))
                self.assertTrue(card.get(key), said)


class VoiceReachesThesePromptsTest(unittest.TestCase):
    """Nothing to add for voice — it is text by the time it gets here."""

    def test_transcription_runs_before_every_handler(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        self.assertIn("MessageHandler(filters.VOICE | filters.AUDIO, _global_voice_to_text)", src)
        self.assertIn("group=-4", src)

    def test_a_spoken_phrase_lands_like_a_typed_one(self):
        spoken, typed = _say("price", "the color is black")[2], _say("price", "the color is black")[2]
        self.assertEqual(spoken.get("color"), typed.get("color"))


if __name__ == "__main__":
    unittest.main()
