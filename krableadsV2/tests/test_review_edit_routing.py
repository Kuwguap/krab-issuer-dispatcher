"""Offline routing tests for single-line review edits (typed or voice-transcribed).

Locks in the 2026-08 fix for "edits do nothing": text listeners in every
conversation state, the restart re-entry (now also for "special_request_drivers"),
the mid-dispatch wipe guard, the one-shot storage-miss guard, and the group -2
ghost-state safety net. All network seams (db, AI, Telegram sends) are mocked —
these tests drive the REAL routing functions in bot.py.

Run:  venv\\Scripts\\python.exe -m pytest tests/test_review_edit_routing.py -q
"""
import os
import sys
import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# bot.py runs `db = Database()` at import; give create_client dummy-but-valid strings and
# mock the Database class so no network I/O happens (same seam as the sibling test file).
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy-key")

import utils.database as udb  # noqa: E402
udb.Database = mock.MagicMock()
import bot  # noqa: E402


def _phase1_row(**overrides):
    data = {
        "name": "-", "address": "-", "city_state_zip": "-", "delivery_address": "-",
        "delivery_city_state_zip": "-", "vin": "-", "car": "-", "color": "-",
        "insurance_company": "-", "insurance_policy_number": "-", "extra_info": "-",
        "pending_phone_number": "", "pending_price": "", "email": "",
        "driver_license_id": "", "special_request_issuers": "", "special_request_drivers": "",
    }
    data.update(overrides.pop("data", {}))
    row = {"state": "phase1", "data": data}
    row.update(overrides)
    return row


def _mk_update(text, chat_id=111, user_id=222):
    msg = SimpleNamespace(
        text=text,
        chat_id=chat_id,
        chat=SimpleNamespace(type="private", id=chat_id),
        photo=None,
        document=None,
        voice=None,
        audio=None,
        delete=mock.AsyncMock(),
        reply_text=mock.AsyncMock(return_value=SimpleNamespace(message_id=999, chat_id=chat_id)),
    )
    return SimpleNamespace(
        message=msg,
        effective_message=msg,
        effective_user=SimpleNamespace(id=user_id, username="tester"),
        effective_chat=SimpleNamespace(type="private", id=chat_id),
    )


def _mk_context(user_data=None):
    return SimpleNamespace(
        user_data=user_data if user_data is not None else {},
        bot=mock.AsyncMock(),
        application=SimpleNamespace(handlers={}),
        args=None,
    )


def _quiet_patches(db):
    """Patch every outward side effect around the routing under test."""
    return [
        mock.patch.object(bot, "db", db),
        mock.patch.object(bot, "_send_vanishing", mock.AsyncMock()),
        mock.patch.object(bot, "_update_review_message_text", mock.AsyncMock()),
        mock.patch.object(bot, "_cleanup_voice_echo", mock.AsyncMock()),
        mock.patch.object(bot, "_autoclean_user_msg", mock.AsyncMock()),
        mock.patch.object(bot.Config, "is_ai_vision_configured", classmethod(lambda cls: False)),
    ]


class ReviewMessageEditTest(unittest.TestCase):
    """handle_phase1_review_message applies labeled single-line edits."""

    def _run_edit(self, text, user_data=None):
        db = mock.MagicMock()
        db.get_user_state.return_value = _phase1_row()
        update = _mk_update(text)
        context = _mk_context(user_data if user_data is not None else {"review_message_id": 5, "review_chat_id": 111})
        patches = _quiet_patches(db)
        for p in patches:
            p.start()
        try:
            result = asyncio.run(bot.handle_phase1_review_message(update, context))
        finally:
            for p in patches:
                p.stop()
        return result, db, context

    def test_name_edit_applies(self):
        result, db, _ = self._run_edit("name John Damian")
        self.assertEqual(result, bot.STATE_AI_REVIEW)
        args = db.set_user_state.call_args
        self.assertEqual(args.args[1], "phase1")
        self.assertEqual(args.args[2].get("name"), "John Damian")

    def test_price_edit_applies(self):
        result, db, _ = self._run_edit("price 150")
        self.assertEqual(result, bot.STATE_AI_REVIEW)
        args = db.set_user_state.call_args
        self.assertEqual(args.args[2].get("pending_price"), "$150")

    def test_storage_miss_warns_once_then_data_lost(self):
        db = mock.MagicMock()
        db.get_user_state.return_value = None
        update = _mk_update("price 150")
        context = _mk_context({"review_message_id": 5, "review_chat_id": 111})
        patches = _quiet_patches(db)
        for p in patches:
            p.start()
        try:
            first = asyncio.run(bot.handle_phase1_review_message(update, context))
            self.assertEqual(first, bot.STATE_AI_REVIEW)          # warned, stayed
            self.assertTrue(context.user_data.get("state_miss_once"))
            second = asyncio.run(bot.handle_phase1_review_message(update, context))
            self.assertEqual(second, bot.ConversationHandler.END)  # gave up honestly
            self.assertNotIn("review_message_id", context.user_data)
        finally:
            for p in patches:
                p.stop()


