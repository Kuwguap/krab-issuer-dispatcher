r"""The images the issuer uploaded follow the lead to whoever takes it.

An issuer sends a photo of the title/registration; the bot reads it AND the
picture rides along to the team and the driver who end up working the job.

That held on the ordinary Accept and it still does. Skip Dispatch and the paid
Instant Tag were built later and bypass Accept entirely, and they were the one
delivery path that dropped the paperwork on the floor -- so an office that moved
to instant tags stopped seeing any of it.

Run:  venv\Scripts\python.exe -m pytest tests/test_paperwork_follows_the_lead.py -q
"""
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy-key")

SRC = (ROOT / "bot.py").read_text(encoding="utf-8")


def _fn(name: str) -> str:
    body = SRC.split(f"async def {name}(", 1)[1]
    return body.split("\nasync def ", 1)[0]


class EveryDeliveryPathSendsThePaperworkTest(unittest.TestCase):

    def test_the_ordinary_team_accept_still_does(self):
        self.assertIn("_forward_phase1_attached_files_to_targets",
                      _fn("handle_accept_group_offer"))

    def test_the_driver_who_accepts_still_does(self):
        self.assertIn("_forward_accepted_lead_files", _fn("_send_driver_lead_details"))

    def test_skip_dispatch_sends_it_to_the_driver(self):
        """Password release and the paid Instant Tag both come through here."""
        self.assertIn("_forward_lead_paperwork", _fn("_deliver_skip_dispatch"))

    def test_skip_dispatch_sends_it_to_the_groups_too(self):
        body = _fn("_deliver_skip_dispatch")
        after_groups = body.split("for i, g in enumerate(groups):", 1)
        self.assertEqual(2, len(after_groups), "the group loop moved")
        self.assertIn("_forward_lead_paperwork", after_groups[1],
                      "the groups posted to do not get the paperwork")

    def test_the_driver_gets_it_before_the_groups_do(self):
        """The driver is the one doing the delivery; they should not be last."""
        body = _fn("_deliver_skip_dispatch")
        first = body.index("_forward_lead_paperwork")
        loop = body.index("for i, g in enumerate(groups):")
        self.assertLess(first, loop)


class TheHelperIsSafeToCallAnywhereTest(unittest.TestCase):

    def setUp(self):
        self.body = _fn("_forward_lead_paperwork")

    def test_no_chat_is_a_no_op(self):
        self.assertIn("if not chat_id:", self.body)

    def test_it_re_reads_when_the_row_has_no_descriptors(self):
        """The sweep's lead row and the review's row are not the same object."""
        self.assertIn("db.get_lead_by_id", self.body)

    def test_the_re_read_is_off_the_event_loop(self):
        self.assertIn("asyncio.to_thread", self.body)

    def test_it_can_never_fail_a_delivery(self):
        """The tag matters more than the paperwork: a chat that refuses
        documents must not take the whole delivery down with it."""
        self.assertIn("except Exception", self.body)
        self.assertNotIn("raise", self.body)

    def test_a_lead_with_no_files_sends_nothing(self):
        self.assertIn("if not (isinstance(att, list) and att):", self.body)


class TheDescriptorShapesAreBothStillHandledTest(unittest.TestCase):
    """Two payload shapes exist on real rows: legacy Telegram file_id
    references, and inline base64 for the phone-censored copies."""

    def test_the_forwarder_handles_file_id_and_base64(self):
        body = _fn("_forward_phase1_attached_files_to_targets")
        self.assertIn("file_id", body)
        self.assertIn("data_b64", body)


if __name__ == "__main__":
    unittest.main()
