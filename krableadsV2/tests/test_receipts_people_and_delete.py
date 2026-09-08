r"""tristatetags.com/receipts — the Drivers & Supervisors tab, and deleting a lead.

Asked for: "add a new tab for drivers & superviosrs / users can
add/suspened/unsuspend/remove receipt or delete driver and add or remove
supervisors(basically all operaions available for drivers and supervsiors) also
allow deleting a lead / once a lead is delted ask the reason, duplicate,
mistake, test, cancelled and send broadcast to all groups that the lead and all
its trails were delted by user / show who it was assigned to the refrence id and
name of client and reason for deleting".

Two things these tests exist to hold still:

* The board and the bot must mean the SAME THING by "suspended" and by
  "waived". They are different classes with different clients
  (AdminDatabase vs utils.database.Database), so nothing but a test stops one
  of them drifting onto its own storage — a suspension the bot cannot see is
  a driver who keeps getting work.
* Deleting is a FLAG, never a DELETE. Money has been taken against these rows
  and drivers owe receipts for them.

Run:  venv\Scripts\python.exe -m pytest tests/test_receipts_people_and_delete.py -q
"""
import inspect
import json
import os
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

LEAD = "11111111-2222-3333-4444-555555555555"
DRIVER = "22222222-3333-4444-5555-666666666666"

DRIVERS = [
    {"id": DRIVER, "driver_name": "Kita", "driver_telegram_id": "111222333",
     "phone_number": "732-555-0000", "is_active": True},
    {"id": "d-2", "driver_name": "Marco", "driver_telegram_id": "444555666",
     "is_active": False},
]

LEAD_ROW = {"id": LEAD, "reference_id": "LAB4CDVZ",
            "vehicle_details": "John Damian\n2017 M Benz\nVIN 1HG"}

# What get_transmissions(deleted=True) hands back: a whole board row, plus the
# deletion. The Deleted list is meant to answer "was this right?", which needs
# the issuer and the driver, not just a reference.
DELETED_ROW = {
    "lead_id": LEAD, "reference_id": "LAB4CDVZ", "client_name": "John Damian",
    "car": "2017 M Benz", "price": "$150", "issuer": "kingkrab",
    "group_name": "HighKage", "driver_name": "Marco", "driver_pending": False,
    "status": "new", "created_at": "2026-09-01T09:00:00+00:00",
    "has_receipt": True, "vin": "1HG",
    "deleted_at": "2026-09-08T10:00:00+00:00",
    "deleted_reason": "duplicate", "deleted_by": "kita",
}


def _fake_db(**over):
    db = mock.MagicMock()
    db.get_all_drivers.return_value = list(DRIVERS)
    db.get_manually_suspended_driver_ids.return_value = {"d-2"}
    db._get_all_pending_receipts_per_driver.return_value = [
        {"driver_id": DRIVER, "lead_id": LEAD, "reference_id": "LAB4CDVZ"}]
    db.get_setting.return_value = json.dumps([{"id": "999888777", "label": "Boss"}])
    db.set_setting.return_value = True
    db.lead_deletion_ready.return_value = True
    db.get_lead_by_id.return_value = dict(LEAD_ROW)
    db.soft_delete_lead.return_value = True
    db.get_lead_assignment_status.return_value = {
        "driver": {"driver_name": "Kita"}}
    # Board rows, because get_deleted_leads reads through get_transmissions.
    db.get_deleted_leads.return_value = [DELETED_ROW]
    db.restore_lead.return_value = True
    db.get_all_groups.return_value = [
        {"group_telegram_id": "-1001", "is_active": True, "group_name": "HighKage"},
        {"group_telegram_id": "-1002", "is_active": True, "group_name": "Second"},
        {"group_telegram_id": "-1003", "is_active": False, "group_name": "Retired"},
    ]
    for k, v in over.items():
        getattr(db, k).return_value = v
    return db


class _Signed(unittest.TestCase):
    """A signed-in board client. Everything below /receipts needs the gate."""

    def setUp(self):
        # SUPERVISORY_TELEGRAM_ID is read live, so pin it: these tests must not
        # depend on whoever's .env happens to be loaded.
        self._env = mock.patch.object(receipts_page, "_env_supervisors",
                                      return_value=[{"id": "1253370362", "label": "",
                                                     "fixed": True}])
        self._env.start()
        self.addCleanup(self._env.stop)
        ad.app.config["TESTING"] = True
        self.client = ad.app.test_client()
        self.client.post("/receipts/login",
                         data={"password": receipts_page._receipts_password()})

    def call(self, method, path, db=None, **kw):
        with mock.patch.object(ad, "db", db if db is not None else _fake_db()):
            return getattr(self.client, method)(path, **kw)


