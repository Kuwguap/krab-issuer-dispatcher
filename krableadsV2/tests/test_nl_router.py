r"""Routing a sentence to a CRM action, with OpenAI function calling.

This replaced the extraction half of the supervisor router — which asked for JSON
in prose and salvaged whatever came back with a regex — and added a tier to the
review card for messages the deterministic rules do not understand.

THE PROPERTY EVERYTHING ELSE RESTS ON, and the first class below:

    the model can only ever ADD understanding.

It is consulted after `_classify_review_command` returns NONE, never before. So a
phrasing that works today works identically tomorrow, at the same speed and for
free — and when the account runs out of credit, which has happened, the bot
behaves exactly as it does now. `TheBotWorksWithNoApiAtAllTest` is the proof, and
it is the test to keep if any of these are ever thrown away.

Run:  venv\Scripts\python.exe -m pytest tests/test_nl_router.py -q
"""
import asyncio
import json
import os
import sys
import time
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
from utils import nl_router as nr  # noqa: E402
from utils.ai_vision import AIVisionQuotaError  # noqa: E402


def fake_response(tool, args):
    """An OpenAI response carrying one tool call, shaped like the real one."""
    call = mock.Mock()
    call.function.name = tool
    call.function.arguments = json.dumps(args)
    msg = mock.Mock()
    msg.tool_calls = [call]
    choice = mock.Mock()
    choice.message = msg
    resp = mock.Mock()
    resp.choices = [choice]
    return resp


def classify_returning(tool, args, key="sk-test"):
    """Run nr.classify with the API stubbed to answer `tool(args)`."""
    client = mock.Mock()
    client.chat.completions.create.return_value = fake_response(tool, args)
    with mock.patch.object(bot.Config, "OPENAI_API_KEY", key), \
         mock.patch.object(nr.Config, "OPENAI_API_KEY", key), \
         mock.patch("openai.OpenAI", return_value=client):
        return nr.classify("whatever the operator typed")


class EverySchemaIsValidTest(unittest.TestCase):
    """`strict` is what stops the model inventing a field. It is also silently
    rejected by the API if the schema does not satisfy its rules, so these are
    checked here rather than discovered in production."""

    def test_strict_is_set_everywhere(self):
        for t in nr.TOOLS:
            with self.subTest(tool=t["function"]["name"]):
                self.assertIs(t["function"].get("strict"), True)

    def test_required_lists_every_property(self):
        """Under strict, an optional argument is a NULLABLE one — omitting it
        from `required` is rejected outright."""
        for t in nr.TOOLS:
            p = t["function"]["parameters"]
            with self.subTest(tool=t["function"]["name"]):
                self.assertEqual(set(p["required"]), set(p["properties"]))
                self.assertIs(p["additionalProperties"], False)

    def test_optional_arguments_are_nullable(self):
        create = next(t for t in nr.TOOLS if t["function"]["name"] == "create_lead")
        for name, prop in create["function"]["parameters"]["properties"].items():
            with self.subTest(field=name):
                self.assertIn("null", prop["type"])

    def test_names_are_unique(self):
        self.assertEqual(len(nr.TOOL_NAMES), len(set(nr.TOOL_NAMES)))

    def test_it_is_json_serialisable(self):
        json.dumps(nr.TOOLS)          # must not raise


