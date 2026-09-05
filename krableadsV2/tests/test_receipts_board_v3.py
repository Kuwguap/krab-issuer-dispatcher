r"""The /receipts board v3 — month folders, numbering, renewal countdown, the
full status ladder with bot triggers, five views, voice commands, and the
optional asset/game modules.

Run:  venv\Scripts\python.exe -m pytest tests/test_receipts_board_v3.py -q
"""
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

_real_db_module = None


def _real_database_class():
    """The REAL Database class, no matter what other suites did.

    Several dispatch suites assign ``udb.Database = MagicMock()`` at module
    import and never restore it — so both the module attribute AND anything
    bound from it at collection time can be a mock. Load a private copy of
    the module straight from its file instead."""
    global _real_db_module
    if _real_db_module is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_receipts_v3_real_udb", str(ROOT / "utils" / "database.py"))
        _real_db_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_real_db_module)
    return _real_db_module.Database

LEAD = "11111111-2222-3333-4444-555555555555"


class TheBoardStructureTest(unittest.TestCase):

    def setUp(self):
        ad.app.config["TESTING"] = True
        self.client = ad.app.test_client()
        # /receipts now carries a password (receipts_page._receipts_password).
        # Signing in here means these tests exercise the board the way a real
        # operator reaches it, rather than around the gate.
        self.client.post("/receipts/login",
                         data={"password": receipts_page._receipts_password()})
        self.body = self.client.get("/receipts").get_data(as_text=True)

    def test_the_new_columns_exist_in_order(self):
        heads = ("<th>#</th>", "<th>Client</th>", "<th>Receipt</th>",
                 "<th>Client phone</th>", "<th>Tags</th>", "<th>Client contact</th>",
                 "<th>Driver</th>", "<th>Issuer</th>", "<th>Dispatcher</th>",
                 "<th>Renewal</th>", "<th>Status</th>")
        positions = [self.body.index(h) for h in heads]
        self.assertEqual(positions, sorted(positions))

    def test_month_folders_numbering_and_stats_are_wired(self):
        for needle in ("monthGroups", "mrow", "renderStats", "renewalDays",
                       'id="stats"', "COLLAPSED"):
            self.assertIn(needle, self.body, needle)

    def test_the_five_views_and_the_controls_exist(self):
        for needle in ('data-view="table"', 'data-view="cards"', 'data-view="sheet"',
                       'data-view="chart"', 'data-view="crm"', 'id="csv"',
                       'id="themeMount"', 'id="gamechip"', "krabVoiceAction"):
            self.assertIn(needle, self.body, needle)

    def test_the_minigame_is_wired_but_optional(self):
        self.assertIn("/receipts/asset/tetris.js", self.body)
        self.assertIn('case "play_tetris"', self.body)
        self.assertIn("window.krabTetris", self.body)

    def test_a_modal_hides_the_mic_and_pauses_the_game(self):
        """The floating mic sits above the sheets, and arrow keys must never
        steer a game hidden behind one."""
        self.assertIn("krab-modal-open", self.body)
        self.assertIn("pauseTetrisForModal", self.body)
        self.assertIn('attributeFilter: ["hidden"]', self.body)

    def test_toasts_move_out_of_the_way_of_a_game(self):
        self.assertIn("body.krab-tetris-on #toasts", self.body)
        self.assertIn("pointer-events:none", self.body)

    def test_the_phone_keeps_sixteen_pixel_inputs(self):
        """Anything smaller and iOS zooms the whole page on focus."""
        self.assertIn(".vc-input { font-size:16px", self.body)

    def test_the_page_narrates_on_the_event_bus(self):
        for needle in ("deal.won", "driver.on_the_way", "stage.advanced",
                       "goal.hit", "CustomEvent"):
            self.assertIn(needle, self.body, needle)

    def test_sticky_headers_scroll_container(self):
        self.assertIn("position:sticky", self.body)
        self.assertIn("max-height:calc(100vh", self.body)


class TheAssetsServeTest(unittest.TestCase):

    def setUp(self):
        ad.app.config["TESTING"] = True
        self.client = ad.app.test_client()
        # /receipts now carries a password (receipts_page._receipts_password).
        # Signing in here means these tests exercise the board the way a real
        # operator reaches it, rather than around the gate.
        self.client.post("/receipts/login",
                         data={"password": receipts_page._receipts_password()})

    def test_every_view_module_serves(self):
        for name, must_contain in (("sheet.js", "renderSheetView"),
                                   ("charts.js", "renderChartView"),
                                   ("crm.js", "renderCrmView"),
                                   ("themes.js", "applyTheme"),
                                   ("themes.css", "data-theme"),
                                   ("voice.js", "initVoice")):
            r = self.client.get(f"/receipts/asset/{name}")
            self.assertEqual(200, r.status_code, name)
            self.assertIn(must_contain, r.get_data(as_text=True), name)

    def test_an_unknown_asset_is_a_404_not_a_crash(self):
        self.assertEqual(404, self.client.get("/receipts/asset/nope.js").status_code)

    def test_the_game_layer_route_always_answers(self):
        r = self.client.get("/receipts/game.js")
        self.assertEqual(200, r.status_code)
        self.assertIn("javascript", r.content_type)


