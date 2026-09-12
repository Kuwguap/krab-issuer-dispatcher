r"""Supervisory alerts carry the actions they are about.

Asked for: "every alert has a button to reassign to add to edit to upload
receipt receipts".

The headline is that this needs ZERO new callback prefixes. Every action named
already exists and is already redeploy-safe -- reassign_lead_ and setem_ are
top-level, receipt_for_, another_tag_ and instantpdf_ are conversation ENTRY
POINTS -- so attaching them adds no routing and no restart can kill them.

Three invariants worth the file:

  * An alert with no keyboard must make exactly the call it made before
    reply_markup existed. This path carries payment and dispatch notices; a
    behavioural change here is a change to everything.

  * One oversize callback makes Telegram refuse the WHOLE keyboard and the
    message with it. That is not hypothetical in this codebase -- a lead once
    reached zero drivers that way. _safe_inline_keyboard drops the button, never
    the alert.

  * _lead_alert_keyboard is SYNC AND PURE. It is called from handle_accept_lead,
    handle_driver_selection and the 20-second paid-tag sweep, where a blocking
    Supabase read freezes every user of the bot.

  * "Edit" is named honestly. There is no post-submit lead editor -- ph1edit_*
    is unparameterised, lives in user_data, and its Submit creates a NEW lead.
    So the button says what it does: add or fix the client's email.

Run:  venv\Scripts\python.exe -m pytest tests/test_alerts_carry_their_actions.py -q
"""
import ast
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

SRC = (ROOT / "bot.py").read_text(encoding="utf-8")
LEAD = {"id": "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa", "reference_id": "REF12345"}


def _flat(kb):
    return [] if kb is None else [b.callback_data for row in kb.inline_keyboard for b in row]


class ASafeKeyboardLosesTheButtonNotTheMessageTest(unittest.TestCase):

    def test_an_oversize_callback_is_dropped(self):
        kb = bot._safe_inline_keyboard([
            [bot.InlineKeyboardButton("fine", callback_data="ok_1")],
            [bot.InlineKeyboardButton("huge", callback_data="x" * 65)],
        ])
        self.assertEqual(["ok_1"], _flat(kb))

    def test_exactly_64_bytes_is_allowed(self):
        """Telegram's limit is 1-64 bytes inclusive; rejecting 64 would drop
        working buttons."""
        self.assertEqual(["y" * 64], _flat(bot._safe_inline_keyboard(
            [[bot.InlineKeyboardButton("ok", callback_data="y" * 64)]])))

    def test_a_url_button_has_no_callback_and_survives(self):
        kb = bot._safe_inline_keyboard(
            [[bot.InlineKeyboardButton("open", url="https://example.com/" + "z" * 200)]])
        self.assertEqual(1, len(kb.inline_keyboard[0]))

    def test_nothing_left_means_no_keyboard_at_all(self):
        """An empty InlineKeyboardMarkup is itself refused by Telegram."""
        self.assertIsNone(bot._safe_inline_keyboard([]))
        self.assertIsNone(bot._safe_inline_keyboard([[], []]))
        self.assertIsNone(bot._safe_inline_keyboard(
            [[bot.InlineKeyboardButton("huge", callback_data="x" * 65)]]))

    def test_an_emptied_row_does_not_leave_a_hole(self):
        kb = bot._safe_inline_keyboard([
            [bot.InlineKeyboardButton("huge", callback_data="x" * 65)],
            [bot.InlineKeyboardButton("fine", callback_data="ok_2")],
        ])
        self.assertEqual(1, len(kb.inline_keyboard))


