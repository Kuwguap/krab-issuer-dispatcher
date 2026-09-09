r"""Four things asked for together, all of them about not flying blind.

  1. "at the end of lead ... add email tag to client" — on the lead-sent
     message, beside reassign driver / dispatcher / another tag / skip dispatch.
  2. "when emails sends to client alert all supervisory who sent, the client
     name, the refrence and add the tag that was sent".
  3. "/dump to see all the actions going on in the bot ... 10 per page and
     inline buttons to change page".
  4. a /settings toggle so the bot stops deleting what people type and reposts
     its reading underneath instead, "so bots message is always the last".

The measurement that produced them: eleven leads asked for a tag email and not
one had ever been sent, because the release button existed only on the tag PDF
and only for a lead already carrying both the toggle and the address. Nothing
announced either end of that, so a month passed with nobody noticing.

Run:  venv\Scripts\python.exe -m pytest tests/test_visibility_batch.py -q
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


class EmailTagToClientOnTheLeadSentMessageTest(unittest.TestCase):

    def _kb(self):
        return B._after_send_keyboard(LEAD)

    def test_it_is_there_with_the_other_four(self):
        labels = [b.text for row in self._kb().inline_keyboard for b in row]
        for want in ("Reassign driver", "Reassign dispatcher",
                     "Another tag", "Skip Dispatch", "Email tag to client"):
            self.assertTrue(any(want in l for l in labels), (want, labels))

    def test_it_is_not_a_mis_tap_away_from_skip_dispatch(self):
        """Skip Dispatch withdraws the lead from the team and every driver.
        These two must never share a row."""
        for row in self._kb().inline_keyboard:
            texts = " ".join(b.text for b in row)
            self.assertFalse("Skip Dispatch" in texts and "Email tag" in texts, texts)

    def test_it_reaches_the_release_handler(self):
        data = [b.callback_data for row in self._kb().inline_keyboard for b in row]
        self.assertIn(B.TAG_EMAIL_CB + LEAD, data)
        for d in data:
            self.assertLessEqual(len(d.encode()), 64, d)

    def test_tapping_it_before_a_tag_exists_says_what_it_is_doing(self):
        """That message is posted before any driver has accepted, so the sweep
        would build the tag — and building one allocates a plate."""
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        i = src.index("async def handle_tag_email_to_client(")
        block = src[i:i + 3000]
        self.assertIn("krab_tag_pdf_sent_at", block)
        self.assertIn("has not gone to the team yet", block)


class SupervisorsHearAboutTheSendTest(unittest.TestCase):

    def _notice(self, lead, fail_document=False):
        sent = []
        ctx = mock.MagicMock()

        async def send_document(chat_id, document, caption=None, **kw):
            if fail_document:
                raise RuntimeError("can_send_documents=false")
            sent.append(("doc", chat_id, caption))

        async def send_message(chat_id, text=None, **kw):
            sent.append(("text", chat_id, text))

        ctx.bot.send_document = mock.AsyncMock(side_effect=send_document)
        ctx.bot.send_message = mock.AsyncMock(side_effect=send_message)
        with mock.patch.object(B, "_global_supervisory_chat_ids", return_value=[111, 222]), \
             mock.patch.object(B, "_client_display_name_from_lead", return_value="John Damian"):
            run(B._tell_supervisors_tag_emailed(ctx, lead, "client@example.com",
                                                b"%PDF-1.4", "tag.pdf"))
        return sent

    def test_every_supervisor_gets_the_tag_and_the_facts(self):
        sent = self._notice({"id": LEAD, "reference_id": "LAB4CDVZ"})
        self.assertEqual(["doc", "doc"], [s[0] for s in sent])
        self.assertEqual([111, 222], [s[1] for s in sent])
        caption = sent[0][2]
        for needle in ("John Damian", "LAB4CDVZ", "client@example.com", "emailed"):
            self.assertIn(needle, caption, needle)

    def test_it_says_who_released_it(self):
        B._TAG_EMAIL_APPROVED_BY[LEAD] = "@kingkrab"
        caption = self._notice({"id": LEAD, "reference_id": "LAB4CDVZ"})[0][2]
        self.assertIn("@kingkrab", caption)

    def test_a_chat_that_refuses_documents_still_hears(self):
        """A supervisor barred from receiving files must not simply hear
        nothing — that is the failure this whole batch is about."""
        sent = self._notice({"id": LEAD, "reference_id": "LAB4CDVZ"},
                            fail_document=True)
        self.assertEqual(["text", "text"], [s[0] for s in sent])
        self.assertIn("LAB4CDVZ", sent[0][2])

    def test_it_fires_on_the_send_not_the_approval(self):
        """"Approved" was a dead end for eleven leads and nobody noticed."""
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        i = src.index('{"tag_emailed_at": now_iso,')
        self.assertIn("_tell_supervisors_tag_emailed(", src[i:i + 900])


class TheDumpTest(unittest.TestCase):

    ROWS = [
        {"id": "l1", "reference_id": "AAA11111", "created_at": "2026-09-09T10:00:00+00:00",
         "telegram_username": "kaye7600", "email": "c@x.com", "wants_tag_email": True},
        {"id": "l2", "reference_id": "BBB22222", "created_at": "2026-09-08T10:00:00+00:00",
         "telegram_username": "jb", "email": "d@x.com",
         "tag_emailed_at": "2026-09-08T12:00:00+00:00"},
        {"id": "l3", "reference_id": "CCC33333", "created_at": "2026-09-07T10:00:00+00:00",
         "telegram_username": "jb", "email": "e@x.com",
         "tag_email_approved_at": "2026-09-07T11:00:00+00:00"},
    ]

    def _events(self, rows=None):
        db = mock.MagicMock()
        db.get_recent_leads_for_dump.return_value = rows if rows is not None else self.ROWS
        with mock.patch.object(B, "db", db), \
             mock.patch.object(B, "_client_display_name_from_lead", return_value="A Client"):
            return B._dump_events()

    def test_it_reports_who_created_a_lead(self):
        said = " ".join(e["what"] for e in self._events())
        self.assertIn("lead entered by kaye7600", said)

    def test_it_reports_an_email_that_actually_went(self):
        said = " ".join(e["what"] for e in self._events())
        self.assertIn("tag EMAILED to d@x.com", said)

    def test_it_reports_an_address_whose_tag_never_went(self):
        """The state that held eleven leads for a month, invisibly."""
        said = " ".join(e["what"] for e in self._events())
        self.assertIn("has c@x.com — tag NEVER sent", said)

    def test_it_reports_a_release_that_has_not_landed(self):
        said = " ".join(e["what"] for e in self._events())
        self.assertIn("tag released — NOT sent yet", said)

    def test_newest_first(self):
        ats = [e["at"] for e in self._events()]
        self.assertEqual(ats, sorted(ats, reverse=True))

    def test_ten_to_a_page_with_buttons_to_move(self):
        events = [{"at": "2026-09-09T%02d:00:00+00:00" % (23 - i), "icon": "🆕",
                   "ref": "R%d" % i, "client": "C", "what": "did a thing"}
                  for i in range(24)]
        text, kb = B._dump_page(events, 0)
        self.assertEqual(10, text.count("did a thing"))
        self.assertIn("Page 1 of 3", text)
        self.assertEqual(["Older ➡️"], [b.text for r in kb.inline_keyboard for b in r])

        text, kb = B._dump_page(events, 1)
        self.assertIn("Page 2 of 3", text)
        self.assertEqual(["⬅️ Newer", "Older ➡️"],
                         [b.text for r in kb.inline_keyboard for b in r])

        text, kb = B._dump_page(events, 2)
        self.assertIn("Page 3 of 3", text)
        self.assertEqual(["⬅️ Newer"], [b.text for r in kb.inline_keyboard for b in r])

    def test_a_page_past_the_end_is_clamped_not_crashed(self):
        text, _ = B._dump_page([], 99)
        self.assertIn("Nothing recorded yet", text)

    def test_it_is_supervisors_only(self):
        """It spans every issuer's leads and every client's address."""
        msg = mock.MagicMock()
        msg.reply_text = mock.AsyncMock()
        u = mock.MagicMock()
        u.effective_message = msg
        u.effective_user = types.SimpleNamespace(id=7)
        with mock.patch.object(B, "_user_is_global_supervisor", return_value=False), \
             mock.patch.object(B, "db", mock.MagicMock()):
            run(B.cmd_dump(u, mock.MagicMock()))
        self.assertIn("Supervisors only", msg.reply_text.await_args[0][0])

    def test_one_read_for_the_whole_log(self):
        db = mock.MagicMock()
        db.get_recent_leads_for_dump.return_value = self.ROWS
        with mock.patch.object(B, "db", db), \
             mock.patch.object(B, "_client_display_name_from_lead", return_value="C"):
            B._dump_events()
        self.assertEqual(1, db.get_recent_leads_for_dump.call_count)

    def test_it_is_registered_and_on_the_menu(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        self.assertIn('CommandHandler(["dump", "activity", "log"], cmd_dump)', src)
        self.assertIn('BotCommand("dump"', src)
        self.assertIn('pattern=f"^{DUMP_CB}"', src)


class TheChatHistoryToggleTest(unittest.TestCase):

    def _keep(self, on):
        db = mock.MagicMock()
        db.get_setting.return_value = "1" if on else "0"
        return mock.patch.object(B, "db", db)

    def test_off_by_default_is_how_the_bot_has_always_behaved(self):
        db = mock.MagicMock()
        db.get_setting.return_value = None
        with mock.patch.object(B, "db", db):
            self.assertFalse(B._keep_user_messages())

    def test_on_it_keeps_what_was_typed(self):
        msg = mock.MagicMock()
        msg.text = "a lead"
        msg.delete = mock.AsyncMock()
        u = mock.MagicMock()
        u.effective_message = msg
        u.effective_chat = types.SimpleNamespace(type="private")
        for k in ("voice", "audio", "photo", "document"):
            setattr(msg, k, None)
        with self._keep(True):
            run(B._autoclean_user_msg(u, mock.MagicMock()))
        msg.delete.assert_not_awaited()

    def test_off_it_still_cleans_up(self):
        msg = mock.MagicMock()
        msg.text = "a lead"
        msg.delete = mock.AsyncMock()
        u = mock.MagicMock()
        u.effective_message = msg
        u.effective_chat = types.SimpleNamespace(type="private")
        for k in ("voice", "audio", "photo", "document"):
            setattr(msg, k, None)
        with self._keep(False):
            run(B._autoclean_user_msg(u, mock.MagicMock()))
        msg.delete.assert_awaited_once()

    def test_on_the_card_is_reposted_at_the_bottom(self):
        """Their messages are still there, so a card edited ten messages up is
        exactly what they said they could not read."""
        ctx = mock.MagicMock()
        ctx.user_data = {"review_chat_id": -100, "review_message_id": 5}
        with self._keep(True), \
             mock.patch.object(B, "_reanchor_review_card", new=mock.AsyncMock()) as re_:
            run(B._update_review_message_text(ctx, {"name": "x"}))
        re_.assert_awaited_once()

    def test_the_reposts_own_fallback_does_not_ask_for_another(self):
        """Otherwise a failed repost loops."""
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        self.assertIn("_update_review_message_text(context, state_data, allow_reanchor=False)",
                      src)

    def test_the_screen_and_the_switch_exist(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        self.assertIn('callback_data="tset_keep"', src)
        self.assertIn('if data == "tset_keep_toggle":', src)
        self.assertIn('"tset_keep": _settings_view_keep,', src)
        self.assertIn("KEEP_MESSAGES_KEY", src)


if __name__ == "__main__":
    unittest.main()
