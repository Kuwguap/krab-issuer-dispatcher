r"""Cross-checking a transaction on the board, and the driver hub's Receipts button.

Asked for: "drivers get a different message that looks like 'welcome back,
john / you owe.... / to add a lead, type /lead....' it has inline buttons /
lets add more / add a receipts inline button and receipts / so inline buttons
are now: add a lead, receipts, help" and then "THE CHECK IN SHOULD BE ON THE
TABLE AFTER UPDATED / SO ITS FOR EACH TRANSACTION / USERS CAN CHECK A
TRANSACTION AND CAN SEE WHO ELSE DID AND THE TIME".

A check is an accountability record about one lead, so the things that would
make it worthless are pinned here: the time is the database's, one person
cannot check the same lead twice, and nobody can clear somebody else's check.

Run:  venv\Scripts\python.exe -m pytest tests/test_checkin_and_driver_hub.py -q
"""
import inspect
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy-key")

import admin_dashboard as ad  # noqa: E402
import receipts_page  # noqa: E402

BOT_SRC = (ROOT / "bot.py").read_text(encoding="utf-8")
LEAD = "11111111-2222-3333-4444-555555555555"

CHECKS = [
    {"who": "Kita", "at": "2026-09-08T21:00:00+00:00", "note": "matches the receipt"},
    {"who": "Marco", "at": "2026-09-08T20:00:00+00:00", "note": ""},
]


def _fake_db(checks=None, ready=True):
    db = mock.MagicMock()
    store = {LEAD: list(CHECKS if checks is None else checks)}
    db.checkins_ready.return_value = ready

    def get(lead_id):
        return list(store.get(str(lead_id), []))

    def add(lead_id, who, note=""):
        if not ready:
            return False, ("This database is missing "
                           "database/migration_lead_checkins.sql — a check "
                           "cannot be recorded yet.")
        rows = store.setdefault(str(lead_id), [])
        if any(r["who"].lower() == who.lower() for r in rows):
            return True, ""                     # already checked; time stands
        rows.insert(0, {"who": who, "at": "2026-09-08T22:00:00+00:00", "note": note})
        return True, ""

    def remove(lead_id, who):
        rows = store.get(str(lead_id), [])
        store[str(lead_id)] = [r for r in rows if r["who"].lower() != who.lower()]
        return True, ""

    db.get_lead_checkins.side_effect = get
    db.add_lead_checkin.side_effect = add
    db.remove_lead_checkin.side_effect = remove
    db.lead_deletion_ready.return_value = True
    db.get_all_drivers.return_value = []
    db.get_manually_suspended_driver_ids.return_value = set()
    db._get_all_pending_receipts_per_driver.return_value = []
    db.resolve_telegram_names.return_value = {}
    db.get_all_groups.return_value = []
    db.get_setting.return_value = ""
    db._store = store
    return db


class _Signed(unittest.TestCase):

    def setUp(self):
        ad.app.config["TESTING"] = True
        self.client = ad.app.test_client()
        self.client.post("/receipts/login",
                         data={"password": receipts_page._receipts_password()})

    def call(self, method, path, db=None, **kw):
        with mock.patch.object(ad, "db", db if db is not None else _fake_db()):
            return getattr(self.client, method)(path, **kw)


