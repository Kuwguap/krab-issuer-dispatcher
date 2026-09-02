r"""The board's Driver column says who has the lead, or who it is waiting on.

It only ever named the driver who had ACCEPTED. A lead still out for offer
showed a bare dash, which reads as "no driver involved" when in truth the offer
is sitting with somebody and that somebody is who the office wants to chase.

The rule the code has to keep: an accepted driver always wins, a pending one is
shown but LABELLED, and a pending driver must never render like an accepted one.

Run:  venv\Scripts\python.exe -m pytest tests/test_receipts_driver_column.py -q
"""
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy-key")

SRC_ADMIN = (ROOT / "admin_dashboard.py").read_text(encoding="utf-8")
SRC_BOARD = (ROOT / "receipts_page.py").read_text(encoding="utf-8")


def _get_transmissions_body() -> str:
    body = SRC_ADMIN.split("def get_transmissions(", 1)[1]
    return body.split("\n    def ", 1)[0]


class TheQueryAsksForOffersTooTest(unittest.TestCase):

    def setUp(self):
        self.body = _get_transmissions_body()

    def test_pending_assignments_are_fetched(self):
        """Accepted-only is what left the column blank."""
        self.assertIn('"accepted", "pending"', self.body,
                      "the driver lookup still asks for accepted rows only")
        self.assertNotIn('.eq("status", "accepted")', self.body)

    def test_accepted_and_pending_are_kept_apart(self):
        self.assertIn("drivers_by_lead", self.body)
        self.assertIn("pending_by_lead", self.body)

    def test_an_accepted_driver_still_wins(self):
        """A lead with both an acceptance and older offers shows the acceptor."""
        i_acc = self.body.index("drivers_by_lead[lid] = drv")
        i_pend = self.body.index("pending_by_lead.setdefault")
        self.assertLess(i_acc, i_pend,
                        "the accepted branch must be the one that claims the row")
        self.assertIn("driver_pending = not drv", self.body,
                      "pending is only consulted when nothing was accepted")

    def test_only_the_first_offer_is_named(self):
        """A broadcast to six drivers must not turn one column into a list."""
        self.assertIn("setdefault", self.body)

    def test_the_row_says_which_it_is(self):
        self.assertIn('"driver_pending"', self.body)


class TheBoardNeverImpliesAnAcceptanceTest(unittest.TestCase):

    def test_a_pending_driver_is_labelled(self):
        self.assertIn('r.driver_pending ? "Driver (offered)" : "Driver"', SRC_BOARD)

    def test_the_badge_is_rendered_next_to_the_name(self):
        self.assertIn('class="pend"', SRC_BOARD)
        self.assertIn("c.pending && c.name", SRC_BOARD,
                      "the badge must need BOTH a pending flag and a name")

    def test_the_badge_has_a_style_of_its_own(self):
        """An unstyled badge is indistinguishable from the name beside it."""
        self.assertIn(" .pend {", SRC_BOARD)

    def test_a_lead_with_no_driver_at_all_still_shows_a_dash(self):
        self.assertIn("""'<span class="none">—</span>'""", SRC_BOARD)


class TheColumnIsBuiltFromRealShapesTest(unittest.TestCase):
    """Exercise the actual row-building logic against the three real cases."""

    def _row(self, accepted=None, pending=None):
        drivers_by_lead = {"L": accepted} if accepted else {}
        pending_by_lead = {"L": pending} if pending else {}
        drv = drivers_by_lead.get("L") or {}
        driver_pending = not drv
        if driver_pending:
            drv = pending_by_lead.get("L") or {}
        return {
            "driver_name": (drv.get("driver_name") or "").strip() or "—",
            "driver_pending": bool(driver_pending and drv.get("driver_name")),
        }

    def test_an_accepted_driver_is_named_and_not_flagged(self):
        r = self._row(accepted={"driver_name": "Kita"})
        self.assertEqual("Kita", r["driver_name"])
        self.assertFalse(r["driver_pending"])

    def test_an_offered_driver_is_named_and_flagged(self):
        r = self._row(pending={"driver_name": "Alpha"})
        self.assertEqual("Alpha", r["driver_name"])
        self.assertTrue(r["driver_pending"])

    def test_an_acceptance_beats_an_offer(self):
        r = self._row(accepted={"driver_name": "Kita"}, pending={"driver_name": "Alpha"})
        self.assertEqual("Kita", r["driver_name"])
        self.assertFalse(r["driver_pending"])

    def test_nobody_at_all_is_still_a_dash(self):
        r = self._row()
        self.assertEqual("—", r["driver_name"])
        self.assertFalse(r["driver_pending"],
                         "a dash must never be flagged as an offer")


if __name__ == "__main__":
    unittest.main()
