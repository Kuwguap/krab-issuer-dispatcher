r"""The supervisory "Driver Suspended" alert can clear the debt it reports.

Asked for:
  "the supervisory message should have an inline button to upload receipts for
   drivers ... in this case where there are multiple show the most recent and
   another inline button to show all"

  * "📤 Upload receipt — <REF> (most recent)" -> receipt_for_<REF>: the entry
    point a driver's own Upload button uses, so it answers after a redeploy and
    a supervisor's upload is credited to the lead's driver.
  * 2+ refs: "📋 Show all <n> receipts" -> recsup_all_<short driver id>, which
    REPLIES with the driver's live list. It never edits: the alert is the record
    of the suspension, and its references are a snapshot of that moment.
  * "Most recent" is lead_assignments.accepted_at, which get_driver_pending_receipts
    now returns -- without ever letting that column cost the count, because an
    empty list there silently lifts every suspension.

Run:  venv\Scripts\python.exe -m pytest tests/test_suspension_alert_buttons.py -q
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
from telegram.error import BadRequest, Forbidden  # noqa: E402

SRC = (ROOT / "bot.py").read_text(encoding="utf-8")
_real_db_module = None


def _real_database():
    """The REAL Database class, whatever other suites did to the module.

    Several dispatch suites assign utils.database.Database = MagicMock() at import
    and never put it back, and pytest imports every file before running any -- so
    a name bound from it here is a mock in a full run. Load a private copy straight
    from the file instead, as tests/test_receipts_board_v3.py does."""
    global _real_db_module
    if _real_db_module is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_susp_alert_real_udb", str(ROOT / "utils" / "database.py"))
        _real_db_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_real_db_module)
    return _real_db_module.Database


DRIVER_ID = "cccccccc-3333-4333-8333-cccccccccccc"
LEAD_ID = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
GROUP_ID = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"

# Mack's five, in the order the live alert listed them, with their accept times
# (mixed formats on purpose: PostgREST trims the fraction, some carry a Z).
MACK = [
    {"reference_id": "0H9L16XH", "lead_id": "l1", "accepted_at": "2026-09-09T14:57:10.5+00:00"},
    {"reference_id": "1CTRR77V", "lead_id": "l2", "accepted_at": "2026-09-09T14:57:31.123456+00:00"},
    {"reference_id": "XTKSZ6EF", "lead_id": "l3", "accepted_at": "2026-09-09T15:14:00+00:00"},
    {"reference_id": "1DJE7PD8", "lead_id": "l4", "accepted_at": "2026-09-10T22:13:05.12345Z"},
    {"reference_id": "UY3OMAPI", "lead_id": "l5", "accepted_at": "2026-09-09 14:58:44.9+00:00"},
]
NEWEST_FIRST = ["1DJE7PD8", "XTKSZ6EF", "UY3OMAPI", "1CTRR77V", "0H9L16XH"]

OLD_SELECT = (
    "lead_id, lead:leads(reference_id, receipt_image_url, vehicle_details, delivery_details, "
    "extra_info, special_request_note, special_request_issuers, special_request_drivers)"
)


def _buttons(kb):
    return [[(b.text, b.callback_data) for b in row] for row in kb.inline_keyboard]


def _action_rows(driver_id=None):
    """The supervisor rows every supervisory suspension surface now carries.

    Expressed as the code's own answer on purpose: this helper exists so the
    eight expectations below change in one place, not so it re-implements the
    builder and drifts from it.
    """
    return [[(b.text, b.callback_data) for b in row]
            for row in bot._suspension_action_rows(driver_id or DRIVER_ID)]


def _refs(rows):
    return [r["reference_id"] for r in rows]


class MostRecentMeansLatestAcceptTest(unittest.TestCase):

    def test_macks_five_come_out_newest_first(self):
        self.assertEqual(NEWEST_FIRST, _refs(bot._pending_receipts_most_recent_first(MACK)))

    def test_rows_without_a_time_go_last_in_their_original_order(self):
        rows = [
            {"reference_id": "A", "accepted_at": None},
            {"reference_id": "B", "accepted_at": "2026-09-01T10:00:00+00:00"},
            {"reference_id": "C"},
            {"reference_id": "D", "accepted_at": "2026-09-05T10:00:00+00:00"},
            {"reference_id": "E", "accepted_at": "not a time"},
        ]
        self.assertEqual(["D", "B", "A", "C", "E"],
                         _refs(bot._pending_receipts_most_recent_first(rows)))

    def test_equal_times_keep_their_order(self):
        rows = [{"reference_id": x, "accepted_at": "2026-09-01T10:00:00Z"} for x in "PQR"]
        self.assertEqual(["P", "Q", "R"], _refs(bot._pending_receipts_most_recent_first(rows)))

    def test_a_z_suffix_and_an_offset_compare_as_the_same_clock(self):
        rows = [
            {"reference_id": "EARLY", "accepted_at": "2026-09-01T12:00:00+02:00"},  # 10:00Z
            {"reference_id": "LATE", "accepted_at": "2026-09-01T11:00:00Z"},
        ]
        self.assertEqual(["LATE", "EARLY"], _refs(bot._pending_receipts_most_recent_first(rows)))

    def test_blank_and_na_refs_are_dropped(self):
        rows = [{"reference_id": "N/A", "accepted_at": "2026-09-10T00:00:00Z"},
                {"reference_id": "  ", "accepted_at": "2026-09-10T00:00:00Z"},
                {"reference_id": None},
                {"reference_id": "n/a"},
                {"reference_id": "KEEP", "accepted_at": "2026-09-01T00:00:00Z"}]
        self.assertEqual(["KEEP"], _refs(bot._pending_receipts_most_recent_first(rows)))

    def test_the_input_is_not_reordered(self):
        rows = [dict(r) for r in MACK]
        bot._pending_receipts_most_recent_first(rows)
        self.assertEqual(_refs(MACK), _refs(rows))


class TheAlertKeyboardTest(unittest.TestCase):

    def test_nothing_uploadable_still_gets_the_supervisor_actions(self):
        """A driver owing five unreadable references is still one a supervisor
        needs to look at and lift. Only an unaddressable driver id leaves the
        alert bare."""
        self.assertEqual(_action_rows(), _buttons(bot._suspension_alert_keyboard(DRIVER_ID, [])))
        self.assertEqual(_action_rows(), _buttons(bot._suspension_alert_keyboard(
            DRIVER_ID, [{"reference_id": "N/A"}, {"reference_id": ""}])))

    def test_a_driver_id_that_is_not_a_uuid_means_no_keyboard(self):
        """_short_uuid cannot address it, so there is nothing to offer."""
        self.assertEqual([], bot._suspension_action_rows("d1"))
        self.assertIsNone(bot._suspension_alert_keyboard("d1", []))

    def test_one_ref_is_just_upload_without_most_recent(self):
        kb = bot._suspension_alert_keyboard(
            DRIVER_ID, [{"reference_id": "N/A"}, {"reference_id": "ONLY1234"}])
        self.assertEqual([[("📤 Upload receipt — ONLY1234", "receipt_for_ONLY1234")]]
                         + _action_rows(), _buttons(kb))

    def test_two_refs_add_show_all(self):
        kb = bot._suspension_alert_keyboard(DRIVER_ID, [
            {"reference_id": "OLDER111", "accepted_at": "2026-09-01T00:00:00Z"},
            {"reference_id": "NEWER222", "accepted_at": "2026-09-02T00:00:00Z"},
        ])
        short = bot._short_uuid(DRIVER_ID)
        self.assertEqual([
            [("📤 Upload receipt — NEWER222 (most recent)", "receipt_for_NEWER222")],
            [("📋 Show all 2 receipts", "recsup_all_" + short)],
        ] + _action_rows(), _buttons(kb))
        self.assertEqual(DRIVER_ID, bot._long_uuid(short))

    def test_macks_alert(self):
        kb = bot._suspension_alert_keyboard(DRIVER_ID, MACK)
        self.assertEqual([
            [("📤 Upload receipt — 1DJE7PD8 (most recent)", "receipt_for_1DJE7PD8")],
            [("📋 Show all 5 receipts", "recsup_all_" + bot._short_uuid(DRIVER_ID))],
        ] + _action_rows(), _buttons(kb))

    def test_the_lead_just_accepted_is_not_the_one_to_upload(self):
        """The alert fires as the driver accepts, so the newest accept is always
        that lead -- no receipt for it can exist yet. The button names the next."""
        kb = bot._suspension_alert_keyboard(DRIVER_ID, MACK, just_accepted_lead_id="l4")
        self.assertEqual([
            [("📤 Upload receipt — XTKSZ6EF (most recent)", "receipt_for_XTKSZ6EF")],
            [("📋 Show all 5 receipts", "recsup_all_" + bot._short_uuid(DRIVER_ID))],
        ] + _action_rows(), _buttons(kb))

    def test_only_the_lead_just_accepted_falls_back_to_it(self):
        kb = bot._suspension_alert_keyboard(DRIVER_ID, [
            {"reference_id": "ONLY0001", "lead_id": "l9", "accepted_at": "2026-09-10T10:00:00+00:00"},
        ], just_accepted_lead_id="l9")
        self.assertEqual([[("📤 Upload receipt — ONLY0001", "receipt_for_ONLY0001")]]
                         + _action_rows(), _buttons(kb))

    def test_without_a_lead_rows_with_no_lead_id_still_count(self):
        rows = [{"reference_id": "OLDER111", "accepted_at": "2026-09-09T10:00:00+00:00"},
                {"reference_id": "NEWER222", "accepted_at": "2026-09-10T10:00:00+00:00"}]
        self.assertEqual("receipt_for_NEWER222",
                         _buttons(bot._suspension_alert_keyboard(DRIVER_ID, rows))[0][0][1])

    def test_na_refs_do_not_count_towards_show_all(self):
        kb = bot._suspension_alert_keyboard(DRIVER_ID, MACK + [{"reference_id": "N/A"}] * 3)
        self.assertEqual("📋 Show all 5 receipts", _buttons(kb)[1][0][0])

    def test_every_callback_fits_telegrams_64_bytes(self):
        """One oversize button and Telegram refuses the whole keyboard -- and the
        message with it."""
        kb = bot._suspension_alert_keyboard(DRIVER_ID, MACK)
        for row in _buttons(kb):
            for _, data in row:
                self.assertLess(len(data.encode("utf-8")), 64, data)
        self.assertEqual(33, len(bot.RECSUP_ALL_PREFIX + bot._short_uuid(DRIVER_ID)))

    def test_an_oversize_ref_gets_no_upload_button(self):
        kb = bot._suspension_alert_keyboard(DRIVER_ID, [{"reference_id": "X" * 60}])
        self.assertEqual(_action_rows(), _buttons(kb))

    def test_a_driver_id_that_is_not_a_uuid_still_gets_upload(self):
        """Upload needs only the reference; every supervisor action needs the
        driver. So the upload survives and the actions drop out."""
        kb = bot._suspension_alert_keyboard("d1", MACK)
        self.assertEqual([[("📤 Upload receipt — 1DJE7PD8 (most recent)",
                            "receipt_for_1DJE7PD8")]], _buttons(kb))


class TheAcceptPathSendsTheButtonsTest(unittest.IsolatedAsyncioTestCase):
    """Driven through the real handle_accept_lead, as far as the alert."""

    async def _accept(self, pending, *, markdown_fails=False):
        lead = {
            "id": LEAD_ID, "reference_id": "REF7", "group_id": GROUP_ID,
            "instant_tag": False, "price": "$250", "driver_amount": "$200",
            "vehicle_details": "Client\n-\n-\n-\n-\n-\n-\n-\n-\n-\n-",
        }
        q = mock.MagicMock()
        q.data = f"accept_lead_{LEAD_ID}"
        q.answer = mock.AsyncMock()
        q.from_user = mock.MagicMock(id=999, username="mack", full_name="Mack")
        q.message.chat_id = 999
        q.message.edit_text = mock.AsyncMock()
        q.message.reply_text = mock.AsyncMock()
        upd = mock.MagicMock(callback_query=q)
        db = mock.MagicMock()
        db.get_lead_by_id.return_value = lead
        db.accept_lead_assignment.return_value = {"id": "asg1"}
        db.get_group_lead_offers.return_value = []
        db.apply_paper_on_lead_accept.return_value = None
        db.get_driver_pending_receipts.return_value = pending
        db.get_active_renewal_for_lead.return_value = None
        db.get_group_by_id.return_value = {
            "id": GROUP_ID, "group_name": "HighKage", "group_telegram_id": "-100123"}

        sent = []

        async def _send(**k):
            sent.append(k)
            if markdown_fails and k.get("chat_id") in (11, 22) and k.get("parse_mode") == "Markdown":
                raise BadRequest("Can't parse entities")

        ctx = mock.MagicMock()
        ctx.bot.send_message = mock.AsyncMock(side_effect=_send)
        with mock.patch.object(bot, "db", db), \
                mock.patch.object(bot, "_driver_row_for_telegram_user",
                                  lambda uid: {"id": DRIVER_ID, "driver_name": "Mack",
                                               "driver_telegram_id": "999"}), \
                mock.patch.object(bot, "_global_supervisory_chat_ids", lambda: [11, 22]), \
                mock.patch.object(bot, "_send_all_tag_pdfs", mock.AsyncMock()), \
                mock.patch.object(bot, "_instant_tag_link_after_accept", mock.AsyncMock()), \
                mock.patch.object(bot, "_start_tracking_gate_or_send_details", mock.AsyncMock()), \
                mock.patch.object(bot, "_notify_initiator_lead_accepted_summary", mock.AsyncMock()), \
                mock.patch.object(bot, "_send_supervisory_new_lead_notices_from_lead",
                                  mock.AsyncMock()), \
                mock.patch.object(bot, "_should_defer_supervisory_until_source",
                                  lambda l: True):
            await bot.handle_accept_lead(upd, ctx)
        to_sups = [k for k in sent if k.get("chat_id") in (11, 22)]
        return to_sups, q

    async def test_each_supervisor_gets_the_alert_with_the_buttons(self):
        to_sups, _ = await self._accept(MACK)
        self.assertEqual([11, 22], [k["chat_id"] for k in to_sups])
        for k in to_sups:
            self.assertEqual("Markdown", k["parse_mode"])
            self.assertIn("Driver Suspended", k["text"])
            self.assertEqual(
                _buttons(bot._suspension_alert_keyboard(DRIVER_ID, MACK)),
                _buttons(k["reply_markup"]))

    async def test_the_plain_fallback_keeps_the_buttons(self):
        to_sups, _ = await self._accept(MACK, markdown_fails=True)
        self.assertEqual([11, 11, 22, 22], [k["chat_id"] for k in to_sups])
        for md, plain in (to_sups[0:2], to_sups[2:4]):
            self.assertEqual("Markdown", md["parse_mode"])
            self.assertNotIn("parse_mode", plain)
            self.assertNotIn("*", plain["text"])
            self.assertIsNotNone(plain["reply_markup"])
            self.assertEqual(_buttons(md["reply_markup"]), _buttons(plain["reply_markup"]))
            self.assertEqual("receipt_for_1DJE7PD8",
                             _buttons(plain["reply_markup"])[0][0][1])

    async def test_the_references_line_is_newest_first(self):
        to_sups, _ = await self._accept(MACK)
        self.assertIn("Receipt references: " + ", ".join(NEWEST_FIRST), to_sups[0]["text"])
        self.assertIn("Reason: 5 unpaid receipt(s)", to_sups[0]["text"])

    async def test_the_accepted_lead_counts_but_is_not_the_button(self):
        """Live shape: the lead being accepted is itself one of the five owed."""
        pending = [dict(r, lead_id=LEAD_ID) if r["reference_id"] == "1DJE7PD8" else r
                   for r in MACK]
        to_sups, _ = await self._accept(pending)
        self.assertEqual([11, 22], [k["chat_id"] for k in to_sups])
        for k in to_sups:
            self.assertEqual([
                [("📤 Upload receipt — XTKSZ6EF (most recent)", "receipt_for_XTKSZ6EF")],
                [("📋 Show all 5 receipts", "recsup_all_" + bot._short_uuid(DRIVER_ID))],
            ] + _action_rows(), _buttons(k["reply_markup"]))
            self.assertIn("Receipt references: " + ", ".join(NEWEST_FIRST), k["text"])
            self.assertIn("Reason: 5 unpaid receipt(s)", k["text"])

    async def test_nothing_uploadable_still_sends_the_alert(self):
        to_sups, _ = await self._accept([{"reference_id": "N/A", "lead_id": f"x{i}"}
                                         for i in range(5)])
        self.assertEqual([11, 22], [k["chat_id"] for k in to_sups])
        # It used to arrive with no way to act on it at all.
        self.assertEqual(_action_rows(), _buttons(to_sups[0]["reply_markup"]))
        self.assertIn("Receipt references: (none on file)", to_sups[0]["text"])

    async def test_the_drivers_own_notice_is_unchanged(self):
        _, q = await self._accept(MACK)
        mine = [c for c in q.message.reply_text.call_args_list
                if c.args and "You have been suspended" in str(c.args[0])]
        self.assertEqual(1, len(mine))
        data = [d for row in _buttons(mine[0].kwargs["reply_markup"]) for _, d in row]
        for p in MACK:
            self.assertIn("receipt_for_" + p["reference_id"], data)
        self.assertFalse(any(d.startswith("recsup_") for d in data))
        # THE FIREWALL. Every susp_ button is a supervisor's decision ABOUT this
        # driver -- view their receipts, forgive their debt, lift their
        # suspension. One reaching them is the whole feature turned inside out.
        self.assertFalse(any(d.startswith("susp_") for d in data), data)

    async def test_under_the_threshold_nobody_is_alerted(self):
        to_sups, _ = await self._accept(MACK[:2])
        self.assertEqual([], to_sups)


class OneSupervisorCannotCostTheOthersTheAlertTest(unittest.IsolatedAsyncioTestCase):
    """Each supervisor gets their own try: a chat that blocked the bot, a stale id,
    or a keyboard Telegram refuses must never stop the alert reaching the rest."""

    async def _send(self, fail):
        sent = []

        async def _send_message(**k):
            sent.append(k)
            err = fail(k)
            if err:
                raise err

        ctx = mock.MagicMock()
        ctx.bot.send_message = mock.AsyncMock(side_effect=_send_message)
        with mock.patch.object(bot, "_global_supervisory_chat_ids", lambda: [11, 22, 33]):
            await bot._send_suspension_alert_to_supervisors(
                ctx, {"id": DRIVER_ID, "driver_name": "Mack"}, MACK)
        return sent

    async def test_a_supervisor_who_blocked_the_bot_does_not_stop_the_rest(self):
        sent = await self._send(
            lambda k: Forbidden("bot was blocked by the user") if k["chat_id"] == 11 else None)
        self.assertEqual([11, 22, 33], [k["chat_id"] for k in sent])
        for k in sent[1:]:
            self.assertEqual("Markdown", k["parse_mode"])
            self.assertEqual("receipt_for_1DJE7PD8", _buttons(k["reply_markup"])[0][0][1])

    async def test_a_stale_chat_does_not_stop_the_rest(self):
        """'Chat not found' is a BadRequest: it fails the plain fallback too."""
        sent = await self._send(
            lambda k: BadRequest("Chat not found") if k["chat_id"] == 11 else None)
        self.assertEqual([22, 33], [k["chat_id"] for k in sent][-2:])
        for k in sent[-2:]:
            self.assertEqual("Markdown", k["parse_mode"])
            self.assertIsNotNone(k["reply_markup"])

    async def test_a_refused_keyboard_costs_the_buttons_not_the_alert(self):
        sent = await self._send(
            lambda k: BadRequest("Button_data_invalid")
            if k.get("reply_markup") is not None else None)
        for sup in (11, 22, 33):
            mine = [k for k in sent if k["chat_id"] == sup]
            self.assertEqual(3, len(mine), mine)
            self.assertEqual("Markdown", mine[0]["parse_mode"])
            self.assertNotIn("parse_mode", mine[1])
            self.assertIsNotNone(mine[1]["reply_markup"])
            self.assertNotIn("reply_markup", mine[2])
            self.assertNotIn("parse_mode", mine[2])
            self.assertIn("Driver Suspended", mine[2]["text"])
            self.assertNotIn("*", mine[2]["text"])


class ShowAllRepliesWithTheLiveListTest(unittest.IsolatedAsyncioTestCase):

    async def _tap(self, data, *, owed=MACK, supervisor=True, name="Mack"):
        q = mock.MagicMock()
        q.data = data
        q.answer = mock.AsyncMock()
        q.from_user = mock.MagicMock(id=555)
        q.edit_message_text = mock.AsyncMock()
        q.message.edit_text = mock.AsyncMock()
        q.message.reply_text = mock.AsyncMock()
        q.message.chat.id = 555
        upd = mock.MagicMock(callback_query=q)
        db = mock.MagicMock()
        db.get_driver_pending_receipts.return_value = [dict(r) for r in owed]
        ctx = mock.MagicMock()
        ctx.bot.send_message = mock.AsyncMock()
        self.sent = ctx.bot.send_message
        with mock.patch.object(bot, "db", db), \
                mock.patch.object(bot, "_user_is_global_supervisor", lambda uid: supervisor), \
                mock.patch.object(bot, "_driver_row_by_id",
                                  lambda did: {"id": did, "driver_name": name}):
            await bot.handle_supervisor_receipts_nav(upd, ctx)
        return q, db

    def _show_all(self):
        return bot.RECSUP_ALL_PREFIX + bot._short_uuid(DRIVER_ID)

    async def test_it_replies_and_never_edits_the_alert(self):
        q, db = await self._tap(self._show_all())
        db.get_driver_pending_receipts.assert_called_once_with(DRIVER_ID)
        q.edit_message_text.assert_not_called()
        q.message.edit_text.assert_not_called()
        self.sent.assert_awaited_once()
        c = self.sent.call_args
        self.assertEqual(555, c.kwargs["chat_id"])
        self.assertEqual("HTML", c.kwargs["parse_mode"])
        self.assertIn("<b>Mack</b>", c.kwargs["text"])
        self.assertIn("5 pending receipt(s)", c.kwargs["text"])
        buttons = _buttons(c.kwargs["reply_markup"])
        self.assertEqual(["receipt_for_" + r for r in NEWEST_FIRST],
                         [row[0][1] for row in buttons[:-1]])
        self.assertEqual(bot.RECSUP_BACK, buttons[-1][0][1])

    async def test_it_shows_what_is_owed_now_not_what_the_alert_said(self):
        """Three of Mack's five were uploaded after his alert went out."""
        still = [r for r in MACK if r["reference_id"] in ("1DJE7PD8", "UY3OMAPI")]
        q, _ = await self._tap(self._show_all(), owed=still)
        c = self.sent.call_args
        self.assertIn("2 pending receipt(s)", c.kwargs["text"])
        self.assertEqual(["receipt_for_1DJE7PD8", "receipt_for_UY3OMAPI"],
                         [row[0][1] for row in _buttons(c.kwargs["reply_markup"])[:-1]])

    async def test_nothing_owed_says_so(self):
        q, _ = await self._tap(self._show_all(), owed=[], name="Mack <&>")
        q.edit_message_text.assert_not_called()
        c = self.sent.call_args
        self.assertEqual("✅ Mack &lt;&amp;&gt; has no outstanding receipts.", c.kwargs["text"])
        self.assertEqual("HTML", c.kwargs["parse_mode"])

    async def test_a_non_supervisor_is_refused(self):
        q, db = await self._tap(self._show_all(), supervisor=False)
        db.get_driver_pending_receipts.assert_not_called()
        self.assertIn("Only supervisors", q.message.reply_text.call_args.args[0])

    async def test_a_broken_link_is_refused(self):
        q, db = await self._tap(bot.RECSUP_ALL_PREFIX + "xx")
        db.get_driver_pending_receipts.assert_not_called()
        self.assertIn("Invalid driver link", self.sent.call_args.kwargs["text"])

    async def test_the_drivers_menu_drill_down_still_edits_in_place(self):
        q, _ = await self._tap(bot.RECSUP_DRI_PREFIX + bot._short_uuid(DRIVER_ID))
        q.edit_message_text.assert_awaited_once()
        q.message.reply_text.assert_not_called()
        self.sent.assert_not_called()

    async def test_an_alert_telegram_calls_inaccessible_still_gets_the_list(self):
        """An old alert can come back as an InaccessibleMessage: it has a chat but
        no reply_text, so the list has to go out through the bot."""
        from telegram import CallbackQuery, Chat, InaccessibleMessage, Update, User
        cq = CallbackQuery(
            id="1", from_user=User(555, "Sup", False), chat_instance="ci",
            message=InaccessibleMessage(chat=Chat(555, "private"), message_id=42),
            data=self._show_all())
        ctx = mock.MagicMock()
        ctx.bot.send_message = mock.AsyncMock()
        db = mock.MagicMock()
        db.get_driver_pending_receipts.return_value = [dict(r) for r in MACK]
        with mock.patch.object(bot, "db", db), \
                mock.patch.object(bot, "_user_is_global_supervisor", lambda uid: True), \
                mock.patch.object(bot, "_driver_row_by_id",
                                  lambda did: {"id": did, "driver_name": "Mack"}):
            await bot.handle_supervisor_receipts_nav(Update(update_id=1, callback_query=cq), ctx)
        ctx.bot.send_message.assert_awaited_once()
        c = ctx.bot.send_message.call_args
        self.assertEqual(555, c.kwargs["chat_id"])
        self.assertIn("5 pending receipt(s)", c.kwargs["text"])
        self.assertEqual("receipt_for_1DJE7PD8", _buttons(c.kwargs["reply_markup"])[0][0][1])


