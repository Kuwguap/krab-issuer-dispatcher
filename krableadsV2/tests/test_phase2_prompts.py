"""Phase 2 asks only for what is actually missing, and accepts just that.

Before: the prompt always said "Phone number and price are required" with the full
two-part example, even when one had already been captured — and handle_phase2 REQUIRED
both in the reply, so a phone that was already on the lead had to be typed again.

Run:  venv\\Scripts\\python.exe -m pytest tests/test_phase2_prompts.py -q
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

PHONE = "+17325551212"
PRICE = "$150"


def _reply(existing, text):
    """Send one Phase 2 reply; return (next_state, saved_data, what_the_bot_said)."""
    msg = SimpleNamespace(text=text, caption=None, chat_id=1, photo=None, document=None,
                          reply_text=mock.AsyncMock())
    update = SimpleNamespace(
        message=msg, effective_message=msg,
        effective_chat=SimpleNamespace(id=1, type="private"),
        effective_user=SimpleNamespace(id=7, username="tester"))
    ctx = SimpleNamespace(user_data={}, bot=mock.AsyncMock(),
                          application=SimpleNamespace(handlers={}))
    fake_db = mock.MagicMock()
    data = {"name": "X"}
    data.update(existing)
    fake_db.get_user_state.return_value = {"state": "phase1", "data": data}
    saved = {}
    fake_db.set_user_state.side_effect = lambda u, s, d: saved.update(d or {})
    with mock.patch.object(bot, "db", fake_db), \
            mock.patch.object(bot, "_prompt_issuer_special_request",
                              mock.AsyncMock(return_value="FORWARD")):
        state = asyncio.run(bot.handle_phase2(update, ctx))
    said = msg.reply_text.await_args.args[0] if msg.reply_text.await_args else None
    return state, saved, said


class PromptNamesOnlyWhatIsMissingTest(unittest.TestCase):

    def test_price_only(self):
        said = bot._phase2_prompt({"pending_phone_number": PHONE})
        self.assertIn("Price missing", said)
        self.assertNotIn("Phone number and price", said)

    def test_phone_only(self):
        said = bot._phase2_prompt({"pending_price": PRICE})
        self.assertIn("phone number missing", said.lower())
        self.assertNotIn("Price missing", said)

    def test_both_missing_asks_for_both(self):
        said = bot._phase2_prompt({})
        self.assertIn("Phone number and price missing", said)

    def test_nothing_missing_says_nothing(self):
        self.assertEqual(
            bot._phase2_prompt({"pending_phone_number": PHONE, "pending_price": PRICE}), "")

    def test_prompts_are_short(self):
        """Two lines at most — the old one was a four-line block."""
        for state in ({}, {"pending_phone_number": PHONE}, {"pending_price": PRICE}):
            said = bot._phase2_prompt(state)
            self.assertLessEqual(said.count("\n"), 1, said)


class AcceptsOnlyTheMissingPieceTest(unittest.TestCase):

    def test_price_alone_when_the_phone_is_already_known(self):
        state, saved, _ = _reply({"pending_phone_number": PHONE}, "$150")
        self.assertEqual(state, "FORWARD", "it must move on, not ask again")
        self.assertEqual(saved.get("pending_price"), PRICE)
        self.assertEqual(saved.get("pending_phone_number"), PHONE, "the known phone is kept")

    def test_a_bare_number_is_the_price_when_only_price_is_missing(self):
        state, saved, _ = _reply({"pending_phone_number": PHONE}, "150")
        self.assertEqual(state, "FORWARD")
        self.assertEqual(saved.get("pending_price"), PRICE)

    def test_phone_alone_when_the_price_is_already_known(self):
        state, saved, _ = _reply({"pending_price": PRICE}, "732-555-1212")
        self.assertEqual(state, "FORWARD")
        self.assertEqual(saved.get("pending_phone_number"), PHONE)

    def test_both_together_still_works(self):
        state, saved, _ = _reply({}, "+1234567890 $150")
        self.assertEqual(state, "FORWARD")
        self.assertTrue(saved.get("pending_phone_number"))
        self.assertEqual(saved.get("pending_price"), PRICE)

    def test_partial_reply_asks_only_for_the_remainder(self):
        state, _, said = _reply({}, "$150")
        self.assertEqual(state, bot.STATE_PHASE2)
        self.assertIn("phone number missing", said.lower())
        self.assertNotIn("Price missing", said)

    def test_a_pasted_phone_is_not_accepted_as_a_price(self):
        """Ten digits is not a price — ask again instead of storing $7325551212."""
        state, saved, said = _reply({"pending_phone_number": PHONE}, "7325551212")
        self.assertEqual(state, bot.STATE_PHASE2)
        self.assertIn("Price missing", said)
        self.assertNotEqual(saved.get("pending_price"), "$7325551212")


if __name__ == "__main__":
    unittest.main()