class TheVoiceEndpointTest(unittest.TestCase):

    def setUp(self):
        ad.app.config["TESTING"] = True
        self.client = ad.app.test_client()
        # /receipts now carries a password (receipts_page._receipts_password).
        # Signing in here means these tests exercise the board the way a real
        # operator reaches it, rather than around the gate.
        self.client.post("/receipts/login",
                         data={"password": receipts_page._receipts_password()})
        # The endpoint throttles per client and caches the aggregates — a test
        # firing several commands in a row must reset that state between posts.
        receipts_page._voice_last_by_ip.clear()
        receipts_page._voice_window.clear()
        receipts_page._voice_summary_cache.update(at=0.0, value=None)

    def _post(self, text, theme="dark", view="table"):
        receipts_page._voice_last_by_ip.clear()
        receipts_page._voice_window.clear()
        receipts_page._voice_summary_cache.update(at=0.0, value=None)
        db = mock.MagicMock()
        db.get_transmissions.return_value = [
            {"lead_id": LEAD, "price": "$150", "has_receipt": True,
             "created_at": "2026-08-25T09:00:00+00:00", "status": "new",
             "issuer": "tester", "driver_name": "Kita",
             "client_name": "John", "reference_id": "REF1"}]
        with mock.patch.object(ad, "db", db), \
                mock.patch.object(receipts_page, "_voice_openai", return_value=None):
            return self.client.post("/receipts/api/voice",
                                    data=json.dumps({"text": text, "theme": theme,
                                                     "view": view}),
                                    content_type="application/json")

    def test_empty_text_is_refused(self):
        r = self.client.post("/receipts/api/voice", data=json.dumps({"text": ""}),
                             content_type="application/json")
        self.assertEqual(400, r.status_code)

    def test_view_commands_map_to_actions_without_openai(self):
        for text, view in (("give me the excel sheet list view", "sheet"),
                           ("diagram view of all the numbers", "chart"),
                           ("go to CRM mode view", "crm"),
                           ("card view please", "cards")):
            r = self._post(text)
            self.assertEqual(200, r.status_code, text)
            got = r.get_json()
            self.assertEqual("set_view", got["action"], text)
            self.assertEqual(view, got["args"]["view"], text)

    def test_game_mode_and_celebrate(self):
        self.assertEqual("game_mode", self._post("game mode view").get_json()["action"])
        self.assertEqual("celebrate", self._post("celebrate!").get_json()["action"])

    def test_tetris_is_a_voice_command(self):
        self.assertEqual("play_tetris", self._post("let's play tetris").get_json()["action"])
        self.assertEqual("play_tetris", self._post("wanna play a game?").get_json()["action"])

    def test_money_questions_answer_from_the_data(self):
        got = self._post("how much did we make this year").get_json()
        self.assertEqual("none", got["action"])
        self.assertIn("$", got["say"])

    def test_the_most_recent_lead_question(self):
        got = self._post("which issuer sent the most recent lead").get_json()
        self.assertIn("tester", got["say"])

    def test_the_phrasings_that_used_to_fail(self):
        """Reported broken in production: bare plurals and a theme request with
        no theme named. The parser is what answers when OPENAI_API_KEY is unset,
        which is the normal state of this service."""
        for text, action in (("charts", "set_view"),
                             ("chart", "set_view"),
                             ("change to chart view", "set_view"),
                             ("change theme", "set_theme"),
                             ("a theme", "set_theme"),
                             ("download csv", "download_csv"),
                             ("i dont like this theme", "set_theme")):
            got = self._post(text).get_json()
            self.assertEqual(action, got["action"], f"{text!r} -> {got}")

    def test_a_theme_change_never_repeats_the_current_one(self):
        for _ in range(12):
            got = self._post("change the theme").get_json()
            self.assertEqual("set_theme", got["action"])
            self.assertNotEqual("dark", got["args"]["theme"],
                                "must pick a theme other than the current one")

    def test_one_utterance_can_answer_and_act(self):
        """The headline: 'tell me X and change the theme' must do BOTH."""
        got = self._post(
            "tell me the name of last client and change theme i dont like this one"
        ).get_json()
        self.assertIn("John", got["say"])                  # the answer
        names = [a["action"] for a in got["actions"]]
        self.assertIn("set_theme", names, got)             # and the deed

    def test_two_commands_both_run_in_order(self):
        got = self._post("switch to charts and download the csv").get_json()
        self.assertEqual(["set_view", "download_csv"],
                         [a["action"] for a in got["actions"]], got)
        self.assertEqual("chart", got["actions"][0]["args"]["view"])

    def test_the_single_intent_shape_still_works_for_old_clients(self):
        got = self._post("give me the charts").get_json()
        self.assertEqual("set_view", got["action"])
        self.assertEqual({"view": "chart"}, got["args"])