class IdleReentryTest(unittest.TestCase):
    """handle_idle_lead_start: restore the card instead of wiping the lead."""

    def _run_idle(self, text, db_row, user_data=None):
        db = mock.MagicMock()
        db.get_user_state.return_value = db_row
        update = _mk_update(text)
        context = _mk_context(user_data if user_data is not None else {})
        patches = _quiet_patches(db) + [
            mock.patch.object(bot, "_repost_review_card", mock.AsyncMock()),
            mock.patch.object(bot, "handle_phase1_review_message",
                              mock.AsyncMock(return_value=bot.STATE_AI_REVIEW)),
            mock.patch.object(bot, "_user_is_global_supervisor", mock.MagicMock(return_value=False)),
            mock.patch.object(bot, "_begin_lead_flow", mock.AsyncMock()),
            mock.patch.object(bot, "handle_phase1", mock.AsyncMock(return_value=bot.STATE_PHASE1)),
        ]
        for p in patches:
            p.start()
        try:
            result = asyncio.run(bot.handle_idle_lead_start(update, context))
        finally:
            for p in patches:
                p.stop()
        return result, update, context

    def test_phase1_row_reenters_review(self):
        result, _, _ = self._run_idle("name John Damian", _phase1_row())
        self.assertEqual(result, bot.STATE_AI_REVIEW)

    def test_phase1_row_calls_repost_and_review_handler(self):
        db = mock.MagicMock()
        db.get_user_state.return_value = _phase1_row()
        update = _mk_update("name John Damian")
        context = _mk_context({})
        repost = mock.AsyncMock()
        review = mock.AsyncMock(return_value=bot.STATE_AI_REVIEW)
        begin = mock.AsyncMock()
        patches = _quiet_patches(db) + [
            mock.patch.object(bot, "_repost_review_card", repost),
            mock.patch.object(bot, "handle_phase1_review_message", review),
            mock.patch.object(bot, "_user_is_global_supervisor", mock.MagicMock(return_value=False)),
            mock.patch.object(bot, "_begin_lead_flow", begin),
        ]
        for p in patches:
            p.start()
        try:
            result = asyncio.run(bot.handle_idle_lead_start(update, context))
        finally:
            for p in patches:
                p.stop()
        self.assertEqual(result, bot.STATE_AI_REVIEW)
        repost.assert_awaited_once()
        review.assert_awaited_once()
        begin.assert_not_awaited()
        db.clear_user_state.assert_not_called()

    def test_special_request_row_also_reenters(self):
        db = mock.MagicMock()
        db.get_user_state.return_value = _phase1_row(state="special_request_drivers")
        update = _mk_update("price 150")
        context = _mk_context({})
        repost = mock.AsyncMock()
        review = mock.AsyncMock(return_value=bot.STATE_AI_REVIEW)
        begin = mock.AsyncMock()
        patches = _quiet_patches(db) + [
            mock.patch.object(bot, "_repost_review_card", repost),
            mock.patch.object(bot, "handle_phase1_review_message", review),
            mock.patch.object(bot, "_user_is_global_supervisor", mock.MagicMock(return_value=False)),
            mock.patch.object(bot, "_begin_lead_flow", begin),
        ]
        for p in patches:
            p.start()
        try:
            result = asyncio.run(bot.handle_idle_lead_start(update, context))
        finally:
            for p in patches:
                p.stop()
        self.assertEqual(result, bot.STATE_AI_REVIEW)
        repost.assert_awaited_once()
        begin.assert_not_awaited()
        db.clear_user_state.assert_not_called()

    def test_mid_dispatch_row_is_protected_not_wiped(self):
        result, update, _ = self._run_idle("name John Damian", _phase1_row(state="select_driver"))
        self.assertIsNone(result)
        update.effective_message.reply_text.assert_awaited()  # guidance message
        # the guidance text mentions the buttons
        text_sent = update.effective_message.reply_text.await_args.args[0]
        self.assertIn("button", text_sent.lower())

    def test_await_group_accept_still_starts_next_lead(self):
        db = mock.MagicMock()
        db.get_user_state.return_value = _phase1_row(state="await_group_accept")
        update = _mk_update("2019 Honda Accord for Mary Smith")
        context = _mk_context({})
        begin = mock.AsyncMock()
        patches = _quiet_patches(db) + [
            mock.patch.object(bot, "_user_is_global_supervisor", mock.MagicMock(return_value=False)),
            mock.patch.object(bot, "_begin_lead_flow", begin),
            mock.patch.object(bot, "handle_phase1", mock.AsyncMock(return_value=bot.STATE_PHASE1)),
        ]
        for p in patches:
            p.start()
        try:
            result = asyncio.run(bot.handle_idle_lead_start(update, context))
        finally:
            for p in patches:
                p.stop()
        self.assertEqual(result, bot.STATE_PHASE1)
        begin.assert_awaited_once()  # next lead intentionally starts

    def test_storage_miss_one_shot_then_normal_routing(self):
        db = mock.MagicMock()
        db.get_user_state.return_value = None
        update = _mk_update("name John Damian")
        context = _mk_context({"review_message_id": 5, "review_chat_id": 111})
        begin = mock.AsyncMock()
        patches = _quiet_patches(db) + [
            mock.patch.object(bot, "_user_is_global_supervisor", mock.MagicMock(return_value=False)),
            mock.patch.object(bot, "_begin_lead_flow", begin),
            mock.patch.object(bot, "handle_phase1", mock.AsyncMock(return_value=bot.STATE_PHASE1)),
        ]
        for p in patches:
            p.start()
        try:
            first = asyncio.run(bot.handle_idle_lead_start(update, context))
            self.assertIsNone(first)                      # warned, nothing wiped
            begin.assert_not_awaited()
            self.assertTrue(context.user_data.get("state_miss_once"))
            second = asyncio.run(bot.handle_idle_lead_start(update, context))
        finally:
            for p in patches:
                p.stop()
        self.assertNotIn("review_message_id", context.user_data)  # stale ids dropped
        begin.assert_awaited_once()                                # routed normally
        self.assertEqual(second, bot.STATE_PHASE1)


