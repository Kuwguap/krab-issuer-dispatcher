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

    def test_the_driver_also_gets_the_clients_card(self):
        """The office asked for the same message the group gets. The driver's own
        ticket has the address and the price and none of the vehicle detail -- no
        VIN, no car, no colour, no insurer, no policy number."""
        self.assertIn("_send_group_card_to_chat(", self.body)
        i_card = self.body.index("_send_group_card_to_chat(")
        i_tag = self.body.index("_send_all_tag_pdfs(")
        self.assertLess(i_card, i_tag, "the card should arrive before the tag, as in a group")

    def test_the_card_goes_to_the_driver_not_the_group(self):
        after = self.body.split("_send_group_card_to_chat(", 1)[1][:200]
        self.assertIn("query.message.chat_id", after)


class TheClientCardIsTheSameOneTest(unittest.TestCase):
    """Same builder as the group's, or it is not the same message."""

    def setUp(self):
        self.body = _fn("_send_group_card_to_chat")

    def test_it_uses_the_group_formatter(self):
        self.assertIn("_format_group_lead_message_html(", self.body)
        self.assertIn("_issue_and_expiration_for_group_display(", self.body)

    def test_it_names_who_accepted(self):
        self.assertIn("Accepted by", self.body)

    def test_it_falls_back_to_plain_text(self):
        """A card that trips Telegram's HTML parser must still arrive."""
        self.assertIn("except BadRequest:", self.body)
        self.assertIn('re.sub(r"<[^>]+>", "", full_html)', self.body)

    def test_it_is_remembered_for_a_reassign(self):
        self.assertIn("_remember_dispatch_message", self.body)

    def test_a_dead_chat_does_not_stop_the_accept(self):
        self.assertGreaterEqual(self.body.count("return"), 2)


class AReassignTakesTheLeadBackTest(unittest.TestCase):

    def setUp(self):
        self.body = _fn("_reassign_lead_to")

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
        """The permission check lives on the entry point now; the move itself is
        only ever reached through it."""
        self.assertIn("_user_is_global_supervisor(presser_id)", _fn("handle_reassign_lead"))


class ASupervisorMovesAnyLeadToAnyoneTest(unittest.TestCase):
    """"any lead any time to any driver or all driver".

    Reassign used to do exactly one thing: throw an ACCEPTED lead back to the
    whole pool. A lead nobody had accepted answered "nothing to reassign", and
    there was no way to hand one to a named person.
    """

    def setUp(self):
        self.entry = _fn("handle_reassign_lead")
        self.move = _fn("_reassign_lead_to")

    def test_an_unaccepted_lead_can_still_be_moved_by_a_supervisor(self):
        self.assertIn("if not assignment and not is_supervisor:", self.entry)

    def test_the_supervisor_is_asked_who_takes_it(self):
        self.assertIn("_reassign_target_keyboard(lead_id)", self.entry)
        self.assertIn("Who takes it?", self.entry)

    def test_the_driver_handing_it_back_is_not_asked(self):
        """They are letting go of it, not choosing a successor."""
        self.assertIn("if is_supervisor and not is_the_driver:", self.entry)

    def test_a_named_driver_gets_it_directly(self):
        self.assertIn("if to_driver_id:", self.move)
        self.assertIn("db.create_lead_assignment(lead_id, target[\"id\"]", self.move)

    def test_the_pool_is_not_also_spammed_when_one_driver_was_named(self):
        self.assertIn("if not to_driver_id and group:", self.move)
        self.assertIn("elif not to_driver_id:", self.move)

    def test_an_unreachable_driver_really_does_fall_back_to_the_pool(self):
        """The lead is already released by this point. A chained elif here meant
        the fallback branch could not run: the lead ended up belonging to nobody
        while the supervisor was told it had gone back to the pool.

        So the clearing of to_driver_id must come BEFORE the pool branches, and
        those branches must test it rather than being an elif of the named-driver
        block.
        """
        i_clear = self.move.index("to_driver_id = None")
        i_pool = self.move.index("if not to_driver_id and group:")
        self.assertLess(i_clear, i_pool,
                        "the fallback is unreachable — the lead would be lost")

    def test_supervisors_are_told_who_it_went_to(self):
        self.assertIn("To: {new_driver_name}", self.move)
        self.assertIn("From: {old_driver_name}", self.move)
        self.assertIn("By: {_moved_by}", self.move)


class TheReassignPickerFitsInACallbackTest(unittest.TestCase):
    """Two raw UUIDs behind a prefix is 81 bytes against a 64-byte limit, and
    Telegram drops the WHOLE keyboard — the message never arrives at all."""

    def setUp(self):
        self.body = SRC.split("def _reassign_target_keyboard(", 1)[1].split("\ndef ", 1)[0]

    def test_the_ids_are_short_encoded(self):
        self.assertIn("_short_uuid(str(lead_id))", self.body)
        self.assertIn('_short_uuid(str(d.get("id")))', self.body)

    def test_the_prefixes_are_short(self):
        self.assertIn('REASSIGN_PICK_CB = "rsp_"', SRC)
        self.assertIn('REASSIGN_ALL_CB = "rsa_"', SRC)
        # 4 + 22 + 22 = 48 bytes, inside the 64 the Bot API allows.
        self.assertLessEqual(len("rsp_") + 22 + 22, 64)

    def test_suspended_and_chatless_drivers_are_left_out(self):
        self.assertIn("suspended", self.body)
        self.assertIn('_parse_chat_id(d.get("driver_telegram_id"))', self.body)

    def test_all_drivers_alone_is_not_offered_as_a_choice(self):
        self.assertIn("len(rows) > 1", self.body)


class ThePickersSurviveARestartTest(unittest.TestCase):
    """No PTB persistence here: a button only reachable from inside a
    conversation goes dead the moment the process restarts."""

    def test_both_pickers_are_registered_at_the_top_level(self):
        self.assertIn('CallbackQueryHandler(handle_reassign_pick, pattern="^" + REASSIGN_PICK_CB)', SRC)
        self.assertIn('CallbackQueryHandler(handle_reassign_all, pattern="^" + REASSIGN_ALL_CB)', SRC)

    def test_only_a_supervisor_may_use_them(self):
        for name in ("handle_reassign_pick", "handle_reassign_all"):
            self.assertIn("_user_is_global_supervisor(update.effective_user.id)", _fn(name), name)


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
