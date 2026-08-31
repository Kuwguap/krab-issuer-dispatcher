r"""A lead is never entered by the bot.

Reported: "sometimes the person that creates lead isn't properly checked and
saved as KrabDispatchBot".

207 leads on the live database were credited to the bot between 2026-05-17 and
2026-08-27 — every one of them somebody's real work. Only the display fields
were wrong: user_id was correct on all 207, which is what made them repairable
(database/repair_bot_credited_leads.py).

The paths that resolved the entrant from the bot's own message were fixed on
2026-08-28. This covers the write itself, so no path can do it again.

Run:  venv\Scripts\python.exe -m pytest tests/test_lead_credit.py -q
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy-key")

BOT_SRC = (ROOT / "bot.py").read_text(encoding="utf-8")


def _Database():
    """A private copy: other suites replace utils.database.Database with a mock
    and never put it back, and these tests would then assert on nothing."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_udb_private_for_credit_tests", ROOT / "utils" / "database.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Database


class TheBotIsNeverTheEntrantTest(unittest.TestCase):

    def setUp(self):
        self.Database = _Database()

    def _scrub(self, payload):
        out = dict(payload)
        self.Database._scrub_bot_entrant(out)
        return out

    def test_the_bots_handle_is_refused(self):
        for handle in ("KrabDispatchBot", "krabdispatchbot", "@KrabDispatchBot",
                       "KrabIssuerBot"):
            out = self._scrub({"telegram_username": handle, "user_id": 5994570412})
            self.assertEqual("Unknown", out["telegram_username"], handle)

    def test_the_real_person_is_left_alone(self):
        for handle in ("highkage0_0", "KINGKRAB", "sensei_vi", "NJGIRLY"):
            out = self._scrub({"telegram_username": handle})
            self.assertEqual(handle, out["telegram_username"], handle)

    def test_a_person_whose_name_merely_ends_in_bot_is_left_alone(self):
        out = self._scrub({"telegram_username": "robotgirl", "telegram_name": "Bot Marley"})
        self.assertEqual("robotgirl", out["telegram_username"])
        self.assertEqual("Bot Marley", out["telegram_name"])

    def test_the_user_id_is_never_touched(self):
        """It is the only field that stayed correct, and the only thing that
        made 198 of the 207 repairable."""
        out = self._scrub({"telegram_username": "KrabDispatchBot", "user_id": 5994570412})
        self.assertEqual(5994570412, out["user_id"])

    def test_a_bot_display_name_goes_with_it(self):
        out = self._scrub({"telegram_username": "KrabDispatchBot",
                           "telegram_name": "KrabDispatchBot"})
        self.assertNotIn("telegram_name", out)

    def test_the_running_bots_handle_is_learned_at_startup(self):
        Db = self.Database
        Db.set_bot_identity("@SomeOtherBot")
        out = self._scrub({"telegram_username": "SomeOtherBot"})
        self.assertEqual("Unknown", out["telegram_username"])

    def test_a_blank_identity_does_not_blank_everyone(self):
        Db = self.Database
        before = set(Db._BOT_HANDLES)
        Db.set_bot_identity("")
        self.assertEqual(before, set(Db._BOT_HANDLES))
        self.assertEqual("kita", self._scrub({"telegram_username": "kita"})["telegram_username"])

    def test_create_lead_actually_runs_it(self):
        src = (ROOT / "utils" / "database.py").read_text(encoding="utf-8")
        body = src.split("    def create_lead(", 1)[1].split("\n    def ", 1)[0]
        self.assertIn("self._scrub_bot_entrant(payload)", body)

    def test_the_bot_registers_its_own_handle_on_startup(self):
        self.assertIn("set_bot_identity(getattr(app.bot, \"username\", \"\")", BOT_SRC)


class TheBoardCountsPeopleNotLabelsTest(unittest.TestCase):
    """Repairing the rows added display names, and the board — which grouped by
    whatever the row called them — split each person in two: "highkage0_0" 263
    and "Highkage" 98 were the same human."""

    def _tally(self, rows):
        db = mock.MagicMock()
        db._check_tables_exist.return_value = True
        db.client.table.return_value.select.return_value.execute.return_value = \
            mock.MagicMock(data=rows)
        Db = _Database()
        db._EXCLUDE_COL = Db._EXCLUDE_COL
        db._warn_missing_exclude_col = lambda *a: None
        return dict(Db.get_lead_counts_by_sender(db))

    def test_one_person_counted_once_across_labels(self):
        out = self._tally([
            {"user_id": 1, "telegram_username": "highkage0_0", "telegram_name": None},
            {"user_id": 1, "telegram_username": "highkage0_0", "telegram_name": "Highkage"},
            {"user_id": 1, "telegram_username": None, "telegram_name": "Highkage"},
        ])
        self.assertEqual({"Highkage": 3}, out)

    def test_the_name_wins_over_the_handle(self):
        out = self._tally([
            {"user_id": 7, "telegram_username": "jbravo", "telegram_name": "J B"},
        ])
        self.assertEqual({"J B": 1}, out)

    def test_a_row_with_only_a_handle_still_counts(self):
        out = self._tally([
            {"user_id": 9, "telegram_username": "kita", "telegram_name": None}])
        self.assertEqual({"kita": 1}, out)

    def test_different_people_stay_separate(self):
        out = self._tally([
            {"user_id": 1, "telegram_username": "a", "telegram_name": "Ann"},
            {"user_id": 2, "telegram_username": "b", "telegram_name": "Bob"},
        ])
        self.assertEqual({"Ann": 1, "Bob": 1}, out)

    def test_struck_leads_do_not_count(self):
        out = self._tally([
            {"user_id": 1, "telegram_name": "Ann", "exclude_from_count": True},
            {"user_id": 1, "telegram_name": "Ann"},
        ])
        self.assertEqual({"Ann": 1}, out)

    def test_a_row_with_no_identity_at_all_is_not_lost(self):
        out = self._tally([{"user_id": None, "telegram_username": None,
                            "telegram_name": None}])
        self.assertEqual({"Unknown": 1}, out)

    def test_unknown_handles_fall_back_to_the_id(self):
        out = self._tally([{"user_id": 42, "telegram_username": "Unknown",
                            "telegram_name": None}])
        self.assertEqual({"id 42": 1}, out)


class TheRepairScriptTest(unittest.TestCase):

    def test_it_exists_and_defaults_to_a_dry_run(self):
        src = (ROOT / "database" / "repair_bot_credited_leads.py").read_text(encoding="utf-8")
        self.assertIn('add_argument("--apply"', src)
        self.assertIn("dry run", src)

    def test_it_never_guesses_an_identity(self):
        src = (ROOT / "database" / "repair_bot_credited_leads.py").read_text(encoding="utf-8")
        self.assertIn("getChat", src)
        self.assertIn("SKIP", src)


if __name__ == "__main__":
    unittest.main()
