"""Names on the board, names on the posts, and the anti-gaming browser.

Three operator asks in one change:
  * Skip Dispatch (and every supervisory line) shows the NAME of who entered
    the lead — "just the name", never the @handle;
  * /leaderboard counts and ranks BY NAME — "🥇 JB — 30 clients";
  * /settings → 🧾 Recent Leads: newest 10 with paging, showing name,
    @username, reference and client — with a one-tap strike that pulls a fake
    lead out of every count (exclude_from_count), reversibly.

Run:  venv\\Scripts\\python.exe -m pytest tests/test_leaderboard_names_and_recent_leads.py -q
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
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11")
os.environ.pop("OPENAI_API_KEY", None)

import utils.database as udb  # noqa: E402

if "bot" not in sys.modules:
    udb.Database = mock.MagicMock()
import bot  # noqa: E402

LEAD_UUID = "44444444-4444-4444-8444-444444444444"


def _real_database_class():
    """The REAL Database class, however many suites have already swapped
    utils.database.Database for a MagicMock in this process: load a private
    second instance of the module straight from its file."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "udb_real_for_leaderboard_test", str(ROOT / "utils" / "database.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Database


def _db_with_rows(rows):
    cls = _real_database_class()
    d = cls.__new__(cls)
    d._check_tables_exist = lambda: True
    chain = mock.MagicMock()
    chain.table.return_value.select.return_value.execute.return_value = \
        SimpleNamespace(data=rows)
    d.client = chain
    return d


class TheBoardCountsByNameTest(unittest.TestCase):

    def test_names_win_handles_fall_back_and_struck_rows_vanish(self):
        d = _db_with_rows([
            {"telegram_name": "JB", "telegram_username": "jbhandle", "user_id": 1},
            {"telegram_name": "JB", "telegram_username": "otherhandle", "user_id": 2},
            {"telegram_name": "", "telegram_username": "sensei", "user_id": 3},
            {"telegram_name": None, "telegram_username": "sensei", "user_id": 3},
            {"telegram_name": "Faker", "telegram_username": "f", "user_id": 4,
             "exclude_from_count": True},
        ])
        rows = d.get_lead_counts_by_sender()
        self.assertEqual(rows[0], ("JB", 2))
        self.assertEqual(rows[1], ("sensei", 2))
        self.assertTrue(all(name != "Faker" for name, _ in rows))

    def test_the_command_prints_medals_and_clients(self):
        msg = mock.MagicMock()
        msg.reply_text = mock.AsyncMock()
        update = mock.MagicMock()
        update.effective_message = msg
        with mock.patch.object(bot.db, "get_lead_counts_by_sender",
                               mock.MagicMock(return_value=[("JB", 30), ("Sensei", 28)])):
            asyncio.run(bot.cmd_leaderboard(update, mock.MagicMock()))
        said = msg.reply_text.await_args[0][0]
        self.assertIn("🥇 JB — *30* clients", said)
        self.assertIn("🥈 Sensei — *28* clients", said)


