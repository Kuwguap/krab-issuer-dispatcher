r"""The board's Check in section, and the driver hub's Receipts button.

Asked for: "drivers get a different message that looks like 'welcome back,
john / you owe.... / to add a lead, type /lead....' it has inline buttons /
lets add more / add a receipts inline button and receipts / so inline buttons
are now: add a lead, receipts, help" and "for tristatetags.com/receipts at the
very end add CHECK IN SECTION / HERE LOGGED IN USERS CAN CHECK AND SEE THOSE
WHO HAVE CHECKED AND THE TIME THIS INDICATES THE TRANSACTION OR LEAD HAS BEEN
CROSSCHECKED BY THEM".

A check-in is an audit record, so the two things that would make it worthless
are pinned here: the time is the SERVER's, and a lead count that hit the
reader's cap says so rather than passing "1000" off as the whole board.

Run:  venv\Scripts\python.exe -m pytest tests/test_checkin_and_driver_hub.py -q
"""
import json
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


def _fake_db(log=None, leads=3):
    db = mock.MagicMock()
    store = {receipts_page._CHECKINS_KEY: json.dumps(log) if log is not None else ""}

    def get_setting(key):
        return store.get(key, "")

    def set_setting(key, value):
        store[key] = value
        return True

    db.get_setting.side_effect = get_setting
    db.set_setting.side_effect = set_setting
    db.get_transmissions.return_value = [
        {"reference_id": "REF%d" % i} for i in range(leads)]
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


class TheCheckInSectionTest(_Signed):

    def test_it_is_at_the_very_end_of_the_board(self):
        body = self.client.get("/receipts").get_data(as_text=True)
        self.assertIn('id="checkin"', body)
        # After every view, and outside <main> — a cross-check is of the board,
        # not of whichever view happens to be open.
        self.assertGreater(body.index('id="checkin"'), body.index("</main>"))
        for needle in ("Check in", "ci-go", "ci-note", "ci-list",
                       "loadCheckins", "cross-checked"):
            self.assertIn(needle, body, needle)

    def test_it_loads_on_boot(self):
        body = self.client.get("/receipts").get_data(as_text=True)
        self.assertRegex(body, r"load\(\);\s*\n\s*loadCheckins\(\);")

    def test_nobody_has_checked_in_yet(self):
        r = self.call("get", "/receipts/api/checkins")
        self.assertEqual(200, r.status_code)
        self.assertEqual([], r.get_json()["rows"])

    def test_checking_in_records_who_and_what_they_saw(self):
        db = _fake_db(leads=7)
        r = self.call("post", "/receipts/api/checkins", db=db,
                      json={"who": "Kita", "note": "all matched"})
        self.assertEqual(200, r.status_code, r.get_data(as_text=True))
        row = r.get_json()["rows"][0]
        self.assertEqual("Kita", row["who"])
        self.assertEqual("all matched", row["note"])
        self.assertEqual(7, row["leads"])
        self.assertEqual("REF0", row["newest"])
        self.assertFalse(row["leads_capped"])
        self.assertTrue(row["at"])

    def test_the_time_is_the_servers_and_not_the_browsers(self):
        """A check-in is a claim about when somebody looked. A browser clock an
        hour out would file that claim in the wrong place in the log."""
        db = _fake_db()
        r = self.call("post", "/receipts/api/checkins", db=db,
                      json={"who": "Kita", "at": "1999-01-01T00:00:00+00:00"})
        self.assertNotIn("1999", r.get_json()["rows"][0]["at"])

    def test_a_full_reader_says_so_rather_than_rounding_the_truth(self):
        """The reader caps at 1000. "1000 leads" in an audit log would quietly
        mean "at least 1000"."""
        db = _fake_db(leads=1000)
        row = self.call("post", "/receipts/api/checkins", db=db,
                        json={"who": "Kita"}).get_json()["rows"][0]
        self.assertEqual(1000, row["leads"])
        self.assertTrue(row["leads_capped"])
        body = self.client.get("/receipts").get_data(as_text=True)
        self.assertIn('r.leads_capped ? "+" : ""', body)

    def test_a_check_in_with_no_name_is_refused(self):
        db = _fake_db()
        r = self.call("post", "/receipts/api/checkins", db=db, json={"note": "hi"})
        self.assertEqual(400, r.status_code)
        db.set_setting.assert_not_called()

    def test_check_ins_stack_newest_first(self):
        db = _fake_db(log=[{"who": "Older", "at": "2026-09-01T00:00:00+00:00"}])
        rows = self.call("post", "/receipts/api/checkins", db=db,
                         json={"who": "Newer"}).get_json()["rows"]
        self.assertEqual(["Newer", "Older"], [r["who"] for r in rows])

    def test_the_log_cannot_grow_without_bound(self):
        many = [{"who": "n%d" % i, "at": "2026-09-01T00:00:00+00:00"}
                for i in range(receipts_page._CHECKINS_MAX + 40)]
        db = _fake_db(log=many)
        rows = self.call("post", "/receipts/api/checkins", db=db,
                         json={"who": "Newest"}).get_json()["rows"]
        self.assertEqual(receipts_page._CHECKINS_MAX, len(rows))
        self.assertEqual("Newest", rows[0]["who"])
        self.assertEqual(receipts_page._CHECKINS_MAX,
                         len(json.loads(db._store[receipts_page._CHECKINS_KEY])))

    def test_a_board_it_cannot_size_still_records_the_check_in(self):
        """The point is who looked and when. What they saw is best effort."""
        db = _fake_db()
        db.get_transmissions.side_effect = RuntimeError("no")
        r = self.call("post", "/receipts/api/checkins", db=db, json={"who": "Kita"})
        self.assertEqual(200, r.status_code)
        self.assertEqual(0, r.get_json()["rows"][0]["leads"])

    def test_a_corrupt_log_does_not_take_the_section_down(self):
        db = _fake_db()
        db._store[receipts_page._CHECKINS_KEY] = "{not json"
        r = self.call("get", "/receipts/api/checkins", db=db)
        self.assertEqual(200, r.status_code)
        self.assertEqual([], r.get_json()["rows"])

    def test_both_ends_are_behind_the_password(self):
        fresh = ad.app.test_client()
        self.assertEqual(401, fresh.get("/receipts/api/checkins").status_code)
        self.assertEqual(401, fresh.post("/receipts/api/checkins",
                                         json={"who": "x"}).status_code)

    def test_it_is_not_on_the_open_api_alias(self):
        rules = {str(r) for r in ad.app.url_map.iter_rules()
                 if r.endpoint in ("api_checkins", "api_check_in")}
        self.assertTrue(rules)
        for rule in rules:
            self.assertTrue(rule.startswith("/receipts/"), rule)

    def test_a_name_or_note_cannot_carry_markup_into_the_log(self):
        db = _fake_db()
        row = self.call("post", "/receipts/api/checkins", db=db,
                        json={"who": "<b>K</b>" * 40,
                              "note": "x" * 400}).get_json()["rows"][0]
        self.assertLessEqual(len(row["who"]), 60)
        self.assertLessEqual(len(row["note"]), 200)
        # And the page escapes it rather than trusting the cap.
        body = self.client.get("/receipts").get_data(as_text=True)
        self.assertIn("esc(r.who", body)
        self.assertIn("esc(r.note)", body)


