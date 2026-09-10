r"""A paper order reaches everyone who works it, and a name cannot break it.

Asked for: "they dont have to accept just send full delivery address and all to
paper girls groups and supervisory".

What the order path did before this file:

* The paper girls (personal DMs and the notify group) got the full order; the
  supervisors got a separate, shortened copy -- driver name, city and ZIP only.
  Now every recipient gets the same full order and the same buttons, once.
  Nobody accepts anything; that step was removed in b45e1ba.
* The order, the reminder and the supervisor copy went out as Markdown with the
  driver's name, address and phone pasted in raw. One underscore, asterisk,
  bracket or backtick in any of them and Telegram rejects the whole message, so
  that recipient silently gets nothing. None of the live orders contain such a
  character -- this is not why orders are stuck -- but the next "John_Smith"
  would have got no paper.
* Nothing could say whether the bot can reach those chats at all. /api/health
  now reports how many are configured and how many answer, as counts only.

Run:  python -m pytest tests/test_paper_orders_reach_everyone.py -q
"""
import asyncio
import contextlib
import html
import os
import re
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy-key")

import bot as B  # noqa: E402
from api import bot_bridge  # noqa: E402


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


SHIP_ID = "11111111-2222-3333-4444-555555555555"

ADA = {
    "id": "i-1",
    "full_name": "Ada Lovelace",
    "mailing_address": "12 Main St, Newark, NJ 07102",
    "phone_number": "732-555-0000",
    "email": "ada@example.com",
    "telegram_id": "8887186065",
}

# Every character that breaks Markdown, and every one that breaks HTML.
HOSTILE = dict(
    ADA,
    full_name="John_Smith *Jr* [x] `y` <b> & co",
    mailing_address="Apt_4 [rear] <gate> & *side* `door`, Newark, NJ 07102",
    phone_number="<732>_555*0000",
)
HOSTILE_NAME_HTML = "John_Smith *Jr* [x] `y` &lt;b&gt; &amp; co"
HOSTILE_ADDR_HTML = "Apt_4 [rear] &lt;gate&gt; &amp; *side* `door`, Newark, NJ 07102"
HOSTILE_PHONE_HTML = "&lt;732&gt;_555*0000"

# An & that does not start an entity is one Telegram's HTML parser refuses.
BARE_AMPERSAND = re.compile(r"&(?!(?:amp|lt|gt|quot|#\d+|#x[0-9a-fA-F]+);)")


def recipients(dms=None, groups="", sups=None):
    """Set the three env-driven lists; the bot reads them at call time."""
    stack = contextlib.ExitStack()
    stack.enter_context(mock.patch.object(B.Config, "PAPER_GIRL_TELEGRAM_ID", dms))
    stack.enter_context(mock.patch.object(B.Config, "PAPER_GIRL_NOTIFY_CHAT_IDS", groups))
    stack.enter_context(mock.patch.object(B.Config, "SUPERVISORY_TELEGRAM_ID", sups))
    return stack


def fake_context(fail=()):
    ctx = mock.MagicMock()
    sent = []

    async def send(**kw):
        if kw.get("chat_id") in fail:
            raise RuntimeError("Forbidden: bot was blocked by the user")
        sent.append(kw)

    ctx.bot.send_message = mock.AsyncMock(side_effect=send)
    return ctx, sent


def hire(interview, *, fail=()):
    """Run the order step of a hire. Returns (shipment, warning, sends, inserts)."""
    ctx, sent = fake_context(fail)
    rows = []

    def create_shipment(**kw):
        rows.append(kw)
        return {
            "id": SHIP_ID,
            "status": kw["status"],
            "quantity": kw["quantity"],
            "driver_name": kw["driver_name"],
            "driver_address": kw["driver_address"],
            "driver_phone": kw["driver_phone"],
        }

    with mock.patch.object(B.shipments_db, "create_shipment", side_effect=create_shipment):
        ship, warn = run(B._create_and_notify_paper_girl(
            ctx, interview, created_by_telegram_id="999"))
    return ship, warn, sent, rows