class TheCheckedColumnTest(_Signed):

    def setUp(self):
        super().setUp()
        self.body = self.client.get("/receipts").get_data(as_text=True)

    def test_the_column_comes_after_updated(self):
        self.assertIn("<th>Checked</th>", self.body)
        self.assertGreater(self.body.index("<th>Checked</th>"),
                           self.body.index('<th class="hide-sm">Updated</th>'))

    def test_the_table_still_spans_its_own_width(self):
        """A seventeenth column with sixteen-wide dividers leaves the month rows
        and the detail panel short of the table."""
        heads = self.body[self.body.index("<thead>"):self.body.index("</thead>")]
        n = len(re.findall(r"<th[\s>]", heads))
        self.assertEqual(17, n)
        for span in set(re.findall(r'colspan="(\d+)"', receipts_page.BOARD_HTML
                                   .split("// ── People:", 1)[0])):
            self.assertEqual(str(n), span)

    def test_a_row_carries_its_own_checks(self):
        for needle in ("checkCell", "checkList", "toggleCheck", "myCheck",
                       "chkbtn", "r.checkins"):
            self.assertIn(needle, self.body, needle)

    def test_the_names_and_times_are_readable_in_the_row(self):
        """"can see who else did and the time" — in the detail panel, where
        there is room, and on the cell's own tooltip."""
        self.assertIn("Cross-checked by", self.body)
        self.assertIn("when(c.at)", self.body)
        self.assertIn("esc(c.who)", self.body)

    def test_a_card_can_be_checked_too(self):
        """The table hides its last columns under 860px, and a phone works the
        cards. A check that only existed on the table would not exist there."""
        i = self.body.index("function cardHtml")
        card = self.body[i:self.body.index("function setView")]
        self.assertIn("checkCell(r)", card)
        self.assertIn("detailBody(r)", card)      # and the names are in its detail

    def test_the_page_level_section_is_gone(self):
        """It moved onto the row. Two check-in ideas on one page would compete."""
        self.assertNotIn('id="checkin"', self.body)
        self.assertNotIn("loadCheckins", self.body)
        self.assertFalse(hasattr(receipts_page, "_CHECKINS_KEY"))


class CheckingATransactionTest(_Signed):

    def test_checking_one_records_who(self):
        db = _fake_db(checks=[])
        r = self.call("post", "/receipts/api/transmissions/%s/check" % LEAD,
                      db=db, json={"who": "Kita", "note": "matches"})
        self.assertEqual(200, r.status_code, r.get_data(as_text=True))
        db.add_lead_checkin.assert_called_once_with(LEAD, "Kita", "matches")
        self.assertEqual(["Kita"], [c["who"] for c in r.get_json()["checkins"]])

    def test_the_answer_carries_everyone_who_checked_it(self):
        """Two people checking at once must not leave one looking at a short
        count — the server answers with the whole list, not a delta."""
        r = self.call("post", "/receipts/api/transmissions/%s/check" % LEAD,
                      json={"who": "Nana"})
        self.assertEqual(["Nana", "Kita", "Marco"],
                         [c["who"] for c in r.get_json()["checkins"]])

    def test_checking_twice_is_still_one_check(self):
        db = _fake_db()
        r = self.call("post", "/receipts/api/transmissions/%s/check" % LEAD,
                      db=db, json={"who": "kita"})
        self.assertEqual(200, r.status_code)
        self.assertEqual(2, len(r.get_json()["checkins"]))
        self.assertEqual("2026-09-08T21:00:00+00:00",
                         r.get_json()["checkins"][0]["at"])   # the FIRST time

    def test_a_check_with_no_name_is_refused(self):
        db = _fake_db()
        r = self.call("post", "/receipts/api/transmissions/%s/check" % LEAD,
                      db=db, json={"note": "hi"})
        self.assertEqual(400, r.status_code)
        db.add_lead_checkin.assert_not_called()

    def test_taking_back_your_own_check(self):
        db = _fake_db()
        r = self.call("delete", "/receipts/api/transmissions/%s/check" % LEAD,
                      db=db, json={"who": "Kita"})
        self.assertEqual(200, r.status_code)
        self.assertEqual(["Marco"], [c["who"] for c in r.get_json()["checkins"]])

    def test_an_unchecked_needs_a_name_so_it_cannot_clear_the_column(self):
        """Without a name this would delete every check on the lead."""
        db = _fake_db()
        r = self.call("delete", "/receipts/api/transmissions/%s/check" % LEAD,
                      db=db, json={})
        self.assertEqual(400, r.status_code)
        db.remove_lead_checkin.assert_not_called()

    def test_the_page_only_ever_removes_your_own(self):
        body = self.client.get("/receipts").get_data(as_text=True)
        self.assertIn('method: mine ? "DELETE" : "POST"', body)

    def test_without_the_migration_both_ends_say_so(self):
        db = _fake_db(ready=False)
        r = self.call("post", "/receipts/api/transmissions/%s/check" % LEAD,
                      db=db, json={"who": "Kita"})
        self.assertEqual(503, r.status_code)
        self.assertIn("migration_lead_checkins.sql", r.get_json()["error"])

        db.remove_lead_checkin.side_effect = lambda l, w: (
            False, "This database is missing database/migration_lead_checkins.sql — "
                   "a check cannot be recorded yet.")
        r = self.call("delete", "/receipts/api/transmissions/%s/check" % LEAD,
                      db=db, json={"who": "Kita"})
        self.assertEqual(503, r.status_code)

    def test_the_people_tab_says_when_the_column_is_dead(self):
        db = _fake_db(ready=False)
        got = self.call("get", "/receipts/api/people", db=db).get_json()
        self.assertFalse(got["can_check"])
        body = self.client.get("/receipts").get_data(as_text=True)
        self.assertIn("can_check", body)
        self.assertIn("migration_lead_checkins.sql", body)

    def test_both_ends_are_behind_the_password(self):
        fresh = ad.app.test_client()
        for method in ("post", "delete"):
            r = getattr(fresh, method)(
                "/receipts/api/transmissions/%s/check" % LEAD, json={"who": "x"})
            self.assertEqual(401, r.status_code, method)

    def test_it_is_not_on_the_open_api_alias(self):
        rules = [str(r) for r in ad.app.url_map.iter_rules()
                 if r.endpoint in ("api_check_lead", "api_uncheck_lead")]
        self.assertEqual(2, len(rules))
        for rule in rules:
            self.assertTrue(rule.startswith("/receipts/"), rule)


