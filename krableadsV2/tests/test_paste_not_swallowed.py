r"""A pasted lead that happens to say "ADD INSURANCE" is a lead, not a command.

Reported: an issuer pasted a whole lead into the bot and NOTHING happened — no
reply at all. Reproduced: when the database still holds an unfinished "phase1"
card (a previous lead, or any card that outlived a redeploy), the paste is
routed to the review handler, where the step-0 insurance short-circuit matched
the words "ADD INSURANCE" anywhere in the message. It then deleted the paste,
wrote the flag onto the PREVIOUS card, redrew that card with identical text and
returned without a word. The whole lead — name, address, VIN, car, colour,
phone, price, email, licence — was discarded in silence.

Run:  venv\Scripts\python.exe -m pytest tests/test_paste_not_swallowed.py -q
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

import bot  # noqa: E402

PASTE = """Magnolia Diaz
3125 park ave apt 11D
Bronx New York,10451


Client phone number
551-301-3737

JTLKT324364094480
2006 Scion xB
Color grey


ADD INSURANCE

Delivery tomorrow morning
Plate + insurance
250 total
DataOrganizeOnline@gmail.com
Driver license 123123123"""

REPARSED = ("Magnolia Diaz\n3125 Park Ave Apt 11D\nBronx, NY 10451\n-\n-\n"
            "JTLKT324364094480\n2006 Scion xB\ngrey\n-\n-\nDelivery tomorrow morning")

UID = 12345
STALE = {"name": "Prior Client", "address": "1 Old St",
         "city_state_zip": "Newark, NJ 07101", "car": "2003 Honda Accord"}


class TheShortCircuitKnowsALeadFromACommandTest(unittest.TestCase):
    """The guard itself, without the machinery around it."""

    def test_a_bare_insurance_command_is_still_a_command(self):
        for text in ("add insurance", "ADD INSURANCE", "no insurance"):
            self.assertIsNotNone(bot._insurance_intent(text), text)
            self.assertFalse(bot._looks_like_multifield_block(text), text)

    def test_a_whole_pasted_lead_is_not_a_command(self):
        """Both are true of the paste — which is why the guard is needed."""
        self.assertTrue(bot._insurance_intent(PASTE))
        self.assertTrue(bot._looks_like_multifield_block(PASTE))

    def test_the_guard_is_wired_into_step_zero(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        body = src.split("async def handle_phase1_review_message", 1)[1]
        body = body.split("\nasync def ", 1)[0]
        self.assertIn("_ins_is_whole_lead", body)
        self.assertIn("if _ins is not None and not _ins_is_whole_lead:", body)
        # …and the wish is still honoured, on the card the re-parse creates.
        self.assertIn('_fresh["wants_insurance"] = _ins', body)


class ThePasteBecomesTheLeadTest(unittest.TestCase):
    """End to end through the real handler, with the AI re-parse stubbed."""

    def _run(self, text):
        store = {UID: {"state": "phase1", "data": dict(STALE)}}
        db = mock.MagicMock()
        db.get_user_state.side_effect = lambda u: store.get(u)

        def _set(u, st, data):
            store[u] = {"state": st, "data": dict(data)}
            return True
        db.set_user_state.side_effect = _set

        msg = mock.MagicMock()
        msg.text = text
        # A MagicMock is truthy: without these the handler takes its photo branch.
        msg.photo = None
        msg.document = None
        msg.caption = None
        msg.chat_id = 999
        msg.delete = mock.AsyncMock()
        msg.reply_text = mock.AsyncMock()
        upd = mock.MagicMock()
        upd.effective_message = msg
        upd.message = msg
        upd.effective_user = mock.MagicMock(id=UID, username="tester")
        upd.effective_chat = mock.MagicMock(id=999, type="private")
        ctx = mock.MagicMock()
        ctx.user_data = {"review_message_id": 42, "review_chat_id": 999}
        ctx.bot.send_message = mock.AsyncMock()
        ctx.bot.edit_message_text = mock.AsyncMock()

        seen = {"reparsed": False}

        async def fake_adjust(update, context, fill_only_empty=False):
            seen["reparsed"] = True
            db.set_user_state(UID, "phase1", bot.parse_phase1_structured(REPARSED))
            return bot.STATE_ADJUST_INPUT

        with mock.patch.object(bot, "db", db), \
                mock.patch.object(bot, "handle_phase1_adjust_input", fake_adjust), \
                mock.patch.object(bot, "_update_review_message_text", mock.AsyncMock()), \
                mock.patch.object(bot, "_cleanup_voice_echo", mock.AsyncMock()), \
                mock.patch.object(bot, "_autoclean_user_msg", mock.AsyncMock()), \
                mock.patch.object(bot, "_send_vanishing", mock.AsyncMock()):
            asyncio.run(bot.handle_phase1_review_message(upd, ctx))
        return seen, store[UID]["data"]

    def test_the_paste_reaches_the_reparse_and_replaces_the_card(self):
        seen, card = self._run(PASTE)
        self.assertTrue(seen["reparsed"], "the paste must reach the AI re-parse")
        self.assertEqual("Magnolia Diaz", card.get("name"))
        self.assertEqual("JTLKT324364094480", card.get("vin"))
        self.assertNotEqual("Prior Client", card.get("name"))

    def test_the_insurance_wish_in_that_paste_still_lands(self):
        _, card = self._run(PASTE)
        self.assertIs(True, card.get("wants_insurance"))

    def test_it_also_holds_with_the_chat_layer_on(self):
        """Production runs with KRAB_CHAT_LAYER on, and the layer reads review
        text BEFORE the deterministic ladder. It must not claim a whole lead
        either — that is the path that merged the paste into the old card."""
        import os as _os
        prev = _os.environ.get("KRAB_CHAT_LAYER")
        _os.environ["KRAB_CHAT_LAYER"] = "1"
        try:
            with mock.patch.object(bot, "_ai_review_command",
                                   mock.AsyncMock(return_value=bot.STATE_AI_REVIEW)) as layer:
                seen, card = self._run(PASTE)
            layer.assert_not_awaited()
            self.assertTrue(seen["reparsed"])
            self.assertEqual("Magnolia Diaz", card.get("name"))
        finally:
            if prev is None:
                _os.environ.pop("KRAB_CHAT_LAYER", None)
            else:
                _os.environ["KRAB_CHAT_LAYER"] = prev

    def test_the_chat_layer_still_sees_ordinary_text(self):
        """Only a whole-lead paste bypasses it; a normal message must not."""
        import os as _os
        prev = _os.environ.get("KRAB_CHAT_LAYER")
        _os.environ["KRAB_CHAT_LAYER"] = "1"
        try:
            with mock.patch.object(bot, "_ai_review_command",
                                   mock.AsyncMock(return_value=bot.STATE_AI_REVIEW)) as layer:
                self._run("change the price to 300")
            layer.assert_awaited()
        finally:
            if prev is None:
                _os.environ.pop("KRAB_CHAT_LAYER", None)
            else:
                _os.environ["KRAB_CHAT_LAYER"] = prev

    def test_a_bare_command_still_flips_the_flag_without_a_reparse(self):
        seen, card = self._run("add insurance")
        self.assertFalse(seen["reparsed"], "a bare command must NOT trigger a re-parse")
        self.assertIs(True, card.get("wants_insurance"))
        self.assertEqual("Prior Client", card.get("name"), "the card must be untouched")


if __name__ == "__main__":
    unittest.main()