def chat_ids(sent):
    return [kw["chat_id"] for kw in sent]


class OneOrderEveryoneTest(unittest.TestCase):

    def test_it_goes_to_every_paper_girl_dm_group_and_supervisor(self):
        with recipients(dms="7100000001,7100000002", groups="-1007100000003",
                        sups="7100000004,7100000005"):
            ship, warn, sent, rows = hire(ADA)
        self.assertEqual(SHIP_ID, ship["id"])
        self.assertIsNone(warn)
        self.assertEqual(
            [7100000001, 7100000002, -1007100000003, 7100000004, 7100000005],
            chat_ids(sent))
        # One order, the same for everyone: the full address, sent as HTML.
        self.assertEqual(1, len({kw["text"] for kw in sent}))
        for kw in sent:
            self.assertIn("12 Main St, Newark, NJ 07102", kw["text"])
            self.assertEqual("HTML", kw["parse_mode"])
        # The row itself is written as before, city and ZIP included.
        self.assertEqual(1, len(rows))
        self.assertEqual("Newark", rows[0]["driver_city"])
        self.assertEqual("07102", rows[0]["driver_zip"])
        self.assertEqual("awaiting_tracking", rows[0]["status"])

    def test_a_chat_in_two_lists_gets_it_exactly_once(self):
        # 7100000001 is a paper girl, is repeated in the notify groups and is a
        # supervisor; the group is listed twice, once with stray spaces.
        with recipients(dms="7100000001", groups="-1007100000003, 7100000001",
                        sups=" 7100000001 ,-1007100000003,7100000004"):
            _, warn, sent, _ = hire(ADA)
        ids = chat_ids(sent)
        self.assertEqual(len(ids), len(set(ids)), ids)
        self.assertEqual({7100000001, -1007100000003, 7100000004}, set(ids))
        self.assertIsNone(warn)

    def test_the_same_id_as_int_and_as_string_is_sent_once(self):
        with mock.patch.object(B, "_paper_girl_notify_chat_ids",
                               return_value=[7100000001, -1007100000003]):
            with mock.patch.object(B, "_global_supervisory_chat_ids",
                                   return_value=["7100000001", " -1007100000003 ",
                                                 7100000004]):
                _, warn, sent, _ = hire(ADA)
        self.assertEqual([7100000001, -1007100000003, 7100000004], chat_ids(sent))
        self.assertIsNone(warn)

    def test_supervisors_get_the_full_address_not_city_and_zip(self):
        with recipients(dms="7100000001", groups="-1007100000003", sups="7100000004"):
            _, _, sent, _ = hire(ADA)
        by_chat = {kw["chat_id"]: kw["text"] for kw in sent}
        sup = by_chat[7100000004]
        for needle in ("Ada Lovelace", "12 Main St, Newark, NJ 07102", "732-555-0000",
                       f"{B.Config.DEFAULT_PAPER_QTY} temp tag papers"):
            self.assertIn(needle, sup, needle)
        # Not a summary: the very message the paper girls get.
        self.assertEqual({sup}, set(by_chat.values()))

    def test_everyone_gets_the_same_buttons_and_none_of_them_accepts(self):
        with recipients(dms="7100000001", groups="-1007100000003", sups="7100000004"):
            _, _, sent, _ = hire(ADA)
        expected = {"ship_track_" + B._short_uuid(SHIP_ID), "ship_view_all"}
        self.assertEqual(3, len(sent))
        for kw in sent:
            buttons = [b for row in kw["reply_markup"].inline_keyboard for b in row]
            self.assertEqual(expected, {b.callback_data for b in buttons})
            for b in buttons:
                blob = f"{b.text} {b.callback_data}".lower()
                self.assertNotIn("accept", blob)
                self.assertNotIn("claim", blob)