class TheLeadKeyboardTest(unittest.TestCase):

    def test_each_action_uses_the_prefix_that_already_routes(self):
        kb = bot._lead_alert_keyboard(LEAD, reassign=True, add=True, email=True,
                                      receipt=True, release=True)
        self.assertEqual([
            "reassign_lead_" + LEAD["id"],
            "another_tag_" + LEAD["id"],
            "setem_" + LEAD["id"],
            "receipt_for_REF12345",
            bot.INSTANT_PDF_CB + LEAD["id"],
        ], _flat(kb))

    def test_nothing_asked_for_means_no_keyboard(self):
        self.assertIsNone(bot._lead_alert_keyboard(LEAD))

    def test_a_lead_with_no_id_gets_nothing(self):
        """Every one of these callbacks addresses the lead. Without an id they
        would route to a handler that cannot find it."""
        self.assertIsNone(bot._lead_alert_keyboard({}, reassign=True, email=True))
        self.assertIsNone(bot._lead_alert_keyboard(None, reassign=True))

    def test_upload_needs_a_real_reference(self):
        """receipt_for_ carries the REFERENCE, not the id; 'N/A' would route to
        a lead that does not exist."""
        for ref in ("", "   ", "N/A", "n/a", None):
            with self.subTest(ref=ref):
                kb = bot._lead_alert_keyboard(dict(LEAD, reference_id=ref), receipt=True)
                self.assertIsNone(kb)

    def test_a_reference_too_long_to_address_drops_only_that_button(self):
        kb = bot._lead_alert_keyboard(dict(LEAD, reference_id="R" * 80),
                                      reassign=True, receipt=True)
        self.assertEqual(["reassign_lead_" + LEAD["id"]], _flat(kb))

    def test_every_button_fits_telegrams_64_bytes(self):
        kb = bot._lead_alert_keyboard(LEAD, reassign=True, add=True, email=True,
                                      receipt=True, release=True)
        for data in _flat(kb):
            with self.subTest(data=data):
                self.assertLessEqual(len(data.encode("utf-8")), 64, data)

    def test_edit_is_named_for_what_it_actually_does(self):
        """There is no post-submit lead editor. A button labelled Edit would be
        a lie; this one says email because email is what it changes."""
        kb = bot._lead_alert_keyboard(LEAD, email=True)
        self.assertIn("email", kb.inline_keyboard[0][0].text.lower())


class ItMustNotTouchTheEventLoopTest(unittest.TestCase):
    """It is called from handle_accept_lead, handle_driver_selection and the
    20-second paid-tag sweep. One sync Supabase read there is ~200ms with the
    whole bot stopped behind it -- the scar that put 373 calls on to_thread."""

    def _fn(self):
        tree = ast.parse(SRC)
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and node.name in ("_lead_alert_keyboard", "_safe_inline_keyboard"):
                yield node

    def test_neither_helper_is_async(self):
        fns = list(self._fn())
        self.assertEqual(2, len(fns))
        for f in fns:
            self.assertIsInstance(f, ast.FunctionDef, f.name)

    def test_neither_awaits_nor_reads_the_database(self):
        for f in self._fn():
            for node in ast.walk(f):
                self.assertNotIsInstance(node, ast.Await, f.name)
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                    self.assertNotEqual("db", node.value.id,
                                        f"{f.name} reads db.{node.attr}")