class SelectStateTextTest(unittest.TestCase):
    """handle_select_state_text: review edit on a live lead, nudge otherwise."""

    def test_live_phase1_routes_to_review_editor(self):
        db = mock.MagicMock()
        db.get_user_state.return_value = _phase1_row()
        update = _mk_update("price 150")
        context = _mk_context({})
        review = mock.AsyncMock(return_value=bot.STATE_AI_REVIEW)
        patches = [
            mock.patch.object(bot, "db", db),
            mock.patch.object(bot, "handle_phase1_review_message", review),
            mock.patch.object(bot, "_send_vanishing", mock.AsyncMock()),
        ]
        for p in patches:
            p.start()
        try:
            result = asyncio.run(bot.handle_select_state_text(update, context))
        finally:
            for p in patches:
                p.stop()
        self.assertEqual(result, bot.STATE_AI_REVIEW)
        review.assert_awaited_once()

    def test_dispatch_pick_treats_text_as_a_driver_name(self):
        """Text during a driver pick is a NAME now, not a stray message. An
        unmatched one keeps the picker open and says so — see
        tests/test_reassign_by_name.py for the matching cases."""
        db = mock.MagicMock()
        db.get_user_state.return_value = _phase1_row(state="select_driver")
        update = _mk_update("some text that matches no driver")
        context = _mk_context({})
        review = mock.AsyncMock()
        resend = mock.AsyncMock()
        patches = [
            mock.patch.object(bot, "db", db),
            mock.patch.object(bot, "handle_phase1_review_message", review),
            mock.patch.object(bot, "_handle_resend_to_drivers", resend),
            mock.patch.object(bot, "_get_all_drivers_cached",
                              mock.MagicMock(return_value=[
                                  {"id": "d1", "driver_name": "Kita", "is_active": True}])),
            mock.patch.object(bot, "_get_suspended_driver_ids",
                              mock.MagicMock(return_value=set())),
            mock.patch.object(bot, "_send_vanishing", mock.AsyncMock()),
        ]
        for p in patches:
            p.start()
        try:
            result = asyncio.run(bot.handle_select_state_text(update, context))
        finally:
            for p in patches:
                p.stop()
        self.assertIsNone(result)          # None keeps the picker state in PTB
        review.assert_not_awaited()        # it is not a review edit
        resend.assert_not_awaited()        # and nothing is reassigned on a miss
        said = update.message.reply_text.await_args.args[0]
        self.assertIn("No driver matched", said)