class ANameCannotBreakDeliveryTest(unittest.TestCase):

    def test_every_value_is_escaped_and_sent_as_html(self):
        with recipients(dms="7100000001", groups="-1007100000003", sups="7100000004"):
            _, warn, sent, _ = hire(HOSTILE)
        self.assertIsNone(warn)
        self.assertEqual(3, len(sent))
        for kw in sent:
            self.assertEqual("HTML", kw["parse_mode"])
            text = kw["text"]
            self.assertIn(HOSTILE_NAME_HTML, text)
            self.assertIn(HOSTILE_ADDR_HTML, text)
            self.assertIn(HOSTILE_PHONE_HTML, text)
            # No tag survives into the order, and no & that is not an entity.
            self.assertEqual([], re.findall(r"<[^>]*>", text))
            self.assertIsNone(BARE_AMPERSAND.search(text))
            # Read back, it says exactly what the driver wrote.
            plain = html.unescape(text)
            self.assertIn(HOSTILE["full_name"], plain)
            self.assertIn(HOSTILE["mailing_address"], plain)

    def test_the_fixed_text_has_no_markdown_left_and_nothing_html_would_misread(self):
        with recipients(dms="7100000001"):
            _, _, sent, _ = hire(ADA)
        text = sent[0]["text"]
        for ch in ("*", "_", "`", "[", "]", "<", ">", "&"):
            self.assertNotIn(ch, text, ch)

    def test_the_driver_notice_is_still_plain_text(self):
        """The driver's own copy is sent without a parse mode: escaping it would
        print &amp; at them."""
        text = B._format_driver_paper_ship_notice(HOSTILE)
        self.assertIn(HOSTILE["full_name"], text)
        self.assertNotIn("&amp;", text)


class DeliveryFailuresAreReportedTest(unittest.TestCase):

    def test_one_unreachable_recipient_does_not_stop_the_others(self):
        # A paper girl and a supervisor both fail. Everyone after them still
        # gets the order, and BOTH failures are in the warning.
        with recipients(dms="7100000001,7100000002", groups="-1007100000003",
                        sups="7100000004,7100000005"):
            ship, warn, sent, _ = hire(ADA, fail=(7100000002, 7100000004))
        self.assertEqual(SHIP_ID, ship["id"])
        self.assertEqual([7100000001, -1007100000003, 7100000005], chat_ids(sent))
        self.assertIn("Some paper order notifications failed", warn)
        self.assertIn("chat 7100000002", warn)
        self.assertIn("chat 7100000004", warn)
        self.assertIn("blocked by the user", warn)

    def test_when_every_send_fails_the_warning_says_nobody_got_it(self):
        with recipients(dms="7100000001", groups="-1007100000003", sups="7100000004"):
            ship, warn, sent, _ = hire(
                ADA, fail=(7100000001, -1007100000003, 7100000004))
        self.assertEqual(SHIP_ID, ship["id"])
        self.assertEqual([], sent)
        self.assertIn("reached nobody", warn)

    def test_no_recipient_configured_still_saves_the_shipment_and_says_so(self):
        with recipients(dms=None, groups="", sups=None):
            ship, warn, sent, rows = hire(ADA)
        self.assertEqual(SHIP_ID, ship["id"])
        self.assertEqual(1, len(rows))
        self.assertEqual([], sent)
        self.assertIn("nobody was notified", warn)

    def test_supervisors_alone_still_get_it_with_a_warning(self):
        with recipients(dms=None, groups="", sups="7100000004"):
            ship, warn, sent, _ = hire(ADA)
        self.assertEqual([7100000004], chat_ids(sent))
        self.assertIn("only supervisors", warn)


