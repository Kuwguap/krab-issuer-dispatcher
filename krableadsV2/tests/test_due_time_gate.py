r"""The bot asks when the tag is due, the way it asks for a missing price.

Asked for: "this means just the same way when price is missing in main lead the
bot asks now also ask for arrival date and time".

Modelled on the price gate, NOT on the missing-fields queue, and the difference
matters: that queue only fires through a model call that returns nothing without
an OPENAI_API_KEY, it writes its answer into free-text extra_info, and
_field_already_filled treats any note at all as the answer — so a lead whose
notes read "gate code 4432" would never be asked.

Two things held still here:

* "No time promised" must stay reachable. Without it the gate is a wall, and a
  lead that genuinely has no promised time could not be entered at all. NULL is
  an honest answer; it is what lets the board tell an assumption from a promise.
* Typed input that cannot be read is never stored. handle_missing_field falls
  back to keeping whatever was typed; against a timestamptz that is a write
  error, and a value that LOOKS stored is the promise-nobody-made failure.

Run:  venv\Scripts\python.exe -m pytest tests/test_due_time_gate.py -q
"""
import asyncio
import os
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy-key")

import bot as B  # noqa: E402
from utils.timezone import NY_TZ  # noqa: E402

NOW = NY_TZ.localize(datetime(2026, 9, 9, 10, 0))    # a Wednesday morning


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def _msg():
    m = mock.MagicMock()
    m.reply_text = mock.AsyncMock()
    m.chat_id = -100123
    return m


def _said(m):
    return " ".join(str(c[0][0]) for c in m.reply_text.await_args_list)


class ItOnlyAsksForWhatIsAbsentTest(unittest.TestCase):
    """The price gate's rule, and the reason this is not the missing-fields queue."""

    def test_a_lead_with_no_due_time_is_asked(self):
        self.assertTrue(B._due_missing({}))
        self.assertTrue(B._due_missing({"pending_due_at": ""}))
        self.assertTrue(B._due_missing({"pending_due_at": "not a timestamp"}))

    def test_a_lead_that_has_one_is_not_asked_again(self):
        self.assertFalse(B._due_missing(
            {"pending_due_at": "2026-09-10T15:00:00-04:00"}))

    def test_no_time_promised_is_remembered_so_it_stops_asking(self):
        """Otherwise every pass through the funnel asks again and the lead can
        never be finished."""
        self.assertFalse(B._due_missing({"due_declined": "1"}))

    def test_it_does_not_touch_the_missing_fields_queue(self):
        """delivery_date there maps onto free-text extra_info; leaving it alone
        is the point."""
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        i = src.index("PHASE1_OPTIONAL_FIELDS = {")
        self.assertIn("delivery_date", src[i:i + 200])


class ThePickerTest(unittest.TestCase):

    def _kb(self, now=NOW):
        return B._due_picker_keyboard(now=now)

    def _labels(self, kb):
        return [b.text for row in kb.inline_keyboard for b in row]

    def _data(self, kb):
        return [b.callback_data for row in kb.inline_keyboard for b in row]

    def test_today_and_tomorrow_are_both_offered(self):
        labels = self._labels(self._kb())
        self.assertIn("— Today —", labels)
        self.assertIn("— Tomorrow —", labels)

    def test_an_hour_that_has_gone_is_not_offered(self):
        """At 10am, 9am today is not a choice — offering it is how a lead gets
        promised for a time that has already passed."""
        data = self._data(self._kb())
        self.assertNotIn(f"{B.PH1_DUE_CB}20260909_0900", data)
        self.assertIn(f"{B.PH1_DUE_CB}20260909_1100", data)

    def test_late_in_the_day_today_disappears_entirely(self):
        kb = self._kb(now=NY_TZ.localize(datetime(2026, 9, 9, 23, 30)))
        self.assertNotIn("— Today —", self._labels(kb))
        self.assertIn("— Tomorrow —", self._labels(kb))
        self.assertTrue(any("20260910_" in d for d in self._data(kb)))

    def test_one_tap_settles_the_day_and_the_hour(self):
        """No half-armed state of the kind the toll toggle has to carry."""
        for d in self._data(self._kb()):
            if d.startswith(B.PH1_DUE_CB) and d != B.PH1_DUE_CB + B.PH1_DUE_NONE:
                self.assertRegex(d, r"^ph1due_\d{8}_\d{4}$")

    def test_there_is_always_a_way_out(self):
        """Without it the gate is a wall and a lead with no promised time
        cannot be entered."""
        self.assertIn(B.PH1_DUE_CB + B.PH1_DUE_NONE, self._data(self._kb()))

    def test_every_callback_fits_telegrams_limit(self):
        for d in self._data(self._kb()):
            self.assertLessEqual(len(d.encode()), 64, d)


