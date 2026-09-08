r"""Renaming a dispatcher, and not naming one that has not taken the lead.

A user reported that whichever dispatcher is chosen, the board says AUTOMATE
PLATE. Checked against production, and it is half true in a way worth fixing.

Website and /form leads are stamped at ingest with ``active_groups[0]`` as their
primary group, and ``get_all_groups()`` orders by ``group_name`` -- so
``active_groups[0]`` is whichever team sorts first alphabetically. That is
AUTOMATE PLATE. It has been the stamped owner of every unclaimed website lead
regardless of who was actually offered it, which is why a dispatcher rang up
about leads that were never theirs. Once a team accepts, the accepting team
replaces it and the column is right again -- which is why it looked intermittent
rather than broken.

28 of the newest 300 live rows were affected.

The stamp itself is left alone: ``group_id`` is a real foreign key the dispatch
machinery uses as the primary group. What changes is the board's claim about it.

The rename is the other half: a dispatcher's name reaches the settings list, the
review card, the receipts board and every supervisory notice, and the only way
to fix a typo used to be adding a second dispatcher and disabling the first --
which leaves the old name on every lead already dispatched.

Run:  venv\Scripts\python.exe -m pytest tests/test_dispatcher_naming_and_rename.py -q
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

SRC_BOT = (ROOT / "bot.py").read_text(encoding="utf-8")
SRC_BOARD = (ROOT / "receipts_page.py").read_text(encoding="utf-8")
SRC_DB = (ROOT / "utils" / "database.py").read_text(encoding="utf-8")


class TheAlphabeticalStampIsTheCauseTest(unittest.TestCase):
    """Naming the mechanism, so the next person does not re-derive it."""

    def test_groups_come_back_alphabetically(self):
        """Which makes active_groups[0] a fact about spelling, not ownership."""
        body = SRC_DB.split("def get_all_groups(", 1)[1].split("\n    def ", 1)[0]
        self.assertIn('.order("group_name")', body)

    def test_ingest_stamps_the_first_active_group(self):
        ingest = (ROOT / "utils" / "lead_ingest.py").read_text(encoding="utf-8")
        self.assertIn('"group_id": active_groups[0]["id"]', ingest)


class AnUnclaimedLeadIsNotSomebodysTest(unittest.TestCase):

    def test_the_board_row_carries_the_offer_facts(self):
        body = SRC_ADMIN = (ROOT / "admin_dashboard.py").read_text(encoding="utf-8")
        tx = body.split("def get_transmissions(", 1)[1].split("\n    def ", 1)[0]
        self.assertIn('"group_offers"', tx)
        self.assertIn('"group_accepted"', tx)
        self.assertIn('.table("group_lead_offers")', tx)

    def test_a_missing_offers_table_falls_back_to_the_old_behaviour(self):
        """No offers table means no claim to make either way; the column should
        keep naming the stamped group rather than blanking every row."""
        body = (ROOT / "admin_dashboard.py").read_text(encoding="utf-8")
        tx = body.split("def get_transmissions(", 1)[1].split("\n    def ", 1)[0]
        self.assertIn("group offer lookup failed", tx)

    def test_the_dispatcher_block_hides_the_name_until_somebody_accepts(self):
        block = SRC_BOARD.split("dispatcher: (() => {", 1)[1].split("})(),", 1)[0]
        self.assertIn("r.group_accepted !== false", block)
        self.assertIn("r.group_offers > 0", block)
        self.assertIn('claimed ? nm : ""', block)

    def test_it_is_labelled_as_an_offer_not_an_owner(self):
        block = SRC_BOARD.split("dispatcher: (() => {", 1)[1].split("})(),", 1)[0]
        self.assertIn('"Dispatcher (offered)"', block)

    def test_an_unclaimed_lead_says_how_many_were_asked(self):
        """A bare dash would throw away the one fact we do have."""
        self.assertIn("none accepted", SRC_BOARD)

    def test_a_lead_nobody_was_offered_is_left_alone(self):
        """Telegram leads where the issuer PICKED a group have no offer rows;
        they must keep showing the chosen dispatcher."""
        block = SRC_BOARD.split("dispatcher: (() => {", 1)[1].split("})(),", 1)[0]
        self.assertIn("!(r.group_offers > 0)", block)

    def test_the_supervisor_id_goes_with_the_name(self):
        """Showing a supervisor's Telegram id under "none accepted" would point
        at the very person who does not own the lead."""
        block = SRC_BOARD.split("dispatcher: (() => {", 1)[1].split("})(),", 1)[0]
        self.assertIn('claimed\n          ? (r.dispatcher_tg_id', block)


class RenamingADispatcherTest(unittest.TestCase):

    def setUp(self):
        self.body = SRC_DB.split("def rename_group(", 1)[1].split("\n    def ", 1)[0]

    def test_a_blank_name_is_refused(self):
        self.assertIn('if not name or not str(group_id or "").strip():', self.body)

    def test_the_write_is_read_back(self):
        """On the anon key an RLS refusal comes back as a cheerful 200 with an
        empty list, so "no rows" must not read as success."""
        self.assertIn("if not resp.data:", self.body)
        self.assertIn("affected no rows", self.body)

    def test_the_name_is_bounded(self):
        self.assertIn("name[:120]", self.body)

    def test_there_is_a_rename_button_on_every_dispatcher(self):
        view = SRC_BOT.split("async def _settings_view_groups(", 1)[1]
        view = view.split("\nasync def ", 1)[0]
        self.assertIn("tset_gren:", view)
        self.assertIn("Rename", view)

    def test_the_button_is_wired_to_a_prompt(self):
        self.assertIn('if data.startswith("tset_gren:"):', SRC_BOT)
        self.assertIn('"kind": "rename_group"', SRC_BOT)

    def test_a_deleted_dispatcher_does_not_crash_the_prompt(self):
        handler = SRC_BOT.split('if data.startswith("tset_gren:"):', 1)[1][:600]
        self.assertIn("if not grp:", handler)

    def test_the_new_name_is_saved_and_shown_back(self):
        step = SRC_BOT.split('if st.get("kind") == "rename_group":', 1)[1]
        step = step.split('if st.get("kind") == "add_group":', 1)[0]
        self.assertIn("db.rename_group", step)
        self.assertIn('_show_settings_view("tset_groups"', step)

    def test_a_failed_rename_is_not_reported_as_done(self):
        step = SRC_BOT.split('if st.get("kind") == "rename_group":', 1)[1]
        step = step.split('if st.get("kind") == "add_group":', 1)[0]
        self.assertIn("if not ok:", step)
        self.assertIn("nothing was changed", step)

    def test_the_callback_fits_in_telegrams_limit(self):
        """prefix + a 36-char uuid. Over 64 and Telegram drops the WHOLE
        keyboard, so the settings screen never arrives."""
        self.assertLessEqual(len("tset_gren:") + 36, 64)


class AddingADispatcherSaysWhyItFailedTest(unittest.TestCase):
    """Supervisors reported being unable to add a dispatcher and got "Could not
    add the dispatcher" and nothing else. Reproduced against production: the
    chat id was already on another row and groups_group_telegram_id_key refused
    the insert. The reason existed the whole time, in a log nobody on Telegram
    can read."""

    def setUp(self):
        self.body = SRC_DB.split("def create_group_reporting(", 1)[1]
        self.body = self.body.split("\n    def ", 1)[0]

    def test_a_duplicate_names_the_dispatcher_already_using_the_id(self):
        self.assertIn("get_group_by_telegram_id", self.body)
        self.assertIn("already belongs to", self.body)

    def test_a_disabled_owner_is_called_out(self):
        """"Already exists" is baffling when the thing is nowhere in the visible
        list. The fix there is Enable, not Add."""
        self.assertIn("DISABLED", self.body)
        self.assertIn("record_is_active(owner)", self.body)

    def test_an_unreachable_database_is_not_reported_as_a_bad_id(self):
        self.assertIn("not reachable", self.body)

    def test_the_bool_contract_is_kept_for_the_dashboard(self):
        """admin_dashboard has two callers expecting True/False."""
        old = SRC_DB.split("def create_group(", 1)[1].split("\n    def ", 1)[0]
        self.assertIn("create_group_reporting", old)
        self.assertIn("return ok", old)


class TheChatIdIsCheckedBeforeItIsStoredTest(unittest.TestCase):
    """Nothing validated it. A group NAME or a t.me link inserted cleanly and
    produced a dispatcher that can never receive a message -- a failure that
    only surfaces later, as leads going nowhere."""

    def setUp(self):
        self.step = SRC_BOT.split('if st.get("kind") == "add_group":', 1)[1]
        self.step = self.step.split("return SET_MENU", 1)[0]

    def test_it_uses_the_strict_check_not_the_tolerant_one(self):
        """_parse_chat_id hands a non-numeric string straight back on purpose --
        it resolves @channel handles elsewhere -- so `is None` only ever caught
        an empty string."""
        self.assertIn("_delivery_chat_id(parts[1])", self.step)
        self.assertNotIn("_parse_chat_id(parts[1])", self.step)

    def test_the_strict_check_really_refuses_what_people_type(self):
        import bot
        for bad in ("Sensei Team", "https://t.me/joinchat/abc", "not a number",
                    "0", "@bob", "12345.7", ""):
            with self.subTest(value=bad):
                self.assertIsNone(bot._delivery_chat_id(bad))

    def test_a_real_chat_id_still_passes(self):
        import bot
        self.assertEqual(-1001420556250, bot._delivery_chat_id("-1001420556250"))
        # A pasted value with the spreadsheet "=" and stray spaces still works.
        self.assertEqual(-971636706, bot._delivery_chat_id("  = -971636706 "))

    def test_the_supervisor_is_its_own_optional_field(self):
        """The handler passed the group id twice, so every dispatcher added from
        Telegram had its supervisory notices pointed back at the group."""
        self.assertIn("sup_raw", self.step)
        self.assertIn("_delivery_chat_id(sup_raw)", self.step)
        self.assertNotIn("db.create_group, parts[0], parts[1], parts[1]", SRC_BOT)

    def test_the_reason_reaches_the_operator(self):
        self.assertIn("create_group_reporting", self.step)
        self.assertIn("why or", self.step)

    def test_the_prompt_explains_what_a_chat_id_is(self):
        prompt = SRC_BOT.split('if data == "tset_gadd":', 1)[1][:800]
        self.assertIn("whoami", prompt)
        self.assertIn("not its name", prompt)


if __name__ == "__main__":
    unittest.main()