class TheDriverHubKeyboardTest(unittest.TestCase):
    """"add a lead, receipts, help" — on the hub, and only there."""

    def _hub(self):
        i = BOT_SRC.index("def _driver_hub_keyboard")
        return BOT_SRC[i:BOT_SRC.index("\ndef ", i + 10)]

    def test_the_hub_offers_the_three_buttons_in_order(self):
        hub = self._hub()
        for btn in ("_DRIVER_ADD_LEAD_BTN", "_DRIVER_RECEIPTS_BTN", "_DRIVER_HELP_BTN"):
            self.assertIn(btn, hub, btn)
        order = [hub.index(b) for b in
                 ("_DRIVER_ADD_LEAD_BTN", "_DRIVER_RECEIPTS_BTN", "_DRIVER_HELP_BTN")]
        self.assertEqual(order, sorted(order))

    def test_the_welcome_message_uses_it(self):
        i = BOT_SRC.index('lines = [f"Welcome back, {driver_nm}! 🚗"]')
        block = BOT_SRC[i:i + 2200]
        self.assertIn("_driver_hub_keyboard()", block)

    def test_the_receipts_button_opens_the_receipts_list(self):
        """Same callback as /receipts — not a second implementation of it."""
        m = re.search(r'_DRIVER_RECEIPTS_BTN\s*=\s*InlineKeyboardButton\(\s*"([^"]+)",'
                      r'\s*callback_data="([^"]+)"', BOT_SRC)
        self.assertIsNotNone(m)
        label, cb = m.group(1), m.group(2)
        self.assertIn("Receipt", label)
        self.assertEqual("driver_add_receipt", cb)

    def test_that_button_still_works_after_a_redeploy(self):
        """This bot has no PTB persistence: a button that is only reachable from
        inside a conversation state goes dead on the next deploy. The receipts
        callback is an ENTRY POINT, so it survives."""
        entries = BOT_SRC.count(
            'CallbackQueryHandler(handle_driver_add_receipt_callback, '
            'pattern="^driver_add_receipt$")')
        self.assertGreaterEqual(entries, 2, entries)

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
