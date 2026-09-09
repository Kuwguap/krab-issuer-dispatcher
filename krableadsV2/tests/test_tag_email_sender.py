r"""The client's temporary tag comes from the tag business.

Asked for: "the email lead to client should use a different email also linked by
resend info@tristatetags.com / runs on the same api key so set that up".

One Resend account and one API key, two senders. The FS-20 insurance card has
always gone out as Tri State Coverage, and a client receiving a temporary TAG
from an insurance agency is a confusing piece of post.

The thing this file guards: the override must not leak the other way. The card
keeps the insurance sender, and a caller that passes nothing still gets
RESEND_FROM — otherwise a change meant for the tag quietly re-brands every
insurance card that has been going out for months.

Run:  venv\Scripts\python.exe -m pytest tests/test_tag_email_sender.py -q
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

from utils import resend_client as rc  # noqa: E402


class TheTagHasItsOwnSenderTest(unittest.TestCase):

    def test_it_defaults_to_the_address_that_was_asked_for(self):
        from config import Config
        self.assertIn("info@tristatetags.com", Config.RESEND_TAG_FROM)

    def test_it_can_be_pointed_elsewhere_without_a_deploy(self):
        with mock.patch("config.Config.RESEND_TAG_FROM", "Someone <a@b.com>"):
            self.assertEqual("Someone <a@b.com>", rc.get_tag_from_address())

    def test_blanking_it_falls_back_to_the_sender_known_to_work(self):
        """A misconfiguration should degrade to the address that has been
        delivering insurance cards, not to no email at all."""
        with mock.patch("config.Config.RESEND_TAG_FROM", ""), \
             mock.patch("config.Config.RESEND_FROM", "Coverage <hello@x.com>"):
            self.assertEqual("Coverage <hello@x.com>", rc.get_tag_from_address())

    def test_the_insurance_sender_is_untouched(self):
        with mock.patch("config.Config.RESEND_FROM", "Coverage <hello@x.com>"), \
             mock.patch("config.Config.RESEND_TAG_FROM", "Tags <info@y.com>"):
            self.assertEqual("Coverage <hello@x.com>", rc.get_resend_from_address())


class TheSenderReachesResendTest(unittest.TestCase):

    def _send(self, **kw):
        sent = {}

        class _Emails:
            @staticmethod
            def send(params):
                sent.update(params)
                return {"id": "e1"}

        fake = mock.MagicMock()
        fake.Emails = _Emails
        with mock.patch.object(rc, "get_resend_client", return_value=fake), \
             mock.patch.object(rc, "get_resend_from_address",
                               return_value="Coverage <hello@x.com>"):
            rc.send_insurance_card_email(
                to_address="client@example.com", subject="s", body="b",
                pdf_bytes=b"%PDF-1.4", pdf_filename="tag.pdf", **kw)
        return sent

    def test_an_override_is_the_sender_resend_is_given(self):
        sent = self._send(from_address="Tags <info@tristatetags.com>")
        self.assertEqual("Tags <info@tristatetags.com>", sent["from"])

    def test_no_override_keeps_the_insurance_sender(self):
        """The card has been going out under this address for months."""
        self.assertEqual("Coverage <hello@x.com>", self._send()["from"])

    def test_a_blank_override_is_not_a_sender(self):
        """An empty env var must fall through, not send with an empty From."""
        self.assertEqual("Coverage <hello@x.com>", self._send(from_address="  ")["from"])

    def test_the_attachment_still_rides_along(self):
        sent = self._send(from_address="Tags <info@tristatetags.com>")
        self.assertEqual("tag.pdf", sent["attachments"][0]["filename"])
        self.assertEqual(["client@example.com"], sent["to"])


class TheSweepUsesItTest(unittest.TestCase):

    def test_the_client_tag_send_asks_for_the_tag_sender(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        i = src.index('subject = f"Your temporary tag')
        block = src[i:i + 1600]
        self.assertIn("from_address=rc.get_tag_from_address()", block)

    def test_the_insurance_card_send_does_not(self):
        """Two senders, and only one of them changed."""
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        self.assertEqual(1, src.count("from_address=rc.get_tag_from_address()"))


if __name__ == "__main__":
    unittest.main()
