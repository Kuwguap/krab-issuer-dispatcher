r"""A suspension alert a supervisor can finish acting on without leaving Telegram.

Asked for:
  "Every suspension alert should have a view receipts alert right under it ...
   every suspension alert should have view receipts upload receipt. I should be
   able to view receipts up receipts on lift suspension make an exception all
   the buttons."

The three that carry the weight:

  * VIEW RECEIPTS must answer even when there is nothing to show. An owed
    receipt has no stored image BY DEFINITION (get_driver_pending_receipts
    drops every row that has one), so zero images is the normal case for a
    suspended driver. A tap that answers with an empty room reads as broken.

  * MAKE AN EXCEPTION is per-receipt, by the owner's decision. It writes the
    same exclude_from_count flag the suspension counter already subtracts, one
    lead at a time. It must never reach waive_driver_pending_receipts -- that
    is the blanket forgiveness Lift does, and the office asked for the opposite.

  * LIFT needs BOTH halves. Suspension is the union of drivers.is_suspended and
    a count derived from receipt debt; clearing the flag alone leaves the very
    next read to re-suspend.

Run:  venv\Scripts\python.exe -m pytest tests/test_suspension_alert_actions.py -q
"""
import os
import re
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

DRIVER_ID = "cccccccc-3333-4333-8333-cccccccccccc"
L1 = "11111111-1111-4111-8111-111111111111"
L2 = "22222222-2222-4222-8222-222222222222"
L3 = "33333333-3333-4333-8333-333333333333"
SUP = 777

ROWS = [
    {"lead_id": L1, "reference_id": "OWED0001", "lead": {}, "waived": False,
     "accepted_at": "2026-09-10T10:00:00+00:00", "has_receipt": False},
    {"lead_id": L2, "reference_id": "DONE0002", "lead": {}, "waived": False,
     "accepted_at": "2026-09-09T10:00:00+00:00", "has_receipt": True},
    {"lead_id": L3, "reference_id": "GONE0003", "lead": {}, "waived": True,
     "accepted_at": "2026-09-08T10:00:00+00:00", "has_receipt": False},
]


def _short(x):
    return bot._short_uuid(str(x))


def _buttons(kb):
    if kb is None:
        return []
    return [[(b.text, b.callback_data) for b in row] for row in kb.inline_keyboard]


def _flat(kb):
    return [d for row in _buttons(kb) for _, d in row]


class TheCallbacksFitTelegramTest(unittest.TestCase):
    """The scar this codebase already carries: one oversize callback and Telegram
    drops the WHOLE keyboard, so the message never arrives at all."""

    def test_every_prefix_plus_its_payload_is_under_64_bytes(self):
        d, l = _short(DRIVER_ID), _short(L1)
        for data in (bot.SUSP_VIEW_CB + d, bot.SUSP_EXC_CB + d, bot.SUSP_LIFT_CB + d,
                     bot.SUSP_LIFT_OK_CB + d, bot.SUSP_EXC_ONE_CB + d + l,
                     bot.SUSP_UNEXC_ONE_CB + d + l):
            with self.subTest(data=data):
                self.assertLess(len(data.encode("utf-8")), 64, data)

    def test_a_pair_round_trips(self):
        data = bot.SUSP_EXC_ONE_CB + _short(DRIVER_ID) + _short(L1)
        pair = bot._parse_paired_short_uuids(data, bot.SUSP_EXC_ONE_CB)
        self.assertEqual((DRIVER_ID, L1),
                         (bot._long_uuid(pair[0]), bot._long_uuid(pair[1])))


class TheHandlerIsReachableForeverTest(unittest.TestCase):
    """A suspension alert sits in a supervisor's chat for days and there is no PTB
    persistence, so anything conversation-scoped is dead after the next redeploy."""

    def setUp(self):
        m = re.search(
            r"CallbackQueryHandler\(\s*handle_suspension_actions,\s*"
            r'pattern=(r"[^"]+"\s*(?:\n\s*r"[^"]+"\s*)*)\)', SRC)
        self.assertIsNotNone(m, "handle_suspension_actions is not registered")
        self.pat = re.compile("".join(re.findall(r'r"([^"]+)"', m.group(1))))

    def test_registered_exactly_once(self):
        self.assertEqual(1, SRC.count("CallbackQueryHandler(\n            handle_suspension_actions"))

    def test_it_is_top_level_not_inside_the_conversation(self):
        """Registered on the application, not among conv's entry_points/states."""
        i = SRC.index("handle_suspension_actions,\n            pattern=")
        self.assertIn("application.add_handler(", SRC[i - 200:i])

    def test_every_button_it_can_emit_matches(self):
        d, l = _short(DRIVER_ID), _short(L1)
        for data in (bot.SUSP_VIEW_CB + d, bot.SUSP_EXC_CB + d, bot.SUSP_LIFT_CB + d,
                     bot.SUSP_LIFT_OK_CB + d, bot.SUSP_EXC_ONE_CB + d + l,
                     bot.SUSP_UNEXC_ONE_CB + d + l):
            with self.subTest(data=data):
                self.assertTrue(self.pat.match(data), data)

    def test_and_nothing_else(self):
        d = _short(DRIVER_ID)
        for data in ("susp_v_", "susp_v_" + d[:21], "susp_v_" + d + "x", "susp_q_" + d,
                     "xsusp_v_" + d, "susp_xl_" + d, "receipt_for_ABC"):
            with self.subTest(data=data):
                self.assertIsNone(self.pat.match(data), data)


