r"""Conversation state is served from memory, and the table is its log.

Why this exists: one Supabase round trip measured ~200ms, and bot.py calls
get_user_state 42 times and set_user_state 71 times from inside async handlers,
none of them on a thread. Every button tap in the lead flow therefore spent
~400ms with the event loop frozen -- and a frozen loop cannot acknowledge ANY
other user's tap, so the bot got slower the more people used it.

What makes it safe rather than a cache-coherency bug waiting to happen: the
states table has exactly one writer in the entire system (only utils/database.py
touches it -- not dispatch_web, not admin_dashboard, not public_form, not
lead_ingest), and the worker runs as a single instance, which polling enforces.
So the dict is not a copy of somebody else's data; it is the owner's.

The tests below hold the three things that could turn this from a speedup into
the "edits that do nothing" bug this repo already wears a scar from:

* a read after a write returns the WRITE, always, even with the network down;
* a write still reaches the table, and a failed one is retried, not dropped;
* a read already in flight when a write lands must not overwrite the newer value.

Run:  venv\Scripts\python.exe -m pytest tests/test_state_cache.py -q
"""
import importlib.util
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy-key")
os.environ["KRAB_STATE_CACHE"] = "1"

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


udb = _private_database_module("_udb_for_state_cache_tests")


def _fresh_db(upsert=None, delete=None, select_row=None):
    """A Database whose Supabase client is a stub, with a fresh state store."""
    udb._STATES = udb._StateStore()
    with mock.patch.object(udb, "create_client", return_value=mock.MagicMock()):
        db = udb.Database()
    db._tables_checked = True
    db._tables_exist = True

    calls = {"upsert": [], "delete": [], "select": 0}

    def _table(name):
        t = mock.MagicMock()

        def _upsert(row, **kw):
            calls["upsert"].append(row)
            if upsert:
                upsert(row)
            return mock.MagicMock(execute=mock.MagicMock())
        t.upsert.side_effect = _upsert

        sel = mock.MagicMock()

        def _exec():
            calls["select"] += 1
            r = mock.MagicMock()
            r.data = [select_row] if select_row else []
            return r
        sel.execute.side_effect = _exec
        t.select.return_value.eq.return_value = sel
        t.select.return_value.eq.return_value.limit.return_value = sel

        def _del_exec():
            calls["delete"].append(True)
            if delete:
                delete()
            return mock.MagicMock()
        t.delete.return_value.eq.return_value.execute.side_effect = _del_exec
        return t

    db.client.table.side_effect = _table
    return db, calls


class AReadAfterAWriteTest(unittest.TestCase):

    def test_it_returns_what_was_just_written_without_asking_the_database(self):
        db, calls = _fresh_db()
        db.set_user_state(7, "phase1", {"price": "150"})
        got = db.get_user_state(7)
        self.assertEqual("phase1", got["state"])
        self.assertEqual({"price": "150"}, got["data"])
        self.assertEqual(0, calls["select"], "a read went to the wire anyway")

    def test_it_returns_the_write_even_when_every_write_fails(self):
        """The scar this replaces: a dropped upsert used to lose the edit and the
        next read handed back the stale row."""
        db, calls = _fresh_db(upsert=lambda row: (_ for _ in ()).throw(RuntimeError("GOAWAY")))
        db.set_user_state(8, "phase1", {"name": "John Damian"})
        self.assertEqual({"name": "John Damian"}, db.get_user_state(8)["data"])

    def test_a_cleared_state_reads_as_absent_and_not_as_the_old_row(self):
        db, calls = _fresh_db(select_row={"user_id": 9, "state": "phase1", "data": {}})
        db.set_user_state(9, "phase1", {"x": 1})
        db.clear_user_state(9)
        self.assertIsNone(db.get_user_state(9))
        self.assertEqual(0, calls["select"])

    def test_the_caller_cannot_mutate_the_stored_value_by_accident(self):
        """Handlers mutate the blob they are handed and then save it; handing out
        the live dict would let one that dies halfway leave a half-applied edit
        behind as the truth."""
        db, _ = _fresh_db()
        db.set_user_state(10, "phase1", {"price": "150"})
        got = db.get_user_state(10)
        got["data"]["price"] = "999"
        self.assertEqual("150", db.get_user_state(10)["data"]["price"])


