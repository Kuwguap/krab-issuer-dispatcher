"""/settings: manage dispatchers, drivers, suspensions and client sources.

Run:  venv\\Scripts\\python.exe -m pytest tests/test_settings_admin.py -q
"""
import asyncio
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

import utils.database as udb  # noqa: E402

if "bot" not in sys.modules:
    udb.Database = mock.MagicMock()
import bot  # noqa: E402

SUP = 999


def _query(data):
    return SimpleNamespace(
        data=data,
        answer=mock.AsyncMock(),
        edit_message_text=mock.AsyncMock(),
        from_user=SimpleNamespace(id=SUP),
        message=SimpleNamespace(chat_id=SUP, reply_text=mock.AsyncMock()),
    )


def _cb_update(data):
    return SimpleNamespace(callback_query=_query(data), effective_user=SimpleNamespace(id=SUP))


def _txt_update(text):
    return SimpleNamespace(
        message=SimpleNamespace(text=text, chat_id=SUP, reply_text=mock.AsyncMock()),
        effective_user=SimpleNamespace(id=SUP),
        effective_chat=SimpleNamespace(id=SUP, type="private"),
    )


def _ctx():
    return SimpleNamespace(user_data={}, bot=mock.AsyncMock(),
                           application=SimpleNamespace(handlers={}))


def _run(update, ctx, fn, fake_db):
    with mock.patch.object(bot, "db", fake_db), \
            mock.patch.object(bot, "_user_is_global_supervisor", mock.MagicMock(return_value=True)), \
            mock.patch.object(bot, "_get_all_drivers_cached",
                              mock.MagicMock(return_value=fake_db.get_all_drivers())):
        return asyncio.run(fn(update, ctx))


def _fake_db():
    d = mock.MagicMock()
    d.get_all_groups.return_value = [
        {"id": "g1", "group_name": "HighKage", "is_active": True, "group_telegram_id": "-100"},
        {"id": "g2", "group_name": "NullState", "is_active": None, "group_telegram_id": "-200"},
    ]
    d.get_all_drivers.return_value = [
        {"id": "d1", "driver_name": "Kita", "is_active": True},
        {"id": "d2", "driver_name": "Sam", "is_active": False},
    ]
    d.get_all_contact_info_sources.return_value = [
        {"id": "s1", "label": "Facebook", "is_active": True},
        {"id": "s2", "label": "Old Source", "is_active": False},
    ]
    d.get_manually_suspended_driver_ids.return_value = set()
    d.get_driver_ids_with_pending_receipt_count_at_least.return_value = set()
    d.get_driver_pending_receipts.return_value = []
    d.waive_driver_pending_receipts.return_value = 3
    d.set_driver_suspended.return_value = True
    d.create_driver.return_value = True
    d.create_contact_info_source.return_value = True
    return d


class MenuTest(unittest.TestCase):
    def test_all_four_capabilities_are_on_the_menu(self):
        datas = [b.callback_data for row in bot._settings_main_kb().inline_keyboard for b in row]
        for want in ("tset_groups", "tset_drivers", "tset_susp", "tset_srcs"):
            self.assertIn(want, datas)

    def test_groups_are_called_dispatchers(self):
        labels = [b.text for row in bot._settings_main_kb().inline_keyboard for b in row]
        joined = " ".join(labels)
        self.assertIn("Dispatchers", joined)
        self.assertNotIn("Groups", joined)


class RenderTest(unittest.TestCase):
    def _render(self, fn):
        db = _fake_db()
        q = _query("x")
        with mock.patch.object(bot, "db", db), \
                mock.patch.object(bot, "_get_all_drivers_cached",
                                  mock.MagicMock(return_value=db.get_all_drivers())), \
                mock.patch.object(bot, "_get_suspended_driver_ids",
                                  mock.MagicMock(return_value={"d1"})):
            asyncio.run(fn(q))
        return q.edit_message_text.await_args

    def test_dispatchers_null_is_active_shows_as_active(self):
        """A JSON-null is_active means ACTIVE to the dispatch path."""
        text = self._render(bot._settings_render_groups).args[0]
        self.assertIn("✅ NullState", text)

    def test_drivers_list_offers_add_and_toggle(self):
        call = self._render(bot._settings_render_drivers)
        datas = [b.callback_data for row in call.kwargs["reply_markup"].inline_keyboard for b in row]
        self.assertIn("tset_dadd", datas)
        self.assertTrue(any(d.startswith("tset_dtog:") for d in datas))

    def test_suspensions_show_reason_and_offer_lift(self):
        call = self._render(bot._settings_render_suspensions)
        text = call.args[0]
        self.assertIn("unpaid receipts", text)      # d1 is suspended by debt
        datas = [b.callback_data for row in call.kwargs["reply_markup"].inline_keyboard for b in row]
        self.assertIn("tset_susplift:d1", datas)    # suspended -> Lift
        self.assertIn("tset_suspon:d2", datas)      # not suspended -> Suspend

    def test_sources_can_be_restored_after_removal(self):
        call = self._render(bot._settings_render_sources)
        datas = [b.callback_data for row in call.kwargs["reply_markup"].inline_keyboard for b in row]
        self.assertIn("tset_stog:s1:0", datas)      # active -> Remove
        self.assertIn("tset_stog:s2:1", datas)      # disabled -> Restore (recoverable)
        self.assertIn("tset_sadd", datas)


