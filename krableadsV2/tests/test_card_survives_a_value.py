r"""A value that looks like a command must not destroy the card.

Two ways it could, both verified against the live code before the fix:

1. `_bare_command_to_slash` asked "is this a cancel/restart phrase?" BEFORE
   asking "is a prompt currently waiting for a literal value?". "Temp Tag" —
   the product this business sells — matches the new-lead family, so typing it
   as a client source, an issuer note, or any field value rewrote the message to
   /restart and wiped everything:

       _cancel_restart_kind("Temp Tag")   -> 'restart'
       _cancel_restart_kind("new lead")   -> 'restart'
       _cancel_restart_kind("the order")  -> 'restart'
       _cancel_restart_kind("a tag")      -> 'restart'

2. `_ask_next_missing` re-read the live card and stashed a COPY, but never wrote
   the card back. `handle_missing_field` then answered from that copy and saved
   it — so anything changed while the question was on screen was reverted.

Cancel and restart are the same destructive action here (the docstring on
`_do_cancel_or_restart` says so) and there is no confirmation anywhere, which is
what makes an accidental match expensive.

The fix is not "never obey a command at a prompt" — the operator still has to be
able to leave. It is `strict`: the exact cancel/restart words get through; the
family made of ordinary nouns does not.

Run:  venv\Scripts\python.exe -m pytest tests/test_card_survives_a_value.py -q
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


class WordsThatAreAlsoValuesTest(unittest.TestCase):

    # Ordinary nouns. Every one is a plausible client source, note or field value.
    NOUNY = ["Temp Tag", "temp tag", "Temporary Tag", "new lead", "another client",
             "next customer", "a tag", "the order", "new entry", "add tag"]

    # Nobody types these into a field expecting them to be stored.
    COMMANDS = ["cancel", "stop", "never mind", "nvm", "abort", "quit", "discard",
                "restart", "start over", "reset", "redo", "scratch that"]

    def test_a_noun_never_wipes_a_card_at_a_prompt(self):
        for t in self.NOUNY:
            with self.subTest(typed=t):
                self.assertIsNone(bot._cancel_restart_kind(t, strict=True), t)

    def test_the_operator_can_still_leave(self):
        for t in self.COMMANDS:
            with self.subTest(typed=t):
                self.assertIsNotNone(bot._cancel_restart_kind(t, strict=True), t)

    def test_a_noun_still_starts_a_fresh_lead_when_nothing_is_open(self):
        """That behaviour is wanted — it is only dangerous mid-prompt."""
        for t in self.NOUNY:
            with self.subTest(typed=t):
                self.assertEqual(bot._cancel_restart_kind(t), "restart", t)

    def test_a_note_that_trails_off_is_not_a_cancel(self):
        """"never mind that" as a driver note used to wipe the card."""
        for t in ("never mind that", "forget the lead", "scrap that one",
                  "cancel it"):
            with self.subTest(typed=t):
                self.assertIsNone(bot._cancel_restart_kind(t, strict=True), t)

    def test_but_they_still_work_with_no_prompt_open(self):
        for t in ("cancel it", "scrap that one", "never mind that"):
            with self.subTest(typed=t):
                self.assertIsNotNone(bot._cancel_restart_kind(t), t)


class TheRewriterAsksAboutThePromptFirstTest(unittest.TestCase):

    class _Msg:
        def __init__(self, text):
            self.text = text
            self.entities = ()
            self.caption = None

    class _Update:
        def __init__(self, msg):
            self._m = msg

        @property
        def effective_message(self):
            return self._m

    class _Ctx:
        def __init__(self, ud):
            self.user_data = ud

    def _rewritten(self, text, user_data):
        import asyncio
        msg = self._Msg(text)
        asyncio.run(bot._bare_command_to_slash(self._Update(msg), self._Ctx(user_data)))
        return msg.text

    AWAITING = [
        {"tset_await": {"kind": "add_source"}},
        {"phase1_pending_edit_key": "ins"},
        {"missing_fields": ["color"]},
        {"fu": {"pending": "email"}},
    ]

    def test_a_value_survives_every_kind_of_open_prompt(self):
        for ud in self.AWAITING:
            for t in ("Temp Tag", "new lead", "the order", "Test", "Me"):
                with self.subTest(prompt=list(ud)[0], typed=t):
                    self.assertEqual(self._rewritten(t, dict(ud)), t)

    def test_cancel_still_escapes_every_kind_of_open_prompt(self):
        for ud in self.AWAITING:
            with self.subTest(prompt=list(ud)[0]):
                self.assertEqual(self._rewritten("cancel", dict(ud)), "/cancel")
                self.assertEqual(self._rewritten("restart", dict(ud)), "/restart")

    def test_with_nothing_open_the_shortcuts_all_still_work(self):
        self.assertEqual(self._rewritten("Temp Tag", {}), "/restart")
        self.assertEqual(self._rewritten("cancel", {}), "/cancel")


class ACaptionCarriesIntentTest(unittest.TestCase):
    """handle_media_in_any_state already reads the image; the caption was ignored."""

    class _Msg:
        text = None

        def __init__(self, caption):
            self.caption = caption

    class _Update:
        def __init__(self, m):
            self._m = m

        @property
        def effective_message(self):
            return self._m

    def test_a_photo_captioned_cancel(self):
        u = self._Update(self._Msg("cancel"))
        self.assertEqual(bot._cancel_restart_kind_from_update(u), "cancel")

    def test_a_photo_with_an_ordinary_caption(self):
        u = self._Update(self._Msg("here is the title"))
        self.assertIsNone(bot._cancel_restart_kind_from_update(u))


class AnsweringAQuestionDoesNotUndoTheAnswersAroundItTest(unittest.TestCase):

    def test_the_card_is_persisted_before_the_question_goes_up(self):
        """Without this, handle_missing_field answers from a stale copy and saves
        it — reverting whatever arrived while the question was on screen."""
        saved = []

        class FakeDB:
            def get_user_state(self, uid):
                return {"data": {"color": "", "pending_price": "$150"}}

            def set_user_state(self, uid, phase, data):
                saved.append(dict(data))

        class Msg:
            async def reply_text(self, *a, **k):
                class S:
                    chat_id = 1
                    message_id = 2
                return S()

        import asyncio

        class Ctx:
            user_data = {}

        with mock.patch.object(bot, "db", FakeDB()):
            asyncio.run(bot._ask_next_missing(Msg(), Ctx(), 1, ["color"], {}))

        self.assertTrue(saved, "the live card was never written back")
        self.assertEqual(saved[-1].get("pending_price"), "$150",
                         "an edit made during the question was lost")


if __name__ == "__main__":
    unittest.main()
