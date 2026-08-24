r"""The client source can be said, typed or tapped — like everything else.

Reported: "client source doesnt work with voice note and parsing". Four gaps:

  * the card calls it "Client source" and that exact wording was the one the
    command regex did not know;
  * "set source to Instagram" kept the "to" and matched nothing;
  * a bare source name ("Facebook") meant nothing at all; and
  * while the source picker itself was on screen, typing or saying the name got
    "tap a button above" — the picker was tap-only.

Voice arrives here as text (group -4 transcribes before any handler), so what is
tested is the parsing.

Run:  venv\Scripts\python.exe -m pytest tests/test_client_source_voice.py -q
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

SOURCES = [{"id": "s1", "label": "Facebook"},
           {"id": "s2", "label": "Instagram"},
           {"id": "s3", "label": "Word of Mouth"}]


def _db():
    db = mock.MagicMock()
    db.get_contact_info_sources.return_value = SOURCES
    db.get_contact_info_source_by_id.side_effect = (
        lambda i: next((x for x in SOURCES if x["id"] == i), None))
    db.get_all_groups.return_value = [{"id": "g1", "group_name": "HighKage", "is_active": True}]
    db.get_all_drivers.return_value = [{"id": "d1", "driver_name": "Kita", "is_active": True}]
    return db


class SayingItOnTheCardTest(unittest.TestCase):

    def _classify(self, text):
        with mock.patch.object(bot, "db", _db()):
            return bot._classify_review_command(text)

    def test_the_wording_the_card_itself_uses(self):
        """"Client source" is what the button says, and it was the one missing."""
        for line in ("client source Facebook", "client's source Facebook",
                     "clients source Facebook"):
            with self.subTest(line=line):
                self.assertEqual(("SELECT_SOURCE", "Facebook"), self._classify(line))

    def test_the_other_wordings(self):
        for line in ("source Facebook", "contact source Facebook",
                     "lead source Facebook", "src Facebook", "came from Facebook",
                     "origin Facebook"):
            with self.subTest(line=line):
                self.assertEqual(("SELECT_SOURCE", "Facebook"), self._classify(line))

    def test_a_joining_word_is_not_part_of_the_name(self):
        for line in ("set source to Instagram", "source is Instagram",
                     "change the source to Instagram", "update client source Instagram",
                     "switch source to Instagram"):
            with self.subTest(line=line):
                self.assertEqual(("SELECT_SOURCE", "Instagram"), self._classify(line))

    def test_just_the_source_name_on_its_own(self):
        for line in ("Facebook", "facebook", "Instagram", "Word of Mouth"):
            with self.subTest(line=line):
                kind, payload = self._classify(line)
                self.assertEqual("SELECT_SOURCE", kind, line)

    def test_dictation_running_the_words_together(self):
        self.assertEqual("SELECT_SOURCE", self._classify("face book")[0])

    def test_the_new_verbs_reach_the_other_pickers_too(self):
        self.assertEqual(("SELECT_DRIVER", "Kita"), self._classify("change driver to Kita"))
        self.assertEqual(("SELECT_GROUP", "HighKage"),
                         self._classify("change dispatcher to HighKage"))


class ItActuallyLandsTest(unittest.TestCase):

    def _apply(self, payload):
        card = {}
        with mock.patch.object(bot, "db", _db()):
            ok, note = asyncio.run(
                bot._apply_selection("SELECT_SOURCE", payload, card, 1))
        return ok, note, card

    def test_the_source_is_set(self):
        ok, _, card = self._apply("Facebook")
        self.assertTrue(ok)
        self.assertEqual("Facebook", card.get("selected_source_label"))

    def test_run_together_words_still_land(self):
        _, _, card = self._apply("face book")
        self.assertEqual("Facebook", card.get("selected_source_label"))

    def test_a_multi_word_source(self):
        _, _, card = self._apply("word of mouth")
        self.assertEqual("Word of Mouth", card.get("selected_source_label"))

    def test_something_that_is_not_a_source_is_reported(self):
        ok, note, card = self._apply("Twitter")
        self.assertFalse(ok)
        self.assertIn("Twitter", note)
        self.assertIsNone(card.get("selected_source_label"))


class SayingItWhileThePickerIsUpTest(unittest.TestCase):
    """The post-dispatch picker was tap-only."""

    def _send(self, text, state="select_contact_source"):
        msg = mock.MagicMock()
        msg.text = text
        msg.chat_id = 1
        msg.reply_text = mock.AsyncMock()
        update = mock.MagicMock()
        update.effective_message = msg
        update.message = msg
        update.effective_user = mock.MagicMock(id=7)
        update.effective_chat = mock.MagicMock(id=1)
        db = _db()
        db.get_user_state.return_value = {"state": state, "data": {"lead_id": "L1"}}
        picked = {}
        async def _fake(u, c):
            picked["data"] = u.callback_query.data
            return None
        with mock.patch.object(bot, "db", db), \
                mock.patch.object(bot, "handle_contact_source_selection", _fake), \
                mock.patch.object(bot, "_send_vanishing", mock.AsyncMock()):
            asyncio.run(bot.handle_select_state_text(update, mock.MagicMock()))
        return picked.get("data"), msg.reply_text

    def test_saying_the_name_picks_it(self):
        data, _ = self._send("Facebook")
        self.assertEqual("contact_source_s1", data)

    def test_a_multi_word_source_picks_it(self):
        data, _ = self._send("word of mouth")
        self.assertEqual("contact_source_s3", data)

    def test_a_near_miss_still_resolves(self):
        data, _ = self._send("instagran")
        self.assertEqual("contact_source_s2", data)

    def test_something_unknown_says_so_instead_of_going_quiet(self):
        data, reply = self._send("Twitter")
        self.assertIsNone(data)
        reply.assert_awaited()
        self.assertIn("client source", reply.await_args[0][0].lower())


class NothingElseWasHijackedTest(unittest.TestCase):
    """A bare word only counts when it IS a configured source."""

    def _classify(self, text):
        with mock.patch.object(bot, "db", _db()):
            return bot._classify_review_command(text)

    def test_a_persons_name_is_not_a_source(self):
        self.assertEqual(("NONE", None), self._classify("John Damian"))

    def test_a_colour_is_not_a_source(self):
        self.assertEqual(("NONE", None), self._classify("black"))

    def test_a_field_edit_still_wins(self):
        self.assertEqual("FIELD_EDITS", self._classify("price 150")[0])

    def test_the_strict_matcher_needs_a_real_label(self):
        with mock.patch.object(bot, "db", _db()):
            self.assertIsNone(bot._source_by_exact_label("Twitter"))
            self.assertIsNone(bot._source_by_exact_label("fa"))
            self.assertIsNotNone(bot._source_by_exact_label("FACEBOOK"))


if __name__ == "__main__":
    unittest.main()