class TellSupervisorsIsUnchangedWithoutAKeyboardTest(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.calls = []
        self.ctx = mock.MagicMock()

        async def send(**kw):
            self.calls.append(kw)
            if kw.get("_fail"):
                raise RuntimeError("no")
            return mock.MagicMock()

        self.ctx.bot.send_message = mock.AsyncMock(side_effect=send)
        self.enterContext(mock.patch.object(bot, "_all_supervisory_chat_ids", lambda: [11, 22]))

    async def test_text_is_still_positional(self):
        """Ten call sites and test_website_lead_insurance.py pass it that way."""
        await bot._tell_supervisors(self.ctx, "hello")
        self.assertEqual(["hello", "hello"], [k["text"] for k in self.calls])

    async def test_no_keyboard_means_no_reply_markup_key_at_all(self):
        await bot._tell_supervisors(self.ctx, "hello")
        for k in self.calls:
            self.assertNotIn("reply_markup", k)

    async def test_a_keyboard_reaches_every_supervisor(self):
        kb = bot._lead_alert_keyboard(LEAD, reassign=True)
        await bot._tell_supervisors(self.ctx, "hello", reply_markup=kb)
        self.assertEqual([kb, kb], [k["reply_markup"] for k in self.calls])

    async def test_skip_still_skips(self):
        await bot._tell_supervisors(self.ctx, "hello", skip=11)
        self.assertEqual([22], [k["chat_id"] for k in self.calls])


class ARefusedKeyboardCostsTheButtonsNotTheAlertTest(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.calls = []
        self.ctx = mock.MagicMock()
        self.enterContext(mock.patch.object(bot, "_all_supervisory_chat_ids", lambda: [11]))

    def _bot(self, fail_while):
        async def send(**kw):
            self.calls.append(kw)
            if fail_while(kw):
                raise RuntimeError("refused")
            return mock.MagicMock()
        self.ctx.bot.send_message = mock.AsyncMock(side_effect=send)

    async def test_html_then_plain_then_no_keyboard(self):
        """Telegram can refuse the keyboard itself. The words matter more."""
        kb = bot._lead_alert_keyboard(LEAD, reassign=True)
        self._bot(lambda kw: "reply_markup" in kw)
        await bot._tell_supervisors(self.ctx, "<b>hi</b>", reply_markup=kb)
        self.assertEqual(3, len(self.calls))
        self.assertEqual("hi", self.calls[-1]["text"])
        self.assertNotIn("reply_markup", self.calls[-1])

    async def test_plain_succeeding_stops_there(self):
        kb = bot._lead_alert_keyboard(LEAD, reassign=True)
        self._bot(lambda kw: kw.get("parse_mode") == "HTML")
        await bot._tell_supervisors(self.ctx, "<b>hi</b>", reply_markup=kb)
        self.assertEqual(2, len(self.calls))
        self.assertIs(kb, self.calls[-1]["reply_markup"])

    async def test_without_a_keyboard_there_is_no_third_attempt(self):
        """The old two-step ladder, byte for byte, when nobody asked for
        buttons."""
        self._bot(lambda kw: True)
        await bot._tell_supervisors(self.ctx, "<b>hi</b>")
        self.assertEqual(2, len(self.calls))


class WhichAlertsCarryActionsIsADecisionTest(unittest.TestCase):
    """Not "every alert" by reflex -- every alert that has something true to
    offer. Both halves of that are pinned here so neither drifts silently."""

    CARRY = 8

    def test_the_count_is_what_was_decided(self):
        self.assertEqual(self.CARRY, SRC.count("reply_markup=_lead_alert_keyboard("))

    def test_release_appears_only_on_instant_tag_alerts(self):
        """instantpdf_ means "send the CASH tag now". On any other lead that
        button is a promise the handler cannot keep."""
        for i, line in enumerate(SRC.splitlines(), 1):
            if "_lead_alert_keyboard(" in line and "release=True" in line:
                fn = self._enclosing(i)
                with self.subTest(line=i, fn=fn):
                    self.assertIn(fn, {"_deliver_skip_dispatch", "_warn_stuck_instant_tag",
                                       "_instant_tag_link_after_accept"}, fn)

    def _enclosing(self, lineno):
        import re as _re
        head = "\n".join(SRC.splitlines()[:lineno])
        return _re.findall(r"^(?:async )?def ([A-Za-z_][A-Za-z0-9_]*)\(", head, _re.M)[-1]

    def test_the_four_deliberate_omissions_stay_bare(self):
        """Each of these would be a lie or a hazard, and each was flagged to the
        owner rather than quietly skipped. If one gains a keyboard, it should be
        because somebody decided to, not because a sweep caught it."""
        for marker in (
            # fires BEFORE delivery, superseded seconds later -- a Release here
            # forces a tag already in flight
            "<b>Cash payment PAID</b>",
        ):
            i = SRC.index(marker)
            end = SRC.index("))", i)
            with self.subTest(marker=marker):
                self.assertNotIn("_lead_alert_keyboard", SRC[i:end + 80], marker)


if __name__ == "__main__":
    unittest.main()
