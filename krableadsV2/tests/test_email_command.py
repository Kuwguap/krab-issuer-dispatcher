r"""/email — the shorter name, and a picker when it is sent on its own.

Asked for: "instead of /setclientemail just make it /email {refrencce number}
{client email} / and if /email is sent alone send inline buttons of the client
adders recent leads that need the client email to proceed, when supervisors send
/email only as well they will see all most recent insurance clients that need
emails and theyll clcik hte inline button to see it / inline button will have
the name of client and refrence number".

Two things this file exists to hold still:

* The question is "which leads carry a flag that needs an email", never "of the
  newest leads, which are blocked". Measured against the live database, the
  second made a supervisor's list SHORTER than one of their own issuers' --
  9 against 10 -- because the newest leads overall held fewer blocked ones.
* An ordinary sender sees only their own. Someone else's client list is both
  useless to them and not theirs to read.

Run:  venv\Scripts\python.exe -m pytest tests/test_email_command.py -q
"""
import asyncio
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy-key")

import bot as B  # noqa: E402

LEAD = "11111111-2222-3333-4444-555555555555"


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def _msg():
    m = mock.MagicMock()
    m.reply_text = mock.AsyncMock()
    m.chat_id = -100123
    return m


def _said(m):
    return " ".join(str(c[0][0]) for c in m.reply_text.await_args_list)