class TheTabIsOnTheBoardTest(_Signed):
    """The markup, so the tab cannot be half-wired again."""

    def setUp(self):
        super().setUp()
        self.body = self.client.get("/receipts").get_data(as_text=True)

    def test_the_people_tab_exists_and_has_somewhere_to_render(self):
        self.assertIn('data-view="people"', self.body)
        self.assertIn('id="vw-people"', self.body)
        self.assertIn("renderPeople", self.body)

    def test_people_survives_a_reload(self):
        """BOTH view allowlists must know it.

        There are two: the one that validates the remembered view on boot and
        the one the voice command checks. Miss either and the tab silently
        snaps back to the table.
        """
        allowlists = [ln for ln in self.body.splitlines()
                      if '"crm"' in ln and '"table"' in ln and "includes" in ln]
        self.assertGreaterEqual(len(allowlists), 2, allowlists)
        for ln in allowlists:
            self.assertIn('"people"', ln, ln)

    def test_the_people_buttons_do_not_collide_with_the_board_handler(self):
        """The document-level handler already owns .act and .clr.

        People buttons carry their own p- names; an unprefixed .act inside the
        tab would be caught by the board's row handler instead.
        """
        for cls in ("p-susp", "p-act", "p-waive", "p-delv", "p-dels"):
            self.assertIn(cls, self.body, cls)
        self.assertNotIn('class="fixbtn act"', self.body)

    def test_every_operation_is_offered(self):
        for needle in ("/people", "/drivers", "/suspend", "/active", "/waive",
                       "/supervisors", "Add driver", "Add supervisor"):
            self.assertIn(needle, self.body, needle)

    def test_deleting_asks_why_with_the_reasons_that_were_asked_for(self):
        for value in ("duplicate", "mistake", "test", "cancelled"):
            self.assertIn('value="%s"' % value, self.body, value)
        self.assertIn('id="del"', self.body)
        self.assertIn("dellead", self.body)
        self.assertIn("/delete", self.body)


class ThePeopleFeedTest(_Signed):

    def test_it_answers_with_everyone_and_what_they_owe(self):
        r = self.call("get", "/receipts/api/people")
        self.assertEqual(200, r.status_code)
        got = r.get_json()
        kita = [d for d in got["drivers"] if d["id"] == DRIVER][0]
        marco = [d for d in got["drivers"] if d["id"] == "d-2"][0]
        self.assertTrue(kita["active"])
        self.assertFalse(kita["suspended"])
        self.assertEqual([{"lead_id": LEAD, "reference_id": "LAB4CDVZ"}],
                         kita["owed"])
        self.assertFalse(marco["active"])
        self.assertTrue(marco["suspended"])
        self.assertEqual(
            [{"id": "1253370362", "label": "", "fixed": True},
             {"id": "999888777", "label": "Boss", "fixed": False}],
            got["supervisors"])
        self.assertTrue(got["can_delete_lead"])

    def test_a_broken_suspension_read_does_not_take_the_tab_down(self):
        db = _fake_db()
        db.get_manually_suspended_driver_ids.side_effect = RuntimeError("42703")
        db._get_all_pending_receipts_per_driver.side_effect = RuntimeError("nope")
        r = self.call("get", "/receipts/api/people", db=db)
        self.assertEqual(200, r.status_code)
        self.assertEqual(2, len(r.get_json()["drivers"]))

    def test_the_tab_is_behind_the_password(self):
        fresh = ad.app.test_client()
        for method, path in (("get", "/receipts/api/people"),
                             ("post", "/receipts/api/drivers"),
                             ("delete", "/receipts/api/drivers/x"),
                             ("post", "/receipts/api/supervisors"),
                             ("post", "/receipts/api/transmissions/%s/delete" % LEAD)):
            r = getattr(fresh, method)(path)
            self.assertEqual(401, r.status_code, path)

    def test_none_of_it_is_reachable_on_the_open_api_alias(self):
        """/api/* has no login gate.

        Most of the board's own reads are aliased there for convenience. These
        endpoints are not: they change who gets sent work and who may approve
        it, so every rule that reaches them has to sit under /receipts/.
        (/api/drivers is an older dashboard route and is somebody else's
        problem — this only guards the ones registered here.)
        """
        mine = {"api_people", "api_add_driver", "api_suspend_driver",
                "api_toggle_driver_active", "api_delete_driver",
                "api_waive_receipt", "api_add_supervisor",
                "api_remove_supervisor", "api_delete_lead"}
        for rule in ad.app.url_map.iter_rules():
            if rule.endpoint in mine:
                self.assertTrue(str(rule).startswith("/receipts/"),
                                "%s -> %s" % (rule.endpoint, rule))
        registered = {r.endpoint for r in ad.app.url_map.iter_rules()}
        self.assertTrue(mine <= registered, mine - registered)