class TheReminderTest(unittest.TestCase):

    def _remind(self, shipments):
        ctx, sent = fake_context()
        with mock.patch.object(B.shipments_db, "list_shipments", return_value=shipments), \
             mock.patch.object(B.shipments_db, "update_shipment", return_value=True) as upd:
            run(B.paper_girl_receipt_reminder_job(ctx))
        return sent, upd

    @staticmethod
    def _row(interview, status, sid=SHIP_ID):
        return {
            "id": sid,
            "status": status,
            "quantity": 10,
            "driver_name": interview["full_name"],
            "driver_address": interview["mailing_address"],
            "driver_phone": interview["phone_number"],
        }

    def test_it_is_html_escaped_and_goes_to_paper_girls_only(self):
        with recipients(dms="7100000001", groups="-1007100000003",
                        sups="7100000004,7100000005"):
            sent, _ = self._remind([self._row(HOSTILE, "awaiting_tracking")])
        self.assertEqual([7100000001, -1007100000003], chat_ids(sent))
        for kw in sent:
            text = kw["text"]
            self.assertEqual("HTML", kw["parse_mode"])
            self.assertTrue(text.startswith("⏰ <b>Reminder</b>"), text[:40])
            self.assertEqual(["<b>", "</b>"], re.findall(r"<[^>]*>", text))
            self.assertNotIn("**", text)
            self.assertIn(HOSTILE_NAME_HTML, text)
            self.assertIn(HOSTILE_ADDR_HTML, text)
            self.assertIsNone(BARE_AMPERSAND.search(text))

    def test_a_legacy_pending_accept_row_is_moved_on_and_still_reminded(self):
        done = self._row(ADA, "driver_notified", sid="22222222-3333-4444-5555-666666666666")
        with recipients(dms="7100000001"):
            sent, upd = self._remind([self._row(ADA, "pending_accept"), done])
        upd.assert_called_once_with(SHIP_ID, {"status": "awaiting_tracking"})
        # One reminder: the open order. The finished one is left alone.
        self.assertEqual(1, len(sent))
        self.assertIn("12 Main St", sent[0]["text"])


DMS = "8100000001,8100000002"
GROUPS = "-1008100000003,-1008100000004,-1008100000005"
SUPS = "8100000006,8100000001"   # 8100000001 is a paper girl AND a supervisor
BOT_ID = 8100000009
EVERY_ID = ("8100000001", "8100000002", "8100000003", "8100000004",
            "8100000005", "8100000006", str(BOT_ID))


def fake_bot():
    """A bot that knows two people, is in one group, was removed from another
    and is muted in a third. 8100000002 never pressed Start."""
    chats = {
        8100000001: "private",
        8100000006: "private",
        -1008100000003: "supergroup",
        -1008100000004: "group",
        -1008100000005: "supergroup",
    }
    members = {
        -1008100000003: types.SimpleNamespace(status="member"),
        -1008100000004: types.SimpleNamespace(status="left"),
        -1008100000005: types.SimpleNamespace(
            status="restricted", is_member=True, can_send_messages=False),
    }
    bot = mock.MagicMock()
    bot.id = BOT_ID

    async def get_chat(chat_id):
        if chat_id not in chats:
            raise RuntimeError("Bad Request: chat not found")
        return types.SimpleNamespace(id=chat_id, type=chats[chat_id],
                                     title="Paper Girls HQ", username="papergirlshq")

    async def get_chat_member(chat_id, user_id):
        if user_id != BOT_ID:
            raise RuntimeError("asked about somebody other than the bot")
        return members[chat_id]

    bot.get_chat = mock.AsyncMock(side_effect=get_chat)
    bot.get_chat_member = mock.AsyncMock(side_effect=get_chat_member)
    bot.send_message = mock.AsyncMock()
    return bot