class TheLadderNeverWalksBackwardsTest(unittest.TestCase):
    """The bot advances statuses ATOMICALLY: one conditional UPDATE whose
    filter names only the strictly-lower ranks, so a racing receipt upload can
    never be demoted by a slower timed write landing after it."""

    def _db(self, affected_rows):
        cls = _real_database_class()
        d = cls.__new__(cls)
        d._check_tables_exist = lambda: True
        d.client = mock.MagicMock()
        table = mock.MagicMock()
        for meth in ("select", "eq", "limit", "update", "or_"):
            getattr(table, meth).return_value = table
        table.execute.return_value = mock.MagicMock(data=affected_rows)
        d.client.table.return_value = table
        return d, table

    def test_forward_moves_write_conditionally(self):
        d, table = self._db([{"id": LEAD}])
        self.assertTrue(d.advance_delivery_status(LEAD, "on_the_way"))
        table.update.assert_called_once()
        cond = table.or_.call_args_list[0][0][0]
        self.assertIn("delivery_status.is.null", cond)
        for lower in ("new", "followup", "tag_issued", "tag_emailed", "tag_printed"):
            self.assertIn(lower, cond)
        self.assertNotIn("receipt_uploaded", cond)
        self.assertNotIn("delivered", cond)

    def test_backward_moves_are_refused_silently(self):
        # The server-side filter matches nothing → zero affected rows → False.
        d, table = self._db([])
        self.assertFalse(d.advance_delivery_status(LEAD, "on_the_way"))

    def test_paid_never_appears_below_the_ladder_top(self):
        d, table = self._db([])
        self.assertFalse(d.advance_delivery_status(LEAD, "tag_printed"))
        cond = table.or_.call_args_list[0][0][0]
        self.assertNotIn("paid", cond)

    def test_unknown_status_is_a_no(self):
        d, table = self._db([{"id": LEAD}])
        self.assertFalse(d.advance_delivery_status(LEAD, "invented"))
        table.update.assert_not_called()

    def test_the_sweep_respects_human_writes(self):
        d, table = self._db([{"id": LEAD}])
        self.assertTrue(d.advance_delivery_status(LEAD, "on_the_way",
                                                  respect_human=True))
        conds = [c[0][0] for c in table.or_.call_args_list]
        self.assertEqual(2, len(conds))
        self.assertIn("status_updated_by.eq.bot", conds[1])

    def test_a_missing_column_latches_quietly(self):
        d, table = self._db([{"id": LEAD}])
        table.execute.side_effect = Exception("42703 column delivery_status missing")
        self.assertFalse(d.advance_delivery_status(LEAD, "on_the_way"))
        table.reset_mock()
        table.execute.side_effect = None
        self.assertFalse(d.advance_delivery_status(LEAD, "on_the_way"))
        table.update.assert_not_called()   # latched — no more writes for now


class TheBotTriggersAreWiredTest(unittest.TestCase):
    """Source-sliced, like the rest of the suite — each trigger lives exactly
    where the journey happens."""

    SRC = (ROOT / "bot.py").read_text(encoding="utf-8")
    ADMIN = (ROOT / "admin_dashboard.py").read_text(encoding="utf-8")

    def _fn(self, name, src=None):
        body = (src or self.SRC).split(f"def {name}", 1)[1]
        return body.split("\nasync def ", 1)[0].split("\ndef ", 1)[0]

    def test_tag_issued_fires_from_the_one_tag_funnel(self):
        self.assertIn('advance_delivery_status, str(lead.get("id") or ""), "tag_issued"',
                      self._fn("_send_all_tag_pdfs"))

    def test_receipt_upload_fires_from_bot_and_portal(self):
        self.assertIn('advance_delivery_status(str(lead_id), "receipt_uploaded")',
                      self._fn("handle_receipt_image(update"))
        self.assertIn('set_lead_status(lead_id, "receipt_uploaded", "portal")',
                      self._fn("receipt_portal(token)", self.ADMIN))

    def test_followup_fires_when_a_followup_is_saved(self):
        body = self._fn("_fu_finish_save")
        self.assertEqual(2, body.count("_fu_mark_lead_followup"),
                         "both save paths (with and without frequency)")

    def test_the_timed_sweep_is_registered(self):
        self.assertIn('advance_timed_statuses, interval=', self.SRC)
        sweep = self._fn("advance_timed_statuses")
        self.assertIn('"tag_printed"', sweep)
        self.assertIn('"on_the_way"', sweep)

    def test_the_issuer_is_never_the_bot(self):
        body = self._fn("_finalize_lead_after_notes")
        self.assertIn('getattr(msg_user, "is_bot", False)', body)
        self.assertIn("get_chat(user_id)", body)
        self.assertIn("get_chat(user_id)", self._fn("_submit_lead_from_review"))


if __name__ == "__main__":
    unittest.main()