class DriverOperationsTest(_Signed):

    def test_a_driver_is_added_with_a_name_and_a_telegram_id(self):
        db = _fake_db()
        db.get_driver_by_telegram_id.return_value = None
        db.create_driver.return_value = True
        r = self.call("post", "/receipts/api/drivers", db=db,
                      json={"name": "New Guy", "telegram_id": "123456789",
                            "phone": "732-555-1212"})
        self.assertEqual(200, r.status_code, r.get_data(as_text=True))
        db.create_driver.assert_called_once()
        self.assertEqual(("New Guy", "123456789"), db.create_driver.call_args[0][:2])

    def test_a_name_without_a_telegram_id_is_refused(self):
        for body in ({"name": "New Guy", "telegram_id": ""},
                     {"name": "", "telegram_id": "123456789"},
                     {"name": "New Guy", "telegram_id": "@handle"}):
            r = self.call("post", "/receipts/api/drivers", json=body)
            self.assertEqual(400, r.status_code, body)

    def test_the_same_person_is_not_added_twice(self):
        db = _fake_db()
        db.get_driver_by_telegram_id.return_value = DRIVERS[0]
        r = self.call("post", "/receipts/api/drivers", db=db,
                      json={"name": "Kita again", "telegram_id": "111222333"})
        self.assertEqual(409, r.status_code)
        db.create_driver.assert_not_called()

    def test_suspend_and_unsuspend(self):
        db = _fake_db()
        db.set_driver_suspended.return_value = True
        r = self.call("post", "/receipts/api/drivers/%s/suspend" % DRIVER, db=db,
                      json={"suspended": True})
        self.assertEqual(200, r.status_code)
        db.set_driver_suspended.assert_called_with(DRIVER, True)

        db.reset_mock()
        db.set_driver_suspended.return_value = True
        self.call("post", "/receipts/api/drivers/%s/suspend" % DRIVER, db=db,
                  json={"suspended": False})
        db.set_driver_suspended.assert_called_with(DRIVER, False)

    def test_a_suspension_the_database_refuses_is_reported_not_swallowed(self):
        db = _fake_db()
        db.set_driver_suspended.return_value = False
        r = self.call("post", "/receipts/api/drivers/%s/suspend" % DRIVER, db=db,
                      json={"suspended": True})
        self.assertEqual(500, r.status_code)

    def test_activating_and_deactivating(self):
        db = _fake_db()
        db.toggle_driver_status.return_value = True
        r = self.call("post", "/receipts/api/drivers/%s/active" % DRIVER, db=db)
        self.assertEqual(200, r.status_code)
        db.toggle_driver_status.assert_called_with(DRIVER)

    def test_a_driver_with_history_is_not_deleted(self):
        db = _fake_db()
        db.delete_driver.return_value = (False, "This driver has 4 lead(s) on record.")
        r = self.call("delete", "/receipts/api/drivers/%s" % DRIVER, db=db)
        self.assertEqual(409, r.status_code)
        self.assertIn("4 lead(s)", r.get_json()["error"])

    def test_a_driver_who_never_worked_can_be_deleted(self):
        db = _fake_db()
        db.delete_driver.return_value = (True, "")
        r = self.call("delete", "/receipts/api/drivers/%s" % DRIVER, db=db)
        self.assertEqual(200, r.status_code)

    def test_clearing_what_a_driver_owes(self):
        """'remove receipt' = stop it counting toward the five."""
        db = _fake_db()
        db.set_lead_excluded.return_value = True
        r = self.call("post", "/receipts/api/drivers/%s/waive" % DRIVER, db=db,
                      json={"lead_id": LEAD})
        self.assertEqual(200, r.status_code)
        db.set_lead_excluded.assert_called_with(LEAD, True)

    def test_waiving_nothing_in_particular_is_refused(self):
        r = self.call("post", "/receipts/api/drivers/%s/waive" % DRIVER, json={})
        self.assertEqual(400, r.status_code)


