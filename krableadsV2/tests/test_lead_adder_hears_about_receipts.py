r"""The person who added the client hears when the receipt lands.

Asked for: "client adders should get notification or alert when driver uploads a
receipt".

They were the only party who never heard. Supervisors got the receipt and the ST
chat got the receipt; the issuer who entered the client -- the one the client
actually rings to ask whether it is done -- had to go and look at the board.
This finishes the pair with _notify_initiator_lead_accepted_summary, which
already DMs them when a driver ACCEPTS.

Both ways in are covered, and they are genuinely different code:

  * Telegram upload -> _notify_supervisory_receipt_submission, which has a bot.
  * The receipt LINK -> admin_dashboard, a separate process with no bot at all,
    which posts to the Telegram HTTP API directly. That path notified NOBODY
    before this change -- a driver who used the link was invisible, which is the
    opposite of what the link was built for.

Three ways to tell the wrong person, each pinned below: the adder may also be a
supervisory chat (twice), the adder may BE the uploader (drivers add leads too,
and telling somebody what they just did is noise), and user_id may be junk on an
old row.

Run:  venv\Scripts\python.exe -m pytest tests/test_lead_adder_hears_about_receipts.py -q
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

import utils.database as udb  # noqa: E402

if "bot" not in sys.modules:
    udb.Database = mock.MagicMock()
import bot  # noqa: E402
import admin_dashboard as ad  # noqa: E402
from config import Config  # noqa: E402

SRC = (ROOT / "bot.py").read_text(encoding="utf-8")
ADM = (ROOT / "admin_dashboard.py").read_text(encoding="utf-8")

ADDER = 4242
DRIVER = 9001
SUP = 11
LEAD = {"id": "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa", "reference_id": "REF12345",
        "user_id": ADDER}


def run(coro):
    import asyncio
    return asyncio.new_event_loop().run_until_complete(coro)


class TheTelegramPathTest(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.sent = []

        async def send(chat_id, label, text=None):
            self.sent.append((chat_id, label, text))

        self.send = send
        self.update = mock.MagicMock()
        self.update.effective_user.id = DRIVER

    async def _tell(self, lead=None, seen=None):
        return await bot._tell_the_lead_adder(
            mock.MagicMock(), self.update, lead if lead is not None else LEAD,
            "New receipt sent\nReference: REF12345",
            sent_norm=seen if seen is not None else set(), send=self.send)

    async def test_the_adder_is_told(self):
        self.assertTrue(await self._tell())
        self.assertEqual([ADDER], [c for c, _, _ in self.sent])

    async def test_the_message_says_it_is_about_their_client(self):
        await self._tell()
        body = self.sent[0][2]
        self.assertIn("your client", body)
        self.assertIn("REF12345", body)

    async def test_it_is_plain_text(self):
        """These go out with no parse_mode, so a tag would be shown, not
        rendered -- and a client's name is free text that would break a parse."""
        await self._tell()
        self.assertNotIn("<b>", self.sent[0][2])

    async def test_a_supervisor_who_added_it_is_not_told_twice(self):
        seen = {bot._norm_chat_id(ADDER)}
        self.assertFalse(await self._tell(seen=seen))
        self.assertEqual([], self.sent)

    async def test_the_uploader_is_not_told_what_they_just_did(self):
        """Drivers add leads too."""
        self.update.effective_user.id = ADDER
        self.assertFalse(await self._tell())
        self.assertEqual([], self.sent)

    async def test_a_lead_with_no_adder(self):
        for lead in ({"id": "x"}, {"id": "x", "user_id": None}):
            with self.subTest(lead=lead):
                self.assertFalse(await self._tell(lead=lead))
        self.assertEqual([], self.sent)

    async def test_an_unusable_user_id_is_skipped_not_crashed(self):
        for junk in ("", "   ", "not-a-number", [], {}):
            with self.subTest(junk=junk):
                self.assertFalse(await self._tell(lead={"id": "x", "user_id": junk}))
        self.assertEqual([], self.sent)

    async def test_a_string_user_id_still_works(self):
        """Some rows carry it as text."""
        self.assertTrue(await self._tell(lead=dict(LEAD, user_id=str(ADDER))))
        self.assertEqual([ADDER], [c for c, _, _ in self.sent])

    async def test_an_unreachable_adder_does_not_raise(self):
        """The receipt is already stored; a blocked DM is not a failed upload."""
        async def boom(*a, **k):
            raise RuntimeError("blocked")
        self.send = boom
        self.assertFalse(await self._tell())

    async def test_they_are_marked_as_told(self):
        """So a later pass over the same set cannot send it again."""
        seen = set()
        await self._tell(seen=seen)
        self.assertIn(bot._norm_chat_id(ADDER), seen)


class ItIsWiredIntoTheReceiptNoticeTest(unittest.TestCase):

    def test_the_supervisory_notice_calls_it(self):
        i = SRC.index("async def _notify_supervisory_receipt_submission(")
        body = SRC[i:SRC.index("\nasync def _tell_the_lead_adder", i)]
        self.assertIn("_tell_the_lead_adder(", body)

    def test_it_runs_after_the_supervisors_so_it_can_skip_them(self):
        """The dedupe set only knows who was reached once they have been."""
        i = SRC.index("async def _notify_supervisory_receipt_submission(")
        body = SRC[i:SRC.index("\nasync def _tell_the_lead_adder", i)]
        self.assertLess(body.index('_send_caption_and_receipt(sup_chat_id'),
                        body.index("_tell_the_lead_adder("))

    def test_the_shared_caption_is_not_shadowed(self):
        """_send_caption_and_receipt grew a caption parameter. If the closed-over
        text kept the same name, every chat would get the adder's wording."""
        i = SRC.index("async def _notify_supervisory_receipt_submission(")
        body = SRC[i:SRC.index("\nasync def _tell_the_lead_adder", i)]
        self.assertIn("_caption = (", body)
        self.assertIn("caption = text if text is not None else _caption", body)


