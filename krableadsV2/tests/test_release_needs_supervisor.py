r"""Releasing a tag without payment is a supervisor's act, on their own lead.

Asked for: "i should have supervisor put in password AdminPassword123! and
supervisor only who sends lead".

Both flows offer that password — Skip Dispatch always, Instant Tag whenever the
issuer may actually use it. Neither used to check who was typing.

Run:  venv\Scripts\python.exe -m pytest tests/test_release_needs_supervisor.py -q
"""
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

import bot  # noqa: E402

SUPERVISOR = 900500
ISSUER = 900501          # sends leads, is not a supervisor
STRANGER = 900502
LEAD_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
D1 = "11111111-1111-4111-8111-111111111111"
D2 = "22222222-2222-4222-8222-222222222222"
D3 = "33333333-3333-4333-8333-333333333333"
D9 = "99999999-9999-4999-8999-999999999999"
_ids = [D1, D2, D3]


def _lead(**over):
    lead = {"id": LEAD_ID, "reference_id": "REF9", "user_id": SUPERVISOR}
    lead.update(over)
    return lead


def _cb(lead_id, driver_id):
    """What the release picker actually puts on a button."""
    return (bot.SKIP_DISPATCH_RELEASE_CB + bot._short_uuid(lead_id)
            + bot._short_uuid(driver_id))


def _sup(*ids):
    """Only these ids are supervisors, for the duration of the block."""
    allowed = {str(i) for i in ids}
    return mock.patch.object(bot, "_user_is_global_supervisor",
                             lambda uid: str(uid) in allowed)


class WhoMayReleaseTest(unittest.TestCase):

    def test_the_supervisor_who_sent_it(self):
        with _sup(SUPERVISOR):
            self.assertEqual((True, "ok"),
                             bot._skip_dispatch_allowed(_lead(), SUPERVISOR))

    def test_the_issuer_alone_is_not_enough(self):
        with _sup(SUPERVISOR):
            ok, why = bot._skip_dispatch_allowed(_lead(user_id=ISSUER), ISSUER)
        self.assertFalse(ok)
        self.assertEqual("not_supervisor", why)

    def test_a_supervisor_may_not_release_someone_elses_lead(self):
        with _sup(SUPERVISOR):
            ok, why = bot._skip_dispatch_allowed(_lead(user_id=ISSUER), SUPERVISOR)
        self.assertFalse(ok)
        self.assertEqual("not_the_issuer", why)

    def test_a_stranger_gets_nothing(self):
        with _sup(SUPERVISOR):
            ok, _ = bot._skip_dispatch_allowed(_lead(), STRANGER)
        self.assertFalse(ok)

    def test_ids_compare_across_str_and_int(self):
        """Telegram hands ints; Supabase hands the column back as a string."""
        with _sup(SUPERVISOR):
            self.assertTrue(bot._skip_dispatch_allowed(
                _lead(user_id=str(SUPERVISOR)), SUPERVISOR)[0])
            self.assertTrue(bot._skip_dispatch_allowed(
                _lead(user_id=SUPERVISOR), str(SUPERVISOR))[0])

    def test_a_lead_with_no_issuer_is_not_a_lead_nobody_can_release(self):
        """Web-dispatch and API leads carry no user_id. A supervisor still can."""
        with _sup(SUPERVISOR):
            ok, why = bot._skip_dispatch_allowed(_lead(user_id=None), SUPERVISOR)
            self.assertTrue(ok)
            self.assertEqual("ok_no_issuer_on_lead", why)
            self.assertFalse(bot._skip_dispatch_allowed(_lead(user_id=None), ISSUER)[0])

    def test_no_user_at_all(self):
        with _sup(SUPERVISOR):
            self.assertEqual((False, "no_user"),
                             bot._skip_dispatch_allowed(_lead(), None))


class ThePasswordComparisonTest(unittest.TestCase):

    def test_it_matches(self):
        self.assertTrue(bot._password_matches(bot._skip_dispatch_password()))

    def test_a_near_miss_does_not(self):
        self.assertFalse(bot._password_matches(bot._skip_dispatch_password() + "x"))
        self.assertFalse(bot._password_matches(""))

    def test_an_accented_message_does_not_crash_it(self):
        """hmac.compare_digest raises TypeError on non-ASCII str, and this
        handler reads every text message in the bot."""
        for text in ("café", "José Martínez", "🚗", "naïve"):
            self.assertFalse(bot._password_matches(text), text)


