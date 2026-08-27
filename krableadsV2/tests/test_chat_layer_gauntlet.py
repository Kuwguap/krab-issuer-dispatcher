"""THE UTTERANCE GAUNTLET: real sentences thrown at the chat-layer front door.

test_chat_layer.py pins the MECHANISM (model first, ladder as fallback, breaker,
kill switch). This file pins the BEHAVIOR across the utterances operators
actually send: labeled edits, whole sentences, polarity traps, notes that must
never become commands, dossier pastes, driver-name sentences, cancel words,
soft submits, a quota outage mid-conversation, and a model that hallucinates.

Ground rules, mirrored from the layer's design:

  * the model's answer is a MOCKED classify return (a plausible tool call);
  * for the traps the model ABSTAINS (None / intent "none") and the
    deterministic ladder must do exactly what its own suites already pin;
  * cancel/restart words are handled BEFORE the front door and must never
    reach the model at all;
  * a quota error warns once and falls back — the edit still lands.

Tests marked GAUNTLET-FINDING deliberately pin CURRENT behavior that the
gauntlet exposed as questionable in production code. They are documentation,
not endorsement — the finding list travels with the review, not this file.

Run:  venv\\Scripts\\python.exe -m pytest tests/test_chat_layer_gauntlet.py -q
"""
import asyncio
import contextlib
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
from utils.ai_vision import AIVisionQuotaError  # noqa: E402

VIN17 = "4T1BE46K17U832142"
ROSTER = [{"id": 1, "driver_name": "Kita"}, {"id": 2, "driver_name": "Rob"}]