class TheButtonsAreRoutedTest(unittest.TestCase):

    def setUp(self):
        m = re.search(
            r'CallbackQueryHandler\(handle_supervisor_receipts_nav,\s*pattern=r"([^"]+)"\)', SRC)
        self.assertIsNotNone(m, "handle_supervisor_receipts_nav is not registered")
        self.pat = re.compile(m.group(1))

    def test_registered_once(self):
        self.assertEqual(1, SRC.count("CallbackQueryHandler(handle_supervisor_receipts_nav"))

    def test_it_takes_all_three_buttons(self):
        short = bot._short_uuid(DRIVER_ID)
        for data in ("recsup_all_" + short, "recsup_dri_" + short, "recsup_back",
                     "recsup_all_" + "a_b-" * 5 + "Z9"):
            with self.subTest(data=data):
                self.assertTrue(self.pat.match(data))

    def test_and_nothing_else_new(self):
        short = bot._short_uuid(DRIVER_ID)
        for data in ("recsup_all_", "recsup_all_" + short[:21], "recsup_all_" + short + "x",
                     "recsup_all_" + "!" * 22, "recsup_allx", "recsup_backx", "recsup_back_",
                     "recsup_other_" + short, "xrecsup_all_" + short):
            with self.subTest(data=data):
                self.assertIsNone(self.pat.match(data))

    def test_the_upload_button_reaches_the_receipt_conversation_entry_point(self):
        self.assertIn(
            'CallbackQueryHandler(handle_receipt_for_ref_callback, pattern="^receipt_for_")', SRC)