class SupervisorOperationsTest(_Signed):

    def test_adding_one_keeps_the_existing_list(self):
        db = _fake_db()
        r = self.call("post", "/receipts/api/supervisors", db=db,
                      json={"id": "555444333", "label": "Nana"})
        self.assertEqual(200, r.status_code, r.get_data(as_text=True))
        key, raw = db.set_setting.call_args[0]
        self.assertEqual(receipts_page._SUPERVISORS_KEY, key)
        # Only the extras are written back — the environment's are not ours to save.
        self.assertEqual([{"id": "999888777", "label": "Boss", "fixed": False},
                          {"id": "555444333", "label": "Nana"}], json.loads(raw))

    def test_adding_the_same_id_twice_replaces_rather_than_duplicates(self):
        db = _fake_db()
        self.call("post", "/receipts/api/supervisors", db=db,
                  json={"id": "999888777", "label": "Boss renamed"})
        self.assertEqual([{"id": "999888777", "label": "Boss renamed"}],
                         json.loads(db.set_setting.call_args[0][1]))

    def test_a_supervisor_the_environment_owns_cannot_be_removed_here(self):
        """A web page cannot edit a service environment variable. Saying where to
        change it beats a Remove button that appears to work and does not."""
        db = _fake_db()
        r = self.call("delete", "/receipts/api/supervisors/1253370362", db=db)
        self.assertEqual(409, r.status_code)
        self.assertIn("SUPERVISORY_TELEGRAM_ID", r.get_json()["error"])
        db.set_setting.assert_not_called()

    def test_adding_one_the_environment_already_has_is_refused(self):
        db = _fake_db()
        r = self.call("post", "/receipts/api/supervisors", db=db,
                      json={"id": "1253370362", "label": "Boss"})
        self.assertEqual(409, r.status_code)
        db.set_setting.assert_not_called()

    def test_the_list_is_never_empty_when_the_environment_has_one(self):
        """The bug: the tab read only the extras, which are empty on this system,
        so a board with a supervisor showed none."""
        db = _fake_db()
        db.get_setting.return_value = None
        got = self.call("get", "/receipts/api/people", db=db).get_json()
        self.assertEqual([{"id": "1253370362", "label": "", "fixed": True}],
                         got["supervisors"])

    def test_a_supervisor_needs_a_numeric_id(self):
        r = self.call("post", "/receipts/api/supervisors",
                      json={"id": "@boss", "label": "Boss"})
        self.assertEqual(400, r.status_code)

    def test_removing_one(self):
        db = _fake_db()
        r = self.call("delete", "/receipts/api/supervisors/999888777", db=db)
        self.assertEqual(200, r.status_code)
        self.assertEqual([], json.loads(db.set_setting.call_args[0][1]))

    def test_the_board_writes_the_key_the_bot_reads(self):
        """One list, not two. The bot's /settings must show the same people."""
        import bot as _bot  # noqa: F401  (imported for the constant only)
        self.assertEqual(getattr(_bot, "EXTRA_SUPERVISORS_KEY", None)
                         or receipts_page._SUPERVISORS_KEY,
                         receipts_page._SUPERVISORS_KEY)