class TappingOneTest(unittest.TestCase):

    def _tap(self, data, state=None):
        q = mock.MagicMock()
        q.data = data
        q.message = _msg()
        q.from_user = types.SimpleNamespace(id=777)
        u = mock.MagicMock()
        u.callback_query = q
        db = mock.MagicMock()
        db.get_user_state.return_value = {"data": dict(state or {})}
        saved = {}

        def set_state(uid, step, data_):
            saved.update(data_)
            return True

        db.set_user_state.side_effect = set_state
        ctx = mock.MagicMock()
        ctx.user_data = {}
        with mock.patch.object(B, "db", db), \
             mock.patch.object(B, "_safe_answer_callback_query", new=mock.AsyncMock()), \
             mock.patch.object(B, "_send_vanishing", new=mock.AsyncMock()), \
             mock.patch.object(B, "_prompt_issuer_special_request",
                               new=mock.AsyncMock(return_value="went-on")) as onward:
            out = run(B.handle_due_pick(u, ctx))
        return out, saved, onward, q.message

    def test_a_slot_is_stored_as_a_real_timestamp(self):
        out, saved, onward, _ = self._tap(f"{B.PH1_DUE_CB}20260910_1500")
        self.assertEqual("went-on", out)
        stamped = datetime.fromisoformat(saved["pending_due_at"]).astimezone(NY_TZ)
        self.assertEqual((2026, 9, 10, 15, 0),
                         (stamped.year, stamped.month, stamped.day,
                          stamped.hour, stamped.minute))
        onward.assert_awaited_once()

    def test_no_time_promised_stores_nothing_and_moves_on(self):
        out, saved, onward, _ = self._tap(B.PH1_DUE_CB + B.PH1_DUE_NONE)
        self.assertEqual("went-on", out)
        self.assertNotIn("pending_due_at", saved)
        self.assertEqual("1", saved.get("due_declined"))
        onward.assert_awaited_once()

    def test_choosing_a_time_clears_an_earlier_refusal(self):
        _, saved, _, _ = self._tap(f"{B.PH1_DUE_CB}20260910_1500",
                                   state={"due_declined": "1"})
        self.assertNotIn("due_declined", saved)

    def test_a_malformed_tap_re_asks_instead_of_crashing(self):
        out, saved, onward, msg = self._tap(f"{B.PH1_DUE_CB}rubbish")
        self.assertEqual(B.STATE_DUE_TIME, out)
        self.assertNotIn("pending_due_at", saved)
        onward.assert_not_awaited()