def _drive(text, card=None, classify=None, layer_on=True, user_data=None,
           configured=True, drivers=None):
    """Send one text into handle_phase1_review_message with the layer arranged.

    Copied from test_chat_layer.py and extended with ``drivers`` (a roster for
    the driver-pick paths). Returns (saved, toasts, replies, calls) where
    ``calls`` records how many times the model was actually consulted.
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
    with contextlib.ExitStack() as st:
        st.enter_context(mock.patch.object(bot, "db", fake_db))
        st.enter_context(mock.patch.dict(os.environ, env))
        st.enter_context(mock.patch.object(nl_router, "classify", _classify))
        st.enter_context(mock.patch.object(nl_router, "is_configured",
                                           lambda: configured))
        st.enter_context(mock.patch.object(nl_router, "breaker_open",
                                           lambda: False))
        st.enter_context(mock.patch.object(
            bot.Config, "is_ai_vision_configured",
            classmethod(lambda cls: False)))
        st.enter_context(mock.patch.object(
            bot, "_update_review_message_text", mock.AsyncMock()))
        st.enter_context(mock.patch.object(
            bot, "_cleanup_voice_echo", mock.AsyncMock()))
        st.enter_context(mock.patch.object(
            bot, "_autoclean_user_msg", mock.AsyncMock()))
        st.enter_context(mock.patch.object(
            bot, "_send_vanishing",
            mock.AsyncMock(side_effect=lambda c, ch, t, **k: toasts.append(t))))
        if drivers is not None:
            st.enter_context(mock.patch.object(
                bot, "_get_all_drivers_cached", lambda: list(drivers)))
            st.enter_context(mock.patch.object(
                bot, "_get_suspended_driver_ids", lambda: set()))
        asyncio.run(bot.handle_phase1_review_message(update, ctx))
    return saved, toasts, replies, calls


def _tool(name, **args):
    return {"intent": name, "args": args, "tool": name}


ABSTAIN = None                      # model declined / timed out
NONE_INTENT = {"intent": "none", "args": {}}   # model answered "no tool"


def _shared():
    return {"review_message_id": 5, "review_chat_id": 1}


class SingleLabeledEditsTest(unittest.TestCase):
    """One field at a time — model answering, model abstaining, both land."""

    def test_colour_via_model(self):                                   # (1)
        saved, toasts, _, calls = _drive(
            "colour dark blue",
            classify=_tool("update_lead", field="color", value="dark blue"))
        self.assertEqual(calls["classify"], 1)
        self.assertEqual(saved.get("color"), "dark blue")
        self.assertTrue(any("Updated" in t for t in toasts), toasts)

    def test_price_via_model_gets_its_dollar(self):                    # (2)
        saved, _, _, _ = _drive(
            "price 200", classify=_tool("update_lead", field="price",
                                        value="200"))
        self.assertEqual(saved.get("pending_price"), "$200")

    def test_phone_when_the_model_abstains(self):                      # (3)
        saved, toasts, _, calls = _drive("phone 551-301-3737", classify=ABSTAIN)
        self.assertEqual(calls["classify"], 1)
        self.assertEqual(saved.get("pending_phone_number"), "551-301-3737")
        self.assertTrue(any("Updated" in t for t in toasts), toasts)

    def test_insurer_via_model_is_canonicalised(self):                 # (4)
        saved, _, _, _ = _drive(
            "insurance geico",
            classify=_tool("update_lead", field="insurance_company",
                           value="geico"))
        self.assertEqual((saved.get("insurance_company") or "").lower(), "geico")

    def test_vin_when_the_model_returns_intent_none(self):             # (5)
        saved, toasts, _, calls = _drive(f"vin {VIN17}", classify=NONE_INTENT)
        self.assertEqual(calls["classify"], 1)
        self.assertEqual(saved.get("vin"), VIN17)
        self.assertTrue(any("vin" in t.lower() for t in toasts), toasts)

    def test_last_name_only_via_model(self):                           # (6)
        saved, _, _, _ = _drive(
            "the last name is Delgado-Cruz",
            classify=_tool("update_lead", field="last_name",
                           value="Delgado-Cruz"))
        self.assertEqual(saved.get("last_name"), "Delgado-Cruz")
        self.assertEqual(saved.get("name"), "Delgado-Cruz")


class WholeSentencesTest(unittest.TestCase):
    """The point of the layer: sentences with no label become the right edit."""

    def test_the_car_is_actually_grey(self):                           # (7)
        saved, _, _, _ = _drive(
            "the client said the car is actually grey",
            classify=_tool("update_lead", field="color", value="grey"))
        self.assertEqual((saved.get("color") or "").lower(), "grey")

    def test_a_phone_said_mid_sentence_is_extracted(self):             # (8)
        saved, _, _, _ = _drive(
            "her number is 551 301 3737 call after 5",
            classify=_tool("update_lead", field="phone",
                           value="551 301 3737 call after 5"))
        # the sanitizer pulls the number OUT of the sentence
        self.assertEqual(saved.get("pending_phone_number"), "551 301 3737")

    def test_delivery_address_in_a_sentence(self):                     # (9)
        saved, _, _, _ = _drive(
            "they want it delivered to 55 River Rd",
            classify=_tool("update_lead", field="delivery_address",
                           value="55 River Rd"))
        self.assertEqual(saved.get("delivery_address"), "55 River Rd")


class PolarityTrapsTest(unittest.TestCase):
    """Negations must never read as their own opposite."""

    def test_no_toll_writes_no_price(self):                            # (10)
        saved, toasts, _, calls = _drive("no toll", classify=ABSTAIN)
        self.assertEqual(calls["classify"], 1)
        self.assertEqual(saved, {})
        self.assertFalse(any("Updated" in t for t in toasts), toasts)

    def test_already_has_insurance_means_addon_off(self):              # (11)
        saved, _, _, _ = _drive(
            "client already has insurance so none needed", classify=ABSTAIN)
        self.assertIs(saved.get("wants_insurance"), False)
        # and the carrier field was not touched by the remark
        self.assertNotIn("insurance_company", saved)

    def test_dont_add_insurance_contains_add_insurance(self):          # (12)
        saved, _, _, _ = _drive("don't add insurance", classify=ABSTAIN)
        self.assertIs(saved.get("wants_insurance"), False)

    def test_model_reads_already_covered_as_off(self):                 # (13)
        saved, _, _, _ = _drive(
            "they're already covered, no need",
            classify=_tool("set_insurance_addon", enable=False))
        self.assertIs(saved.get("wants_insurance"), False)

    def test_hallucinated_price_from_no_toll_is_declined(self):        # (14)
        # The model wrongly claims "no toll on this job" is a price edit. The
        # sanitizer finds no digits, the tool declines, nothing is written.
        saved, toasts, _, calls = _drive(
            "no toll on this job",
            classify=_tool("update_lead", field="price",
                           value="no toll on this job"))
        self.assertEqual(calls["classify"], 1)
        self.assertEqual(saved, {})
        self.assertFalse(any("Updated" in t for t in toasts), toasts)


class NotesAreNotCommandsTest(unittest.TestCase):
    """A note may CONTAIN command words. It stays a note."""

    def test_driver_note_containing_the_word_submit(self):             # (15)
        with mock.patch.object(bot, "_continue_phase1_after_ai_review",
                               mock.AsyncMock(return_value=-1)) as submit:
            saved, _, _, calls = _drive(
                "driver note: call before you submit anything",
                classify=ABSTAIN)
            submit.assert_not_called()
        self.assertEqual(calls["classify"], 1)
        self.assertIn("call before you submit",
                      saved.get("special_request_drivers") or "")

    def test_issuer_note_is_filed_as_a_note(self):                     # (16)
        saved, _, _, _ = _drive(
            "issuer note waiting on the title photo", classify=ABSTAIN)
        self.assertIn("waiting on the title photo",
                      saved.get("special_request_issuers") or "")

    def test_hallucinated_submit_on_a_note_only_asks(self):            # (17)
        # GAUNTLET-FINDING (documented, reported): if the model misreads a note
        # as submit_lead, the strict-word guard correctly refuses to send — but
        # the note text itself is CONSUMED by the confirmation ask and never
        # written to the card. The operator has to resend the note.
        with mock.patch.object(bot, "_continue_phase1_after_ai_review",
                               mock.AsyncMock(return_value=-1)) as submit:
            saved, _, replies, _ = _drive(
                "driver note: send it out after John confirms",
                classify=_tool("submit_lead"))
            submit.assert_not_called()
        self.assertTrue(any("yes" in r.lower() for r in replies), replies)
        self.assertNotIn("special_request_drivers", saved)


class DossierPasteTest(unittest.TestCase):
    """A whole client pasted at once, extracted by the model."""

    DOSSIER = ("Rolando Rodriguez\n"
               "616 Adams Ave, Elizabeth NJ 07201\n"
               f"{VIN17}\n"
               "2007 Toyota Camry\n"
               "Grey\n"
               "551 301 3737\n"
               "RRod782@Gmail.com\n"
               "200")

    def test_every_empty_field_lands_sanitized(self):                  # (18)
        saved, toasts, _, calls = _drive(
            self.DOSSIER,
            classify=_tool("update_lead_fields",
                           first_name="Rolando", last_name="Rodriguez",
                           address="616 Adams Ave",
                           city_state_zip="Elizabeth NJ 07201",
                           vin=VIN17, car="2007 Toyota Camry", color="Grey",
                           phone="551 301 3737", email="RRod782@Gmail.com",
                           price="200"))
        self.assertEqual(calls["classify"], 1)
        self.assertEqual(saved.get("name"), "Rolando Rodriguez")
        self.assertEqual(saved.get("address"), "616 Adams Ave")
        self.assertEqual(saved.get("city_state_zip"), "Elizabeth NJ 07201")
        self.assertEqual(saved.get("vin"), VIN17)
        self.assertEqual(saved.get("car"), "2007 Toyota Camry")
        self.assertEqual((saved.get("color") or "").lower(), "grey")
        self.assertEqual(saved.get("pending_phone_number"), "551 301 3737")
        self.assertEqual(saved.get("email"), "rrod782@gmail.com")
        self.assertEqual(saved.get("pending_price"), "$200")
        self.assertTrue(any("Updated" in t for t in toasts), toasts)

    def test_a_dossier_fills_around_existing_values(self):             # (19)
        # "-" counts as blank (so vin fills); a real value is never clobbered.
        saved, _, _, _ = _drive(
            "second paste for the same client",
            card={"vin": "-", "car": "-", "color": "Black",
                  "first_name": "Dana", "name": "Dana"},
            classify=_tool("update_lead_fields", color="white",
                           first_name="Rolando", vin=VIN17,
                           phone="5513013737"))
        self.assertEqual(saved.get("color"), "Black")
        self.assertEqual(saved.get("first_name"), "Dana")
        self.assertEqual(saved.get("vin"), VIN17)
        self.assertEqual(saved.get("pending_phone_number"), "5513013737")


class DriverNameSentencesTest(unittest.TestCase):
    """"give it to Kita" — the model names the driver, the picker code decides."""

    def test_give_it_to_kita(self):                                    # (20)
        saved, toasts, _, _ = _drive(
            "give it to Kita", drivers=ROSTER,
            classify=_tool("select_driver", driver="Kita"))
        self.assertEqual(saved.get("selected_driver_names"), "Kita")
        self.assertEqual(saved.get("selected_driver_ids"), [1])
        self.assertTrue(any("Kita" in t for t in toasts), toasts)

    def test_let_rob_take_this_one(self):                              # (21)
        saved, _, _, _ = _drive(
            "let Rob take this one", drivers=ROSTER,
            classify=_tool("select_driver", driver="Rob"))
        self.assertEqual(saved.get("selected_driver_names"), "Rob")

    def test_an_unknown_name_opens_the_picker(self):                   # (22)
        saved, _, replies, _ = _drive(
            "hand it to Zorro", drivers=ROSTER,
            classify=_tool("select_driver", driver="Zorro"))
        self.assertNotIn("selected_driver_names", saved)
        self.assertTrue(any("No driver matched" in r for r in replies), replies)

    def test_a_parked_ask_consumes_the_next_message(self):             # (23, 24)
        shared = _shared()
        _, _, replies, calls1 = _drive(
            "assign a driver", drivers=ROSTER, user_data=shared,
            classify=_tool("select_driver"))
        self.assertEqual(calls1["classify"], 1)
        self.assertTrue(any("driver" in r.lower() for r in replies), replies)
        self.assertIn(nl_router.NL_PENDING_KEY, shared)
        saved, _, _, calls2 = _drive("Kita", drivers=ROSTER, user_data=shared,
                                     classify=ABSTAIN)
        self.assertEqual(calls2["classify"], 0)   # the answer needs no model
        self.assertEqual(saved.get("selected_driver_names"), "Kita")
        self.assertNotIn(nl_router.NL_PENDING_KEY, shared)

    def test_a_parked_ask_survives_the_layer_going_dark(self):         # (25)
        # The breaker can trip between the question and its answer. The OR in
        # the front door still consumes the parked slot with the layer off.
        shared = _shared()
        _drive("assign a driver", drivers=ROSTER, user_data=shared,
               classify=_tool("select_driver"))
        saved, _, _, calls = _drive("Rob", drivers=ROSTER, user_data=shared,
                                    classify=ABSTAIN, layer_on=False)
        self.assertEqual(calls["classify"], 0)
        self.assertEqual(saved.get("selected_driver_names"), "Rob")


class CancelWordsNeverReachTheModelTest(unittest.TestCase):
    """Cancel/restart is decided BEFORE the front door — the model never sees it."""

    def test_every_cancel_word_returns_before_the_front_door(self):    # (26-29)
        for word, kind in (("cancel", "cancel"), ("scrap that", "cancel"),
                           ("start over", "restart"), ("new lead", "restart")):
            with self.subTest(word=word):
                with mock.patch.object(
                        bot, "_do_cancel_or_restart",
                        mock.AsyncMock(return_value=-1)) as cancel:
                    saved, _, _, calls = _drive(word, classify=ABSTAIN)
                self.assertEqual(calls["classify"], 0)
                cancel.assert_called_once()
                self.assertEqual(cancel.call_args.args[2], kind)
                self.assertEqual(saved, {})

    def test_cancel_leaves_a_parked_slot_behind(self):                 # (30)
        # GAUNTLET-FINDING (documented, reported): the real cancel cleanup
        # (_clear_lead_conversation_user_data) does NOT drop nl_pending, so a
        # parked question survives the card being cancelled and will eat the
        # first message typed at the FRESH card as its answer (180s TTL).
        shared = _shared()
        _drive("assign a driver", drivers=ROSTER, user_data=shared,
               classify=_tool("select_driver"))
        self.assertIn(nl_router.NL_PENDING_KEY, shared)

        async def _real_cleanup(update, context, kind):
            bot._clear_lead_conversation_user_data(context)
            return -1

        with mock.patch.object(bot, "_do_cancel_or_restart",
                               mock.AsyncMock(side_effect=_real_cleanup)):
            _, _, _, calls = _drive("cancel", user_data=shared, classify=ABSTAIN)
        self.assertEqual(calls["classify"], 0)
        self.assertIn(nl_router.NL_PENDING_KEY, shared)   # <-- the leak


class SubmitConfirmFlowTest(unittest.TestCase):

    def test_soft_submit_then_no_wait_then_yes_never_sends(self):      # (31-33)
        shared = _shared()
        with mock.patch.object(bot, "_continue_phase1_after_ai_review",
                               mock.AsyncMock(return_value=-1)) as submit:
            _, _, replies, _ = _drive(
                "ok looks good, send it out when you can",
                user_data=shared, classify=_tool("submit_lead"))
            self.assertTrue(any("yes" in r.lower() for r in replies), replies)
            self.assertIn("chat_submit_pending", shared)

            _drive("no wait", user_data=shared, classify=ABSTAIN)
            self.assertNotIn("chat_submit_pending", shared)   # abandoned

            _, _, _, calls3 = _drive("yes", user_data=shared, classify=ABSTAIN)
            submit.assert_not_called()
        # with no pending confirm, "yes" went to the model like any text
        self.assertEqual(calls3["classify"], 1)

    def test_a_strict_send_it_out_sends_directly(self):                # (34)
        with mock.patch.object(bot, "_continue_phase1_after_ai_review",
                               mock.AsyncMock(return_value=-1)) as submit:
            _, _, replies, _ = _drive("send it out",
                                      classify=_tool("submit_lead"))
            submit.assert_called_once()
        self.assertFalse(any("Reply" in r for r in replies), replies)

    def test_the_submit_confirm_survives_a_cancel(self):               # (35)
        # GAUNTLET-FINDING (documented, reported): chat_submit_pending is not
        # cleared by the real cancel cleanup either, so within its 90s TTL a
        # "yes" typed at the FRESH card after a cancel still fires the submit.
        shared = _shared()

        async def _real_cleanup(update, context, kind):
            bot._clear_lead_conversation_user_data(context)
            return -1

        with mock.patch.object(bot, "_continue_phase1_after_ai_review",
                               mock.AsyncMock(return_value=-1)) as submit:
            _drive("ok looks good, send it out when you can",
                   user_data=shared, classify=_tool("submit_lead"))
            self.assertIn("chat_submit_pending", shared)
            with mock.patch.object(bot, "_do_cancel_or_restart",
                                   mock.AsyncMock(side_effect=_real_cleanup)):
                _drive("cancel", user_data=shared, classify=ABSTAIN)
            self.assertIn("chat_submit_pending", shared)      # <-- the leak
            _drive("yes", user_data=shared, classify=ABSTAIN)
            submit.assert_called_once()                       # <-- fires anyway


class QuotaMidConversationTest(unittest.TestCase):
    """The account dies mid-lead: warn once, fall back, keep editing."""

    def setUp(self):
        bot._ai_warned_at.clear()
        nl_router._breaker_reset()

    tearDown = setUp

    @staticmethod
    def _quota(_txt):
        raise AIVisionQuotaError("quota")

    def test_quota_warns_once_and_the_ladder_keeps_applying(self):     # (36-38)
        shared = _shared()
        saved1, _, _, _ = _drive(
            "make the color white", user_data=shared,
            classify=_tool("update_lead", field="color", value="white"))
        self.assertEqual((saved1.get("color") or "").lower(), "white")

        saved2, _, replies2, calls2 = _drive(
            "price 150 plus toll", user_data=shared, classify=self._quota)
        self.assertEqual(calls2["classify"], 1)
        self.assertEqual(saved2.get("pending_price"), "$150 + toll")
        self.assertTrue(any("AI understanding is unavailable" in r
                            for r in replies2), replies2)

        saved3, _, replies3, _ = _drive(
            "price 175", user_data=shared, classify=self._quota)
        self.assertEqual(saved3.get("pending_price"), "$175")
        self.assertFalse(any("AI understanding" in r for r in replies3),
                         "the outage warning must not repeat within the hour")


class HallucinationTest(unittest.TestCase):
    """The model invents fields, cars and tools. None of it may corrupt a card."""

    def test_an_unknown_field_is_declined(self):                       # (39)
        saved, _, _, calls = _drive(
            "middle name Quincy",
            classify=_tool("update_lead", field="middle_name", value="Quincy"))
        self.assertEqual(calls["classify"], 1)
        self.assertNotIn("middle_name", saved)   # no invented key ever lands

    def test_a_non_card_tool_falls_through_without_a_crash(self):      # (40)
        with mock.patch.object(bot, "_continue_phase1_after_ai_review",
                               mock.AsyncMock(return_value=-1)) as submit:
            saved, _, _, calls = _drive(
                "who are the drivers",
                classify={"intent": "list_drivers", "args": {},
                          "tool": "list_drivers"})
            submit.assert_not_called()
        self.assertEqual(calls["classify"], 1)
        self.assertNotIn("selected_driver_names", saved)

    def test_an_unknown_tool_name_falls_through(self):                 # (41)
        saved, _, _, calls = _drive(
            "no toll", classify={"intent": "reboot_server", "args": {},
                                 "tool": "reboot_server"})
        self.assertEqual(calls["classify"], 1)
        self.assertEqual(saved, {})

    def test_vehicle_99_lands_nowhere(self):                           # (42)
        # GAUNTLET-FINDING (documented, reported): a hallucinated vehicle=99
        # writes NOTHING (car 1's vin is untouched, no phantom car appears) —
        # but the operator still gets a green "Updated: vin" toast for an edit
        # that landed nowhere.
        saved, toasts, _, _ = _drive(
            f"vin for the ninth car is {VIN17}",
            classify=_tool("update_lead", field="vin", value=VIN17, vehicle=99))
        self.assertEqual(saved.get("vin"), "-")
        self.assertNotIn("extra_vehicles", saved)
        self.assertTrue(any("Updated" in t and "vin" in t for t in toasts),
                        toasts)   # the false-success toast, pinned as-is

    def test_vehicle_2_with_a_real_second_car_lands_on_it(self):       # (43)
        saved, _, _, _ = _drive(
            f"the second car's vin is {VIN17}",
            card={"vin": "-", "car": "-",
                  "extra_vehicles": [{"name": "", "vin": "", "car": ""}]},
            classify=_tool("update_lead", field="vin", value=VIN17, vehicle=2))
        self.assertEqual(saved.get("vin"), "-")               # car 1 untouched
        self.assertEqual((saved.get("extra_vehicles") or [{}])[0].get("vin"),
                         VIN17)

    def test_vehicle_as_a_digit_string_still_works(self):              # (44)
        saved, _, _, _ = _drive(
            f"second car vin {VIN17}",
            card={"vin": "-", "car": "-",
                  "extra_vehicles": [{"name": "", "vin": "", "car": ""}]},
            classify=_tool("update_lead", field="vin", value=VIN17,
                           vehicle="2"))
        self.assertEqual((saved.get("extra_vehicles") or [{}])[0].get("vin"),
                         VIN17)

    def test_update_lead_fields_of_nothing_but_nulls(self):            # (45)
        saved, toasts, _, calls = _drive(
            "sorry ignore that last message",
            classify=_tool("update_lead_fields",
                           **{f: None for f in nl_router.LEAD_FIELDS}))
        self.assertEqual(calls["classify"], 1)
        self.assertEqual(saved, {})
        self.assertFalse(any("Updated" in t for t in toasts), toasts)

    def test_update_lead_with_a_null_value(self):                      # (46)
        saved, _, _, calls = _drive(
            "phone is dead",
            classify=_tool("update_lead", field="phone", value=None))
        self.assertEqual(calls["classify"], 1)
        self.assertNotIn("pending_phone_number", saved)

    def test_a_parked_boolean_swallows_polarity(self):                 # (47, 48)
        # GAUNTLET-FINDING (documented, reported): a set_insurance_addon call
        # that arrives without its boolean parks and asks "What is the enable?"
        # (raw schema-speak). The parked answer is then fed back as a STRING,
        # and bool("leave it off") is True — the answer's polarity is ignored:
        # ANY prose answer switches the add-on ON.
        shared = _shared()
        _, _, replies, _ = _drive(
            "add the coverage thing for them", user_data=shared,
            classify=_tool("set_insurance_addon"))
        self.assertTrue(any("enable" in r.lower() for r in replies), replies)
        saved, _, _, calls = _drive("leave it off", user_data=shared,
                                    classify=ABSTAIN)
        self.assertEqual(calls["classify"], 0)
        self.assertIs(saved.get("wants_insurance"), True)   # <-- inverted

    def test_a_parked_boolean_answered_no_is_abandoned(self):          # (49)
        # "no" is command-like, so the parked call is dropped rather than fed —
        # the add-on is left untouched (the safe half of the same finding).
        shared = _shared()
        _drive("add the coverage thing for them", user_data=shared,
               classify=_tool("set_insurance_addon"))
        saved, _, _, _ = _drive("no", user_data=shared, classify=ABSTAIN)
        self.assertNotIn("wants_insurance", saved)
        self.assertNotIn(nl_router.NL_PENDING_KEY, shared)


if __name__ == "__main__":
    unittest.main()