class _Base(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.sent = []
        self.edits = []
        self.answers = []

        self.ctx = mock.MagicMock()

        async def send(**kw):
            self.sent.append(kw)
            return mock.MagicMock()

        async def photo(**kw):
            self.sent.append(dict(kw, _kind="photo"))
            return mock.MagicMock()

        async def doc(**kw):
            self.sent.append(dict(kw, _kind="document"))
            return mock.MagicMock()

        self.ctx.bot.send_message = mock.AsyncMock(side_effect=send)
        self.ctx.bot.send_photo = mock.AsyncMock(side_effect=photo)
        self.ctx.bot.send_document = mock.AsyncMock(side_effect=doc)

        self.update = mock.MagicMock()
        self.update.effective_user.id = SUP
        q = self.update.callback_query

        async def edit(text, **kw):
            self.edits.append(dict(kw, text=text))

        q.edit_message_text = mock.AsyncMock(side_effect=edit)
        q.message.chat.id = 900

        self.rows = [dict(r) for r in ROWS]
        p = self.enterContext  # py3.11+
        p(mock.patch.object(bot, "_user_is_global_supervisor", lambda uid: uid == SUP))
        p(mock.patch.object(bot, "_safe_answer_callback_query",
                            mock.AsyncMock(side_effect=lambda q, *a, **k: self.answers.append((a, k)))))
        p(mock.patch.object(bot, "_bust_driver_caches", lambda *a, **k: None))
        p(mock.patch.object(bot, "_driver_row_by_id",
                            lambda did: {"id": DRIVER_ID, "driver_name": "Mack",
                                         "driver_telegram_id": "555"}))
        p(mock.patch.object(bot.db, "get_driver_accepted_receipt_rows",
                            lambda did: [dict(r) for r in self.rows]))
        p(mock.patch.object(bot, "_get_suspended_driver_ids", lambda: {DRIVER_ID}))
        p(mock.patch.object(bot, "receipt_portal_url", lambda lid: "https://x/" + str(lid)))

    async def tap(self, data):
        self.update.callback_query.data = data
        await bot.handle_suspension_actions(self.update, self.ctx)


class OnlySupervisorsTest(_Base):

    async def test_a_driver_who_finds_the_button_is_refused(self):
        self.update.effective_user.id = 4242
        await self.tap(bot.SUSP_VIEW_CB + _short(DRIVER_ID))
        self.assertEqual([], self.sent)
        self.assertTrue(any("Supervisors only" in str(a) for a, _ in self.answers))

    async def test_the_handler_checks_for_itself(self):
        """It is top-level, so nothing upstream has vetted the presser."""
        i = SRC.index("async def handle_suspension_actions")
        self.assertIn("_user_is_global_supervisor",
                      SRC[i:SRC.index("\n\n\n", i)])


class ViewReceiptsAnswersEvenWithNoImagesTest(_Base):

    async def test_zero_images_names_what_is_owed(self):
        """THE case for a suspended driver: an owed receipt has no stored image,
        so this path is the normal one, not the edge."""
        self.rows = [r for r in ROWS if not r["has_receipt"]]
        with mock.patch.object(bot.db, "get_receipt_file", lambda lid: None):
            await self.tap(bot.SUSP_VIEW_CB + _short(DRIVER_ID))
        self.assertTrue(self.sent)
        first = self.sent[0]["text"]
        self.assertIn("No receipt images on file yet", first)
        self.assertIn("OWED0001", first)
        # and it is not a dead end
        self.assertEqual(
            [bot.SUSP_VIEW_CB + _short(DRIVER_ID), bot.SUSP_EXC_CB + _short(DRIVER_ID),
             bot.SUSP_LIFT_CB + _short(DRIVER_ID)],
            _flat(self.sent[0]["reply_markup"]))

    async def test_an_uploaded_receipt_comes_back_as_a_photo(self):
        with mock.patch.object(bot.db, "get_receipt_file",
                               lambda lid: {"data": b"\xff\xd8jpeg"}):
            await self.tap(bot.SUSP_VIEW_CB + _short(DRIVER_ID))
        photos = [k for k in self.sent if k.get("_kind") == "photo"]
        self.assertEqual(1, len(photos))
        self.assertIn("DONE0002", photos[0]["caption"])
        self.assertIn("On file: 1", self.sent[0]["text"])

    async def test_a_photo_telegram_refuses_is_retried_as_a_document(self):
        self.ctx.bot.send_photo = mock.AsyncMock(side_effect=RuntimeError("too big"))
        with mock.patch.object(bot.db, "get_receipt_file",
                               lambda lid: {"data": b"x" * 10}):
            await self.tap(bot.SUSP_VIEW_CB + _short(DRIVER_ID))
        self.assertEqual(1, len([k for k in self.sent if k.get("_kind") == "document"]))

    async def test_an_unreadable_image_becomes_a_link_not_a_silence(self):
        with mock.patch.object(bot.db, "get_receipt_file",
                               lambda lid: (_ for _ in ()).throw(RuntimeError("gone"))):
            await self.tap(bot.SUSP_VIEW_CB + _short(DRIVER_ID))
        link = [k for k in self.sent if "not readable" in str(k.get("text"))]
        self.assertEqual(1, len(link))
        self.assertEqual("https://x/" + L2,
                         link[0]["reply_markup"].inline_keyboard[0][0].url)

    async def test_it_never_floods_the_chat(self):
        """Telegram rate-limits, and the tail would silently never arrive."""
        self.rows = [{"lead_id": L2, "reference_id": f"R{i:07d}", "lead": {},
                      "has_receipt": True, "waived": False,
                      "accepted_at": f"2026-09-{(i % 28) + 1:02d}T10:00:00+00:00"}
                     for i in range(25)]
        with mock.patch.object(bot.db, "get_receipt_file",
                               lambda lid: {"data": b"x"}):
            await self.tap(bot.SUSP_VIEW_CB + _short(DRIVER_ID))
        self.assertEqual(bot._RECEIPT_IMAGE_BATCH,
                         len([k for k in self.sent if k.get("_kind") == "photo"]))
        self.assertTrue(any("and 15 more" in str(k.get("text")) for k in self.sent))


class MakeAnExceptionIsPerReceiptTest(_Base):

    async def test_the_picker_lists_owed_and_forgiven_separately(self):
        await self.tap(bot.SUSP_EXC_CB + _short(DRIVER_ID))
        kb = self.sent[0]["reply_markup"]
        flat = _flat(kb)
        self.assertIn(bot.SUSP_EXC_ONE_CB + _short(DRIVER_ID) + _short(L1), flat)
        self.assertIn("receipt_for_OWED0001", flat)
        # forgiven stays listed, with the way back
        self.assertIn(bot.SUSP_UNEXC_ONE_CB + _short(DRIVER_ID) + _short(L3), flat)
        # an uploaded receipt is not owed and has nothing to forgive
        self.assertNotIn(bot.SUSP_EXC_ONE_CB + _short(DRIVER_ID) + _short(L2), flat)
        self.assertIn("Owes 1", self.sent[0]["text"])

    async def test_forgiving_one_writes_only_that_lead(self):
        calls = []
        with mock.patch.object(bot.db, "set_lead_excluded",
                               lambda lid, val: calls.append((lid, val)) or True), \
             mock.patch.object(bot.db, "waive_driver_pending_receipts",
                               mock.Mock(side_effect=AssertionError("blanket waive"))):
            await self.tap(bot.SUSP_EXC_ONE_CB + _short(DRIVER_ID) + _short(L1))
        self.assertEqual([(L1, True)], calls)

    async def test_restoring_one_puts_it_back(self):
        calls = []
        with mock.patch.object(bot.db, "set_lead_excluded",
                               lambda lid, val: calls.append((lid, val)) or True):
            await self.tap(bot.SUSP_UNEXC_ONE_CB + _short(DRIVER_ID) + _short(L3))
        self.assertEqual([(L3, False)], calls)

    async def test_the_picker_is_redrawn_in_place(self):
        with mock.patch.object(bot.db, "set_lead_excluded", lambda lid, val: True):
            await self.tap(bot.SUSP_EXC_ONE_CB + _short(DRIVER_ID) + _short(L1))
        self.assertEqual(1, len(self.edits))
        self.assertIn("Make an exception", self.edits[0]["text"])

    async def test_a_refused_write_says_so_instead_of_lying(self):
        """The anon key returns 200 with an empty list on a refused write, so a
        picker that redraws optimistically reports a forgiveness that never
        happened."""
        with mock.patch.object(bot.db, "set_lead_excluded", lambda lid, val: False):
            await self.tap(bot.SUSP_EXC_ONE_CB + _short(DRIVER_ID) + _short(L1))
        self.assertEqual([], self.edits)
        self.assertTrue(any("Could not update" in str(a) for a, _ in self.answers))

    async def test_the_cache_is_busted_before_the_state_is_re_read(self):
        """The suspended set is memoised. Re-reading it first reports a suspension
        dispatch has already stopped enforcing."""
        order = []
        with mock.patch.object(bot.db, "set_lead_excluded", lambda lid, val: True), \
             mock.patch.object(bot, "_bust_driver_caches",
                               lambda *a, **k: order.append("bust")), \
             mock.patch.object(bot, "_get_suspended_driver_ids",
                               lambda: order.append("read") or {DRIVER_ID}):
            await self.tap(bot.SUSP_EXC_ONE_CB + _short(DRIVER_ID) + _short(L1))
        self.assertEqual(["read", "bust", "read"], order)

    async def test_a_suspension_that_lifts_itself_is_announced(self):
        """Forgiving enough receipts drops the count below the threshold. The
        driver is free either way; saying nothing just means nobody knows."""
        seen = {}
        states = iter([{DRIVER_ID}, set()])
        with mock.patch.object(bot.db, "set_lead_excluded", lambda lid, val: True), \
             mock.patch.object(bot, "_get_suspended_driver_ids", lambda: next(states)), \
             mock.patch.object(bot.db, "get_driver_pending_receipts", lambda did: []), \
             mock.patch.object(bot, "_notify_suspension_lifted",
                               mock.AsyncMock(side_effect=lambda *a, **kw: seen.update(kw))):
            await self.tap(bot.SUSP_EXC_ONE_CB + _short(DRIVER_ID) + _short(L1))
        self.assertEqual("Mack", seen.get("driver", {}).get("driver_name"))


class LiftAsksFirstThenDoesBothHalvesTest(_Base):

    async def test_the_first_tap_only_asks(self):
        with mock.patch.object(bot.db, "set_driver_suspended",
                               mock.Mock(side_effect=AssertionError("acted without asking"))):
            await self.tap(bot.SUSP_LIFT_CB + _short(DRIVER_ID))
        self.assertIn("Lift Mack's suspension?", self.sent[0]["text"])
        self.assertIn("excuses all 1", self.sent[0]["text"])
        self.assertIn(bot.SUSP_LIFT_OK_CB + _short(DRIVER_ID), _flat(self.sent[0]["reply_markup"]))
        # and the softer option is right there
        self.assertIn(bot.SUSP_EXC_CB + _short(DRIVER_ID), _flat(self.sent[0]["reply_markup"]))

    async def test_confirming_clears_the_flag_and_the_debt(self):
        """Either half alone leaves the driver suspended on the next read."""
        calls = []
        with mock.patch.object(bot.db, "set_driver_suspended",
                               lambda did, v: calls.append(("flag", did, v)) or True), \
             mock.patch.object(bot.db, "waive_driver_pending_receipts",
                               lambda did: calls.append(("waive", did)) or 3), \
             mock.patch.object(bot, "_get_suspended_driver_ids", lambda: set()), \
             mock.patch.object(bot.db, "get_driver_pending_receipts", lambda did: []), \
             mock.patch.object(bot, "_notify_suspension_lifted", mock.AsyncMock()):
            await self.tap(bot.SUSP_LIFT_OK_CB + _short(DRIVER_ID))
        self.assertEqual([("flag", DRIVER_ID, False), ("waive", DRIVER_ID)], calls)
        self.assertIn("excused 3", self.edits[0]["text"])

    async def test_a_stuck_flag_is_reported_not_celebrated(self):
        """set_driver_suspended returns False both when the write failed and when
        the column was never migrated. Announcing an unverified success is how a
        driver gets told they are free and still cannot take a lead."""
        with mock.patch.object(bot.db, "set_driver_suspended", lambda did, v: False), \
             mock.patch.object(bot.db, "waive_driver_pending_receipts", lambda did: 2), \
             mock.patch.object(bot, "_get_suspended_driver_ids", lambda: {DRIVER_ID}), \
             mock.patch.object(bot, "_notify_suspension_lifted",
                               mock.AsyncMock(side_effect=AssertionError("told them too soon"))):
            await self.tap(bot.SUSP_LIFT_OK_CB + _short(DRIVER_ID))
        self.assertIn("migration_driver_manual_suspend.sql", self.edits[0]["text"])
        self.assertIn(bot.SUSP_LIFT_OK_CB + _short(DRIVER_ID),
                      _flat(self.edits[0]["reply_markup"]))

    async def test_a_missing_migration_alone_still_lifts(self):
        """Nobody can be manually suspended by a column that does not exist, so a
        False from the flag write is not by itself a failed lift."""
        with mock.patch.object(bot.db, "set_driver_suspended", lambda did, v: False), \
             mock.patch.object(bot.db, "waive_driver_pending_receipts", lambda did: 4), \
             mock.patch.object(bot, "_get_suspended_driver_ids", lambda: set()), \
             mock.patch.object(bot.db, "get_driver_pending_receipts", lambda did: []), \
             mock.patch.object(bot, "_notify_suspension_lifted", mock.AsyncMock()):
            await self.tap(bot.SUSP_LIFT_OK_CB + _short(DRIVER_ID))
        self.assertIn("excused 4", self.edits[0]["text"])

    async def test_every_lift_path_uses_the_same_recipe(self):
        """Three now: this button and the two settings screens, which used to
        write and hope, and announce a lift unconditionally."""
        self.assertEqual(3, SRC.count("lifted, waived = await _lift_driver_suspension(context, did)"))
        # and nobody clears the flag without excusing the debt
        self.assertEqual(1, SRC.count("db.set_driver_suspended, driver_id, False"))
        self.assertEqual(0, SRC.count("db.set_driver_suspended, did, False"))


class TheLiftedNoticeOffersOnlyWhatIsLeftTest(unittest.TestCase):

    def test_it_carries_view_receipts_and_nothing_that_pretends_to_undo(self):
        i = SRC.index("async def _notify_suspension_lifted")
        body = SRC[i:SRC.index("\ndef _norm_chat_id", i)]
        self.assertIn("SUSP_VIEW_CB", body)
        for gone in ("SUSP_LIFT_CB", "SUSP_LIFT_OK_CB", "SUSP_EXC_CB"):
            self.assertNotIn(gone, body, gone)


class NoSuspButtonEverReachesADriverTest(unittest.TestCase):
    """The firewall. Every susp_ button is a supervisor's decision ABOUT a driver."""

    # Where these rows are allowed to be built, and why each one is safe:
    ALLOWED = {
        # the builder's own def line, which sits just after this one
        "_pending_receipts_most_recent_first": "the definition itself",
        # goes only to _global_supervisory_chat_ids()
        "_suspension_alert_keyboard": "supervisory alert",
        # reached only through handle_suspension_actions, which gates first
        "_send_driver_receipt_images": "behind the handler's gate",
        # dispatcher-facing: gated inline on the presser
        "handle_phase1_ai_review_callback": "_user_is_global_supervisor",
        "handle_driver_selection": "_user_is_global_supervisor",
    }

    def _enclosing(self, pos):
        """The nearest preceding top-level def -- which function this call is in."""
        before = SRC[:pos]
        return re.findall(r"^(?:async )?def ([A-Za-z_][A-Za-z0-9_]*)\(",
                          before, re.M)[-1]

    def test_the_rows_are_built_only_where_the_presser_is_known(self):
        """A new call site is a new way for a driver to be handed a button that
        forgives their own debt. It must be added here deliberately."""
        sites = {}
        for m in re.finditer(r"_suspension_action_rows\(", SRC):
            sites.setdefault(self._enclosing(m.start()), 0)
            sites[self._enclosing(m.start())] += 1
        self.assertEqual(set(self.ALLOWED), set(sites), sites)

    def test_the_dispatcher_facing_sites_gate_on_the_presser(self):
        for fn in ("handle_phase1_ai_review_callback", "handle_driver_selection"):
            i = SRC.index(f"def {fn}(")
            m = re.search(r"_suspension_action_rows\(", SRC[i:])
            window = SRC[i + m.start() - 200:i + m.start() + 200]
            with self.subTest(fn=fn):
                self.assertIn("_user_is_global_supervisor", window)


if __name__ == "__main__":
    unittest.main()