class DeletingALeadTest(_Signed):

    def test_it_asks_why_first(self):
        r = self.call("post", "/receipts/api/transmissions/%s/delete" % LEAD,
                      json={"by": "kita"})
        self.assertEqual(400, r.status_code)

    def test_without_the_migration_it_refuses_instead_of_half_deleting(self):
        db = _fake_db()
        db.lead_deletion_ready.return_value = False
        r = self.call("post", "/receipts/api/transmissions/%s/delete" % LEAD, db=db,
                      json={"reason": "duplicate", "by": "kita"})
        self.assertEqual(503, r.status_code)
        self.assertIn("migration_lead_deleted.sql", r.get_json()["error"])
        db.soft_delete_lead.assert_not_called()

    def test_it_flags_the_lead_and_never_deletes_the_row(self):
        db = _fake_db()
        with mock.patch("requests.post") as post,                 mock.patch("config.Config.TELEGRAM_BOT_TOKEN", "test-token"):
            post.return_value = mock.MagicMock(ok=True, json=lambda: {"ok": True})
            r = self.call("post", "/receipts/api/transmissions/%s/delete" % LEAD,
                          db=db, json={"reason": "duplicate", "by": "kita"})
        self.assertEqual(200, r.status_code, r.get_data(as_text=True))
        db.soft_delete_lead.assert_called_once_with(LEAD, "duplicate", "kita")
        # Nothing anywhere may reach for a real delete on leads.
        for name in ("delete_lead", "hard_delete_lead"):
            self.assertFalse(getattr(db, name).called, name)

    def test_a_broadcast_that_reached_nobody_says_why(self):
        """"nothing happened no telegram broadcast" — because a bare 0 was all
        four failures at once. Each one now names itself."""
        db = _fake_db()
        with mock.patch("config.Config.TELEGRAM_BOT_TOKEN", ""):
            report = receipts_page._broadcast_lead_deleted(db, LEAD_ROW, "test", "kita")
        self.assertEqual(0, report["told"])
        self.assertIn("TELEGRAM_BOT_TOKEN", report["error"])

    def test_a_team_that_refuses_the_message_is_named(self):
        db = _fake_db()
        def _send(url, **kw):
            cid = kw["json"]["chat_id"]
            ok = cid == "-1001"
            return mock.MagicMock(
                ok=ok, status_code=200 if ok else 400, text="",
                json=lambda: ({"ok": True} if ok
                              else {"ok": False, "description": "Bad Request: chat not found"}))
        with mock.patch("requests.post", side_effect=_send),                 mock.patch("config.Config.TELEGRAM_BOT_TOKEN", "test-token"):
            report = receipts_page._broadcast_lead_deleted(db, LEAD_ROW, "test", "kita")
        self.assertEqual(1, report["told"])
        self.assertEqual(1, len(report["failed"]))
        self.assertEqual("Second", report["failed"][0]["name"])
        self.assertIn("chat not found", report["failed"][0]["why"])

    def test_no_team_has_a_chat_id_at_all(self):
        db = _fake_db()
        db.get_all_groups.return_value = [{"group_name": "Nowhere", "is_active": True}]
        with mock.patch("requests.post") as post,                 mock.patch("config.Config.TELEGRAM_BOT_TOKEN", "test-token"):
            report = receipts_page._broadcast_lead_deleted(db, LEAD_ROW, "test", "kita")
        post.assert_not_called()
        self.assertIn("nobody to tell", report["error"])

    def test_the_delete_answer_carries_the_report(self):
        db = _fake_db()
        def _send(url, **kw):
            return mock.MagicMock(ok=False, status_code=403, text="",
                                  json=lambda: {"ok": False, "description": "bot was kicked"})
        with mock.patch("requests.post", side_effect=_send),                 mock.patch("config.Config.TELEGRAM_BOT_TOKEN", "test-token"):
            r = self.call("post", "/receipts/api/transmissions/%s/delete" % LEAD,
                          db=db, json={"reason": "duplicate", "by": "kita"})
        body = r.get_json()
        self.assertEqual(200, r.status_code)
        self.assertEqual(0, body["groups_notified"])
        self.assertEqual(2, len(body["groups_failed"]))
        self.assertIn("kicked", body["groups_failed"][0]["why"])

    def test_the_board_shows_what_the_report_said(self):
        body = self.client.get("/receipts").get_data(as_text=True)
        for needle in ("no team was told", "broadcast_error", "groups_failed",
                       "Could not tell"):
            self.assertIn(needle, body, needle)

    def test_every_active_team_is_told_once(self):
        db = _fake_db()
        with mock.patch("requests.post") as post:
            post.return_value = mock.MagicMock(ok=True, json=lambda: {"ok": True})
            with mock.patch("config.Config.TELEGRAM_BOT_TOKEN", "test-token"):
                r = self.call("post", "/receipts/api/transmissions/%s/delete" % LEAD,
                              db=db, json={"reason": "test", "by": "kita"})
        sent = [c for c in post.call_args_list if "sendMessage" in c[0][0]]
        chat_ids = [c[1]["json"]["chat_id"] for c in sent]
        self.assertEqual(["-1001", "-1002"], chat_ids)   # the retired one is skipped
        self.assertEqual(2, r.get_json()["groups_notified"])

    def test_the_broadcast_says_who_had_it_the_reference_the_client_and_why(self):
        text = receipts_page._deleted_lead_notice(
            LEAD_ROW, "duplicate", "kita", "Marco")
        for needle in ("LAB4CDVZ",        # the reference id
                       "John Damian",     # the client, off the stored card
                       "Marco",           # who it was assigned to
                       "duplicate",       # why
                       "kita"):           # who deleted it
            self.assertIn(needle, text, needle)
        self.assertIn("trails", text)

    def test_a_lead_nobody_took_says_so_rather_than_naming_someone(self):
        db = _fake_db()
        db.get_lead_assignment_status.return_value = None
        text = receipts_page._deleted_lead_notice(
            LEAD_ROW, "mistake", "kita",
            receipts_page._deleted_lead_driver_name(db, LEAD_ROW))
        self.assertIn("nobody yet", text)

    def test_a_lead_that_is_already_gone_is_not_broadcast_twice(self):
        db = _fake_db()
        db.soft_delete_lead.return_value = False
        with mock.patch("requests.post") as post:
            r = self.call("post", "/receipts/api/transmissions/%s/delete" % LEAD,
                          db=db, json={"reason": "duplicate", "by": "kita"})
        self.assertEqual(409, r.status_code)
        post.assert_not_called()

    def test_a_lead_that_never_existed_is_a_404(self):
        db = _fake_db()
        db.get_lead_by_id.return_value = None
        r = self.call("post", "/receipts/api/transmissions/%s/delete" % LEAD, db=db,
                      json={"reason": "duplicate", "by": "kita"})
        self.assertEqual(404, r.status_code)

    def test_the_html_in_a_reason_cannot_break_the_broadcast(self):
        text = receipts_page._deleted_lead_notice(
            {"reference_id": "R<1>", "vehicle_details": "A & B"},
            "<b>oops</b>", "<i>me</i>", "D&D")
        self.assertNotIn("<b>oops</b>", text)
        self.assertIn("&lt;b&gt;oops&lt;/b&gt;", text)
        self.assertIn("A &amp; B", text)