class TheRefusalPathTest(unittest.IsolatedAsyncioTestCase):

    def _update(self, uid, text=None):
        upd = mock.MagicMock()
        upd.effective_user = mock.MagicMock(id=uid)
        upd.effective_chat = mock.MagicMock(id=uid, type="private")
        msg = mock.MagicMock()
        msg.text = text if text is not None else bot._skip_dispatch_password()
        msg.photo = None
        msg.delete = mock.AsyncMock()
        upd.effective_message = msg
        return upd, msg

    async def _type_password(self, uid, pending, lead, sups=(SUPERVISOR,)):
        upd, msg = self._update(uid)
        ctx = mock.MagicMock()
        ctx.user_data = {bot.SKIP_DISPATCH_PENDING_KEY: pending}
        ctx.bot.send_message = mock.AsyncMock()
        fake_db = mock.MagicMock()
        fake_db.get_lead_by_id.return_value = lead
        delivered = mock.AsyncMock(return_value=True)
        stopped = False
        with _sup(*sups), \
                mock.patch.object(bot, "db", fake_db), \
                mock.patch.object(bot, "_driver_row_by_id",
                                  lambda i: {"id": i, "driver_name": "Susan"}), \
                mock.patch.object(bot, "_deliver_skip_dispatch", delivered):
            try:
                await bot.handle_skip_dispatch_password(upd, ctx)
            except bot.ApplicationHandlerStop:
                stopped = True
        said = " ".join(str(c.kwargs.get("text") or "")
                        for c in ctx.bot.send_message.call_args_list)
        return {"delivered": delivered.await_count, "said": said, "msg": msg,
                "ctx": ctx, "stopped": stopped}

    def _armed(self, **over):
        p = {"lead_id": LEAD_ID, "driver_id": D1, "driver_ids": [D1],
             "by": SUPERVISOR, "at": time.time()}
        p.update(over)
        return p

    async def test_the_supervisor_who_sent_it_releases_the_tag(self):
        out = await self._type_password(SUPERVISOR, self._armed(), _lead())
        self.assertEqual(1, out["delivered"])

    async def test_a_non_supervisor_releases_nothing(self):
        out = await self._type_password(ISSUER, self._armed(by=ISSUER),
                                        _lead(user_id=ISSUER))
        self.assertEqual(0, out["delivered"], "a non-supervisor released a tag")
        self.assertIn("supervisor", out["said"].lower())

    async def test_a_refusal_still_deletes_the_password_from_the_chat(self):
        out = await self._type_password(ISSUER, self._armed(by=ISSUER),
                                        _lead(user_id=ISSUER))
        out["msg"].delete.assert_awaited()

    async def test_a_refusal_stops_the_message_reaching_any_other_handler(self):
        """The text has been proven to BE the password by this point; letting it
        fall through would write it onto a review card or into the database."""
        out = await self._type_password(ISSUER, self._armed(by=ISSUER),
                                        _lead(user_id=ISSUER))
        self.assertTrue(out["stopped"], "a refused password fell through")

    async def test_a_refusal_leaves_the_window_armed_to_try_again(self):
        """A settings read that blipped must not cost the supervisor their window."""
        out = await self._type_password(ISSUER, self._armed(by=ISSUER),
                                        _lead(user_id=ISSUER))
        self.assertIn(bot.SKIP_DISPATCH_PENDING_KEY, out["ctx"].user_data)

    async def test_a_tag_already_delivered_is_not_sent_a_second_time(self):
        out = await self._type_password(
            SUPERVISOR, self._armed(),
            _lead(instant_tag=True, instant_pdf_delivered_at="2026-08-29T10:00:00Z"))
        self.assertEqual(0, out["delivered"])
        self.assertIn("already", out["said"].lower())

    async def test_a_wrong_password_is_still_silent(self):
        upd, msg = self._update(ISSUER, text="not the password")
        ctx = mock.MagicMock()
        ctx.user_data = {bot.SKIP_DISPATCH_PENDING_KEY: self._armed()}
        ctx.bot.send_message = mock.AsyncMock()
        with _sup(SUPERVISOR), mock.patch.object(bot, "db", mock.MagicMock()):
            await bot.handle_skip_dispatch_password(upd, ctx)
        ctx.bot.send_message.assert_not_awaited()
        msg.delete.assert_not_awaited()