class ThePortalPathTest(unittest.TestCase):
    """It notified nobody at all before this."""

    def setUp(self):
        self.sent = []

    def _run(self, lead_row, sup=""):
        d = mock.MagicMock()
        (d.client.table.return_value.select.return_value.eq.return_value
         .limit.return_value.execute.return_value) = mock.MagicMock(data=[lead_row])

        def fake(chat_id, text):
            self.sent.append((chat_id, text))
            return True

        with mock.patch.object(ad, "db", d), \
             mock.patch.object(ad, "_tg_send_message", fake), \
             mock.patch.object(Config, "SUPERVISORY_TELEGRAM_ID", sup, create=True):
            return ad._notify_portal_receipt(LEAD["id"], "REF12345")

    def test_the_adder_is_told(self):
        self.assertEqual(1, self._run({"user_id": ADDER}))
        self.assertEqual([ADDER], [c for c, _ in self.sent])
        self.assertIn("your client", self.sent[0][1])

    def test_the_message_carries_the_reference_and_a_link(self):
        self._run({"user_id": ADDER})
        body = self.sent[0][1]
        self.assertIn("REF12345", body)
        self.assertIn(LEAD["id"], body)

    def test_it_says_the_link_was_used_not_telegram(self):
        """Otherwise the office cannot tell why nothing arrived in the chat."""
        self._run({"user_id": ADDER})
        self.assertIn("not Telegram", self.sent[0][1])

    def test_supervisors_are_told_too(self):
        self.assertEqual(2, self._run({"user_id": ADDER}, sup=str(SUP)))
        self.assertEqual([ADDER, SUP], [c for c, _ in self.sent])

    def test_an_adder_who_is_also_a_supervisor_hears_once(self):
        self.assertEqual(1, self._run({"user_id": ADDER}, sup=str(ADDER)))

    def test_several_supervisors_in_one_setting(self):
        self._run({"user_id": None}, sup="11, 22 ;33")
        self.assertEqual([11, 22, 33], [c for c, _ in self.sent])

    def test_a_pasted_equals_sign_is_tolerated(self):
        """Render's UI adds one often enough that _parse_chat_id strips it."""
        self._run({"user_id": None}, sup="= -100123")
        self.assertEqual([-100123], [c for c, _ in self.sent])

    def test_rubbish_in_the_setting_is_skipped(self):
        self._run({"user_id": None}, sup="11,notanumber,,22")
        self.assertEqual([11, 22], [c for c, _ in self.sent])

    def test_a_lead_that_cannot_be_read_still_tells_the_supervisors(self):
        d = mock.MagicMock()
        d.client.table.side_effect = Exception("down")

        def fake(chat_id, text):
            self.sent.append((chat_id, text))
            return True

        with mock.patch.object(ad, "db", d), \
             mock.patch.object(ad, "_tg_send_message", fake), \
             mock.patch.object(Config, "SUPERVISORY_TELEGRAM_ID", str(SUP), create=True):
            self.assertEqual(1, ad._notify_portal_receipt(LEAD["id"], "REF12345"))
        self.assertEqual([SUP], [c for c, _ in self.sent])

    def test_the_upload_never_fails_because_the_notice_did(self):
        """The bytes are stored before this runs."""
        i = ADM.index("def receipt_portal(token)")
        body = ADM[i:ADM.index("\n@app.route", i)]
        self.assertLess(body.index("db.save_receipt_file("),
                        body.index("_notify_portal_receipt("))
        after = body[body.index("_notify_portal_receipt("):]
        self.assertIn("except Exception", body[body.index("    try:\n        reached"):])
        self.assertIn("heading=\"Receipt received\"", after)


class TheSenderIsHonestAboutFailureTest(unittest.TestCase):

    def test_a_refused_send_returns_false(self):
        with mock.patch("requests.post",
                        return_value=mock.MagicMock(ok=False, status_code=400, text="no")), \
             mock.patch.object(Config, "TELEGRAM_BOT_TOKEN", "t", create=True):
            self.assertFalse(ad._tg_send_message(1, "x"))

    def test_telegram_saying_not_ok_is_a_failure(self):
        """HTTP 200 with {"ok": false} is the shape that reads as success."""
        with mock.patch("requests.post",
                        return_value=mock.MagicMock(ok=True, json=lambda: {"ok": False})), \
             mock.patch.object(Config, "TELEGRAM_BOT_TOKEN", "t", create=True):
            self.assertFalse(ad._tg_send_message(1, "x"))

    def test_no_token_is_not_an_exception(self):
        with mock.patch.object(Config, "TELEGRAM_BOT_TOKEN", "", create=True):
            self.assertFalse(ad._tg_send_message(1, "x"))

    def test_a_network_error_is_not_an_exception(self):
        with mock.patch("requests.post", side_effect=RuntimeError("down")), \
             mock.patch.object(Config, "TELEGRAM_BOT_TOKEN", "t", create=True):
            self.assertFalse(ad._tg_send_message(1, "x"))


if __name__ == "__main__":
    unittest.main()