class TheNameNotTheHandleTest(unittest.TestCase):

    def test_issuer_html_prefers_the_name(self):
        out = bot._issuer_display_html_from_lead(
            {"telegram_name": "JB", "telegram_username": "jbhandle", "user_id": 5})
        self.assertIn("JB", out)
        self.assertNotIn("@jbhandle", out)

    def test_old_rows_keep_their_handle(self):
        out = bot._issuer_display_html_from_lead(
            {"telegram_name": "", "telegram_username": "jbhandle", "user_id": 5})
        self.assertIn("@jbhandle", out)

    def test_supervisory_line_prefers_the_name_too(self):
        self.assertEqual(bot._lead_issuer_display_from_lead(
            {"telegram_name": "Sensei", "telegram_username": "s"}), "Sensei")
        self.assertEqual(bot._lead_issuer_display_from_lead(
            {"telegram_username": "s"}), "@s")

    def test_a_bot_never_becomes_the_entrant(self):
        self.assertIsNone(bot._user_display_name(
            SimpleNamespace(is_bot=True, full_name="Krab Issuer")))
        self.assertEqual(bot._user_display_name(
            SimpleNamespace(is_bot=False, full_name="  JB  ")), "JB")
        self.assertIsNone(bot._user_display_name(None))

    def test_every_create_site_stores_the_name(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(src.count('"telegram_name":'), 5)


def _recent_rows(n, struck_idx=None, total_named=True):
    rows = []
    for i in range(n):
        rows.append({
            "id": LEAD_UUID[:-2] + f"{i:02d}",
            "reference_id": f"REF{i:05d}",
            "telegram_name": ("JB" if total_named else "") if i % 2 == 0 else "Sensei",
            "telegram_username": "jbhandle" if i % 2 == 0 else "",
            "user_id": 100 + i,
            "vehicle_details": "CHARLES JONES\n-\n-\n-\n-\n-\n-\n-\n-\n-\n-",
            "exclude_from_count": (i == struck_idx),
        })
    return rows


class TheRecentLeadsBrowserTest(unittest.TestCase):

    def _render(self, rows, total, page=0):
        with mock.patch.object(bot.db, "list_recent_leads_for_review",
                               mock.MagicMock(return_value=(rows, total))):
            return asyncio.run(bot._recent_leads_text_kb(page))

    def test_the_page_shows_ref_client_name_and_handle(self):
        text, kb = self._render(_recent_rows(2), 2)
        self.assertIn("REF00000", text)
        self.assertIn("CHARLES JONES", text)
        self.assertIn("JB (@jbhandle)", text)
        self.assertIn("Sensei", text)

    def test_struck_rows_are_marked_and_get_a_restore_button(self):
        text, kb = self._render(_recent_rows(2, struck_idx=0), 2)
        self.assertIn("🚫 struck", text)
        flat = [b for row in kb.inline_keyboard for b in row]
        self.assertTrue(any(b.callback_data.startswith("rlv_un_0_") for b in flat))
        self.assertTrue(any(b.callback_data.startswith("rlv_rm_0_") for b in flat))

    def test_pagination_buttons_come_and_go(self):
        _, kb = self._render(_recent_rows(10), 25, page=1)
        labels = [b.text for row in kb.inline_keyboard for b in row]
        self.assertIn("⬅️ Prev", labels)
        self.assertIn("➡️ Next", labels)
        _, kb0 = self._render(_recent_rows(10), 10, page=0)
        labels0 = [b.text for row in kb0.inline_keyboard for b in row]
        self.assertNotIn("⬅️ Prev", labels0)
        self.assertNotIn("➡️ Next", labels0)

    def _tap(self, data, supervisor=True):
        query = SimpleNamespace(data=data, edit_message_text=mock.AsyncMock(),
                                message=mock.MagicMock())
        update = mock.MagicMock()
        update.callback_query = query
        update.effective_user.id = 7
        calls = {}
        fake_db = mock.MagicMock()
        fake_db.list_recent_leads_for_review.return_value = (_recent_rows(1), 1)
        fake_db.set_lead_excluded.side_effect = \
            lambda lid, ex: calls.setdefault("strike", (lid, ex)) or True
        with mock.patch.object(bot, "db", fake_db), \
                mock.patch.object(bot, "_user_is_global_supervisor",
                                  lambda uid: supervisor), \
                mock.patch.object(bot, "_safe_answer_callback_query",
                                  mock.AsyncMock()) as ans:
            asyncio.run(bot.handle_recent_leads_cb(update, mock.MagicMock()))
        return query, calls, ans

    def test_a_strike_sets_the_flag_and_rerenders(self):
        query, calls, _ = self._tap(f"rlv_rm_0_{LEAD_UUID}")
        self.assertEqual(calls.get("strike"), (LEAD_UUID, True))
        query.edit_message_text.assert_awaited()

    def test_a_restore_clears_the_flag(self):
        _, calls, _ = self._tap(f"rlv_un_0_{LEAD_UUID}")
        self.assertEqual(calls.get("strike"), (LEAD_UUID, False))

    def test_non_supervisors_are_refused(self):
        query, calls, ans = self._tap(f"rlv_rm_0_{LEAD_UUID}", supervisor=False)
        self.assertIsNone(calls.get("strike"))
        query.edit_message_text.assert_not_awaited()
        self.assertTrue(any("Supervisors only" in str(c) for c in ans.await_args_list))


class TheWiringHoldsTest(unittest.TestCase):

    def test_the_browser_buttons_reach_a_registered_handler(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        self.assertIn('pattern=r"^rlv_"', src)
        self.assertIn('"tset_recent"', src)
        self.assertIn('if data == "tset_recent":', src)

    def test_the_migration_exists_and_names_the_column(self):
        sql = (ROOT / "database" / "migration_telegram_name.sql").read_text(encoding="utf-8")
        self.assertIn("telegram_name", sql)

    def test_the_column_is_write_tolerant(self):
        import importlib
        real = importlib.import_module("utils.database")
        self.assertIn("telegram_name", real._OPTIONAL_LEADS_WRITE_KEYS)


if __name__ == "__main__":
    unittest.main()