class TheCountNeverDependsOnTheNewColumnTest(unittest.TestCase):
    """get_driver_pending_receipts feeds every suspension count. [] from it means
    "owes nothing" -- a missing accepted_at column must not get there."""

    ROWS = [
        {"lead_id": "l1", "accepted_at": "2026-09-09T14:57:00+00:00",
         "lead": {"reference_id": "0H9L16XH", "receipt_image_url": None}},
        {"lead_id": "l2", "accepted_at": "2026-09-10T22:13:00+00:00",
         "lead": {"reference_id": "1DJE7PD8", "receipt_image_url": ""}},
        {"lead_id": "l3", "accepted_at": "2026-09-09T15:14:00+00:00",
         "lead": {"reference_id": "XTKSZ6EF", "receipt_image_url": "https://x/r.jpg"}},
    ]

    def _db(self, *, new_select_fails=False, old_select_fails=False, waived=()):
        RealDatabase = _real_database()
        d = RealDatabase.__new__(RealDatabase)
        d._check_tables_exist = lambda: True
        d._waived_lead_ids = lambda ids: set(waived)
        d.selects = []

        def _select(cols):
            d.selects.append(cols)
            chain = mock.MagicMock()
            execute = chain.eq.return_value.eq.return_value.execute
            new = "accepted_at" in cols
            if (new and new_select_fails) or (not new and old_select_fails):
                execute.side_effect = Exception(
                    "column lead_assignments.accepted_at does not exist (42703)")
            else:
                rows = [dict(r) for r in self.ROWS]
                if not new:
                    for r in rows:
                        r.pop("accepted_at")
                execute.return_value = mock.MagicMock(data=rows)
            return chain

        d.client = mock.MagicMock()
        d.client.table.return_value.select.side_effect = _select
        return d

    def test_it_returns_accepted_at(self):
        d = self._db()
        got = d.get_driver_pending_receipts(DRIVER_ID)
        self.assertEqual(1, len(d.selects))
        self.assertIn("accepted_at", d.selects[0])
        d.client.table.assert_called_with("lead_assignments")
        self.assertEqual(
            [("0H9L16XH", "2026-09-09T14:57:00+00:00"), ("1DJE7PD8", "2026-09-10T22:13:00+00:00")],
            [(u["reference_id"], u["accepted_at"]) for u in got])
        self.assertEqual({"lead_id", "reference_id", "lead", "accepted_at"}, set(got[0]))

    def test_a_failing_accepted_at_select_retries_the_old_one(self):
        d = self._db(new_select_fails=True)
        with self.assertLogs(level="WARNING"):
            got = d.get_driver_pending_receipts(DRIVER_ID)
        self.assertEqual(2, len(d.selects))
        self.assertEqual(OLD_SELECT, d.selects[1])
        self.assertEqual(["0H9L16XH", "1DJE7PD8"], [u["reference_id"] for u in got])
        self.assertEqual([None, None], [u["accepted_at"] for u in got])

    def test_the_waiver_still_applies_on_the_retry(self):
        d = self._db(new_select_fails=True, waived={"l1"})
        self.assertEqual(["1DJE7PD8"],
                         [u["reference_id"] for u in d.get_driver_pending_receipts(DRIVER_ID)])

    def test_both_failing_is_still_empty(self):
        d = self._db(new_select_fails=True, old_select_fails=True)
        self.assertEqual([], d.get_driver_pending_receipts(DRIVER_ID))
        self.assertEqual(2, len(d.selects))


if __name__ == "__main__":
    unittest.main()
