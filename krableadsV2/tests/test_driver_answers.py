r"""A driver's answer, in the words drivers actually use.

Getting this wrong is expensive in one direction only. Unrecognised text from a
driver falls through to `handle_idle_lead_start`, so a miss does not merely fail
to accept the offer — it loses the offer AND opens a blank lead form at someone
who is standing next to a car.

THE ORDER IS THE DESIGN. `handle_driver_word_answer` used to match the TEXT and
only then look up whether an offer was even open, which forces the vocabulary to
stay narrow: anything listed would otherwise hijack ordinary chat from anyone who
happened to type it. Settling the offer FIRST changes the economics completely —
"bet", "10-4" and a bare 👍 are unmistakable from a driver who is looking at an
offer this second, and reckless to claim from anybody else.

Two asymmetries worth keeping in mind while reading:

  * a DECLINE may carry a free tail, because the reason matters and losing it
    costs the driver the job: "no I'm in Newark till 6";
  * an ACCEPT may not be qualified. "accept the lead tomorrow" is a promise about
    later, and the bot cannot tell "on my way now" from "on my way after this
    one" — so a qualifier means tap the button.

Run:  venv\Scripts\python.exe -m pytest tests/test_driver_answers.py -q
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


def answer(text):
    """'accept' | 'decline' | None — the same three tests the handler makes."""
    a = bool(bot._DRIVER_ACCEPT_RE.match(text)) and not bot._DRIVER_QUALIFIER_RE.search(text)
    d = bool(bot._DRIVER_DECLINE_RE.match(text))
    if a and not d:
        return "accept"
    if d and not a:
        return "decline"
    return None


class YesTest(unittest.TestCase):

    PLAIN = ["accept", "accepted", "yes", "yep", "yeah", "yup", "ya", "y",
             "ok", "okay", "k", "sure", "mine", "got it", "on it", "claim"]
    TAKING_IT = ["i'll take it", "take it", "ill take it", "i'll grab it",
                 "i can take this", "i got this", "grabbing it"]
    IMPLIED = ["on my way", "omw", "heading out", "going now", "count me in",
               "i'm in", "i'm close", "i'm free", "i'll be there"]
    RADIO = ["10-4", "104", "copy", "copy that", "roger", "roger that", "bet",
             "say less", "wilco"]
    EMOJI = ["👍", "👌", "✅", "🙋", "💪"]
    WITH_A_TAIL = ["yes I'll grab it", "ok on my way now", "sure thing",
                   "yep got it", "accept please"]

    def test_all_of_them(self):
        for group in (self.PLAIN, self.TAKING_IT, self.IMPLIED, self.RADIO,
                      self.EMOJI, self.WITH_A_TAIL):
            for t in group:
                with self.subTest(said=t):
                    self.assertEqual(answer(t), "accept", t)

    def test_a_bare_thumbs_up_counts(self):
        r"""\b after an emoji can never match — a word boundary needs a word
        character beside it — so every emoji alternative was dead."""
        self.assertEqual(answer("👍"), "accept")


class NoTest(unittest.TestCase):

    PLAIN = ["decline", "declined", "no", "nope", "nah", "naw", "pass", "skip",
             "can't", "cant", "cannot", "won't", "unable"]
    WITH_A_REASON = ["no I'm in Newark till 6", "can't, i'm booked",
                     "i'm busy", "i'm out", "too far", "not available today",
                     "no too far from me", "pass, i'm tied up"]
    REDIRECT = ["different driver", "someone else", "give it to Kita", "not me"]
    EMOJI = ["❌", "🙅", "👎"]

    def test_all_of_them(self):
        for group in (self.PLAIN, self.WITH_A_REASON, self.REDIRECT, self.EMOJI):
            for t in group:
                with self.subTest(said=t):
                    self.assertEqual(answer(t), "decline", t)

    def test_the_reason_may_ride_along(self):
        """Losing "no I'm in Newark till 6" costs the driver the offer and hands
        them a blank lead form."""
        self.assertEqual(answer("no I'm in Newark till 6"), "decline")


class NeitherTest(unittest.TestCase):
    """Everything a driver says that is not an answer must pass through
    untouched — it is their normal conversation."""

    ORDINARY = ["what's the address", "how much", "who is this",
                "the client called", "running late for the other one",
                "tell them 20 minutes", "hello", "thanks", "yesterday",
                "nothing yet", "password is 1234", "im at the gate",
                "no answer at the door", "customer says no one is home"]

    QUALIFIED = ["accept the lead tomorrow", "yes but after this one",
                 "ok if nobody else takes it", "i'll take it later",
                 "on my way in 20", "maybe, depends", "yes when I finish this"]

    def test_ordinary_chat_passes_through(self):
        for t in self.ORDINARY:
            with self.subTest(said=t):
                self.assertIsNone(answer(t), t)

    def test_a_qualified_accept_is_not_an_accept(self):
        """The bot cannot tell "on my way now" from "on my way after this one",
        so a qualifier means tap the button."""
        for t in self.QUALIFIED:
            with self.subTest(said=t):
                self.assertNotEqual(answer(t), "accept", t)

    def test_a_decline_with_a_reason_is_still_a_decline(self):
        """The qualifier rule applies to accepts only — "no, not now" is a no."""
        for t in ("no not now", "can't, maybe later", "pass, tomorrow maybe"):
            with self.subTest(said=t):
                self.assertEqual(answer(t), "decline", t)


class TheOfferIsSettledBeforeTheWordsAreReadTest(unittest.TestCase):

    def test_the_source_says_so(self):
        """Reading the text first is what forces a narrow vocabulary. This test
        pins the order, because reverting it would quietly make every generous
        word above a hijack risk for every user of the bot."""
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        body = src.split("async def handle_driver_word_answer", 1)[1]
        body = body.split("\nasync def ", 1)[0]
        offer = body.index("get_driver_pending_assignment")
        words = body.index("_DRIVER_ACCEPT_RE.match")
        self.assertLess(offer, words,
                        "the offer must be confirmed before the text is read")

    def test_it_still_declines_to_guess_when_nothing_is_open(self):
        body = (ROOT / "bot.py").read_text(encoding="utf-8")
        body = body.split("async def handle_driver_word_answer", 1)[1]
        self.assertIn("nothing open", body)


class QuestionsThatUnderstandAnAnswerTest(unittest.TestCase):

    def test_the_receipt_confirmation_has_words(self):
        """It says "Please confirm this is the correct lead" and registered only
        buttons, so a typed or spoken "yes" reached nothing at all."""
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        state = src.split("STATE_WAITING_RECEIPT_CONFIRM: [", 1)[1].split("],", 1)[0]
        self.assertIn("handle_receipt_confirm_words", state)
        self.assertTrue(hasattr(bot, "handle_receipt_confirm_words"))

    def test_a_spoken_answer_is_transcribed_before_anything_reads_it(self):
        """PTB runs handler groups lowest-first. The transcriber sat at -5 while
        the follow-up reply handler sat at -45, so a SPOKEN answer was
        transcribed only after the handler that wanted it had already passed."""
        import re
        src = (ROOT / "bot.py").read_text(encoding="utf-8")

        def registered_group(name):
            """The group= that follows this handler's registration.

            Read forward from the name rather than matching the block's shape —
            every add_handler call in this file is laid out differently.
            """
            for m in re.finditer(re.escape(name), src):
                nxt = re.search(r"group=(-?\d+)", src[m.end():m.end() + 300])
                if nxt:
                    return int(nxt.group(1))
            return None

        voice = registered_group("_global_voice_to_text")
        self.assertIsNotNone(voice, "voice transcriber is not registered")
        for name in ("handle_cf_edit_reply", "handle_driver_word_answer",
                     "_bare_command_to_slash"):
            with self.subTest(handler=name):
                grp = registered_group(name)
                self.assertIsNotNone(grp, f"{name} is not registered")
                self.assertLess(voice, grp, f"voice must run before {name}")

    def test_a_follow_up_reminder_can_be_answered_where_it_was_read(self):
        """The keyboard posts its Edit prompt into the dispatch GROUP, and
        ChatType.PRIVATE refused the answer typed right underneath it."""
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        block = src.split("handle_cf_edit_reply),", 1)[0][-400:]
        self.assertNotIn("ChatType.PRIVATE", block)


if __name__ == "__main__":
    unittest.main()