class ManyDriversNeedAPickTest(unittest.IsolatedAsyncioTestCase):
    """An Instant Tag broadcast has no single driver to release to. Guessing one
    would put the client's address and phone in the wrong driver's chat."""

    async def test_the_password_opens_a_picker_and_sends_nothing(self):
        upd = mock.MagicMock()
        upd.effective_user = mock.MagicMock(id=SUPERVISOR)
        upd.effective_chat = mock.MagicMock(id=SUPERVISOR, type="private")
        upd.effective_message.text = bot._skip_dispatch_password()
        upd.effective_message.delete = mock.AsyncMock()
        ctx = mock.MagicMock()
        ctx.user_data = {bot.SKIP_DISPATCH_PENDING_KEY: {
            "lead_id": LEAD_ID, "driver_id": "", "driver_ids": [D1, D2],
            "by": SUPERVISOR, "at": time.time()}}
        ctx.bot.send_message = mock.AsyncMock()
        fake_db = mock.MagicMock()
        fake_db.get_lead_by_id.return_value = _lead(instant_tag=True)
        delivered = mock.AsyncMock(return_value=True)
        drivers = [{"id": D1, "driver_name": "Susan", "is_active": True},
                   {"id": D2, "driver_name": "Marcus", "is_active": True}]
        with _sup(SUPERVISOR), \
                mock.patch.object(bot, "db", fake_db), \
                mock.patch.object(bot, "_get_all_drivers_cached", lambda: drivers), \
                mock.patch.object(bot, "_deliver_skip_dispatch", delivered):
            with self.assertRaises(bot.ApplicationHandlerStop):
                await bot.handle_skip_dispatch_password(upd, ctx)
        self.assertEqual(0, delivered.await_count, "it guessed a driver")
        kb = ctx.bot.send_message.call_args.kwargs.get("reply_markup")
        names = [b.text for row in kb.inline_keyboard for b in row]
        self.assertEqual(["🚗 Susan", "🚗 Marcus"], names)
        # And the window stays open for the tap that follows.
        self.assertTrue(ctx.user_data[bot.SKIP_DISPATCH_PENDING_KEY]["await_pick"])

    async def _pick(self, presser, data, pending):
        q = mock.MagicMock()
        q.data = data
        q.answer = mock.AsyncMock()
        q.from_user = mock.MagicMock(id=presser)
        q.message.chat_id = presser
        q.message.reply_text = mock.AsyncMock()
        upd = mock.MagicMock(callback_query=q)
        ctx = mock.MagicMock()
        ctx.user_data = {bot.SKIP_DISPATCH_PENDING_KEY: pending}
        fake_db = mock.MagicMock()
        fake_db.get_lead_by_id.return_value = _lead(instant_tag=True)
        delivered = mock.AsyncMock(return_value=True)
        with _sup(SUPERVISOR), \
                mock.patch.object(bot, "db", fake_db), \
                mock.patch.object(bot, "_driver_row_by_id",
                                  lambda i: {"id": i, "driver_name": "Susan"}), \
                mock.patch.object(bot, "_deliver_skip_dispatch", delivered):
            await bot.handle_skip_dispatch_release_pick(upd, ctx)
        return delivered.await_count, fake_db, q

    def _picked(self, **over):
        p = {"lead_id": LEAD_ID, "driver_id": "", "driver_ids": [D1, D2],
             "by": SUPERVISOR, "await_pick": True, "at": time.time()}
        p.update(over)
        return p

    async def test_the_pick_delivers(self):
        n, fake_db, _ = await self._pick(
            SUPERVISOR, _cb(LEAD_ID, D2), self._picked())
        self.assertEqual(1, n)

    async def test_the_release_is_stamped_so_a_late_payment_cannot_send_it_again(self):
        n, fake_db, _ = await self._pick(
            SUPERVISOR, _cb(LEAD_ID, D2), self._picked())
        fake_db.mark_instant_pdf_delivered.assert_called_once_with(LEAD_ID)
        wrote = [c.args[1] for c in fake_db.update_lead.call_args_list]
        self.assertTrue(any(w.get("instant_pdf_driver_id") == D2 for w in wrote))
        self.assertFalse(any("instant_pdf_paid_at" in w for w in wrote),
                         "writing paid_at would hand the sweep a lead to deliver")

    async def test_somebody_else_cannot_finish_the_release(self):
        n, _, q = await self._pick(
            STRANGER, _cb(LEAD_ID, D2), self._picked())
        self.assertEqual(0, n)

    async def test_a_driver_the_lead_never_went_to_is_refused(self):
        n, _, _ = await self._pick(
            SUPERVISOR, _cb(LEAD_ID, D9), self._picked())
        self.assertEqual(0, n, "a driver outside the offer received the tag")

    async def test_another_lead_id_in_the_callback_is_refused(self):
        n, _, _ = await self._pick(
            SUPERVISOR, _cb("bbbbbbbb-cccc-4ddd-8eee-ffffffffffff", D2), self._picked())
        self.assertEqual(0, n)

    async def test_an_expired_release_delivers_nothing(self):
        n, _, _ = await self._pick(
            SUPERVISOR, _cb(LEAD_ID, D2),
            self._picked(at=time.time() - bot._SKIP_DISPATCH_TTL_SEC - 5))
        self.assertEqual(0, n)