class TypingItTest(unittest.TestCase):

    def _type(self, text):
        m = _msg()
        m.text = text
        u = mock.MagicMock()
        u.effective_message = m
        u.effective_user = types.SimpleNamespace(id=777)
        db = mock.MagicMock()
        db.get_user_state.return_value = {"data": {}}
        saved = {}
        db.set_user_state.side_effect = lambda uid, step, d: saved.update(d) or True
        ctx = mock.MagicMock()
        ctx.user_data = {}
        with mock.patch.object(B, "db", db), \
             mock.patch.object(B, "_prompt_issuer_special_request",
                               new=mock.AsyncMock(return_value="went-on")) as onward:
            out = run(B.handle_due_time(u, ctx))
        return out, saved, onward, m

    def test_a_readable_time_is_stored_and_read_back(self):
        out, saved, onward, m = self._type("tomorrow 3pm")
        self.assertEqual("went-on", out)
        self.assertTrue(saved.get("pending_due_at"))
        self.assertIn("Due:", _said(m))
        onward.assert_awaited_once()

    def test_something_it_cannot_read_is_never_stored(self):
        """An unparsed string is a write error against a timestamptz, and one
        that looks stored is a promise nobody made."""
        out, saved, onward, m = self._type("sometime in the morning")
        self.assertEqual(B.STATE_DUE_TIME, out)
        self.assertNotIn("pending_due_at", saved)
        onward.assert_not_awaited()

    def test_a_refusal_comes_back_with_the_picker(self):
        _, _, _, m = self._type("3")
        self.assertIsNotNone(m.reply_text.await_args[1].get("reply_markup"))

    def test_the_refusal_says_what_to_type(self):
        _, _, _, m = self._type("3")
        self.assertRegex(_said(m), r"\d\s*(?:pm|am)|hour|time")


class ItIsWiredIntoTheOneFunnelTest(unittest.TestCase):

    def test_the_gate_sits_at_the_top_of_the_funnel(self):
        """_prompt_issuer_special_request is the single point all three live
        paths reach, so no path can skip the question."""
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        i = src.index("async def _prompt_issuer_special_request(")
        block = src[i:i + 1400]
        self.assertIn("_ensure_due_before_notes(", block)
        self.assertLess(block.index("_ensure_due_before_notes("),
                        block.index("special_request_issuers"))

    def test_it_runs_downstream_of_the_phone_and_price_gate(self):
        """Both roads out of _ensure_phone_price_before_files end at the funnel:
        a valid phone goes straight there, and _phase2_ask arrives there once it
        has what it needs. So the due question is always asked after, never
        instead of, the phone.

        Note what the phone/price gate actually does, which is not what it is
        usually described as: PHONE is the only hard requirement. A missing
        price shows as "-" on the review rather than blocking. The due question
        does block once — with a one-tap "No time promised" out — because a
        lead with no expected time is the thing that cannot be chased."""
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        i = src.index("async def _ensure_phone_price_before_files(")
        block = src[i:i + 1200]
        self.assertIn("_prompt_issuer_special_request(", block)
        self.assertIn("_phase2_ask(", block)
        self.assertIn('_is_valid_pending_phone(state_data.get("pending_phone_number"))',
                      block)

    def test_the_answer_reaches_the_insert(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        self.assertIn('due_at = state_data.pop("pending_due_at", None)', src)
        self.assertIn('"expected_delivery_at": due_at or None,', src)
        self.assertIn('"expected_delivery_set_by"', src)

    def test_the_pending_value_never_leaks_onto_the_card(self):
        self.assertIn("pending_due_at", B._PHASE1_STATE_EXCLUDE)

    def test_the_state_and_the_always_live_tap_are_registered(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        self.assertIn("STATE_DUE_TIME: [", src)
        self.assertIn("CallbackQueryHandler(handle_due_pick, "
                      'pattern=f"^{PH1_DUE_CB}")', src)
        i = src.index("def _card_buttons_always_live()")
        self.assertIn("handle_due_pick", src[i:i + 900])

    def test_extra_info_is_left_alone(self):
        """The free-text note keeps its own life: it goes to drivers verbatim
        and carries public-form keys. It is never parsed into the column."""
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        self.assertIn('"extra_info": state_data.get("extra_info", ""),', src)
        i = src.index('"expected_delivery_at": due_at or None,')
        self.assertNotIn("extra_info", src[i:i + 300])


if __name__ == "__main__":
    unittest.main()
