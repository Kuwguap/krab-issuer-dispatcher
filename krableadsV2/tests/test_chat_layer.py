"""THE CHAT LAYER: the model reads every review message FIRST.

The operator's instruction, verbatim shape: every single text passes through
the AI-enabled chat layer before it is parsed into a command. These tests pin
what that means in practice:

  * the model is consulted before any deterministic parser touches the text;
  * every deterministic parser still works, unchanged, the moment the model
    abstains, times out, is unconfigured, is tripped, or is switched off;
  * model-chosen values pass through the SAME sanitizers a typed edit does;
  * a soft "send it out" asks before submitting; a strict "submit" still sends;
  * a quota error trips a breaker so a dead OpenAI account cannot tax every
    message with a timeout before its fallback.

Run:  venv\\Scripts\\python.exe -m pytest tests/test_chat_layer.py -q
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
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11")
os.environ.pop("OPENAI_API_KEY", None)

import utils.database as udb  # noqa: E402

if "bot" not in sys.modules:
    udb.Database = mock.MagicMock()
import bot  # noqa: E402
from utils import nl_router  # noqa: E402


def _drive(text, card=None, classify=None, layer_on=True, user_data=None,
           configured=True):
    """Send one text into handle_phase1_review_message with the layer arranged.

    Returns (saved, toasts, replies, calls) where `calls` records how many
    times the model was actually consulted.
    """
    card = dict(card if card is not None else {"vin": "-", "car": "-"})
    saved, toasts, replies = {}, [], []
    calls = {"classify": 0}

    def _classify(txt, **kw):
        calls["classify"] += 1
        return classify(txt) if callable(classify) else classify

    msg = SimpleNamespace(text=text, caption=None, chat_id=1, photo=None,
                          document=None, delete=mock.AsyncMock(),
                          reply_text=mock.AsyncMock(
                              side_effect=lambda t, **k: replies.append(t)))
    update = SimpleNamespace(
        message=msg, effective_message=msg,
        effective_chat=SimpleNamespace(id=1, type="private"),
        effective_user=SimpleNamespace(id=7, username="tester"))
    ctx = SimpleNamespace(user_data=user_data if user_data is not None
                          else {"review_message_id": 5, "review_chat_id": 1},
                          bot=mock.AsyncMock(),
                          application=SimpleNamespace(handlers={}))
    ctx.user_data.setdefault("review_message_id", 5)
    fake_db = mock.MagicMock()
    fake_db.get_user_state.return_value = {"state": "phase1", "data": card}
    fake_db.set_user_state.side_effect = lambda u, s, d: saved.update(d or {})
    env = {"KRAB_CHAT_LAYER": "1" if layer_on else "0"}
    with mock.patch.object(bot, "db", fake_db), \
            mock.patch.dict(os.environ, env), \
            mock.patch.object(nl_router, "classify", _classify), \
            mock.patch.object(nl_router, "is_configured", lambda: configured), \
            mock.patch.object(nl_router, "breaker_open", lambda: False), \
            mock.patch.object(bot.Config, "is_ai_vision_configured",
                              classmethod(lambda cls: False)), \
            mock.patch.object(bot, "_update_review_message_text", mock.AsyncMock()), \
            mock.patch.object(bot, "_cleanup_voice_echo", mock.AsyncMock()), \
            mock.patch.object(bot, "_autoclean_user_msg", mock.AsyncMock()), \
            mock.patch.object(bot, "_send_vanishing",
                              mock.AsyncMock(side_effect=lambda c, ch, t, **k: toasts.append(t))):
        asyncio.run(bot.handle_phase1_review_message(update, ctx))
    return saved, toasts, replies, calls


def _tool(name, **args):
    return {"intent": name, "args": args, "tool": name}


class TheModelReadsFirstTest(unittest.TestCase):

    def test_a_model_answer_wins_before_any_parser_runs(self):
        saved, toasts, _, calls = _drive(
            "make the color white", classify=_tool("update_lead", field="color",
                                                   value="white"))
        self.assertEqual(calls["classify"], 1)
        self.assertEqual((saved.get("color") or "").lower(), "white")
        self.assertTrue(any("Updated" in t for t in toasts), toasts)

    def test_model_values_pass_through_the_same_sanitizers_as_typed_ones(self):
        saved, _, _, _ = _drive(
            "the price is 150 plus toll",
            classify=_tool("update_lead", field="price", value="150 plus toll"))
        self.assertEqual(saved.get("pending_price"), "$150 + toll")

    def test_a_value_that_does_not_fit_falls_through_to_the_old_ladder(self):
        # "phone is dead" is not a phone number. The tool declines, the message
        # continues into the deterministic cascade, nothing is written.
        saved, _, _, calls = _drive(
            "phone is dead",
            classify=_tool("update_lead", field="phone", value="is dead"))
        self.assertEqual(calls["classify"], 1)
        self.assertNotIn("pending_phone_number", saved)


class TheFallbackIsTheOldPipelineTest(unittest.TestCase):

    def test_when_the_model_abstains_nothing_changes(self):
        saved, toasts, _, calls = _drive("color white", classify=None)
        self.assertEqual(calls["classify"], 1)
        self.assertEqual((saved.get("color") or "").lower(), "white")
        self.assertTrue(any("Updated" in t for t in toasts), toasts)

    def test_the_kill_switch_means_the_model_is_never_asked(self):
        saved, _, _, calls = _drive("color white", classify=None, layer_on=False)
        self.assertEqual(calls["classify"], 0)
        self.assertEqual((saved.get("color") or "").lower(), "white")

    def test_unconfigured_means_the_model_is_never_asked(self):
        saved, _, _, calls = _drive("color white", classify=None, configured=False)
        self.assertEqual(calls["classify"], 0)
        self.assertEqual((saved.get("color") or "").lower(), "white")


class MultiFieldExtractionTest(unittest.TestCase):

    def test_a_paste_fills_several_fields_at_once(self):
        saved, toasts, _, _ = _drive(
            "rrod782@gmail.com\nEmail now\nColor white",
            classify=_tool("update_lead_fields", email="rrod782@gmail.com",
                           color="white"))
        self.assertEqual(saved.get("email"), "rrod782@gmail.com")
        self.assertEqual((saved.get("color") or "").lower(), "white")

    def test_extraction_never_clobbers_a_filled_field(self):
        saved, _, _, _ = _drive(
            "Color white, client is Dana",
            card={"vin": "-", "car": "-", "color": "Black"},
            classify=_tool("update_lead_fields", color="white",
                           first_name="Dana"))
        # colour was already Black on the card: extraction may not overwrite it
        # (set_user_state persists the whole card, so assert the VALUE held).
        self.assertEqual(saved.get("color"), "Black")
        self.assertEqual(saved.get("first_name"), "Dana")

    def test_an_empty_extraction_falls_through(self):
        saved, _, _, _ = _drive(
            "nothing useful here",
            card={"vin": "-", "car": "-", "color": "Black"},
            classify=_tool("update_lead_fields", color="white"))
        # the only extracted field was filled -> nothing landed -> the old
        # ladder got the text; whatever it did, the colour may not change.
        self.assertIn(saved.get("color"), (None, "Black"))


class SubmitStaysGuardedTest(unittest.TestCase):

    def test_a_soft_submit_asks_before_sending(self):
        with mock.patch.object(bot, "_continue_phase1_after_ai_review",
                               mock.AsyncMock(return_value=-1)) as submit:
            _, _, replies, _ = _drive(
                "ok looks good, send it out when you can",
                classify=_tool("submit_lead"))
            submit.assert_not_called()
        self.assertTrue(any("yes" in r.lower() for r in replies), replies)

    def test_yes_after_the_ask_submits(self):
        shared = {"review_message_id": 5, "review_chat_id": 1}
        with mock.patch.object(bot, "_continue_phase1_after_ai_review",
                               mock.AsyncMock(return_value=-1)) as submit:
            _drive("ok looks good, send it out when you can",
                   classify=_tool("submit_lead"), user_data=shared)
            submit.assert_not_called()
            _drive("yes", classify=None, user_data=shared)
            submit.assert_called_once()

    def test_a_strict_submit_word_still_sends_directly(self):
        with mock.patch.object(bot, "_continue_phase1_after_ai_review",
                               mock.AsyncMock(return_value=-1)) as submit:
            _drive("submit", classify=_tool("submit_lead"))
            submit.assert_called_once()


class InsuranceToggleTest(unittest.TestCase):

    def test_the_model_can_flip_the_addon(self):
        saved, _, _, _ = _drive("client needs insurance too",
                                classify=_tool("set_insurance_addon", enable=True))
        self.assertIs(saved.get("wants_insurance"), True)


class TheBreakerTest(unittest.TestCase):
    """A dead OpenAI account must not tax every message with a timeout."""

    def setUp(self):
        nl_router._breaker_reset()

    tearDown = setUp

    def test_quota_trips_it_and_open_means_instant_none(self):
        boom = mock.MagicMock()
        boom.chat.completions.create.side_effect = Exception("429 insufficient_quota")
        with mock.patch.object(nl_router.Config, "OPENAI_API_KEY", "sk-test"):
            import openai
            with mock.patch.object(openai, "OpenAI", return_value=boom):
                with self.assertRaises(nl_router.AIVisionQuotaError):
                    nl_router.classify("disable HighKage")
            self.assertTrue(nl_router.breaker_open())
            # open breaker: no client is even built
            with mock.patch.object(openai, "OpenAI",
                                   side_effect=AssertionError("must not be called")):
                self.assertIsNone(nl_router.classify("disable HighKage"))

    def test_repeated_transport_failures_trip_it_too(self):
        for _ in range(nl_router.BREAKER_FAILS_TO_TRIP):
            nl_router._breaker_note_failure()
        self.assertTrue(nl_router.breaker_open())

    def test_the_front_door_respects_an_open_breaker(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        gate = src.split("def _chat_layer_enabled", 1)[1][:900]
        self.assertIn("breaker_open()", gate)
        self.assertIn('KRAB_CHAT_LAYER', gate)


class IdleRouterFrontsTheModelTest(unittest.TestCase):
    """The hint-word prefilter used to make hint-less commands BECOME LEADS."""

    def test_the_hint_gate_only_stands_when_the_layer_is_off(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        body = src.split("async def _route_supervisor_message", 1)[1][:6000]
        self.assertIn("not _chat_layer_enabled() and not _ROUTER_HINT_RE.search(text)",
                      body)

    def test_card_tools_at_idle_do_not_start_junk_leads(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        body = src.split("async def _route_supervisor_message", 1)[1]
        body = body.split("\nasync def ", 1)[0]
        self.assertIn("No lead is on screen", body)
        # hint-less "none" still belongs to the lead flow
        self.assertIn("elif not _ROUTER_HINT_RE.search(text):", body)


class TheSuiteStaysDeterministicTest(unittest.TestCase):

    def test_conftest_switches_the_layer_off_for_every_other_suite(self):
        conf = (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
        self.assertIn('os.environ["KRAB_CHAT_LAYER"] = "0"', conf)

    def test_the_switch_is_documented(self):
        env = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("KRAB_CHAT_LAYER", env)

    def test_the_new_tools_are_card_tools(self):
        for tool in ("update_lead_fields", "set_insurance_addon"):
            with self.subTest(tool=tool):
                self.assertIn(tool, bot._AI_CARD_TOOLS)
                self.assertIn(tool, nl_router.TOOL_NAMES)


class TheSkepticsWereRightTest(unittest.TestCase):
    """Each of these is a defect the adversarial review found in the first cut
    of the layer. The scenario IS the name; none of them may come back."""

    def test_a_labeled_edit_answers_the_card_not_a_parked_question(self):
        import time as _t
        ud = {"review_message_id": 5, "review_chat_id": 1,
              nl_router.NL_PENDING_KEY: {"tool": "select_driver", "args": {},
                                         "needs": "driver", "ts": _t.time()}}
        saved, _, replies, _ = _drive("price 150", classify=None, user_data=ud)
        self.assertEqual(saved.get("pending_price"), "$150")
        self.assertFalse(any("driver" in r.lower() for r in replies), replies)

    def test_yes_still_submits_after_the_layer_dies(self):
        # The breaker (or the kill switch) opening between the ask and the
        # answer must not strand the confirmation.
        shared = {"review_message_id": 5, "review_chat_id": 1}
        with mock.patch.object(bot, "_continue_phase1_after_ai_review",
                               mock.AsyncMock(return_value=-1)) as submit:
            _drive("ok looks good, send it out when you can",
                   classify=_tool("submit_lead"), user_data=shared)
            submit.assert_not_called()
            _, _, _, calls = _drive("yes", classify=None, user_data=shared,
                                    layer_on=False)
            submit.assert_called_once()
        self.assertEqual(calls["classify"], 0)

    def test_the_model_stands_down_while_the_dmv_question_is_open(self):
        ud = {"review_message_id": 5, "review_chat_id": 1,
              "vin_choice_api_car": "2017 NISSAN Altima"}
        _, _, _, calls = _drive("use the new one", classify=_tool(
            "update_lead", field="car", value="the new one"), user_data=ud)
        self.assertEqual(calls["classify"], 0)

    def test_a_two_vin_paste_belongs_to_the_multi_car_parser(self):
        text = ("CHARLES JONES\n1N4AL3AP0HC166043\n2017 Nissan Altima\n"
                "SECOND CAR\n4T1BF3EK6AU051219\n2010 Toyota Camry")
        _, _, _, calls = _drive(text, classify=_tool("update_lead_fields",
                                                     first_name="CHARLES"))
        self.assertEqual(calls["classify"], 0)

    def test_an_ai_address_edit_still_gets_the_split(self):
        with mock.patch.object(bot, "_ai_split_addresses_if_needed",
                               mock.AsyncMock(return_value=[])) as split:
            _drive("address is 123 Main St Newark NJ 07102",
                   classify=_tool("update_lead", field="address",
                                  value="123 Main St Newark NJ 07102"))
            split.assert_called()

    def test_idle_mid_dispatch_leads_are_not_told_no_lead_is_on_screen(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        body = src.split("async def _route_supervisor_message", 1)[1]
        body = body.split("\nasync def ", 1)[0]
        block = body.split('elif intent in ("update_lead"', 1)[1][:1600]
        self.assertIn("_LEAD_MID_DISPATCH_STATES", block)
        self.assertIn("return False", block)
        self.assertIn("not _chat_layer_enabled()", block)


if __name__ == "__main__":
    unittest.main()
