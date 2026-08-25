r"""The two rules that ship dark, and why.

Everything else in the fluency work reads a message that already contains command
vocabulary. These two act on messages that contain none:

    "looks good send it out"   two instructions in one sentence, the second of
                               which SUBMITS — and submitting is irreversible
    "give it to Susan"         no noun at all; the only evidence is that Susan
                               happens to be on the driver roster

Both can take a real value away from an operator. "looks good send it out" is
genuinely indistinguishable from a driver note that ends that way, and a rule
that reads a bare name as a command is exactly how a client called Will Smith
stops being a client.

So each has its own switch and both are OFF by default:

    KRAB_FLUENCY_SUBMIT=1      the trailing-submit split
    KRAB_FLUENCY_BARENAME=1    the no-noun pick

The first test in each class is the one that matters most: with the flag unset,
nothing happens at all.

Run:  venv\Scripts\python.exe -m pytest tests/test_fluency_flags.py -q
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

DRIVERS = [
    {"id": "d1", "driver_name": "Susan", "is_active": True},
    {"id": "d2", "driver_name": "Ana Lopez", "is_active": True},
    {"id": "d3", "driver_name": "Marcus Reed", "is_active": True},
]
GROUPS = [{"id": "g1", "group_name": "HighKage", "is_active": True}]


class TrailingSubmitTest(unittest.TestCase):

    def _split(self, text, on=True):
        env = {"KRAB_FLUENCY_SUBMIT": "1"} if on else {"KRAB_FLUENCY_SUBMIT": "0"}
        with mock.patch.dict(os.environ, env):
            return bot._split_trailing_submit(text)

    def test_it_does_nothing_without_its_flag(self):
        for t in ("looks good send it out", "that's everything, ship it"):
            with self.subTest(said=t):
                self.assertIsNone(self._split(t, on=False), t)

    def test_it_splits_a_comment_from_a_dispatch(self):
        for t, head, tail in (
            ("looks good send it out", "looks good", "send it out"),
            ("that's everything, ship it", "that's everything", "ship it"),
            ("I think we're good, go ahead", "I think we're good", "go ahead"),
        ):
            with self.subTest(said=t):
                self.assertEqual(self._split(t), (head, tail))

    def test_a_real_edit_survives_the_split(self):
        """"price 150 send it" should set the price AND submit."""
        self.assertEqual(self._split("price 150 send it"), ("price 150", "send it"))

    def test_a_note_never_splits(self):
        """A note is allowed to end with "send it out" — that is what makes this
        rule dangerous enough to need a flag."""
        for t in ("driver note call ahead and send it out",
                  "issuer note wait for them then send it out"):
            with self.subTest(said=t):
                self.assertIsNone(self._split(t), t)

    def test_a_long_sentence_never_splits(self):
        self.assertIsNone(self._split(
            "tell the client we send it out tomorrow morning at nine sharp ok thanks"))

    def test_a_bare_dispatch_has_no_head_to_split_off(self):
        for t in ("send it out", "submit", "ship it"):
            with self.subTest(said=t):
                self.assertIsNone(self._split(t), t)

    def test_a_paste_never_splits(self):
        self.assertIsNone(self._split("CHARLES JONES\n9 hibiscus Lane\nsend it out"))


class BareNamePickTest(unittest.TestCase):

    def _pick(self, text, card=None, on=True):
        env = {"KRAB_FLUENCY_BARENAME": "1" if on else "0"}
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(bot, "_get_all_drivers_cached", return_value=DRIVERS), \
             mock.patch.object(bot.db, "get_all_groups", return_value=GROUPS):
            return bot._bare_name_pick(text, card or {})

    def test_it_does_nothing_without_its_flag(self):
        self.assertIsNone(self._pick("give it to Susan", on=False))

    def test_a_name_with_no_noun_around_it(self):
        for t in ("give it to Susan", "Susan should take this", "Susan",
                  "let's go with Susan"):
            with self.subTest(said=t):
                self.assertEqual(self._pick(t), ("SELECT_DRIVER", "Susan"))

    def test_a_dispatcher_the_same_way(self):
        self.assertEqual(self._pick("send this one to HighKage"),
                         ("SELECT_GROUP", "HighKage"))

    def test_it_refuses_the_fuzzy_match_that_would_guess(self):
        """_match_name("an", …) returns "Ana Lopez". A rule acting on a message
        with no command vocabulary cannot afford that rung."""
        for t in ("an", "a", "on it", "so"):
            with self.subTest(said=t):
                self.assertIsNone(self._pick(t), t)

    def test_it_never_overwrites_a_choice_already_made(self):
        self.assertIsNone(self._pick("give it to Susan",
                                     {"selected_driver_ids": ["d9"]}))
        self.assertIsNone(self._pick("send this one to HighKage",
                                     {"selected_group_id": "g9"}))

    def test_a_stranger_is_not_a_pick(self):
        """The whole risk in one test: a client's name must stay a client's name."""
        for t in ("Will Smith", "Charles Jones", "Dispatch Solutions LLC",
                  "the client called", "no answer at the door"):
            with self.subTest(said=t):
                self.assertIsNone(self._pick(t), t)

    def test_a_long_message_is_not_a_pick(self):
        self.assertIsNone(self._pick(
            "Susan called and said the client at the second address is not home yet"))

    def test_a_word_naming_two_pools_is_ambiguous_not_a_guess(self):
        both = [{"id": "g2", "group_name": "Susan", "is_active": True}]
        with mock.patch.dict(os.environ, {"KRAB_FLUENCY_BARENAME": "1"}), \
             mock.patch.object(bot, "_get_all_drivers_cached", return_value=DRIVERS), \
             mock.patch.object(bot.db, "get_all_groups", return_value=both):
            self.assertIsNone(bot._bare_name_pick("give it to Susan", {}))

    # Known limitation, deliberate: "put Ana on it" does not resolve. The prefix
    # rung has a four-character floor, and "Ana" is three — lowering it is how a
    # three-letter word in an ordinary sentence starts picking drivers.


class BothAreLastResortsTest(unittest.TestCase):

    def test_they_run_only_after_everything_else_declines(self):
        """Neither may take a message away from a rule that already understood
        it. The source order is what guarantees that."""
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        body = src.split("async def _interpret_review_command", 1)[1]
        body = body.split("\nasync def ", 1)[0]
        classify = body.index("_classify_review_command(")
        for name in ("_split_trailing_submit", "_bare_name_pick"):
            with self.subTest(rule=name):
                self.assertLess(classify, body.index(name),
                                f"{name} must run after the classifier")

    def test_the_flags_are_documented(self):
        doc = (ROOT / "WIRING.md").read_text(encoding="utf-8")
        for flag in ("KRAB_FLUENCY", "KRAB_FLUENCY_SUBMIT", "KRAB_FLUENCY_BARENAME"):
            with self.subTest(flag=flag):
                self.assertIn(flag, doc)

    def test_the_whole_layer_has_one_off_switch(self):
        with mock.patch.dict(os.environ, {"KRAB_FLUENCY": "0"}):
            self.assertEqual(bot._norm_command_text("I'd like to select all drivers"),
                             "I'd like to select all drivers")


if __name__ == "__main__":
    unittest.main()
