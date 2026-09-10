r"""Settings and the group list answer from memory for a few seconds.

These are NOT like the conversation state. The states table has one writer and so
the dict can be authoritative; settings are written by admin_dashboard.py,
receipts_page.py and dispatch_web/settingslite.py, and groups by the admin and the
receipts board -- all in other processes. So the most that is safe here is a short
reprieve from asking the same question at ~200ms a time, and what the tests below
hold is the boundary of that reprieve:

* a repeat read inside the window does not touch the wire;
* the window actually expires;
* a change made THROUGH THE BOT is never read stale, however short the TTL;
* get_group_by_id rides the cached list but still falls through on a miss, so a
  group created elsewhere seconds ago is found rather than reported missing;
* the list handed out is a copy -- callers filter it in place.

Run:  venv\Scripts\python.exe -m pytest tests/test_read_caches.py -q
"""
import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy-key")

def _private_database_module(tag):
    """Load a private copy of utils/database.py.

    Dozens of test modules in this suite do `udb.Database = mock.MagicMock()` at
    IMPORT time, which mutates utils.database for the rest of the pytest session.
    These tests are about the real implementation, so they take their own copy and
    are immune to collection order.
    """
    spec = importlib.util.spec_from_file_location(tag, ROOT / "utils" / "database.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[tag] = mod
    spec.loader.exec_module(mod)
    return mod


udb = _private_database_module("_udb_for_read_cache_tests")


def _db(groups=None, setting_value="yes"):
    udb.invalidate_setting_cache()
    udb.invalidate_groups_cache()
    with mock.patch.object(udb, "create_client", return_value=mock.MagicMock()):
        d = udb.Database()
    d._tables_checked = True
    d._tables_exist = True
    n = {"groups": 0, "settings": 0, "group_by_id": 0, "upsert": 0, "update": 0}

    def _table(name):
        t = mock.MagicMock()
        if name == "groups":
            def _all():
                n["groups"] += 1
                r = mock.MagicMock()
                r.data = list(groups or [])
                return r
            t.select.return_value.order.return_value.execute.side_effect = _all

            def _one():
                n["group_by_id"] += 1
                r = mock.MagicMock()
                r.data = list(groups or [])
                return r
            t.select.return_value.eq.return_value.execute.side_effect = _one
            t.update.return_value.eq.return_value.execute.side_effect = \
                lambda: n.__setitem__("update", n["update"] + 1) or mock.MagicMock()
        if name == "settings":
            def _sel():
                n["settings"] += 1
                r = mock.MagicMock()
                r.data = [{"value": setting_value}]
                return r
            t.select.return_value.eq.return_value.limit.return_value.execute.side_effect = _sel
            t.upsert.return_value.execute.side_effect = \
                lambda: n.__setitem__("upsert", n["upsert"] + 1) or mock.MagicMock()
        return t

    d.client.table.side_effect = _table
    return d, n


class SettingsTest(unittest.TestCase):

    def test_a_repeat_read_inside_the_window_is_free(self):
        d, n = _db()
        self.assertEqual("yes", d.get_setting("driverblock_phone_redaction"))
        self.assertEqual("yes", d.get_setting("driverblock_phone_redaction"))
        self.assertEqual("yes", d.get_setting("driverblock_phone_redaction"))
        self.assertEqual(1, n["settings"])

    def test_the_window_expires(self):
        d, n = _db()
        d.get_setting("k")
        with mock.patch.object(udb, "_SETTING_TTL_SEC", -1):
            d.get_setting("k")
        self.assertEqual(2, n["settings"])

    def test_a_missing_setting_is_remembered_as_missing(self):
        """Otherwise the commonest case -- a toggle never set -- pays full price
        on every single tap."""
        d, n = _db()
        d.client.table.side_effect = None
        t = mock.MagicMock()
        r = mock.MagicMock()
        r.data = []
        calls = []
        t.select.return_value.eq.return_value.limit.return_value.execute.side_effect = \
            lambda: calls.append(1) or r
        d.client.table.return_value = t
        self.assertIsNone(d.get_setting("never_set"))
        self.assertIsNone(d.get_setting("never_set"))
        self.assertEqual(1, len(calls))

    def test_a_write_through_the_bot_is_never_read_stale(self):
        d, n = _db(setting_value="old")
        self.assertEqual("old", d.get_setting("k"))
        d.set_setting("k", "new")
        d.get_setting("k")
        self.assertEqual(2, n["settings"], "the bot read back its own stale value")

    def test_separate_keys_do_not_share_an_entry(self):
        d, n = _db()
        d.get_setting("a")
        d.get_setting("b")
        self.assertEqual(2, n["settings"])


class GroupsTest(unittest.TestCase):
    ROWS = [{"id": "g1", "group_name": "Alpha", "is_active": True},
            {"id": "g2", "group_name": "Beta", "is_active": True}]

    def test_a_repeat_read_inside_the_window_is_free(self):
        d, n = _db(groups=self.ROWS)
        self.assertEqual(2, len(d.get_all_groups()))
        self.assertEqual(2, len(d.get_all_groups()))
        self.assertEqual(1, n["groups"])

    def test_a_lookup_by_id_rides_the_cached_list(self):
        """handle_accept_group_offer reads a lead then a group back to back; the
        second one should not be a second round trip."""
        d, n = _db(groups=self.ROWS)
        d.get_all_groups()
        g = d.get_group_by_id("g2")
        self.assertEqual("Beta", g["group_name"])
        self.assertEqual(0, n["group_by_id"], "the lookup went to the wire anyway")

    def test_a_lookup_that_misses_the_cached_list_still_asks(self):
        """A group created in the admin seconds ago must be findable, not
        reported missing because our 15-second list predates it."""
        d, n = _db(groups=self.ROWS)
        d.get_all_groups()
        d.get_group_by_id("g-brand-new")
        self.assertEqual(1, n["group_by_id"])

    def test_a_lookup_with_no_cached_list_asks(self):
        d, n = _db(groups=self.ROWS)
        d.get_group_by_id("g1")
        self.assertEqual(1, n["group_by_id"])
        self.assertEqual(0, n["groups"])

    def test_the_list_is_a_copy(self):
        """Callers filter this in place; a mutation must not reach the cache."""
        d, n = _db(groups=self.ROWS)
        first = d.get_all_groups()
        first[0]["group_name"] = "MUTATED"
        self.assertEqual("Alpha", d.get_all_groups()[0]["group_name"])

    def test_renaming_a_group_through_the_bot_drops_the_list(self):
        d, n = _db(groups=self.ROWS)
        d.get_all_groups()
        d.rename_group("g1", "Alpha Two")
        d.get_all_groups()
        self.assertEqual(2, n["groups"], "the bot served its own stale rename")

    def test_the_window_expires(self):
        d, n = _db(groups=self.ROWS)
        d.get_all_groups()
        with mock.patch.object(udb, "_GROUPS_TTL_SEC", -1):
            d.get_all_groups()
        self.assertEqual(2, n["groups"])


class TheTtlsAreShortEnoughToBeHonestTest(unittest.TestCase):

    def test_settings_and_groups_stay_within_a_few_seconds(self):
        """These are read-mostly, but another process owns them. A long TTL here
        would turn 'I flipped the toggle and nothing happened' into a bug report."""
        self.assertLessEqual(udb._SETTING_TTL_SEC, 10)
        self.assertLessEqual(udb._GROUPS_TTL_SEC, 30)


if __name__ == "__main__":
    unittest.main()