class GhostStateSafetyNetTest(unittest.TestCase):
    """handle_review_edit_anywhere: fires only for ghost states + live lead."""

    class _FakeConv:
        def __init__(self, state):
            self._conversations = {("k",): state}
        def _get_key(self, update):
            return ("k",)

    def _run_net(self, conv_state, db_row):
        db = mock.MagicMock()
        db.get_user_state.return_value = db_row
        update = _mk_update("name John Damian")
        context = _mk_context({})
        fake = self._FakeConv(conv_state)
        review = mock.AsyncMock(return_value=bot.STATE_AI_REVIEW)
        patches = [
            mock.patch.object(bot, "db", db),
            mock.patch.object(bot, "_MAIN_CONV_HANDLER", fake),
            mock.patch.object(bot, "handle_phase1_review_message", review),
        ]
        for p in patches:
            p.start()
        try:
            raised = False
            try:
                asyncio.run(bot.handle_review_edit_anywhere(update, context))
            except bot.ApplicationHandlerStop:
                raised = True
        finally:
            for p in patches:
                p.stop()
        return raised, review, fake

    def test_ghost_state_rescues_edit_and_repairs_state(self):
        raised, review, fake = self._run_net(999, _phase1_row())  # 999 = unknown state
        self.assertTrue(raised)                       # stops later groups
        review.assert_awaited_once()
        self.assertEqual(fake._conversations[("k",)], bot.STATE_AI_REVIEW)

    def test_known_text_state_stands_down(self):
        raised, review, _ = self._run_net(bot.STATE_AI_REVIEW, _phase1_row())
        self.assertFalse(raised)
        review.assert_not_awaited()

    def test_idle_stands_down(self):
        raised, review, fake = self._run_net(None, _phase1_row())
        fake._conversations[("k",)] = None
        self.assertFalse(raised)
        review.assert_not_awaited()

    def test_no_live_lead_stands_down(self):
        raised, review, _ = self._run_net(999, None)
        self.assertFalse(raised)
        review.assert_not_awaited()


class VoiceTranscribeRobustnessTest(unittest.TestCase):
    """_transcribe_update_voice survives a failed 'Transcribing…' status send."""

    def test_status_send_failure_still_returns_transcript(self):
        update = _mk_update(None)
        update.effective_message.voice = SimpleNamespace(file_id="f1", file_name=None, mime_type="audio/ogg")
        update.effective_message.reply_text = mock.AsyncMock(side_effect=RuntimeError("flood control"))
        context = _mk_context({})
        f = mock.AsyncMock()
        f.download_to_memory = mock.AsyncMock()
        context.bot.get_file = mock.AsyncMock(return_value=f)
        patches = [
            mock.patch.object(bot.ai_vision, "transcribe_voice", mock.MagicMock(return_value="name John Damian")),
            mock.patch.object(bot, "_safe_delete_chat_message", mock.AsyncMock()),
        ]
        for p in patches:
            p.start()
        try:
            transcript = asyncio.run(bot._transcribe_update_voice(update, context))
        finally:
            for p in patches:
                p.stop()
        self.assertEqual(transcript, "name John Damian")


if __name__ == "__main__":
    unittest.main()
