r"""Paper girls — a second roster with its own broadcast button.

Asked for: "under the drivers the way there's send to all drivers add another
option called send to all papergirls ... same flow but send to a new category of
drivers called paper girls ... paper girls can be set under settings using their
name and telegram ID ... also add toggle to show all paper girls under the
drivers just how all drivers show".

Settled with the operator: the two rosters do NOT overlap (📢 Send to All Drivers
never reaches a paper girl), and the show-in-picker toggle starts OFF.

A paper girl is a real row in `drivers` — lead_assignments.driver_id is a foreign
key to drivers(id), so a roster kept only in settings could never be assigned a
lead. Settings holds the membership list, which is why this needs no migration.

Run:  venv\Scripts\python.exe -m pytest tests/test_paper_girls.py -q
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

import bot  # noqa: E402

SRC = (ROOT / "bot.py").read_text(encoding="utf-8")

SUSAN = {"id": "d1", "driver_name": "Susan", "is_active": True,
         "driver_telegram_id": "700001"}
MARCUS = {"id": "d2", "driver_name": "Marcus", "is_active": True,
          "driver_telegram_id": "700002"}
ALINA = {"id": "p1", "driver_name": "Alina", "is_active": True,
         "driver_telegram_id": "700003"}
RAE = {"id": "p2", "driver_name": "Rae", "is_active": True,
       "driver_telegram_id": "700004"}
ROSTER = [SUSAN, MARCUS, ALINA, RAE]


def _rosters(girls=("p1", "p2"), shown=False):
    """Patch the two settings reads the pickers make."""
    return (mock.patch.object(bot, "_paper_girl_ids", lambda force=False: set(girls)),
            mock.patch.object(bot, "_paper_girls_shown_in_picker", lambda: shown))


class _RosterCase(unittest.TestCase):

    def setUp(self):
        for p in _rosters(**getattr(self, "ROSTER_KW", {})):
            p.start()
            self.addCleanup(p.stop)


def _cbs(rows):
    return [b.callback_data for row in rows for b in row]


def _labels(rows):
    return [b.text for row in rows for b in row]


class TheReviewCardPickerTest(_RosterCase):

    def _rows(self, **state):
        with mock.patch.object(bot, "_instant_all_drivers_enabled", lambda: False):
            return bot._driver_picker_rows(ROSTER, set(), state)

    def test_the_papergirls_button_is_there(self):
        self.assertIn("selpg_all", _cbs(self._rows()))
        self.assertIn("📢 Send to all papergirls", _labels(self._rows()))

    def test_paper_girls_are_not_listed_while_the_toggle_is_off(self):
        cbs = _cbs(self._rows())
        self.assertIn("seldrv_d1", cbs)
        self.assertNotIn("seldrv_p1", cbs)
        self.assertNotIn("seldrv_p2", cbs)

    def test_back_is_still_the_last_row(self):
        self.assertEqual("ph1_sel_back", self._rows()[-1][0].callback_data)

    def test_no_papergirls_means_no_button(self):
        with mock.patch.object(bot, "_paper_girl_ids", lambda force=False: set()):
            self.assertNotIn("selpg_all", _cbs(self._rows()))

    def test_every_papergirl_suspended_means_no_button(self):
        with mock.patch.object(bot, "_instant_all_drivers_enabled", lambda: False):
            rows = bot._driver_picker_rows(ROSTER, {"p1", "p2"}, {})
        self.assertNotIn("selpg_all", _cbs(rows))
        self.assertIn("seldrv_all", _cbs(rows), "the drivers are still reachable")

    def test_an_instant_lead_hides_it_like_all_drivers(self):
        """Instant Tag is ONE person paying ONE link unless supervisors allow it."""
        with mock.patch.object(bot, "_instant_all_drivers_enabled", lambda: False):
            rows = bot._driver_picker_rows(ROSTER, set(), {"instant_tag": True})
        self.assertNotIn("selpg_all", _cbs(rows))
        self.assertNotIn("seldrv_all", _cbs(rows))
        with mock.patch.object(bot, "_instant_all_drivers_enabled", lambda: True):
            rows = bot._driver_picker_rows(ROSTER, set(), {"instant_tag": True})
        self.assertIn("selpg_all", _cbs(rows))


class TheToggleListsThemTest(_RosterCase):
    ROSTER_KW = {"shown": True}

    def test_they_appear_beside_the_drivers(self):
        with mock.patch.object(bot, "_instant_all_drivers_enabled", lambda: False):
            rows = bot._driver_picker_rows(ROSTER, set(), {})
        self.assertEqual(["seldrv_d1", "seldrv_d2", "seldrv_p1", "seldrv_p2"],
                         [c for c in _cbs(rows) if c.startswith("seldrv_")
                          and c != "seldrv_all"])

    def test_they_are_marked_so_you_can_tell(self):
        with mock.patch.object(bot, "_instant_all_drivers_enabled", lambda: False):
            labels = _labels(bot._driver_picker_rows(ROSTER, set(), {}))
        self.assertIn("🚗 Susan", labels)
        self.assertIn("📰 Alina", labels)

    def test_a_suspended_papergirl_is_still_dead(self):
        with mock.patch.object(bot, "_instant_all_drivers_enabled", lambda: False):
            cbs = _cbs(bot._driver_picker_rows(ROSTER, {"p1"}, {}))
        self.assertIn("driver_suspended_p1", cbs)
        self.assertNotIn("seldrv_p1", cbs)


class ThePostDispatchPickerTest(_RosterCase):

    def test_it_offers_the_same_two_broadcasts(self):
        with mock.patch.object(bot, "_get_suspended_driver_ids", lambda: set()):
            kb = bot._build_driver_keyboard(ROSTER)
        cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertIn("select_driver_all", cbs)
        self.assertIn("select_driver_pg", cbs)
        self.assertNotIn("select_driver_p1", cbs, "toggle is off")


class TheTwoRostersDoNotOverlapTest(_RosterCase):

    def test_all_drivers_leaves_the_paper_girls_out(self):
        self.assertEqual([SUSAN, MARCUS], bot._only_drivers(ROSTER))

    def test_the_papergirl_broadcast_is_only_them(self):
        self.assertEqual(["p1", "p2"],
                         [d["id"] for d in bot._paper_girl_rows(ROSTER, set())])

    def test_a_suspended_papergirl_is_not_broadcast_to(self):
        self.assertEqual(["p2"],
                         [d["id"] for d in bot._paper_girl_rows(ROSTER, {"p1"})])

    def test_an_unaddressed_lead_never_reaches_them(self):
        """No pick means "use the pool", and the pool is the drivers."""
        with mock.patch.object(bot, "_get_all_drivers_cached", lambda: ROSTER), \
                mock.patch.object(bot, "_get_suspended_driver_ids", lambda: set()), \
                mock.patch.object(bot, "db", mock.MagicMock()):
            ids, _dropped = bot._dispatch_drivers_with_reasons({}, is_all_groups=True)
        self.assertEqual(["d1", "d2"], sorted(ids))

    def test_but_an_explicit_pick_of_one_still_works(self):
        with mock.patch.object(bot, "_get_all_drivers_cached", lambda: ROSTER), \
                mock.patch.object(bot, "_get_suspended_driver_ids", lambda: set()), \
                mock.patch.object(bot, "db", mock.MagicMock()):
            ids, _ = bot._dispatch_drivers_with_reasons(
                {"selected_driver_ids": ["p1"]}, is_all_groups=True)
        self.assertEqual(["p1"], ids)


class TheCardDefaultLeavesThemOutTest(_RosterCase):
    """The widest reach in the bot: every new lead card pre-selects "All
    Drivers". A roster that leaked in HERE would reach the paper girls on
    essentially every job, without anybody choosing it."""

    def _default(self, fn):
        state = {}
        fake_db = mock.MagicMock()
        fake_db.get_all_groups.return_value = []
        fake_db.get_contact_info_sources.return_value = []
        with mock.patch.object(bot, "db", fake_db), \
                mock.patch.object(bot, "_get_all_drivers_cached", lambda: ROSTER), \
                mock.patch.object(bot, "_get_suspended_driver_ids", lambda: set()):
            fn(state, fake_db)
        return state

    def test_a_fresh_card_selects_drivers_only(self):
        state = self._default(
            lambda s, db: bot._select_driver(s, 7, "all"))
        self.assertEqual(["d1", "d2"], state["selected_driver_ids"])

    def test_saying_all_drivers_still_leaves_them_out(self):
        state = self._default(lambda s, db: bot._select_driver(s, 7, "all"))
        self.assertEqual("All Drivers", state["selected_driver_names"])
        self.assertNotIn("p1", state["selected_driver_ids"])

    def test_the_defaults_in_the_source_use_the_driver_roster(self):
        """Both card builders had to be changed, not just the pickers."""
        for fn in ("_send_phase1_ai_review", "_repost_review_card"):
            body = SRC.split(f"async def {fn}", 1)[1].split("\nasync def ", 1)[0]
            self.assertIn("_only_drivers(_get_all_drivers_cached())", body, fn)


class SayingItOutLoudTest(_RosterCase):

    def test_the_phrase_is_understood_as_a_driver_pick(self):
        for said in ("paper girls", "papergirls", "all paper girls",
                     "send to all papergirls", "all the paper girls"):
            kind, _payload = bot._classify_review_command(said, vin_pending=False)
            self.assertEqual("SELECT_DRIVER", kind, said)

    def test_all_drivers_is_untouched(self):
        self.assertEqual(("SELECT_DRIVER", "all"),
                         bot._classify_review_command("all drivers", vin_pending=False))

    def test_prose_that_mentions_one_is_still_a_note(self):
        for said in ("paper girl called about the gate",
                     "tell the paper girls tomorrow"):
            self.assertEqual(("NONE", None),
                             bot._classify_review_command(said, vin_pending=False), said)

    def test_the_papergirls_rule_is_tested_before_the_all_rule(self):
        """"all paper girls" begins with "all"."""
        self.assertTrue(bot._PAPERGIRLS_SELECT_RE.match("all paper girls"))
        self.assertFalse(bot._ALL_SELECT_RE.match("all paper girls"))

    def test_an_empty_roster_never_widens_the_lead(self):
        """An empty selected_driver_ids reads downstream as "nobody picked
        anyone", and the dispatcher then falls back to the whole pool. Saying
        "paper girls" with nobody on the roster used to wipe the driver already
        chosen and send the lead to EVERY driver."""
        state = {"selected_driver_ids": ["d1"], "selected_driver_names": "Susan"}
        fake_db = mock.MagicMock()
        with mock.patch.object(bot, "db", fake_db), \
                mock.patch.object(bot, "_paper_girl_ids", lambda force=False: set()), \
                mock.patch.object(bot, "_get_all_drivers_cached", lambda: ROSTER), \
                mock.patch.object(bot, "_get_suspended_driver_ids", lambda: set()):
            self.assertFalse(bot._select_driver(state, 7, "papergirls"))
            self.assertEqual(["d1"], state["selected_driver_ids"],
                             "the existing pick was destroyed")
            ids, _ = bot._dispatch_drivers_with_reasons(state, is_all_groups=True)
        self.assertEqual(["d1"], ids, "an explicit pick must never widen")

    def test_every_driver_suspended_does_not_widen_either(self):
        state = {"selected_driver_ids": ["d1"], "selected_driver_names": "Susan"}
        with mock.patch.object(bot, "db", mock.MagicMock()), \
                mock.patch.object(bot, "_get_all_drivers_cached", lambda: ROSTER), \
                mock.patch.object(bot, "_get_suspended_driver_ids",
                                  lambda: {"d1", "d2"}):
            self.assertFalse(bot._select_driver(state, 7, "all"))
        self.assertEqual("Susan", state["selected_driver_names"])

    def test_a_pick_that_resolves_reports_success(self):
        state = {}
        with mock.patch.object(bot, "db", mock.MagicMock()), \
                mock.patch.object(bot, "_get_all_drivers_cached", lambda: ROSTER), \
                mock.patch.object(bot, "_get_suspended_driver_ids", lambda: set()):
            self.assertTrue(bot._select_driver(state, 7, "papergirls"))
            self.assertTrue(bot._select_driver(state, 7, "all"))
            self.assertTrue(bot._select_driver(state, 7, SUSAN))

    def test_speaking_it_selects_them(self):
        state = {}
        fake_db = mock.MagicMock()
        with mock.patch.object(bot, "db", fake_db), \
                mock.patch.object(bot, "_get_all_drivers_cached", lambda: ROSTER), \
                mock.patch.object(bot, "_get_suspended_driver_ids", lambda: set()):
            bot._select_driver(state, 7, "papergirls")
        self.assertEqual(["p1", "p2"], state["selected_driver_ids"])
        self.assertEqual("All Paper Girls", state["selected_driver_names"])


class NoAutomaticFanOutReachesThemTest(_RosterCase):
    """The paths that send a lead with NO picker on screen.

    These are the dangerous ones: a website lead, a timeout retry, a reassign,
    a team tapping Accept. Nobody chose the paper girls on any of them, so a
    roster that leaks in here reaches them with no human in the loop at all.
    Each of these was a real leak found after the first pass.
    """

    def test_website_leads_go_to_drivers_only(self):
        with mock.patch.object(bot, "_get_all_drivers_cached", lambda: ROSTER), \
                mock.patch.object(bot, "_get_suspended_driver_ids", lambda: set()):
            self.assertEqual(["d1", "d2"], bot._resolve_all_active_driver_ids())

    def test_the_group_fan_out_uses_the_driver_roster(self):
        """A team taps Accept and the whole roster is offered the lead."""
        body = SRC.split("async def _send_driver_requests_for_group", 1)[1]
        body = body.split("\nasync def ", 1)[0]
        self.assertIn("_only_drivers(db.get_group_driver_rows_for_group(group_id))", body)
        self.assertIn("_only_drivers(_get_all_drivers_cached())", body)

    def test_the_timeout_retry_broadcast_uses_the_driver_roster(self):
        body = SRC.split("async def _handle_resend_to_drivers", 1)[1]
        body = body.split("\nasync def ", 1)[0]
        self.assertIn("_only_drivers(active_drivers)", body)

    def test_the_no_group_reassign_uses_the_driver_roster(self):
        body = SRC.split("async def handle_reassign_lead", 1)[1]
        body = body.split("\nasync def ", 1)[0]
        self.assertIn("_only_drivers(_get_all_drivers_cached())", body)

    def test_the_dispatch_fallback_pool_uses_the_driver_roster(self):
        self.assertNotIn("fallback_pool = linked or _get_all_drivers_cached() or []", SRC)
        self.assertIn("fallback_pool = _only_drivers(_get_all_drivers_cached()) or []", SRC)

    def test_the_resend_papergirl_branch_can_reply(self):
        """It reached for a `query` name that does not exist in that function —
        a NameError on the empty-roster path."""
        body = SRC.split("async def _handle_resend_to_drivers", 1)[1]
        body = body.split("\nasync def ", 1)[0]
        self.assertNotIn("await query.message.reply_text", body)
        self.assertIn("await update.callback_query.message.reply_text", body)


class TheConfirmationNamesTheRightRosterTest(_RosterCase):
    """The "sent to" line is the one thing worth checking before walking away."""

    def _label(self, state, sent):
        with mock.patch.object(bot, "_get_all_drivers_cached", lambda: ROSTER), \
                mock.patch.object(bot, "_get_suspended_driver_ids", lambda: set()):
            return bot._drivers_sent_label(state, sent)

    def test_a_papergirl_blast_says_paper_girls(self):
        self.assertEqual(
            "All paper girls (2)",
            self._label({"selected_driver_names": "All Paper Girls"}, [ALINA, RAE]))

    def test_a_driver_blast_still_says_drivers(self):
        self.assertEqual(
            "All drivers (2)",
            self._label({"selected_driver_names": "All Drivers"}, [SUSAN, MARCUS]))

    def test_two_paper_girls_are_not_reported_as_all_drivers(self):
        """Two drivers and two paper girls are the same NUMBER — the old count
        test called a papergirl blast "All drivers (2)"."""
        self.assertEqual("Alina, Rae", self._label({}, [ALINA, RAE]))

    def test_everyone_on_the_driver_roster_still_collapses(self):
        self.assertEqual("All drivers (2)", self._label({}, [SUSAN, MARCUS]))


class TappingTheBroadcastTest(unittest.IsolatedAsyncioTestCase):

    async def _tap(self, data, roster=ROSTER, suspended=(), girls=("p1", "p2")):
        q = mock.MagicMock()
        q.data = data
        q.answer = mock.AsyncMock()
        q.message.chat_id = 5
        q.message.reply_text = mock.AsyncMock()
        q.from_user = mock.MagicMock(id=7, username="u")
        upd = mock.MagicMock(callback_query=q)
        upd.effective_user = mock.MagicMock(id=7, username="u")
        upd.effective_chat = mock.MagicMock(id=5, type="private")
        # A real card always carries something; an empty dict reads as "data lost".
        state = {"name": "Magnolia Diaz"}
        fake_db = mock.MagicMock()
        fake_db.get_user_state.return_value = {"state": "phase1", "data": state}
        said = []
        with mock.patch.object(bot, "db", fake_db), \
                mock.patch.object(bot, "_safe_answer_callback_query", mock.AsyncMock()), \
                mock.patch.object(bot, "_adopt_review_message", lambda *a, **k: None), \
                mock.patch.object(bot, "_paper_girl_ids", lambda force=False: set(girls)), \
                mock.patch.object(bot, "_get_all_drivers_cached", lambda: roster), \
                mock.patch.object(bot, "_get_suspended_driver_ids", lambda: set(suspended)), \
                mock.patch.object(bot, "_instant_all_drivers_enabled", lambda: True), \
                mock.patch.object(bot, "_update_review_message_text", mock.AsyncMock()), \
                mock.patch.object(bot, "_send_vanishing",
                                  mock.AsyncMock(side_effect=lambda c, ch, t, **k: said.append(t))):
            await bot.handle_phase1_ai_review_callback(upd, mock.MagicMock())
        return state, said

    async def test_it_selects_every_paper_girl(self):
        state, said = await self._tap("selpg_all")
        self.assertEqual(["p1", "p2"], state["selected_driver_ids"])
        self.assertEqual("All Paper Girls", state["selected_driver_names"])
        self.assertEqual([], said)

    async def test_all_drivers_selects_no_paper_girl(self):
        state, _ = await self._tap("seldrv_all")
        self.assertEqual(["d1", "d2"], state["selected_driver_ids"])
        self.assertEqual("All Drivers", state["selected_driver_names"])

    async def test_an_empty_roster_says_so_and_selects_nothing(self):
        state, said = await self._tap("selpg_all", girls=())
        self.assertNotIn("selected_driver_ids", state)
        self.assertTrue(said and "Paper Girls" in said[0])

    async def test_a_papergirl_can_still_be_picked_one_by_one(self):
        state, _ = await self._tap("seldrv_p1")
        self.assertEqual(["p1"], state["selected_driver_ids"])
        self.assertEqual("Alina", state["selected_driver_names"])


class TheButtonReachesAHandlerTest(unittest.TestCase):
    """A review-card callback missing from PH1_REVIEW_CB_PATTERN is a button that
    does nothing at all after a redeploy, with no log line."""

    def test_selpg_is_in_the_review_pattern(self):
        import re
        self.assertTrue(re.match(bot.PH1_REVIEW_CB_PATTERN, "selpg_all"),
                        "selpg_all reaches no handler")

    def test_the_dispatch_prefix_is_already_routed(self):
        self.assertTrue(SRC.count('pattern="^(select_driver_|driver_suspended_)"') >= 1)
        self.assertTrue("select_driver_pg".startswith("select_driver_"))


class TheSettingsScreenTest(unittest.IsolatedAsyncioTestCase):

    def test_it_is_on_the_menu_and_in_the_view_table(self):
        cbs = [b.callback_data for row in bot._settings_main_kb().inline_keyboard
               for b in row]
        self.assertIn("tset_pg", cbs)
        self.assertIn("tset_pg", bot._SETTINGS_VIEWS)

    def test_it_can_be_opened_by_voice(self):
        for said in ("paper girls", "papergirls", "open the paper girls",
                     "paper girl"):
            self.assertEqual("tset_pg", bot._settings_nav_target(said), said)

    def test_saying_drivers_still_opens_drivers(self):
        self.assertEqual("tset_drivers", bot._settings_nav_target("drivers"))

    async def _view(self, girls=("p1", "p2"), shown=False, roster=ROSTER):
        with mock.patch.object(bot, "_get_all_drivers_cached", lambda: roster), \
                mock.patch.object(bot, "_paper_girl_ids", lambda force=False: set(girls)), \
                mock.patch.object(bot, "_get_suspended_driver_ids", lambda: set()), \
                mock.patch.object(bot, "_paper_girls_shown_in_picker", lambda: shown):
            return await bot._settings_view_paper_girls()

    async def test_it_lists_them_with_their_telegram_ids(self):
        text, kb = await self._view()
        self.assertIn("Alina", text)
        self.assertIn("700003", text)
        cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertIn("tset_pgdel:p1", cbs)
        self.assertIn("tset_pgadd", cbs)

    async def test_the_toggle_shows_its_state(self):
        text, kb = await self._view(shown=False)
        labels = [b.text for row in kb.inline_keyboard for b in row]
        self.assertIn("👀 Show in driver list: OFF", labels)
        _t, kb_on = await self._view(shown=True)
        self.assertIn("👀 Show in driver list: ON",
                      [b.text for row in kb_on.inline_keyboard for b in row])

    async def test_an_empty_roster_reads_clearly(self):
        text, _ = await self._view(girls=())
        self.assertIn("Nobody on the roster yet", text)

    async def test_an_id_whose_driver_row_is_gone_can_still_be_removed(self):
        text, kb = await self._view(girls=("p1", "ghost"))
        self.assertIn("ghost", text)
        self.assertIn("tset_pgdel:ghost",
                      [b.callback_data for row in kb.inline_keyboard for b in row])

    async def test_every_button_it_emits_is_routed(self):
        _t, kb = await self._view()
        for row in kb.inline_keyboard:
            for b in row:
                self.assertTrue(b.callback_data.startswith("tset_"), b.callback_data)


class AddingAPaperGirlTest(unittest.IsolatedAsyncioTestCase):

    async def _add(self, text, existing=None):
        upd = mock.MagicMock()
        upd.effective_user = mock.MagicMock(id=7)
        upd.message.text = text
        upd.message.reply_text = mock.AsyncMock()
        ctx = mock.MagicMock()
        ctx.user_data = {"tset_await": {"kind": "add_paper_girl"}}
        fake_db = mock.MagicMock()
        fake_db.get_driver_by_telegram_id.side_effect = (
            [existing] if existing else [None, {"id": "new1", "driver_name": "Alina"}])
        saved = {}
        with mock.patch.object(bot, "db", fake_db), \
                mock.patch.object(bot, "_user_is_global_supervisor", lambda uid: True), \
                mock.patch.object(bot, "_bust_driver_caches", lambda: None), \
                mock.patch.object(bot, "_add_paper_girl",
                                  lambda did: saved.setdefault("id", did) or True):
            await bot.apply_settings_input(upd, ctx)
        said = str(upd.message.reply_text.call_args.args[0])
        return saved, said, fake_db

    async def test_a_new_name_and_id_creates_the_driver_row_and_enrols_her(self):
        saved, said, db = await self._add("Alina | 700003")
        db.create_driver.assert_called_once()
        self.assertEqual("new1", saved.get("id"))
        self.assertIn("paper girls roster", said)

    async def test_someone_already_a_driver_keeps_their_row(self):
        saved, said, db = await self._add(
            "Susan | 700001", existing={"id": "d1", "driver_name": "Susan"})
        db.create_driver.assert_not_called()
        self.assertEqual("d1", saved.get("id"),
                         "her receipts and history must not be orphaned")

    async def test_a_bad_id_is_refused(self):
        _saved, said, db = await self._add("Alina | not-a-number")
        db.create_driver.assert_not_called()
        self.assertIn("digits", said)

    async def test_a_missing_name_is_refused(self):
        _saved, said, db = await self._add("700003")
        db.create_driver.assert_not_called()
        self.assertIn("Name | telegram_id", said)


class TheRosterStoreTest(unittest.TestCase):

    def test_a_missing_setting_is_an_empty_roster(self):
        fake = mock.MagicMock()
        fake.get_setting.return_value = None
        with mock.patch.object(bot, "db", fake):
            bot._paper_girl_cache["at"] = 0.0
            self.assertEqual(set(), bot._paper_girl_ids(force=True))

    def test_junk_in_the_setting_does_not_crash_a_picker(self):
        fake = mock.MagicMock()
        for junk in ("not json", "{}", "42", '["ok", "", null]'):
            fake.get_setting.return_value = junk
            with mock.patch.object(bot, "db", fake):
                bot._paper_girl_cache["at"] = 0.0
                self.assertIsInstance(bot._paper_girl_ids(force=True), set, junk)

    def test_add_and_remove_round_trip(self):
        store = {}
        fake = mock.MagicMock()
        fake.get_setting.side_effect = lambda k: store.get(k)
        fake.set_setting.side_effect = lambda k, v: store.__setitem__(k, v) or True
        with mock.patch.object(bot, "db", fake):
            bot._add_paper_girl("p1")
            bot._add_paper_girl("p2")
            self.assertEqual({"p1", "p2"}, bot._paper_girl_ids(force=True))
            bot._add_paper_girl("p1")                    # twice is once
            self.assertEqual({"p1", "p2"}, bot._paper_girl_ids(force=True))
            bot._remove_paper_girl("p1")
            self.assertEqual({"p2"}, bot._paper_girl_ids(force=True))

    def test_the_picker_toggle_defaults_off(self):
        fake = mock.MagicMock()
        fake.get_setting.return_value = None
        with mock.patch.object(bot, "db", fake):
            self.assertFalse(bot._paper_girls_shown_in_picker())
        for on in ("1", "true", "YES", "on"):
            fake.get_setting.return_value = on
            with mock.patch.object(bot, "db", fake):
                self.assertTrue(bot._paper_girls_shown_in_picker(), on)


if __name__ == "__main__":
    unittest.main()
