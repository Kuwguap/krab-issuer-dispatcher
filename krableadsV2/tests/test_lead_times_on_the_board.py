r"""The board says when a lead was entered and when it was delivered.

Two gaps, both of which cost the office an answer it gets asked for.

  * There was no delivery time at all. The nearest thing was `status_updated_at`,
    which is the last status change of ANY kind -- so a lead that was delivered
    and then had its receipt uploaded overwrote its own delivery moment. The one
    timestamp somebody reads out to a customer was the one the board threw away.

  * Every time was rendered in the VIEWER's timezone with nothing on the page to
    say so. The bot stamps New York time (utils/timezone.py) and the office works
    in it; a laptop set to anything else read the whole board wrong and looked
    right doing it.

`delivered_at` is stamped once, when a lead first reaches `delivered`, and is
not touched by the statuses that follow. Where it is missing, a lead sitting at
`delivered` right now can have its time inferred from `status_updated_at` -- but
that inference is MARKED, because a time being read to a customer should say
whether it was recorded or worked out.

Run:  venv\Scripts\python.exe -m pytest tests/test_lead_times_on_the_board.py -q
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

SRC_BOARD = (ROOT / "receipts_page.py").read_text(encoding="utf-8")


class TheDeliveryMomentTest(unittest.TestCase):
    """_delivery_moment decides what the Delivered column says."""

    def test_a_recorded_stamp_is_used_and_marked_exact(self):
        got = ad._delivery_moment({"delivery_status": "delivered"},
                                  "2026-09-06T18:30:00+00:00")
        self.assertEqual("2026-09-06T18:30:00+00:00", got["delivered_at"])
        self.assertTrue(got["delivered_exact"])

    def test_a_lead_sitting_at_delivered_can_be_inferred(self):
        """No status change since, so status_updated_at IS the delivery."""
        got = ad._delivery_moment(
            {"delivery_status": "delivered", "status_updated_at": "2026-09-06T18:30:00+00:00"},
            None)
        self.assertEqual("2026-09-06T18:30:00+00:00", got["delivered_at"])
        self.assertFalse(got["delivered_exact"], "an inference must not pass as a record")

    def test_a_lead_that_moved_on_is_left_blank_not_guessed(self):
        """Receipt uploaded after delivery: status_updated_at is the receipt, and
        a plausible-looking wrong time is worse than none."""
        got = ad._delivery_moment(
            {"delivery_status": "receipt_uploaded",
             "status_updated_at": "2026-09-06T20:00:00+00:00"}, None)
        self.assertIsNone(got["delivered_at"])

    def test_an_undelivered_lead_has_no_delivery_time(self):
        for status in ("new", "followup", "tag_issued", "on_the_way"):
            with self.subTest(status=status):
                got = ad._delivery_moment(
                    {"delivery_status": status,
                     "status_updated_at": "2026-09-06T20:00:00+00:00"}, None)
                self.assertIsNone(got["delivered_at"])

    def test_a_recorded_stamp_wins_over_the_inference(self):
        got = ad._delivery_moment(
            {"delivery_status": "delivered", "status_updated_at": "2026-09-06T20:00:00+00:00"},
            "2026-09-06T18:30:00+00:00")
        self.assertEqual("2026-09-06T18:30:00+00:00", got["delivered_at"])
        self.assertTrue(got["delivered_exact"])


class TheStampSurvivesWhatComesAfterTest(unittest.TestCase):
    """The whole reason for a column of its own."""

    def setUp(self):
        self.body = SRC_ADMIN = (ROOT / "admin_dashboard.py").read_text(encoding="utf-8")
        self.body = self.body.split("def set_lead_status(", 1)[1].split("\n    #", 1)[0]

    def test_delivered_stamps_the_moment(self):
        self.assertIn('if status == "delivered":', self.body)
        self.assertIn('patch["delivered_at"] = "now()"', self.body)

    def test_no_other_status_touches_it(self):
        """receipt_uploaded must not overwrite the delivery time -- that is the
        bug this column exists to fix."""
        after = self.body.split('patch["delivered_at"]', 1)[1]
        self.assertNotIn('"delivered_at"', after.split("try:")[0])

    def test_a_missing_column_still_saves_the_status(self):
        """The migration may not have been run. Losing the whole status move
        because we could not record a nicety is the worse trade."""
        self.assertIn('if "delivered_at" in str(e):', self.body)
        self.assertIn("migration_lead_delivered_at.sql", self.body)


class TheColumnCannotCollapseTheBoardTest(unittest.TestCase):
    """delivered_at arrives by migration. Folding it into the main select would
    drop the WHOLE board to the lean fallback (losing delivery_status) on any
    database that has not run it."""

    def test_it_is_read_in_its_own_query(self):
        src = (ROOT / "admin_dashboard.py").read_text(encoding="utf-8")
        body = src.split("def get_transmissions(", 1)[1].split("\n    def ", 1)[0]
        # The main select, as a string, is the thing that must stay clean --
        # not the prose around it.
        main_select = body.split('q = self.client.table("leads").select(', 1)[1]
        main_select = main_select.split(")", 1)[0]
        self.assertNotIn("delivered_at", main_select,
                         "delivered_at is in the main select: one missing column "
                         "would take delivery_status down with it")
        self.assertIn('.select("id, delivered_at")', body)

    def test_a_missing_column_costs_one_column_not_the_board(self):
        src = (ROOT / "admin_dashboard.py").read_text(encoding="utf-8")
        body = src.split("def get_transmissions(", 1)[1].split("\n    def ", 1)[0]
        self.assertIn("delivered_at lookup failed", body)


class TheBoardShowsBothTimesTest(unittest.TestCase):

    def test_there_are_entered_and_delivered_columns(self):
        self.assertIn("<th>Entered</th>", SRC_BOARD)
        self.assertIn("<th>Delivered</th>", SRC_BOARD)

    def test_the_columns_carry_the_real_fields(self):
        self.assertIn("stampCell(r.created_at)", SRC_BOARD)
        self.assertIn("stampCell(r.delivered_at", SRC_BOARD)

    def test_the_colspans_grew_with_the_table(self):
        """Two new columns; a stale colspan leaves the month rows and the detail
        panel short of the table's width."""
        import re
        # The board's own header row -- located by a column only it has, so
        # markup elsewhere on the page cannot be counted by mistake.
        block = SRC_BOARD.split("<th>Client phone</th>", 1)[0]
        row_start = block.rindex("<tr>")
        thead = SRC_BOARD[row_start:SRC_BOARD.index("</tr>", row_start)]
        headers = len(re.findall(r"<th[\s>]", thead))
        spans = set(re.findall(r'colspan="(\d+)"', SRC_BOARD))
        self.assertEqual({str(headers)}, spans,
                         f"{headers} columns but colspans {spans}")

    def test_an_undelivered_lead_says_so_rather_than_dashing(self):
        self.assertIn('empty: "not yet"', SRC_BOARD)

    def test_an_inferred_time_is_marked(self):
        self.assertIn("delivered_exact === false", SRC_BOARD)
        self.assertIn("approx", SRC_BOARD)

    def test_the_cards_and_the_detail_panel_show_them_too(self):
        card = SRC_BOARD.split("function cardHtml(", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("stampCell(r.created_at)", card)
        detail = SRC_BOARD.split("function detailBody(", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("<dt>Entered</dt>", detail)
        self.assertIn("<dt>Delivered</dt>", detail)

    def test_the_export_carries_them(self):
        self.assertIn('"created_at","delivered_at"', SRC_BOARD)


class EveryTimeIsNewYorkTimeTest(unittest.TestCase):
    """The bot stamps NY; a viewer's own clock reading the board differently is
    how two people end up quoting different delivery times for one lead."""

    def test_the_zone_is_pinned(self):
        self.assertIn('const NY = "America/New_York";', SRC_BOARD)

    def test_the_short_formatter_uses_it(self):
        body = SRC_BOARD.split("function when(iso)", 1)[1].split("\n// To the second", 1)[0]
        self.assertIn("timeZone: NY", body)

    def test_the_exact_formatter_uses_it_and_shows_seconds(self):
        body = SRC_BOARD.split("function exactWhen(iso)", 1)[1].split("\n/**", 1)[0]
        self.assertIn("timeZone: NY", body)
        self.assertIn('second:"2-digit"', body)

    def test_the_page_says_which_zone(self):
        """A time with no zone on it is a time somebody will read wrong."""
        self.assertIn('class="tz">ET<', SRC_BOARD)


if __name__ == "__main__":
    unittest.main()