class TheReaderTest(unittest.TestCase):
    """The parts that must be true of the database layer itself."""

    def test_the_board_reads_every_row_s_checks_in_one_query(self):
        """One query for the page, not one per row: the board draws 1000."""
        src = inspect.getsource(ad.AdminDatabase._checkins_for_leads)
        self.assertIn('in_("lead_id"', src)
        self.assertIn("checked_at", src)
        board = inspect.getsource(ad.AdminDatabase.get_transmissions)
        self.assertIn("_checkins_for_leads(lead_ids)", board)
        self.assertIn('"checkins": checkins_by_lead.get(lid, [])', board)

    def test_it_pages_past_the_thousand_row_ceiling(self):
        """PostgREST answers at most 1000 rows however large a limit is asked
        for — proven against this database. One query would stop reporting
        checks partway down a full board, and those leads would read as never
        cross-checked."""
        src = inspect.getsource(ad.AdminDatabase._checkins_for_leads)
        self.assertIn(".range(start, start + PAGE - 1)", src)
        self.assertIn("if len(got) < PAGE:", src)
        self.assertNotIn("limit(5000)", src)

    def test_a_missing_table_reads_as_nobody_has_checked(self):
        """Which is the truth on a database without the migration — and the
        board must still draw."""
        src = inspect.getsource(ad.AdminDatabase._checkins_for_leads)
        self.assertIn("return out", src)
        self.assertIn("lead_checkins", src)

    def test_one_person_cannot_erase_anothers_check(self):
        """ilike takes a PATTERN. Somebody calling themselves "%" would have
        cleared every check on the lead, and "K_ta" would have taken "Kata"
        with it — which is the whole worth of the column."""
        src = inspect.getsource(ad.AdminDatabase.remove_lead_checkin)
        self.assertNotIn(".ilike(", src)          # the CALL, not the note saying why
        self.assertIn(".strip().lower() == want", src)

    def test_reading_the_names_and_then_checking_does_not_shut_the_row(self):
        html = ad.app.test_client()
        html.post("/receipts/login",
                  data={"password": receipts_page._receipts_password()})
        page = html.get("/receipts").get_data(as_text=True)
        self.assertIn("const wasOpen =", page)
        self.assertIn("if (back) back.hidden = false;", page)

    def test_the_time_is_the_databases_own(self):
        """A laptop clock an hour out would file the claim in the wrong place."""
        src = inspect.getsource(ad.AdminDatabase.add_lead_checkin)
        self.assertNotIn("datetime", src)
        sql = (ROOT / "database" / "migration_lead_checkins.sql").read_text(
            encoding="utf-8").lower()
        self.assertIn("checked_at timestamptz not null default now()", sql)

    def test_one_check_per_person_per_lead_is_enforced_in_the_database(self):
        """Two taps at once beat any check-then-insert in application code."""
        sql = (ROOT / "database" / "migration_lead_checkins.sql").read_text(
            encoding="utf-8").lower()
        self.assertIn("create unique index", sql)
        self.assertIn("lower(who)", sql)
        src = inspect.getsource(ad.AdminDatabase.add_lead_checkin)
        self.assertIn("duplicate key", src)      # and the loser is not an error

    def test_the_table_is_reachable_with_the_key_the_board_holds(self):
        """receipt_files enabled RLS with no policy on the strength of a
        service_role key this deployment does not use, and its writes were
        refused for months. This table must not repeat it."""
        sql = (ROOT / "database" / "migration_lead_checkins.sql").read_text(
            encoding="utf-8").lower()
        self.assertIn("disable row level security", sql)
        self.assertIn("grant select, insert, update, delete", sql)
        self.assertIn("anon", sql)