class ItStillReachesTheTableTest(unittest.TestCase):

    def test_the_write_lands(self):
        done = threading.Event()
        db, calls = _fresh_db(upsert=lambda row: done.set())
        db.set_user_state(11, "phase1", {"a": 1})
        self.assertTrue(done.wait(5), "the upsert never happened")
        self.assertEqual(11, calls["upsert"][0]["user_id"])

    def test_a_failed_write_is_retried_not_dropped(self):
        hits = []
        ok = threading.Event()

        def flaky(row):
            hits.append(row)
            if len(hits) == 1:
                raise RuntimeError("transient")
            ok.set()

        db, _ = _fresh_db(upsert=flaky)
        db.set_user_state(12, "phase1", {"a": 1})
        self.assertTrue(ok.wait(10), "a failed state write was dropped")
        self.assertGreaterEqual(len(hits), 2)

    def test_a_clear_reaches_the_table(self):
        done = threading.Event()
        db, calls = _fresh_db(delete=lambda: done.set())
        db.clear_user_state(13)
        self.assertTrue(done.wait(5), "the delete never happened")

    def test_five_edits_in_a_burst_cost_fewer_than_five_upserts(self):
        """Newest-wins: a card edited repeatedly is one row, not a queue of them."""
        db, calls = _fresh_db()
        for i in range(5):
            db.set_user_state(14, "phase1", {"price": str(i)})
        udb.flush_user_states(timeout=10)
        self.assertLess(len(calls["upsert"]), 5, calls["upsert"])
        self.assertEqual("4", calls["upsert"][-1]["data"]["price"])

    def test_flush_is_available_for_shutdown(self):
        db, _ = _fresh_db()
        db.set_user_state(15, "phase1", {"a": 1})
        self.assertTrue(udb.flush_user_states(timeout=10))


class TheInFlightReadHazardTest(unittest.TestCase):

    def test_a_read_that_was_already_in_flight_does_not_clobber_a_newer_write(self):
        """prime() refuses to overwrite. Without that, a slow SELECT returning
        the pre-edit row would land on top of the edit the user just made."""
        db, _ = _fresh_db(select_row={"user_id": 16, "state": "phase1",
                                      "data": {"price": "OLD"}})
        db.set_user_state(16, "phase1", {"price": "NEW"})
        udb._STATES.prime(16, {"user_id": 16, "state": "phase1",
                               "data": {"price": "OLD"}})
        self.assertEqual("NEW", db.get_user_state(16)["data"]["price"])

    def test_a_miss_is_filled_from_the_table(self):
        db, calls = _fresh_db(select_row={"user_id": 17, "state": "phase1",
                                          "data": {"price": "150"}})
        self.assertEqual("150", db.get_user_state(17)["data"]["price"])
        self.assertEqual(1, calls["select"])
        self.assertEqual("150", db.get_user_state(17)["data"]["price"])
        self.assertEqual(1, calls["select"], "the second read went to the wire")

    def test_a_known_absent_user_is_not_re_read_every_time(self):
        db, calls = _fresh_db(select_row=None)
        self.assertIsNone(db.get_user_state(18))
        self.assertIsNone(db.get_user_state(18))
        self.assertEqual(1, calls["select"])


class TheKillSwitchTest(unittest.TestCase):

    def test_off_means_the_old_behaviour_exactly(self):
        db, calls = _fresh_db(select_row={"user_id": 19, "state": "p", "data": {}})
        with mock.patch.dict(os.environ, {"KRAB_STATE_CACHE": "0"}):
            db.set_user_state(19, "phase1", {"a": 1})
            self.assertEqual(1, len(calls["upsert"]), "write did not go straight out")
            db.get_user_state(19)
            db.get_user_state(19)
            self.assertEqual(2, calls["select"], "reads were cached with the switch off")

    def test_the_switch_is_documented_where_the_others_are(self):
        src = (ROOT / "utils" / "database.py").read_text(encoding="utf-8")
        self.assertIn("KRAB_STATE_CACHE", src)
        self.assertIn("def flush_user_states", src)


class NothingElseWritesTheTableTest(unittest.TestCase):
    """The whole premise. If a second writer ever appears, the dict stops being
    authoritative and this test is where that gets noticed."""

    def test_only_utils_database_touches_the_states_table(self):
        offenders = []
        for path in ROOT.rglob("*.py"):
            s = str(path).replace("\\", "/")
            if "/.claude/" in s or "/venv/" in s or "/tests/" in s:
                continue
            if path.name == "database.py" and "/utils/" in s:
                continue
            try:
                body = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if 'table("states")' in body or "table('states')" in body:
                offenders.append(s)
        self.assertEqual([], offenders,
                         "a second writer of states would break the cache premise")


if __name__ == "__main__":
    unittest.main()
