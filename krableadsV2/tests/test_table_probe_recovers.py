r"""One bad probe at boot must not empty the dashboard until somebody redeploys.

This is what took /receipts down on 2026-09-06. `_check_tables_exist` latched
BOTH answers on its first call. That call lands on a cold start, over a pooled
HTTP/2 connection that can come back as a RemoteProtocolError — and a single
blip there set `_tables_exist = False` for the life of the process. Every
guarded method then returned `[]`: no leads on the board, no groups, no drivers,
no stats.

The database was fine throughout, which is what made it so hard to see:
`/receipts/api/sendconfig` queries `leads` WITHOUT the guard and answered
normally while the board next door showed nothing at all. The only cure was
another deploy, which is also what had appeared to cause it.

So: success latches (tables do not stop existing, and re-probing every call is a
wasted round trip per request); failure does not.

Run:  venv\Scripts\python.exe -m pytest tests/test_table_probe_recovers.py -q
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

import admin_dashboard as ad                                   # noqa: E402


class Probe:
    """A `groups` probe that fails the first N times, then works."""

    def __init__(self, fail_times=1):
        self.left = fail_times
        self.calls = 0

    def table(self, name):
        return self

    def select(self, *a):
        return self

    def limit(self, n):
        return self

    def execute(self):
        self.calls += 1
        if self.left > 0:
            self.left -= 1
            raise RuntimeError("Server disconnected without sending a response")
        return mock.MagicMock(data=[{"id": "1"}])


def _db(client):
    db = ad.AdminDatabase.__new__(ad.AdminDatabase)
    db.client = client
    db._tables_checked = False
    db._tables_exist = False
    db._tables_retry_at = 0.0
    return db


class AFailedProbeIsRetriedTest(unittest.TestCase):

    def test_a_single_blip_does_not_kill_the_process(self):
        """The whole bug in one assertion."""
        db = _db(Probe(fail_times=1))
        self.assertFalse(db._check_tables_exist(), "the first probe should fail")
        db._tables_retry_at = 0.0          # the backoff, elapsed
        self.assertTrue(db._check_tables_exist(),
                        "the board never recovered without a redeploy")

    def test_the_board_fills_again_after_it_recovers(self):
        """Not just the flag — the method the board actually calls."""
        db = _db(Probe(fail_times=1))
        self.assertEqual([], db.get_transmissions())
        db._tables_retry_at = 0.0
        with mock.patch.object(db, "_check_tables_exist", return_value=True), \
             mock.patch.object(db, "client") as c:
            c.table.return_value.select.return_value.order.return_value \
                .limit.return_value.execute.return_value = mock.MagicMock(data=[])
            self.assertEqual([], db.get_transmissions())

    def test_a_failure_is_logged(self):
        """"Everything is empty" says nothing at all about the cause."""
        db = _db(Probe(fail_times=1))
        with self.assertLogs("admin_dashboard", level="WARNING") as log:
            db._check_tables_exist()
        self.assertIn("table probe failed", "\n".join(log.output))


class ItDoesNotProbeOnEveryCallTest(unittest.TestCase):
    """The other half. Retrying with no backoff would put a round trip in front
    of every request the dashboard serves."""

    def test_a_failing_probe_backs_off(self):
        db = _db(Probe(fail_times=99))
        for _ in range(5):
            db._check_tables_exist()
        self.assertEqual(1, db.client.calls,
                         "the probe ran on every call — that is a query per request")

    def test_the_backoff_is_finite(self):
        db = _db(Probe(fail_times=99))
        db._check_tables_exist()
        self.assertGreater(db._tables_retry_at, 0.0)
        self.assertLessEqual(ad.AdminDatabase._TABLES_RETRY_SECONDS, 120,
                             "an empty board should not persist for minutes")

    def test_success_is_latched(self):
        db = _db(Probe(fail_times=0))
        for _ in range(5):
            self.assertTrue(db._check_tables_exist())
        self.assertEqual(1, db.client.calls, "tables do not stop existing")


class TheGuardIsWhatEmptiesEverythingTest(unittest.TestCase):
    """Naming the blast radius, so the next person seeing an empty board checks
    the probe before the query."""

    def test_the_board_and_the_directory_both_hang_off_it(self):
        db = _db(Probe(fail_times=99))
        self.assertEqual([], db.get_transmissions())
        self.assertEqual([], db.get_issuer_directory())
        self.assertEqual([], db.get_all_groups())
        self.assertEqual([], db.get_all_drivers())


if __name__ == "__main__":
    unittest.main()