class ActionTest(unittest.TestCase):
    def test_lift_clears_the_flag_and_excuses_the_debt(self):
        db = _fake_db()
        ctx = _ctx()
        with mock.patch.object(bot, "_notify_suspension_lifted", mock.AsyncMock()), \
                mock.patch.object(bot, "_driver_row_by_id",
                                  mock.MagicMock(return_value={"id": "d1", "driver_name": "Kita"})), \
                mock.patch.object(bot, "_get_suspended_driver_ids", mock.MagicMock(return_value=set())):
            _run(_cb_update("tset_susplift:d1"), ctx, bot.handle_settings_cb, db)
        db.set_driver_suspended.assert_called_once_with("d1", False)
        db.waive_driver_pending_receipts.assert_called_once_with("d1")

    def test_suspend_without_the_migration_says_so(self):
        db = _fake_db()
        db.set_driver_suspended.return_value = False     # column not added yet
        upd = _cb_update("tset_suspon:d2")
        with mock.patch.object(bot, "_get_suspended_driver_ids", mock.MagicMock(return_value=set())):
            _run(upd, _ctx(), bot.handle_settings_cb, db)
        said = upd.callback_query.message.reply_text.await_args.args[0]
        self.assertIn("migration_driver_manual_suspend.sql", said)

    def test_add_driver_validates_the_telegram_id(self):
        db = _fake_db()
        ctx = _ctx()
        ctx.user_data["tset_await"] = {"kind": "add_driver"}
        upd = _txt_update("Kita | not-a-number")
        state = _run(upd, ctx, bot.apply_settings_input, db)
        db.create_driver.assert_not_called()
        self.assertEqual(state, bot.SET_INPUT, "a typo must not drop out of /settings")
        self.assertEqual(ctx.user_data.get("tset_await"), {"kind": "add_driver"})

    def test_add_driver_succeeds(self):
        db = _fake_db()
        ctx = _ctx()
        ctx.user_data["tset_await"] = {"kind": "add_driver"}
        state = _run(_txt_update("Kita | 12345678 | 555-123-4567"), ctx, bot.apply_settings_input, db)
        db.create_driver.assert_called_once_with("Kita", "12345678", "555-123-4567")
        self.assertEqual(state, bot.SET_MENU)

    def test_add_source_succeeds(self):
        db = _fake_db()
        ctx = _ctx()
        ctx.user_data["tset_await"] = {"kind": "add_source"}
        state = _run(_txt_update("Instagram"), ctx, bot.apply_settings_input, db)
        db.create_contact_info_source.assert_called_once_with("Instagram")
        self.assertEqual(state, bot.SET_MENU)

    def test_bad_plate_value_keeps_the_prompt_open(self):
        db = _fake_db()
        ctx = _ctx()
        ctx.user_data["tset_await"] = {"kind": "plate", "field": "nj_plate_next_number"}
        state = _run(_txt_update("abc"), ctx, bot.apply_settings_input, db)
        self.assertEqual(state, bot.SET_INPUT)


class ManualSuspensionIsEnforcedTest(unittest.TestCase):
    """A hand-suspended driver must actually stop receiving leads."""

    def test_manual_ids_join_the_suspended_set(self):
        db = _fake_db()
        db.get_driver_ids_with_pending_receipt_count_at_least.return_value = {"d9"}
        db.get_manually_suspended_driver_ids.return_value = {"d2"}
        with mock.patch.object(bot, "db", db):
            bot._bust_driver_caches()
            got = bot._get_suspended_driver_ids()
            bot._bust_driver_caches()
        self.assertEqual(got, {"d9", "d2"})

    def test_missing_column_does_not_break_suspension(self):
        db = _fake_db()
        db.get_driver_ids_with_pending_receipt_count_at_least.return_value = {"d9"}
        db.get_manually_suspended_driver_ids.side_effect = RuntimeError("column missing")
        with mock.patch.object(bot, "db", db):
            bot._bust_driver_caches()
            got = bot._get_suspended_driver_ids()
            bot._bust_driver_caches()
        self.assertEqual(got, {"d9"}, "receipt-debt suspension must survive un-migrated DBs")


if __name__ == "__main__":
    unittest.main()
