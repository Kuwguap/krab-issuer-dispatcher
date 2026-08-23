"""The DMV VIN check asks one short question with Yes / No.

It used to be a three-line preamble ("Pulling up 17 Digit Vin in DMV portal", "Success!
Your Vehicle pulls up in the Motor Vehicle system!", "Choose which to use:") over three
stacked buttons.

Run:  venv\\Scripts\\python.exe -m pytest tests/test_vin_choice_prompt.py -q
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

import utils.database as udb  # noqa: E402

if "bot" not in sys.modules:
    udb.Database = mock.MagicMock()
import bot  # noqa: E402

DMV_CAR = "2017 MERCEDES-BENZ E-Class"
STATED_CAR = "2017 Mercedes E350"


class PromptWordingTest(unittest.TestCase):

    def test_asks_the_short_question(self):
        body = bot._vin_conflict_body(STATED_CAR, DMV_CAR)
        self.assertIn("Would you like to use DMV system?", body)

    def test_the_old_preamble_is_gone(self):
        body = bot._vin_conflict_body(STATED_CAR, DMV_CAR)
        for old in ("Pulling up", "Motor Vehicle system", "Choose which to use",
                    "VIN result in DMV", "Success"):
            self.assertNotIn(old, body, old)

    def test_the_decoded_vehicle_is_still_shown(self):
        """Yes/No would be a blind choice without it."""
        self.assertIn(DMV_CAR, bot._vin_conflict_body(STATED_CAR, DMV_CAR))

    def test_message_is_two_lines(self):
        self.assertEqual(bot._vin_conflict_body(STATED_CAR, DMV_CAR).count("\n"), 1)


class ButtonsTest(unittest.TestCase):

    def _buttons(self):
        kb = bot._vin_choice_keyboard(DMV_CAR, STATED_CAR)
        return [b for row in kb.inline_keyboard for b in row]

    def test_exactly_two_buttons_on_one_row(self):
        kb = bot._vin_choice_keyboard(DMV_CAR, STATED_CAR)
        self.assertEqual(len(kb.inline_keyboard), 1)
        self.assertEqual(len(kb.inline_keyboard[0]), 2)

    def test_yes_uses_the_dmv_lookup(self):
        yes = next(b for b in self._buttons() if "Yes" in b.text)
        self.assertEqual(yes.callback_data, "vin_use")

    def test_no_keeps_the_same_vin(self):
        no = next(b for b in self._buttons() if "No" in b.text)
        self.assertEqual(no.callback_data, "vin_keep")

    def test_retype_button_is_gone(self):
        self.assertNotIn("vin_retype", [b.callback_data for b in self._buttons()])

    def test_retype_is_still_reachable_without_a_button(self):
        """Saying "retype vin" must still work — only the button was removed."""
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        self.assertIn("vin_use|vin_keep|vin_retype", src,
                      "the retype callback must stay registered")
        self.assertEqual(bot._classify_review_command("retype vin")[0], "VIN_RETYPE")


class AnswerByTextOrVoiceTest(unittest.TestCase):
    """The prompt asks Yes/No, so those words must answer it — typed or spoken.

    Voice needs no separate wiring: it is transcribed before routing, so it reaches
    this same handler as text."""

    def _answer(self, text):
        import asyncio
        from types import SimpleNamespace
        msg = SimpleNamespace(text=text, chat_id=1, delete=mock.AsyncMock(),
                              reply_text=mock.AsyncMock())
        update = SimpleNamespace(
            message=msg, effective_message=msg,
            effective_chat=SimpleNamespace(id=1, type="private"),
            effective_user=SimpleNamespace(id=7, username="tester"))
        ctx = SimpleNamespace(user_data={}, bot=mock.AsyncMock(),
                              application=SimpleNamespace(handlers={}))
        got = {}

        async def fake_apply(context, message, chat_id, user_id, choice):
            got["choice"] = choice
            return bot.STATE_AI_REVIEW

        with mock.patch.object(bot, "_apply_vin_choice", fake_apply), \
                mock.patch.object(bot, "_autoclean_user_msg", mock.AsyncMock()), \
                mock.patch.object(bot, "_cleanup_voice_echo", mock.AsyncMock()):
            asyncio.run(bot.handle_vin_choice_text(update, ctx))
        said = msg.reply_text.await_args.args[0] if msg.reply_text.await_args else None
        return got.get("choice"), said

    def test_yes_uses_the_dmv_result(self):
        for word in ("yes", "Yes", "YES", "yeah", "yep", "yup", "sure", "y",
                     "ok", "okay", "use it", "go ahead", "yes."):
            with self.subTest(word=word):
                self.assertEqual(self._answer(word)[0], "use")

    def test_no_keeps_the_same_vin(self):
        for word in ("no", "No", "NO", "nope", "nah", "n", "keep it", "leave it", "no."):
            with self.subTest(word=word):
                self.assertEqual(self._answer(word)[0], "keep")

    def test_the_older_phrasing_still_works(self):
        self.assertEqual(self._answer("use the new")[0], "use")
        self.assertEqual(self._answer("keep the same")[0], "keep")
        self.assertEqual(self._answer("retype vin")[0], "retype")

    def test_anything_else_is_not_taken_as_an_answer(self):
        """Non-answers are treated as ordinary edits instead (see
        VinCheckIsOptionalTest) — the VIN question never nags or blocks."""
        routed = mock.AsyncMock(return_value=bot.STATE_AI_REVIEW)
        with mock.patch.object(bot, "handle_phase1_review_message", routed):
            choice, _ = self._answer("what does that mean")
        self.assertIsNone(choice, "it must not silently pick use or keep")
        routed.assert_awaited_once()


class StrayYesIsNeverAFieldValueTest(unittest.TestCase):

    def test_bare_yes_no_are_treated_as_answers(self):
        for word in ("yes", "no", "y", "n", "yeah", "nope", "ok"):
            with self.subTest(word=word):
                self.assertTrue(bot._COMMAND_LIKE_RE.search(word),
                                "a bare answer must never be filed as a field value")

    def test_a_real_name_containing_yes_is_untouched(self):
        self.assertFalse(bot._COMMAND_LIKE_RE.search("Yes Motors LLC"))


class VinCheckIsOptionalTest(unittest.TestCase):
    """The DMV question must never gate ordinary edits."""

    def _send(self, text):
        import asyncio
        from types import SimpleNamespace
        msg = SimpleNamespace(text=text, caption=None, chat_id=1, photo=None,
                              document=None, delete=mock.AsyncMock(),
                              reply_text=mock.AsyncMock())
        update = SimpleNamespace(
            message=msg, effective_message=msg,
            effective_chat=SimpleNamespace(id=1, type="private"),
            effective_user=SimpleNamespace(id=7, username="tester"))
        ctx = SimpleNamespace(user_data={"review_message_id": 5, "review_chat_id": 1},
                              bot=mock.AsyncMock(),
                              application=SimpleNamespace(handlers={}))
        saved, chosen = {}, {}
        fake_db = mock.MagicMock()
        fake_db.get_user_state.return_value = {"state": "phase1",
                                               "data": {"vin": "-", "car": "-"}}
        fake_db.set_user_state.side_effect = lambda u, s, d: saved.update(d or {})

        async def fake_apply(context, message, chat_id, user_id, choice):
            chosen["choice"] = choice
            return bot.STATE_AI_REVIEW

        with mock.patch.object(bot, "db", fake_db), \
                mock.patch.object(bot, "_apply_vin_choice", fake_apply), \
                mock.patch.object(bot.Config, "is_ai_vision_configured",
                                  classmethod(lambda cls: False)), \
                mock.patch.object(bot, "_update_review_message_text", mock.AsyncMock()), \
                mock.patch.object(bot, "_send_vanishing", mock.AsyncMock()), \
                mock.patch.object(bot, "_autoclean_user_msg", mock.AsyncMock()), \
                mock.patch.object(bot, "_cleanup_voice_echo", mock.AsyncMock()):
            state = asyncio.run(bot.handle_vin_choice_text(update, ctx))
        return state, saved, chosen.get("choice")

    def test_a_price_edit_applies_while_the_question_is_open(self):
        state, saved, _ = self._send("price 150")
        self.assertEqual(saved.get("pending_price"), "$150")

    def test_other_edits_apply_too(self):
        for text, key, want in [("color white", "color", "White"),
                                ("name John Damian", "name", "John Damian")]:
            with self.subTest(text=text):
                _, saved, _ = self._send(text)
                self.assertEqual(saved.get(key), want)

    def test_the_question_stays_open_after_an_edit(self):
        """Its Yes/No buttons must still be answerable."""
        state, _, _ = self._send("price 150")
        self.assertEqual(state, bot.STATE_VIN_CHOICE)

    def test_answers_are_unaffected(self):
        for word, want in (("yes", "use"), ("no", "keep"), ("retype vin", "retype")):
            with self.subTest(word=word):
                self.assertEqual(self._send(word)[2], want)

    def test_the_buttons_resolve_from_the_review_state_too(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        review = src.split("STATE_AI_REVIEW: [", 1)[1].split("STATE_ADJUST_INPUT", 1)[0]
        self.assertIn("vin_use|vin_keep|vin_retype", review,
                      "the DMV card outlives the VIN state, so its buttons must work here")


if __name__ == "__main__":
    unittest.main()