class HealthSaysWhoCanBeReachedTest(unittest.TestCase):

    def setUp(self):
        bot_bridge._paper_order_recipients = None

    tearDown = setUp

    @staticmethod
    def _health():
        from fastapi.testclient import TestClient
        from api.app import create_app
        return TestClient(create_app()).get("/api/health")

    def test_it_reports_configured_and_reachable_counts_and_no_ids(self):
        bot = fake_bot()
        ctx = mock.MagicMock()
        ctx.bot = bot
        with recipients(dms=DMS, groups=GROUPS, sups=SUPS):
            run(B.paper_order_recipient_reach_job(ctx))

        # Read-only: nothing was sent, and each chat was asked about once.
        bot.send_message.assert_not_called()
        self.assertEqual(6, bot.get_chat.await_count)

        bot.get_chat.reset_mock()
        bot.get_chat_member.reset_mock()
        resp = self._health()
        resp_again = self._health()
        # A health request never goes to Telegram.
        self.assertEqual(0, bot.get_chat.await_count)
        self.assertEqual(0, bot.get_chat_member.await_count)

        self.assertEqual(200, resp.status_code)
        body = resp.json()
        self.assertEqual(body, resp_again.json())
        self.assertIs(True, body["ok"])
        self.assertIn("commit", body)
        self.assertIn("bot_token", body)
        rec = body["paper_order_recipients"]
        self.assertEqual({"configured": 2, "reachable": 1}, rec["paper_girl_dms"])
        self.assertEqual({"configured": 3, "reachable": 1}, rec["paper_girl_groups"])
        self.assertEqual({"configured": 2, "reachable": 2}, rec["supervisors"])
        self.assertIsInstance(rec["checked_at"], str)
        for secret in EVERY_ID + ("Paper Girls HQ", "papergirlshq", "chat not found"):
            self.assertNotIn(secret, resp.text, secret)

    def test_before_the_first_check_it_still_reports_what_is_configured(self):
        with recipients(dms=DMS, groups=GROUPS, sups=SUPS):
            B._publish_recipient_reach(None)
        rec = self._health().json()["paper_order_recipients"]
        self.assertEqual({"configured": 2, "reachable": None}, rec["paper_girl_dms"])
        self.assertEqual({"configured": 3, "reachable": None}, rec["paper_girl_groups"])
        self.assertEqual({"configured": 2, "reachable": None}, rec["supervisors"])
        self.assertIsNone(rec["checked_at"])

    def test_telegram_down_reads_as_zero_reachable_not_a_crash(self):
        class DownBot:
            @property
            def id(self):
                raise RuntimeError("bot not initialized")

            async def get_chat(self, chat_id):
                raise TimeoutError("Timed out")

            async def get_chat_member(self, chat_id, user_id):
                raise TimeoutError("Timed out")

            async def send_message(self, **kw):
                raise AssertionError("a reachability check must never send")

        ctx = mock.MagicMock()
        ctx.bot = DownBot()
        with recipients(dms=DMS, groups=GROUPS, sups=SUPS):
            run(B.paper_order_recipient_reach_job(ctx))
        resp = self._health()
        self.assertEqual(200, resp.status_code)
        rec = resp.json()["paper_order_recipients"]
        self.assertEqual({"configured": 2, "reachable": 0}, rec["paper_girl_dms"])
        self.assertEqual({"configured": 3, "reachable": 0}, rec["paper_girl_groups"])
        self.assertEqual({"configured": 2, "reachable": 0}, rec["supervisors"])
        self.assertNotIn("Timed out", resp.text)

    def test_a_bot_removed_or_muted_cannot_post(self):
        ns = types.SimpleNamespace
        self.assertTrue(B._bot_can_post(ns(status="member"), "supergroup"))
        self.assertTrue(B._bot_can_post(ns(status="administrator"), "group"))
        self.assertFalse(B._bot_can_post(ns(status="left"), "supergroup"))
        self.assertFalse(B._bot_can_post(ns(status="kicked"), "supergroup"))
        self.assertFalse(B._bot_can_post(
            ns(status="restricted", is_member=True, can_send_messages=False), "group"))
        self.assertTrue(B._bot_can_post(
            ns(status="restricted", is_member=True, can_send_messages=True), "group"))
        # A channel takes an admin with the right to post.
        self.assertFalse(B._bot_can_post(ns(status="member"), "channel"))
        self.assertFalse(B._bot_can_post(
            ns(status="administrator", can_post_messages=False), "channel"))
        self.assertTrue(B._bot_can_post(
            ns(status="administrator", can_post_messages=True), "channel"))


if __name__ == "__main__":
    unittest.main()