class PuttingADeletedLeadBackTest(_Signed):
    """A flag that cannot be cleared from the office is a delete."""

    def test_the_office_can_see_what_was_deleted(self):
        r = self.call("get", "/receipts/api/deleted")
        self.assertEqual(200, r.status_code)
        row = r.get_json()["rows"][0]
        self.assertEqual("LAB4CDVZ", row["reference_id"])
        self.assertEqual("John Damian", row["client_name"])
        self.assertEqual("duplicate", row["reason"])
        self.assertEqual("kita", row["by"])
        self.assertEqual("2026-09-08T10:00:00+00:00", row["at"])

    def test_a_deleted_lead_keeps_everything_needed_to_judge_the_deletion(self):
        """"show who it was assigned to the refrence id and name of client" — and
        the rest of it, because "was this right?" is not answerable from a
        reference and a reason alone."""
        row = self.call("get", "/receipts/api/deleted").get_json()["rows"][0]
        self.assertEqual("kingkrab", row["issuer"])
        self.assertEqual("Marco", row["driver_name"])
        self.assertEqual("HighKage", row["group_name"])
        self.assertEqual("2017 M Benz", row["car"])
        self.assertEqual("$150", row["price"])
        self.assertEqual("new", row["status"])
        self.assertTrue(row["has_receipt"])
        self.assertIn("created_at", row)

    def test_an_unmigrated_database_shows_nothing_rather_than_an_error(self):
        db = _fake_db()
        db.get_deleted_leads.return_value = []
        r = self.call("get", "/receipts/api/deleted", db=db)
        self.assertEqual(200, r.status_code)
        self.assertEqual([], r.get_json()["rows"])

    def test_one_can_be_put_back(self):
        db = _fake_db()
        r = self.call("post", "/receipts/api/transmissions/%s/restore" % LEAD, db=db)
        self.assertEqual(200, r.status_code)
        db.restore_lead.assert_called_once_with(LEAD)

    def test_putting_one_back_tells_nobody(self):
        """The teams were told not to work it. Telling them it is back is only
        useful if somebody is about to work it, and the office re-sends it the
        normal way when that is what it wants."""
        db = _fake_db()
        with mock.patch("requests.post") as post:
            self.call("post", "/receipts/api/transmissions/%s/restore" % LEAD, db=db)
        post.assert_not_called()

    def test_a_refused_restore_is_reported(self):
        db = _fake_db()
        db.restore_lead.return_value = False
        r = self.call("post", "/receipts/api/transmissions/%s/restore" % LEAD, db=db)
        self.assertEqual(500, r.status_code)

    def test_both_are_behind_the_password(self):
        fresh = ad.app.test_client()
        self.assertEqual(401, fresh.get("/receipts/api/deleted").status_code)
        self.assertEqual(401, fresh.post(
            "/receipts/api/transmissions/%s/restore" % LEAD).status_code)

    def test_the_tab_offers_it(self):
        body = self.client.get("/receipts").get_data(as_text=True)
        for needle in ("Deleted leads", "p-undel", "/restore", "Put it back"):
            self.assertIn(needle, body, needle)


