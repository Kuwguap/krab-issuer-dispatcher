r"""A driver's Accept stands on its own, and a reassign takes the lead back.

Two changes the office asked for, both about the same thing: a team's Accept and
a driver's Accept are separate events that must not wait on each other.

  * The driver who accepts gets the tag. It used to defer -- "their Accept
    releases the tag, not this driver's" -- so on any lead that also went to a
    team, which is most of them, the accepting driver got a details card and no
    tag at all. And when a tag WAS released, the target was the group's chat, so
    even then the document never reached the person driving to the client.

  * Reassigning takes the old driver's copy back. They had the client's name,
    home address, the one-time phone link and a printable tag for a car that is
    now somebody else's job.

Run:  venv\Scripts\python.exe -m pytest tests/test_accepts_are_independent.py -q
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


class TheDriverWhoAcceptsGetsTheTagTest(unittest.TestCase):

    def setUp(self):
        self.body = _fn("handle_accept_lead")

    def test_it_no_longer_defers_to_the_team(self):
        """This was the whole bug: a team having been OFFERED the lead meant the
        driver's own Accept released nothing."""
        self.assertNotIn("their Accept releases the tag, \n", self.body)
        self.assertNotIn("not this driver's.\", lead_id)", self.body)

    def test_the_tag_goes_to_the_accepting_driver(self):
        self.assertIn("tag_targets = [query.message.chat_id]", self.body)

    def test_the_group_is_not_sent_a_second_copy_when_it_was_offered(self):
        """A team that was offered the lead posts its own tag on its own Accept;
        adding their chat here would put two in the same place."""
        after = self.body.split("tag_targets = [query.message.chat_id]", 1)[1]
        i_offered = after.index("if offered_to_a_team:")
        i_append = after.index("tag_targets.append(gcid)")
        self.assertLess(i_offered, i_append,
                        "the group chat must only be added on the else branch")

    def test_an_instant_tag_still_waits_for_payment(self):
        """There the tag IS the product; only a cleared card or the password
        releases it."""
        self.assertIn('if lead.get("instant_tag"):', self.body)
        self.assertIn("waits for payment or a", self.body)

    def test_the_tag_still_goes_out_exactly_once_per_audience(self):
        self.assertEqual(1, self.body.count("_send_all_tag_pdfs("))


class AReassignTakesTheLeadBackTest(unittest.TestCase):

    def setUp(self):
        self.body = _fn("handle_reassign_lead")

    def test_the_old_drivers_copy_is_withdrawn(self):
        self.assertIn("_delete_lead_messages_in_chat", self.body)

    def test_the_old_driver_is_told_what_happened(self):
        self.assertIn("withdrawn", self.body)

    def test_what_could_not_be_unsent_is_admitted(self):
        """Telegram refuses deletes older than 48h. Silently assuming it went is
        how a customer's address stays in an ex-driver's chat."""
        self.assertIn("left_behind", self.body)
        self.assertIn("Please delete the details and tag you were sent", self.body)

    def test_supervisors_are_told_who_moved_it_and_from_whom(self):
        self.assertIn("Lead reassigned", self.body)
        self.assertIn("From:", self.body)
        self.assertIn("By:", self.body)

    def test_supervisors_still_get_the_note(self):
        self.assertIn("_global_supervisory_chat_ids()", self.body)

    def test_a_supervisor_may_reassign_anything(self):
        self.assertIn("_user_is_global_supervisor(presser_id)", self.body)


class TheWithdrawalOnlyTouchesThatChatTest(unittest.TestCase):

    def setUp(self):
        self.body = _fn("_delete_lead_messages_in_chat")

    def test_it_filters_to_the_one_chat(self):
        """The team keeps its copy, and the new driver is about to get theirs."""
        self.assertIn("_norm_chat_id(cid) == want", self.body)

    def test_it_counts_what_it_could_not_remove(self):
        self.assertIn("left += 1", self.body)

    def test_it_only_forgets_what_it_actually_removed(self):
        """Forgetting a message we failed to delete would make a retry
        impossible."""
        self.assertIn("_norm_chat_id(cid) != want", self.body)

    def test_a_missing_chat_is_a_no_op(self):
        self.assertIn("if want is None:", self.body)


class WhatTheDriverWasHandedIsRememberedTest(unittest.TestCase):
    """Nothing can be withdrawn that was never recorded."""

    def test_the_details_card_is_remembered(self):
        self.assertIn("_remember_dispatch_message", _fn("_send_driver_lead_details"))

    def test_the_tag_document_is_remembered(self):
        body = _fn("_build_and_send_tag_pdf")
        self.assertIn("_remember_dispatch_message", body)
        self.assertIn("getattr(_doc,", body)


if __name__ == "__main__":
    unittest.main()