class TheButtonRefusesBeforeItUnsendsAnythingTest(unittest.IsolatedAsyncioTestCase):
    """A refusal after the unsend would leave the lead pulled back from the team
    and releasable by nobody."""

    async def test_a_non_supervisor_tap_pulls_nothing_back(self):
        q = mock.MagicMock()
        q.data = f"{bot.INSTANT_PDF_CB}{LEAD_ID}"
        q.answer = mock.AsyncMock()
        q.from_user = mock.MagicMock(id=ISSUER)
        q.message.reply_text = mock.AsyncMock()
        upd = mock.MagicMock(callback_query=q)
        fake_db = mock.MagicMock()
        fake_db.get_lead_by_id.return_value = _lead(user_id=ISSUER)
        unsend = mock.AsyncMock(return_value=(3, 0))
        with _sup(SUPERVISOR), \
                mock.patch.object(bot, "db", fake_db), \
                mock.patch.object(bot, "_delete_dispatch_messages", unsend):
            await bot.handle_instant_pdf_request(upd, mock.MagicMock())
        unsend.assert_not_awaited()
        said = str(q.message.reply_text.call_args.args[0])
        self.assertIn("supervisor", said.lower())


class TheInstantTagArmsTheRightPeopleTest(unittest.IsolatedAsyncioTestCase):
    """It used to arm the password only for a single driver, so once All Drivers
    became the default an Instant Tag had no password release at all."""

    async def _dispatch(self, drivers, by, sups=(SUPERVISOR,)):
        ctx = mock.MagicMock()
        ctx.bot.send_message = mock.AsyncMock()
        user_data = {}
        with _sup(*sups), \
                mock.patch.object(bot, "request_instant_pdf_link",
                                  mock.AsyncMock(return_value=("https://pay", None))), \
                mock.patch.object(bot, "_driver_amount_cents", lambda l: 20000):
            await bot._dispatch_instant_tag_lead(
                ctx, _lead(instant_tag=True, price="$250"), drivers,
                notify_chat_id=by, user_data=user_data, by_user_id=by)
        said = " ".join(str(c.kwargs.get("text") or "")
                        for c in ctx.bot.send_message.call_args_list)
        return user_data.get(bot.SKIP_DISPATCH_PENDING_KEY), said

    def _drivers(self, n):
        return [{"id": _ids[i - 1], "driver_name": f"D{i}",
                 "driver_telegram_id": str(700000 + i)} for i in range(1, n + 1)]

    async def test_all_drivers_still_arms_the_password(self):
        armed, said = await self._dispatch(self._drivers(3), SUPERVISOR)
        self.assertIsNotNone(armed, "a broadcast left the supervisor no password")
        self.assertEqual([D1, D2, D3], armed["driver_ids"])
        self.assertEqual("", armed["driver_id"], "it pre-picked a driver")
        self.assertIn("pick which driver", said)

    async def test_one_driver_arms_that_driver_directly(self):
        armed, said = await self._dispatch(self._drivers(1), SUPERVISOR)
        self.assertEqual(D1, armed["driver_id"])
        self.assertIn("reply here with the password", said)

    async def test_an_ordinary_issuer_is_never_armed_or_told_about_it(self):
        """Offering a bypass they cannot take would only make the password
        handler an oracle for them."""
        with mock.patch.object(bot, "_skip_dispatch_allowed",
                               lambda lead, uid: (False, "not_supervisor")):
            armed, said = await self._dispatch(self._drivers(2), ISSUER)
        self.assertIsNone(armed)
        self.assertNotIn("password", said.lower())

    async def test_a_driver_that_could_not_be_reached_is_not_offered_the_tag(self):
        ctx = mock.MagicMock()
        ctx.bot.send_message = mock.AsyncMock(
            side_effect=[Exception("blocked"), None, None])
        user_data = {}
        with _sup(SUPERVISOR), \
                mock.patch.object(bot, "request_instant_pdf_link",
                                  mock.AsyncMock(return_value=("https://pay", None))), \
                mock.patch.object(bot, "_driver_amount_cents", lambda l: 20000):
            await bot._dispatch_instant_tag_lead(
                ctx, _lead(instant_tag=True), self._drivers(2),
                notify_chat_id=None, user_data=user_data, by_user_id=SUPERVISOR)
        self.assertEqual([D2], user_data[bot.SKIP_DISPATCH_PENDING_KEY]["driver_ids"])


