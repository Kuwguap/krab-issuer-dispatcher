r"""A driver is not unreachable because Telegram was busy for a second.

Reported: "sending to individual drivers fails sometimes and says could not
send to driver".

Every driver-facing send went straight at context.bot.send_message with no
retry, so one 429 or one dropped connection reported a perfectly reachable
driver as unreachable — and on the instant-tag path dropped them from the offer.

Run:  venv\Scripts\python.exe -m pytest tests/test_driver_send_resilience.py -q
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy-key")

import bot  # noqa: E402

SRC = (ROOT / "bot.py").read_text(encoding="utf-8")


class ABusyMomentIsNotAFailedDeliveryTest(unittest.IsolatedAsyncioTestCase):

    def _ctx(self, *side_effect):
        ctx = mock.MagicMock()
        ctx.bot.send_message = mock.AsyncMock(side_effect=list(side_effect))
        return ctx

    async def test_a_first_time_send_is_one_call(self):
        ctx = self._ctx("ok")
        with mock.patch.object(bot.asyncio, "sleep", mock.AsyncMock()):
            out = await bot._send_message_resiliently(ctx, 1, "hi")
        self.assertEqual("ok", out)
        self.assertEqual(1, ctx.bot.send_message.await_count)

    async def test_a_rate_limit_waits_and_succeeds(self):
        ctx = self._ctx(RetryAfter(3), "ok")
        slept = []
        with mock.patch.object(bot.asyncio, "sleep",
                               mock.AsyncMock(side_effect=lambda s: slept.append(s))):
            out = await bot._send_message_resiliently(ctx, 1, "hi")
        self.assertEqual("ok", out)
        self.assertEqual([3], slept, "it must wait exactly as long as Telegram asks")

    async def test_a_dropped_connection_is_retried(self):
        for err in (TimedOut(), NetworkError("connection reset")):
            ctx = self._ctx(err, "ok")
            with mock.patch.object(bot.asyncio, "sleep", mock.AsyncMock()):
                self.assertEqual("ok", await bot._send_message_resiliently(ctx, 1, "hi"))

    async def test_a_bad_request_is_not_retried(self):
        """Bad HTML, a blocked bot or a dead chat id fails identically every
        time — retrying only delays the honest answer."""
        ctx = self._ctx(BadRequest("chat not found"))
        with mock.patch.object(bot.asyncio, "sleep", mock.AsyncMock()):
            with self.assertRaises(BadRequest):
                await bot._send_message_resiliently(ctx, 1, "hi")
        self.assertEqual(1, ctx.bot.send_message.await_count)

    async def test_it_gives_up_and_reports_honestly(self):
        ctx = self._ctx(TimedOut(), TimedOut(), TimedOut())
        with mock.patch.object(bot.asyncio, "sleep", mock.AsyncMock()):
            with self.assertRaises(TimedOut):
                await bot._send_message_resiliently(ctx, 1, "hi")
        self.assertEqual(3, ctx.bot.send_message.await_count)

    async def test_a_long_rate_limit_is_capped(self):
        """Telegram occasionally asks for minutes; the dispatch cannot stall."""
        ctx = self._ctx(RetryAfter(600), "ok")
        slept = []
        with mock.patch.object(bot.asyncio, "sleep",
                               mock.AsyncMock(side_effect=lambda s: slept.append(s))):
            await bot._send_message_resiliently(ctx, 1, "hi")
        self.assertEqual([30], slept)

    async def test_the_kwargs_reach_telegram(self):
        ctx = self._ctx("ok")
        await bot._send_message_resiliently(ctx, 7, "hi", parse_mode="HTML",
                                            reply_markup="kb")
        kw = ctx.bot.send_message.call_args.kwargs
        self.assertEqual(7, kw["chat_id"])
        self.assertEqual("HTML", kw["parse_mode"])
        self.assertEqual("kb", kw["reply_markup"])


class EveryDriverSendUsesItTest(unittest.TestCase):

    def _fn(self, name):
        return SRC.split(f"async def {name}", 1)[1].split("\nasync def ", 1)[0]

    def test_the_instant_tag_offer(self):
        self.assertIn("_send_message_resiliently", self._fn("_dispatch_instant_tag_lead"))

    def test_the_message_after_accept(self):
        self.assertIn("_send_message_resiliently",
                      self._fn("_instant_tag_link_after_accept"))

    def test_the_full_job_ticket(self):
        body = self._fn("_send_full_group_lead_to_chat")
        self.assertIn("_send_message_resiliently", body)
        self.assertNotIn("await context.bot.send_message(chat_id=target_cid", body)

    def test_the_team_fan_out(self):
        self.assertIn("_send_message_resiliently",
                      self._fn("_send_driver_requests_for_group"))


if __name__ == "__main__":
    unittest.main()