class TheBoardAndTheBotAgreeTest(unittest.TestCase):
    """The two Database classes are separate. These are the seams that must match."""

    def test_a_deleted_lead_is_gone_from_the_board(self):
        src = inspect.getsource(ad.AdminDatabase.get_transmissions)
        self.assertIn("deleted_at", src)
        self.assertIn('is_("deleted_at", "null")', src)

    def test_suspension_lives_in_the_same_column_for_both(self):
        """drivers.is_suspended — not a settings blob, not a second table.

        The bot decides who to offer work to from this column. Anything else
        here and the board would 'suspend' somebody who keeps getting leads.
        """
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_people_real_udb", str(ROOT / "utils" / "database.py"))
        udb = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(udb)

        for cls in (ad.AdminDatabase, udb.Database):
            for name in ("set_driver_suspended", "get_manually_suspended_driver_ids"):
                src = inspect.getsource(getattr(cls, name))
                self.assertIn("is_suspended", src, "%s.%s" % (cls.__name__, name))
                self.assertIn('table("drivers")', src,
                              "%s.%s" % (cls.__name__, name))

    def test_waiving_a_receipt_uses_the_same_flag_the_bot_counts(self):
        src = inspect.getsource(ad.AdminDatabase.set_lead_excluded)
        self.assertIn("exclude_from_count", src)

    def test_deleting_a_driver_refuses_when_they_have_worked(self):
        """lead_assignments.driver_id is ON DELETE CASCADE.

        Deleting a driver who has accepted anything would take their accepts —
        and the receipts they owe — with them.
        """
        for cls in (ad.AdminDatabase,):
            src = inspect.getsource(cls.delete_driver)
            self.assertIn("count_driver_assignments", src)

    def test_both_handles_can_undo_a_deletion(self):
        """The bot has to be able to undo one too — it is the same flag."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_people_real_udb2", str(ROOT / "utils" / "database.py"))
        udb = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(udb)
        for cls in (ad.AdminDatabase, udb.Database):
            src = inspect.getsource(cls.restore_lead)
            self.assertIn('"deleted_at": None', src, cls.__name__)

    def test_health_says_whether_this_service_can_broadcast(self):
        """The notice is sent from THIS service with THIS token. Whether the token
        landed was unanswerable from outside, which is why a delete that told
        nobody looked identical to one that worked."""
        r = ad.app.test_client().get("/api/health")
        self.assertEqual(200, r.status_code)
        self.assertIn("telegram_bot_token", r.get_json())

    def test_the_deleted_view_never_answers_with_live_leads(self):
        """The lean fallback cannot name deleted_at. Answering a deleted query
        with it would file every live lead under Deleted leads."""
        src = inspect.getsource(ad.AdminDatabase.get_transmissions)
        self.assertIn("if deleted:", src)
        self.assertIn("return []", src)
        self.assertIn('not_.is_("deleted_at", "null")', src)

    def test_the_migration_says_what_it_adds(self):
        sql = (ROOT / "database" / "migration_lead_deleted.sql").read_text(
            encoding="utf-8").lower()
        for col in ("deleted_at", "deleted_reason", "deleted_by"):
            self.assertIn(col, sql, col)
        self.assertIn("add column", sql)


if __name__ == "__main__":
    unittest.main()
