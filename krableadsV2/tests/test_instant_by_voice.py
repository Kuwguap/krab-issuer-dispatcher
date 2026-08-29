r"""Turning Instant Tag on by saying so, and All Drivers really being default.

Asked for: a list of phrases that should activate Instant Tag by text or voice
("cash payment", "prepay", "collect cash", "instant dispatch", …), and: "all
drivers is toggled on for instant tags but when instant tag is toggled on the
driver section still says auto instead of all drivers (the default)".

Run:  venv\Scripts\python.exe -m pytest tests/test_instant_by_voice.py -q
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

import bot  # noqa: E402

SRC = (ROOT / "bot.py").read_text(encoding="utf-8")

# Every phrase the operator listed, verbatim.
ASKED_FOR = [
    "Cash payment", "Collect cash", "Prepay tag", "Cash", "Payment method cash",
    "Prepay", "Instant tag on", "Prepay on", "Submit cash on", "Prepayment",
    "Temp tag instant", "Instant temp tag", "Activate instant", "Instant tag",
    "Skip dispatch", "Instant dispatch", "Activate instant mode",
]


class EveryPhraseTurnsItOnTest(unittest.TestCase):

    def test_the_whole_list(self):
        for phrase in ASKED_FOR:
            self.assertIs(True, bot._instant_intent(phrase), phrase)

    def test_case_and_punctuation_do_not_matter(self):
        for phrase in ("CASH PAYMENT", "cash payment.", "  Prepay!  ", "instant tag"):
            self.assertIs(True, bot._instant_intent(phrase), phrase)

    def test_a_natural_lead_in_still_reads(self):
        for phrase in ("turn on instant tag", "make it cash", "use prepay"):
            self.assertIs(True, bot._instant_intent(phrase), phrase)

    def test_it_can_be_turned_off_too(self):
        for phrase in ("instant tag off", "no instant", "prepay off",
                       "cancel instant", "normal dispatch"):
            self.assertIs(False, bot._instant_intent(phrase), phrase)


class ItNeverFiresOnOrdinaryTextTest(unittest.TestCase):
    """"cash" is an instruction on its own and just a word inside a lead."""

    def test_a_word_inside_a_sentence_is_not_a_command(self):
        for phrase in ("250 total cash",
                       "the client pays cash on delivery to the driver",
                       "name John Cash", "cash app", "he asked about prepay rules"):
            self.assertIsNone(bot._instant_intent(phrase), phrase)

    def test_blank_input_is_not_a_command(self):
        for phrase in ("", "   ", None):
            self.assertIsNone(bot._instant_intent(phrase))

    def test_a_whole_pasted_lead_is_guarded_in_the_handler(self):
        """The handler applies the same paste guard the insurance switch uses."""
        body = SRC.split("async def handle_phase1_review_message", 1)[1]
        body = body.split("\nasync def ", 1)[0]
        self.assertIn("_itag = _instant_intent(text)", body)
        self.assertIn("if _itag is not None and not _looks_like_multifield_block(text):",
                      body)


class TheChatLayerCanTurnItOnTooTest(unittest.IsolatedAsyncioTestCase):
    """The model reads every review message BEFORE the phrase list does.

    Without a tool of its own it would have classified "cash payment" as
    something else — most likely a field edit — and the phrase list below it
    would never have run. So the model gets the same switch the button has.
    """

    def test_the_router_offers_the_tool(self):
        from utils import nl_router
        names = [t["function"]["name"] for t in nl_router.TOOLS]
        self.assertIn("set_instant_tag", names)

    def test_the_card_accepts_it(self):
        self.assertIn("set_instant_tag", bot._AI_CARD_TOOLS)

    async def _run(self, enable, state):
        toasts = []
        with mock.patch.object(bot, "db", mock.MagicMock()), \
                mock.patch.object(bot, "_instant_all_drivers_enabled", lambda: True), \
                mock.patch.object(
                    bot, "_ai_card_housekeeping",
                    mock.AsyncMock(side_effect=lambda *a, **k: toasts.append(k.get("toast")))):
            out = await bot._run_ai_card_tool(
                mock.MagicMock(), mock.MagicMock(), 7, state,
                "set_instant_tag", {"enable": enable})
        return out, toasts

    async def test_the_model_turning_it_on_sets_the_same_defaults(self):
        state = {"price": "$250"}
        _, toasts = await self._run(True, state)
        self.assertTrue(state["instant_tag"])
        self.assertEqual("All Drivers", state["selected_driver_names"])
        self.assertEqual("$200", state["driver_amount"])
        self.assertIn("All Drivers is the default", toasts[0])

    async def test_the_model_turning_it_off(self):
        state = {"instant_tag": True}
        _, toasts = await self._run(False, state)
        self.assertFalse(state["instant_tag"])
        self.assertIn("off", toasts[0].lower())


class AllDriversIsTheDefaultWhenItIsOnTest(unittest.TestCase):

    def _driver_box(self, state):
        kb = bot._build_review_keyboard_with_selections(state)
        return [b.text for row in kb.inline_keyboard for b in row
                if b.text.startswith("🚗")][0]

    def test_the_card_says_all_drivers_when_the_setting_is_on(self):
        with mock.patch.object(bot, "_instant_all_drivers_enabled", lambda: True):
            state = {"instant_tag": True}
            bot._apply_instant_driver_default(state)
            self.assertEqual("All Drivers", state["selected_driver_names"])
            self.assertEqual("🚗 All Drivers", self._driver_box(state))

    def test_the_note_says_so_too(self):
        with mock.patch.object(bot, "_instant_all_drivers_enabled", lambda: True):
            note = bot._instant_toggle_note({"instant_tag": True, "driver_amount": "$200"})
        self.assertIn("All Drivers is the default", note)
        self.assertIn("$200", note)
        self.assertNotIn("All Drivers is off", note)

    def test_with_the_setting_off_it_is_still_one_driver(self):
        with mock.patch.object(bot, "_instant_all_drivers_enabled", lambda: False):
            state = {"instant_tag": True, "selected_driver_names": "All Drivers"}
            bot._apply_instant_driver_default(state)
            self.assertEqual("auto", state["selected_driver_names"])
            note = bot._instant_toggle_note(state)
        self.assertIn("All Drivers is off for this lead", note)

    def test_a_driver_already_chosen_is_never_overridden(self):
        for broadcast in (True, False):
            with mock.patch.object(bot, "_instant_all_drivers_enabled", lambda: broadcast):
                state = {"instant_tag": True, "selected_driver_names": "Kita"}
                bot._apply_instant_driver_default(state)
            self.assertEqual("Kita", state["selected_driver_names"], str(broadcast))

    def test_the_missing_amount_warning_survives(self):
        with mock.patch.object(bot, "_instant_all_drivers_enabled", lambda: True):
            note = bot._instant_toggle_note({"instant_tag": True})
        self.assertIn("No amount yet", note)

    def test_all_three_ways_in_share_the_rules(self):
        """Button, phrase list and AI tool all defer to the same two helpers,
        so the driver default and the wording cannot drift between them."""
        self.assertEqual(3, SRC.count("_apply_instant_driver_default(state_data)"))
        self.assertEqual(3, SRC.count("_instant_toggle_note(state_data)"))
        # And none of them re-derives the note inline the way the button used to.
        self.assertNotIn("All Drivers is off for this lead.\"\n", SRC)


if __name__ == "__main__":
    unittest.main()