class EveryToolReachesAHandlerTest(unittest.TestCase):
    """The failure this codebase keeps having, in a new place: a name that no
    dispatcher matches does nothing at all, silently."""

    def test_admin_tools_map_onto_the_existing_router(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        router = src.split("async def _route_supervisor_message", 1)[1][:14000]
        card = set(bot._AI_CARD_TOOLS)
        for name in nr.TOOL_NAMES:
            intent = nr._TOOL_TO_INTENT.get(name, name)
            if name in card or intent == "lead":
                continue          # handled on the card, or handed to the lead flow
            with self.subTest(tool=name):
                self.assertIn(f'intent == "{intent}"', router,
                              f"{name} routes to intent {intent!r}, which nothing dispatches")

    def test_card_tools_all_have_a_branch(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        body = src.split("async def _run_ai_card_tool", 1)[1].split("\n# The tool's", 1)[0]
        for name in bot._AI_CARD_TOOLS:
            with self.subTest(tool=name):
                self.assertIn(f'"{name}"', body)

    def test_every_argument_name_is_one_the_dispatcher_reads(self):
        """The dangerous case is a BOOLEAN: a name the branch does not read is
        absent, absent reads as False, and "activate Susan" deactivates her.
        driver_status reads args["active"] while its neighbour group_status reads
        args["enable"] — neighbouring branches, different words."""
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        router = src.split("async def _route_supervisor_message", 1)[1][:14000]
        card = set(bot._AI_CARD_TOOLS)
        for tool in nr.TOOLS:
            name = tool["function"]["name"]
            intent = nr._TOOL_TO_INTENT.get(name, name)
            if name in card or intent == "lead":
                continue
            branch = router.split(f'intent == "{intent}"', 1)
            if len(branch) < 2:
                continue
            branch = branch[1].split("elif intent ==", 1)[0]
            for arg in tool["function"]["parameters"]["properties"]:
                if arg == "reference_id":
                    continue           # aliased to "reference" by classify()
                with self.subTest(tool=name, arg=arg):
                    self.assertIn(f'"{arg}"', branch,
                                  f"{name} declares {arg!r}, which its branch never reads")

    def test_every_lead_field_has_an_edit_key(self):
        """The tool's enum and the card's edit keys must not drift."""
        for field in nr.LEAD_FIELDS:
            with self.subTest(field=field):
                self.assertIn(field, bot._AI_FIELD_TO_EK)

    def test_the_field_enum_matches_the_card(self):
        update = next(t for t in nr.TOOLS if t["function"]["name"] == "update_lead")
        enum = update["function"]["parameters"]["properties"]["field"]["enum"]
        self.assertEqual(set(enum), set(nr.LEAD_FIELDS))


class MissingArgumentsBecomeAQuestionTest(unittest.TestCase):
    """"create lead" with no name asks for the name — locally, with no second
    API call, because the schema already says what is required."""

    def test_a_required_argument_is_reported(self):
        self.assertEqual(nr.missing_args("select_driver", {}), ["driver"])
        self.assertEqual(nr.missing_args("update_lead", {"field": "color"}), ["value"])
        self.assertEqual(nr.missing_args("get_lead_status", {}), ["reference_id"])

    def test_a_supplied_argument_is_not(self):
        self.assertEqual(nr.missing_args("select_driver", {"driver": "Susan"}), [])

    def test_a_blank_string_counts_as_missing(self):
        self.assertEqual(nr.missing_args("select_driver", {"driver": "   "}), ["driver"])

    def test_nullable_arguments_are_never_required(self):
        """create_lead may legitimately arrive with nothing at all — the review
        card collects the rest."""
        self.assertEqual(nr.missing_args("create_lead", {}), [])
        self.assertEqual(nr.missing_args("broadcast", {}), [])

    def test_a_boolean_must_be_present_but_may_be_false(self):
        self.assertEqual(nr.missing_args("driverblock", {}), ["enable"])
        self.assertEqual(nr.missing_args("driverblock", {"enable": False}), [])

    def test_the_question_is_in_the_operators_terms(self):
        self.assertEqual(nr.ask_for("select_driver", "driver"),
                         "Which driver should take it?")
        self.assertIn("reference", nr.ask_for("get_lead_status", "reference_id"))

    def test_an_unknown_pair_still_asks_something_sensible(self):
        self.assertIn("city state zip", nr.ask_for("create_lead", "city_state_zip"))


class TheParkedCallExpiresTest(unittest.TestCase):
    r"""A slot that never closes steals whatever the operator types next, into a
    record they had stopped thinking about — the same failure `_CF_EDIT_TTL_SEC`
    guards, with the same 180 seconds."""

    def test_it_comes_back_once(self):
        ud = {}
        nr.park(ud, "select_driver", {}, "driver")
        got = nr.take_parked(ud)
        self.assertEqual(got["tool"], "select_driver")
        self.assertIsNone(nr.take_parked(ud), "a parked call must be single-use")

    def test_it_expires(self):
        ud = {}
        nr.park(ud, "select_driver", {}, "driver")
        ud[nr.NL_PENDING_KEY]["ts"] = time.time() - nr.NL_PENDING_TTL_SEC - 1
        self.assertIsNone(nr.take_parked(ud))

    def test_an_expired_one_is_still_cleared(self):
        """Left behind, it would be found by a later message."""
        ud = {}
        nr.park(ud, "select_driver", {}, "driver")
        ud[nr.NL_PENDING_KEY]["ts"] = 0
        nr.take_parked(ud)
        self.assertNotIn(nr.NL_PENDING_KEY, ud)

    def test_it_matches_the_routers_own_ttl(self):
        """Two expiries for one idea is how one of them gets forgotten."""
        self.assertEqual(nr.NL_PENDING_TTL_SEC, bot._ROUTER_FOLLOWUP_TTL_SEC)


class WhatComesBackFromTheModelTest(unittest.TestCase):

    def test_a_tool_call_becomes_an_intent(self):
        got = classify_returning("list_drivers", {})
        self.assertEqual(got["intent"], "list_drivers")

    def test_client_details_are_handed_to_the_lead_flow(self):
        """`create_lead` maps to the intent the router already returns False for
        — the expensive mistake is a real client turned into a command."""
        got = classify_returning("create_lead", {"first_name": "John"})
        self.assertEqual(got["intent"], "lead")

    def test_a_lookup_keeps_the_old_argument_name(self):
        """The existing branch reads args['reference']."""
        got = classify_returning("get_lead_status", {"reference_id": "ABC12345"})
        self.assertEqual(got["intent"], "lead_lookup")
        self.assertEqual(got["args"]["reference"], "ABC12345")
        self.assertEqual(got["args"]["reference_id"], "ABC12345")

    def test_nulls_and_blanks_are_dropped(self):
        got = classify_returning("create_lead", {"first_name": "John",
                                                 "last_name": None, "vin": "  "})
        self.assertEqual(got["args"], {"first_name": "John"})

    def test_an_unknown_tool_is_refused(self):
        got = classify_returning("drop_all_leads", {})
        self.assertEqual(got["intent"], "none")

    def test_unparseable_arguments_are_refused(self):
        call = mock.Mock()
        call.function.name = "select_driver"
        call.function.arguments = "{not json"
        msg = mock.Mock(); msg.tool_calls = [call]
        choice = mock.Mock(); choice.message = msg
        resp = mock.Mock(); resp.choices = [choice]
        client = mock.Mock()
        client.chat.completions.create.return_value = resp
        with mock.patch.object(nr.Config, "OPENAI_API_KEY", "sk-test"), \
             mock.patch("openai.OpenAI", return_value=client):
            self.assertEqual(nr.classify("x")["intent"], "none")

    def test_no_tool_chosen_is_not_an_error(self):
        msg = mock.Mock(); msg.tool_calls = []
        choice = mock.Mock(); choice.message = msg
        resp = mock.Mock(); resp.choices = [choice]
        client = mock.Mock()
        client.chat.completions.create.return_value = resp
        with mock.patch.object(nr.Config, "OPENAI_API_KEY", "sk-test"), \
             mock.patch("openai.OpenAI", return_value=client):
            self.assertEqual(nr.classify("hello there")["intent"], "none")

    def test_the_card_summary_carries_no_customer_data(self):
        """It goes to a third party. Field NAMES are enough for the model to know
        that "change it to black" means the colour."""
        card = {"first_name": "CHARLES", "last_name": "JONES",
                "vin": "4T1BF3EK6AU051219", "phone": "845-423-9476",
                "selected_driver_names": "Susan"}
        summary = nr.card_summary(card)
        for secret in ("CHARLES", "JONES", "4T1BF3EK6AU051219", "845-423-9476"):
            with self.subTest(value=secret):
                self.assertNotIn(secret, summary)
        self.assertIn("first_name", summary)


class TheBotWorksWithNoApiAtAllTest(unittest.TestCase):
    """The test to keep if all the others are thrown away.

    This account has run out of credit before. Everything above is an addition;
    none of it may become load-bearing."""

    def test_classify_is_inert_without_a_key(self):
        with mock.patch.object(nr.Config, "OPENAI_API_KEY", ""):
            self.assertIsNone(nr.classify("show me the drivers"))
            self.assertFalse(nr.is_configured())

    def test_a_quota_error_is_raised_as_the_one_the_codebase_knows(self):
        """So every existing catch site keeps working."""
        client = mock.Mock()
        client.chat.completions.create.side_effect = RuntimeError(
            "Error code: 429 - insufficient_quota")
        with mock.patch.object(nr.Config, "OPENAI_API_KEY", "sk-test"), \
             mock.patch("openai.OpenAI", return_value=client):
            with self.assertRaises(AIVisionQuotaError):
                nr.classify("show me the drivers")

    def test_any_other_failure_is_just_a_none(self):
        client = mock.Mock()
        client.chat.completions.create.side_effect = RuntimeError("connection reset")
        with mock.patch.object(nr.Config, "OPENAI_API_KEY", "sk-test"), \
             mock.patch("openai.OpenAI", return_value=client):
            self.assertIsNone(nr.classify("show me the drivers"))

    def test_the_deterministic_layer_is_untouched(self):
        """Every phrasing that worked before the model existed still does, at the
        same speed, with no key set."""
        for text, want in (("driver Susan", "SELECT_DRIVER"),
                           ("price 150", "FIELD_EDITS"),
                           ("all drivers", "SELECT_DRIVER"),
                           ("I'd like to select all drivers", "SELECT_DRIVER"),
                           ("submit", "SUBMIT"),
                           ("Same Day Delivery", "NONE")):
            with self.subTest(said=text):
                self.assertEqual(
                    bot._classify_review_command(text, vin_pending=False)[0], want)

    def test_the_model_is_only_asked_after_the_local_rules_decline(self):
        """The ordering IS the safety property. Reversing it would put a network
        call on every message and let the model overrule a working answer."""
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        body = src.split("async def _interpret_review_command", 1)[1][:3000]
        local = body.index("_classify_review_command(")
        ai = body.index("_ai_review_command(")
        self.assertLess(local, ai)
        self.assertIn('if kind == "NONE":', body[:ai])


class OneChatAtATimeButNotOnePersonAtATimeTest(unittest.TestCase):
    """PTB's docs warn that concurrent updates are unsafe with
    ConversationHandler — and this bot is almost entirely conversations. The
    ordering that actually matters is per chat."""

    def test_two_chats_do_not_wait_for_each_other(self):
        proc = bot.PerChatUpdateProcessor(max_concurrent_updates=8)
        order = []

        def upd(chat_id):
            u = mock.Mock()
            u.effective_chat.id = chat_id
            return u

        async def slow(tag, delay):
            await asyncio.sleep(delay)
            order.append(tag)

        async def go():
            await asyncio.gather(
                proc.do_process_update(upd(1), slow("slow-chat-1", 0.05)),
                proc.do_process_update(upd(2), slow("fast-chat-2", 0.0)),
            )
        asyncio.run(go())
        self.assertEqual(order, ["fast-chat-2", "slow-chat-1"],
                         "chat 2 queued behind chat 1")

    def test_one_chat_stays_in_order(self):
        proc = bot.PerChatUpdateProcessor(max_concurrent_updates=8)
        order = []
        u = mock.Mock()
        u.effective_chat.id = 7

        async def step(tag, delay):
            await asyncio.sleep(delay)
            order.append(tag)

        async def go():
            await asyncio.gather(
                proc.do_process_update(u, step("first", 0.05)),
                proc.do_process_update(u, step("second", 0.0)),
            )
        asyncio.run(go())
        self.assertEqual(order, ["first", "second"],
                         "one chat's messages interleaved")

    def test_an_update_with_no_chat_still_runs(self):
        proc = bot.PerChatUpdateProcessor()
        ran = []

        async def go():
            u = mock.Mock()
            u.effective_chat = None
            await proc.do_process_update(u, _noop(ran))
        asyncio.run(go())
        self.assertEqual(ran, ["ok"])

    def test_it_is_actually_wired_in(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        self.assertIn("concurrent_updates(PerChatUpdateProcessor", src)


async def _noop(sink):
    sink.append("ok")


if __name__ == "__main__":
    unittest.main()