class TheCommandIsCalledEmailTest(unittest.TestCase):

    def test_both_names_reach_the_same_handler(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        self.assertIn('CommandHandler(["email", "setclientemail"], cmd_set_client_email)', src)

    def test_it_is_on_the_command_menu_at_last(self):
        """It has never been listed, which is part of why leads sat blocked."""
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        self.assertIn('BotCommand("email"', src)

    def test_the_bot_advertises_the_new_name_when_a_lead_is_blocked(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        self.assertNotIn("Send /setclientemail", src)
        self.assertNotIn("send /setclientemail", src)
        self.assertIn("Send /email {lead.get('reference_id', '')} ", src)

    def test_the_usage_line_mentions_the_picker(self):
        m = _msg()
        u = mock.MagicMock()
        u.effective_message = m
        u.effective_user = types.SimpleNamespace(id=7, username="x")
        ctx = mock.MagicMock()
        ctx.args = ["only-one-argument"]
        with mock.patch.object(B, "db", mock.MagicMock()):
            run(B.cmd_set_client_email(u, ctx))
        self.assertIn("/email <REFERENCE>", _said(m))
        self.assertIn("on its own", _said(m))


class TheNoArgumentPickerTest(unittest.TestCase):

    def _call(self, rows, supervisor=False, uid=777):
        m = _msg()
        u = mock.MagicMock()
        u.effective_message = m
        u.effective_user = types.SimpleNamespace(id=uid, username="issuer")
        ctx = mock.MagicMock()
        ctx.args = []
        db = mock.MagicMock()
        db.get_leads_needing_client_email.return_value = rows
        with mock.patch.object(B, "db", db), \
             mock.patch.object(B, "_user_is_global_supervisor", return_value=supervisor), \
             mock.patch.object(B, "_client_display_name_from_lead",
                               side_effect=lambda r: r.get("_name", "Client")):
            run(B.cmd_set_client_email(u, ctx))
        return m, db

    ROWS = [
        {"id": LEAD, "reference_id": "LAB4CDVZ", "_name": "John Damian",
         "needs_insurance": True, "needs_tag_email": False},
        {"id": "l-2", "reference_id": "OTS1W0FK", "_name": "Maria Gonzalez",
         "needs_insurance": False, "needs_tag_email": True},
    ]

    def test_a_button_carries_the_client_name_and_the_reference(self):
        m, _ = self._call(self.ROWS)
        kb = m.reply_text.await_args[1]["reply_markup"].inline_keyboard
        labels = [b.text for row in kb for b in row]
        self.assertEqual(2, len(labels))
        self.assertIn("John Damian", labels[0])
        self.assertIn("LAB4CDVZ", labels[0])
        self.assertIn("Maria Gonzalez", labels[1])
        self.assertIn("OTS1W0FK", labels[1])

    def test_the_button_says_which_thing_is_held(self):
        m, _ = self._call(self.ROWS)
        kb = m.reply_text.await_args[1]["reply_markup"].inline_keyboard
        labels = [b.text for row in kb for b in row]
        self.assertTrue(labels[0].startswith("🛡"), labels[0])   # insurance card
        self.assertTrue(labels[1].startswith("🏷"), labels[1])   # tag email

    def test_an_ordinary_sender_sees_only_their_own(self):
        _, db = self._call(self.ROWS, supervisor=False, uid=4242)
        db.get_leads_needing_client_email.assert_called_once_with("4242", 12)

    def test_a_supervisor_sees_everybodys(self):
        _, db = self._call(self.ROWS, supervisor=True)
        db.get_leads_needing_client_email.assert_called_once_with(None, 12)

    def test_a_supervisor_is_told_it_is_everybodys(self):
        m, _ = self._call(self.ROWS, supervisor=True)
        self.assertIn("everybody", _said(m))

    def test_nothing_waiting_says_so_and_still_shows_the_usage(self):
        m, _ = self._call([])
        said = _said(m)
        self.assertIn("Nothing is waiting", said)
        self.assertIn("your leads", said)
        self.assertIn("/email <REFERENCE>", said)
        self.assertIsNone(m.reply_text.await_args[1].get("reply_markup"))

    def test_the_callback_fits_telegrams_limit(self):
        m, _ = self._call(self.ROWS)
        kb = m.reply_text.await_args[1]["reply_markup"].inline_keyboard
        for row in kb:
            for b in row:
                self.assertTrue(b.callback_data.startswith(B.SET_EMAIL_CB))
                self.assertLessEqual(len(b.callback_data.encode()), 64,
                                     b.callback_data)

    def test_its_prefix_collides_with_nothing(self):
        """Several handlers match a bare ^prefix, so a new one must not be a
        prefix of an existing one, or have one as its prefix."""
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        others = {"ins_email_", "ins_card_", "tag_email_", "rlv_", "instantpdf_",
                  "seldrv_", "selgrp_", "selsrc_", "select_driver_", "tset_"}
        for o in others:
            self.assertFalse(B.SET_EMAIL_CB.startswith(o), o)
            self.assertFalse(o.startswith(B.SET_EMAIL_CB), o)
        self.assertIn('pattern=f"^{SET_EMAIL_CB}"', src)


class TappingOneTest(unittest.TestCase):

    def _tap(self, lead):
        q = mock.MagicMock()
        q.data = B.SET_EMAIL_CB + LEAD
        q.message = _msg()
        u = mock.MagicMock()
        u.callback_query = q
        db = mock.MagicMock()
        db.get_lead_by_id.return_value = lead
        with mock.patch.object(B, "db", db), \
             mock.patch.object(B, "_safe_answer_callback_query", new=mock.AsyncMock()), \
             mock.patch.object(B, "_client_display_name_from_lead", return_value="John Damian"):
            run(B.handle_client_email_pick(u, mock.MagicMock()))
        return q.message

    def test_it_hands_back_the_line_to_finish(self):
        m = self._tap({"id": LEAD, "reference_id": "LAB4CDVZ"})
        said = _said(m)
        self.assertIn("John Damian", said)
        self.assertIn("/email LAB4CDVZ", said)

    def test_a_lead_that_is_gone_says_so(self):
        m = self._tap(None)
        self.assertIn("gone", _said(m))


def _real_database_source(method):
    """The REAL Database class, whatever other suites did to the module.

    Several dispatch suites assign utils.database.Database = MagicMock() at
    import time and never restore it, and inspect.getsource cannot read a
    MagicMock. Load a private copy straight from the file instead -- the same
    trick tests/test_receipts_board_v3.py already uses.
    """
    import importlib.util
    import inspect
    spec = importlib.util.spec_from_file_location(
        "_email_cmd_real_udb", str(ROOT / "utils" / "database.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return inspect.getsource(getattr(mod.Database, method))


class TheQueryAsksTheRightQuestionTest(unittest.TestCase):
    """The part that made a supervisor's list shorter than an issuer's."""

    def test_it_filters_on_the_flag_not_on_recency(self):
        src = _real_database_source("get_leads_needing_client_email")
        self.assertIn('.eq(flag, True)', src)
        self.assertIn('fetch("wants_insurance"', src)
        self.assertIn('fetch("wants_tag_email"', src)

    def test_the_blank_email_check_stays_in_python(self):
        """The column holds '' as well as NULL depending on which path wrote the
        lead, so is_("email", "null") would miss every blank-string one."""
        src = _real_database_source("get_leads_needing_client_email")
        self.assertIn('str(r.get("email") or "").strip()', src)
        # The CALL, not the note in the docstring that says why it is not made.
        self.assertNotIn('q.is_("email"', src)
        self.assertNotIn('.eq("email"', src)

    def test_a_lead_whose_card_already_went_is_not_waiting(self):
        src = _real_database_source("get_leads_needing_client_email")
        self.assertIn('insurance_card_sent_at', src)
        self.assertIn('tag_emailed_at', src)


if __name__ == "__main__":
    unittest.main()
