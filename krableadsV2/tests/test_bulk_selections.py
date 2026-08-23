"""Several picks in one message: "driver Kita dispatch HighKage".

The single-selection regexes are greedy — the first noun swallowed everything after
it, so the bot looked for a driver literally called "Kita dispatch HighKage" and the
dispatcher was lost.

Run:  venv\\Scripts\\python.exe -m pytest tests/test_bulk_selections.py -q
"""
import asyncio
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy-key")

import utils.database as udb  # noqa: E402

if "bot" not in sys.modules:
    udb.Database = mock.MagicMock()
import bot  # noqa: E402

GROUPS = [{"id": "g1", "group_name": "HighKage", "is_active": True}]
DRIVERS = [{"id": "d1", "driver_name": "Kita", "is_active": True}]
SOURCES = [{"id": "s1", "label": "Facebook", "is_active": True}]


def _apply(text):
    """Run the real command interpreter; return (state, toasts, pickers_opened)."""
    msg = SimpleNamespace(text=text, chat_id=1, delete=mock.AsyncMock(),
                          reply_text=mock.AsyncMock())
    update = SimpleNamespace(
        message=msg, effective_message=msg,
        effective_chat=SimpleNamespace(id=1, type="private"),
        effective_user=SimpleNamespace(id=7, username="tester"))
    ctx = SimpleNamespace(user_data={}, bot=mock.AsyncMock(),
                          application=SimpleNamespace(handlers={}))
    state, toasts, pickers = {}, [], []
    fake_db = mock.MagicMock()
    fake_db.get_all_groups.return_value = GROUPS
    fake_db.get_contact_info_sources.return_value = SOURCES
    with mock.patch.object(bot, "db", fake_db), \
            mock.patch.object(bot, "_get_all_drivers_cached",
                              mock.MagicMock(return_value=DRIVERS)), \
            mock.patch.object(bot, "_get_suspended_driver_ids",
                              mock.MagicMock(return_value=set())), \
            mock.patch.object(bot, "_update_review_message_text", mock.AsyncMock()), \
            mock.patch.object(bot, "_cleanup_voice_echo", mock.AsyncMock()), \
            mock.patch.object(bot, "_open_driver_picker",
                              mock.AsyncMock(side_effect=lambda c: pickers.append("driver"))), \
            mock.patch.object(bot, "_open_group_picker",
                              mock.AsyncMock(side_effect=lambda c: pickers.append("dispatcher"))), \
            mock.patch.object(bot, "_open_source_picker",
                              mock.AsyncMock(side_effect=lambda c: pickers.append("source"))), \
            mock.patch.object(bot, "_send_vanishing",
                              mock.AsyncMock(side_effect=lambda c, ch, t, **k: toasts.append(t))):
        asyncio.run(bot._interpret_review_command(update, ctx, 7, state, text))
    return state, toasts, pickers


class SplitsSeveralPicksTest(unittest.TestCase):

    def test_driver_and_dispatcher_in_one_message(self):
        kind, pairs = bot._classify_review_command("driver Kita dispatch HighKage")
        self.assertEqual(kind, "SELECTIONS")
        self.assertEqual(pairs, [("SELECT_DRIVER", "Kita"), ("SELECT_GROUP", "HighKage")])

    def test_order_does_not_matter(self):
        _, pairs = bot._classify_review_command("dispatch HighKage driver Kita")
        self.assertEqual(pairs, [("SELECT_GROUP", "HighKage"), ("SELECT_DRIVER", "Kita")])

    def test_three_picks_at_once(self):
        _, pairs = bot._classify_review_command(
            "driver Kita dispatcher HighKage source Facebook")
        self.assertEqual([k for k, _ in pairs],
                         ["SELECT_DRIVER", "SELECT_GROUP", "SELECT_SOURCE"])

    def test_joining_words_are_tolerated(self):
        for text in ("choose driver Kita and dispatcher HighKage",
                     "send to driver Kita, dispatcher HighKage"):
            with self.subTest(text=text):
                _, pairs = bot._classify_review_command(text)
                self.assertEqual([n for _, n in pairs], ["Kita", "HighKage"])

    def test_multi_word_names_survive(self):
        _, pairs = bot._classify_review_command(
            "driver John Smith dispatcher High Kage Motors")
        self.assertEqual([n for _, n in pairs], ["John Smith", "High Kage Motors"])

    def test_all_is_still_all(self):
        _, pairs = bot._classify_review_command("driver all dispatcher all")
        self.assertEqual([n for _, n in pairs], ["all", "all"])


class ProseIsNotAListOfPicksTest(unittest.TestCase):
    """A sentence that happens to contain both words must not be read as picks."""

    PROSE = [
        "the driver said dispatch was late",
        "driver is not coming",
        "tell the dispatcher the driver called",
        "the dispatch team said no",
    ]

    def test_prose_is_not_selections(self):
        wrong = [t for t in self.PROSE
                 if bot._classify_review_command(t)[0] == "SELECTIONS"]
        self.assertEqual([], wrong)

    def test_field_edits_still_win(self):
        for text in ("name John Damian", "price 150", "driver note gate code 4455"):
            with self.subTest(text=text):
                self.assertEqual(bot._classify_review_command(text)[0], "FIELD_EDITS")

    def test_single_selection_keeps_its_own_handling(self):
        self.assertEqual(bot._classify_review_command("driver Kita")[0], "SELECT_DRIVER")
        self.assertEqual(bot._classify_review_command("dispatcher HighKage")[0], "SELECT_GROUP")


class AppliesAllOfThemTest(unittest.TestCase):

    def test_both_picks_are_applied(self):
        state, toasts, pickers = _apply("driver Kita dispatch HighKage")
        self.assertEqual(state.get("selected_driver_names"), "Kita")
        self.assertEqual(state.get("selected_group_name"), "HighKage")
        self.assertEqual(pickers, [], "nothing failed, so no picker should open")
        self.assertIn("Kita", toasts[0])
        self.assertIn("HighKage", toasts[0])

    def test_all_three_are_applied(self):
        state, _, _ = _apply("driver Kita dispatcher HighKage source Facebook")
        self.assertEqual(state.get("selected_driver_names"), "Kita")
        self.assertEqual(state.get("selected_group_name"), "HighKage")
        self.assertEqual(state.get("selected_source_label"), "Facebook")

    def test_a_typo_keeps_the_good_pick_and_offers_the_picker(self):
        state, toasts, pickers = _apply("driver Kita dispatcher Nonexistent")
        self.assertEqual(state.get("selected_driver_names"), "Kita",
                         "the pick that matched must still apply")
        self.assertIn("Nonexistent", toasts[0], "the failure must be named")
        self.assertEqual(pickers, ["dispatcher"])


if __name__ == "__main__":
    unittest.main()