class TheDriverHubKeyboardTest(unittest.TestCase):
    """"add a lead, receipts, help" — on the hub, and only there."""

    def _hub(self):
        i = BOT_SRC.index("def _driver_hub_keyboard")
        return BOT_SRC[i:BOT_SRC.index("\ndef ", i + 10)]

    def test_the_hub_offers_the_three_buttons_in_order(self):
        hub = self._hub()
        names = ("_DRIVER_ADD_LEAD_BTN", "_DRIVER_RECEIPTS_BTN", "_DRIVER_HELP_BTN")
        for btn in names:
            self.assertIn(btn, hub, btn)
        order = [hub.index(b) for b in names]
        self.assertEqual(order, sorted(order))

    def test_the_welcome_message_uses_it(self):
        i = BOT_SRC.index('lines = [f"Welcome back, {driver_nm}! 🚗"]')
        self.assertIn("_driver_hub_keyboard()", BOT_SRC[i:i + 2200])

    def test_the_receipts_button_opens_the_receipts_list(self):
        """Same callback as /receipts — not a second implementation of it."""
        m = re.search(r'_DRIVER_RECEIPTS_BTN\s*=\s*InlineKeyboardButton\(\s*"([^"]+)",'
                      r'\s*callback_data="([^"]+)"', BOT_SRC)
        self.assertIsNotNone(m)
        self.assertIn("Receipt", m.group(1))
        self.assertEqual("driver_add_receipt", m.group(2))

    def test_that_button_still_works_after_a_redeploy(self):
        """This bot has no PTB persistence: a button only reachable from inside
        a conversation state goes dead on the next deploy. That callback is an
        ENTRY POINT, so it survives."""
        self.assertGreaterEqual(BOT_SRC.count(
            'CallbackQueryHandler(handle_driver_add_receipt_callback, '
            'pattern="^driver_add_receipt$")'), 2)

    def test_the_follow_up_keyboard_is_still_a_single_action(self):
        """Deliberate, and documented as such: a receipt button must not ride
        along on every message the bot sends."""
        i = BOT_SRC.index("def _driver_add_lead_keyboard_only")
        block = BOT_SRC[i:BOT_SRC.index("\ndef ", i + 10)]
        self.assertNotIn("_DRIVER_RECEIPTS_BTN", block)
        self.assertNotIn("_DRIVER_ADD_RECEIPT_BTN", block)

    def test_the_hub_text_points_at_the_buttons(self):
        i = BOT_SRC.index('lines = [f"Welcome back, {driver_nm}! 🚗"]')
        block = BOT_SRC[i:i + 2200]
        self.assertIn("Tap a button below", block)
        self.assertIn("/receipts", block)


if __name__ == "__main__":
    unittest.main()