class EveryPickerButtonFitsInTelegramTest(unittest.TestCase):
    """callback_data is capped at 64 bytes and Telegram rejects the WHOLE
    keyboard when one button is over — so the message carrying it never arrives.

    Both Skip Dispatch pickers used to send two raw 36-char UUIDs plus a prefix:
    81 bytes. Tapping "Skip Dispatch" therefore unsent the dispatch and then
    showed nothing at all, in production, for as long as the feature existed.
    """

    LIMIT = 64

    def _drivers(self):
        return [{"id": D1, "driver_name": "Susan", "is_active": True},
                {"id": D2, "driver_name": "Marcus", "is_active": True}]

    def _check(self, kb):
        buttons = [b for row in kb.inline_keyboard for b in row]
        self.assertTrue(buttons, "no buttons to measure")
        for b in buttons:
            n = len((b.callback_data or "").encode("utf-8"))
            self.assertLessEqual(n, self.LIMIT,
                                 f"{b.callback_data!r} is {n} bytes; Telegram takes {self.LIMIT}")
        return buttons

    def test_the_skip_dispatch_picker(self):
        with mock.patch.object(bot, "_get_all_drivers_cached", self._drivers):
            self._check(bot._skip_dispatch_driver_keyboard(LEAD_ID))

    def test_the_release_picker(self):
        with mock.patch.object(bot, "_get_all_drivers_cached", self._drivers):
            self._check(bot._skip_dispatch_release_keyboard(LEAD_ID, [D1, D2]))

    def test_what_the_buttons_say_survives_the_round_trip(self):
        with mock.patch.object(bot, "_get_all_drivers_cached", self._drivers):
            for kb, prefix in (
                    (bot._skip_dispatch_driver_keyboard(LEAD_ID),
                     bot.SKIP_DISPATCH_DRIVER_CB),
                    (bot._skip_dispatch_release_keyboard(LEAD_ID, [D1, D2]),
                     bot.SKIP_DISPATCH_RELEASE_CB)):
                got = [bot._skip_dispatch_ids(b.callback_data, prefix)
                       for b in self._check(kb)]
                self.assertEqual([(LEAD_ID, D1), (LEAD_ID, D2)], got, prefix)

    def test_a_non_uuid_id_is_dropped_rather_than_sent_oversized(self):
        with mock.patch.object(bot, "_get_all_drivers_cached",
                               lambda: [{"id": "not-a-uuid", "driver_name": "X",
                                         "is_active": True}]):
            self.assertFalse(bot._skip_dispatch_driver_keyboard(LEAD_ID).inline_keyboard)

    def test_garbage_callback_data_decodes_to_nothing(self):
        for data in ("", "skiprel_", "skiprel_zzz", "skiprel_" + "A" * 44):
            lead_id, driver_id = bot._skip_dispatch_ids(data, bot.SKIP_DISPATCH_RELEASE_CB)
            self.assertNotEqual((LEAD_ID, D1), (lead_id, driver_id), data)


class TheSupervisorListParsesSpacesTest(unittest.TestCase):
    """A space-separated SUPERVISORY_TELEGRAM_ID used to collapse into one
    unparseable token — an empty supervisor set, which now means nobody at all
    can release a tag. .env.example has always documented "comma or space"."""

    def test_spaces_commas_and_both(self):
        for raw in ("111 222", "111,222", "111, 222", "111;222", " 111\t222 "):
            self.assertEqual(["111", "222"], bot._raw_supervisory_tokens(raw), raw)

    def test_a_single_id_is_unchanged(self):
        self.assertEqual(["900500"], bot._raw_supervisory_tokens("900500"))
        self.assertEqual([], bot._raw_supervisory_tokens("", None))


if __name__ == "__main__":
    unittest.main()
