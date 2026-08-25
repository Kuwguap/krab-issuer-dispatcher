r"""Drivers hear about a lead at once, and can answer it in words.

Asked for:
  * "drivers dont get any message until its accepted by group — remove that and
    make both fire … non is dependent on the other";
  * "as a driver I gotta type 'accept' or 'yes' to accept leads".

Run:  venv\Scripts\python.exe -m pytest tests/test_parallel_dispatch_and_accept.py -q
"""
import asyncio
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

DRIVER = {"id": "d1", "driver_name": "Kita", "is_active": True,
          "driver_telegram_id": "111"}


class NeitherWaitsOnTheOtherTest(unittest.TestCase):
    """The group post and the driver DMs go out together."""

    def _src(self):
        return (ROOT / "bot.py").read_text(encoding="utf-8")

    def test_finalize_fires_the_driver_dispatch_itself(self):
        body = self._src().split("async def _finalize_lead_after_notes", 1)[1]
        body = body.split("\nasync def ", 1)[0]
        self.assertIn("_fire_driver_dispatch(", body,
                      "drivers must be told at finalize, not at group accept")

    def test_it_no_longer_defers_the_dispatch(self):
        body = self._src().split("async def _finalize_lead_after_notes", 1)[1]
        body = body.split("\nasync def ", 1)[0]
        self.assertNotIn('await_mode="dispatch_pending"', body,
                         "a deferred dispatch here is the bug being fixed")

    def test_the_group_post_still_happens(self):
        body = self._src().split("async def _finalize_lead_after_notes", 1)[1]
        body = body.split("\nasync def ", 1)[0]
        self.assertIn("_post_lead_to_all_groups_for_approval", body)
        self.assertIn("_post_single_group_approval", body)

    def test_the_approval_post_is_not_duplicated(self):
        body = self._src().split("async def _finalize_lead_after_notes", 1)[1]
        body = body.split("\nasync def ", 1)[0]
        self.assertIn("skip_duplicate_full_group_post=True", body)

    def test_the_display_builder_is_shared(self):
        """One builder, so drivers and groups cannot describe the lead differently."""
        self.assertEqual(1, self._src().count("def _dispatch_display_parts("))

    def test_the_vehicle_block_is_built_from_the_card(self):
        card = {"name": "John Damian", "address": "325 Prospect St",
                "city_state_zip": "Perth Amboy, NJ 08861", "vin": "WDDZF4JB4HA036041",
                "car": "2017 M Benz E Class", "color": "White",
                "insurance_company": "Geico", "insurance_policy_number": "4570-22",
                "extra_info": "tomorrow 5pm"}
        vehicle, extra = bot._dispatch_display_parts(card, "rush please")
        for want in ("John Damian", "325 Prospect St", "WDDZF4JB4HA036041",
                     "2017 M Benz E Class", "White", "Geico"):
            self.assertIn(want, vehicle, want)
        self.assertIn("rush please", vehicle)
        self.assertIn("tomorrow 5pm", extra)

    def test_no_issuer_note_reads_as_no(self):
        vehicle, _ = bot._dispatch_display_parts({"name": "X"}, "")
        self.assertIn("📝 No", vehicle)


class ADriverCanSayAcceptTest(unittest.TestCase):

    def _say(self, text, driver=DRIVER, pending={"lead_id": "L1"}):
        msg = mock.MagicMock()
        msg.text = text
        msg.chat = mock.MagicMock(type="private")
        update = mock.MagicMock()
        update.effective_message = msg
        update.effective_user = mock.MagicMock(id=111)
        update.effective_chat = mock.MagicMock(id=111)
        db = mock.MagicMock()
        db.get_driver_pending_assignment.return_value = dict(pending) if pending else None
        seen = {}

        async def _accept(u, c):
            seen["accept"] = u.callback_query.data

        async def _decline(u, c):
            seen["decline"] = u.callback_query.data

        stop = False
        with mock.patch.object(bot, "db", db), \
                mock.patch.object(bot, "_driver_row_for_telegram_user",
                                  mock.MagicMock(return_value=driver)), \
                mock.patch.object(bot, "handle_accept_lead", _accept), \
                mock.patch.object(bot, "handle_decline_lead", _decline):
            try:
                asyncio.run(bot.handle_driver_word_answer(update, mock.MagicMock()))
            except bot.ApplicationHandlerStop:
                stop = True
        return seen, stop

    def test_accept_accepts(self):
        seen, stop = self._say("accept")
        self.assertEqual("accept_lead_L1", seen.get("accept"))
        self.assertTrue(stop, "the word must not also flow on to other handlers")

    def test_the_words_people_actually_use(self):
        for said in ("accept", "Accept", "yes", "yep", "yeah", "yup", "ok", "okay",
                     "sure", "I'll take it", "take it", "mine", "got it", "on it",
                     "claim"):
            with self.subTest(said=said):
                seen, _ = self._say(said)
                self.assertEqual("accept_lead_L1", seen.get("accept"), said)

    def test_declining_works_too(self):
        for said in ("no", "nope", "pass", "skip", "different driver", "someone else"):
            with self.subTest(said=said):
                seen, _ = self._say(said)
                self.assertEqual("decline_lead_L1", seen.get("decline"), said)

    def test_it_runs_the_buttons_own_handler(self):
        """Not a second acceptance path that can drift from the button."""
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        body = src.split("async def handle_driver_word_answer", 1)[1].split("\nasync def ", 1)[0]
        self.assertIn("handle_accept_lead", body)
        self.assertIn("_TypedAsTap", body)


class ItStaysOutOfTheWayTest(unittest.TestCase):
    """Everything else must reach the handler it always did."""

    def _say(self, text, driver=DRIVER, pending={"lead_id": "L1"}):
        return ADriverCanSayAcceptTest._say(ADriverCanSayAcceptTest(), text, driver, pending)

    def test_ordinary_text_passes_through(self):
        for said in ("price 150", "name John Damian", "accept the lead tomorrow",
                     "color white"):
            with self.subTest(said=said):
                seen, stop = self._say(said)
                self.assertEqual({}, seen, said)
                self.assertFalse(stop, said)

    def test_someone_who_is_not_a_driver_is_untouched(self):
        seen, stop = self._say("yes", driver=None)
        self.assertEqual({}, seen)
        self.assertFalse(stop)

    def test_a_driver_with_no_open_offer_is_untouched(self):
        """A bare "yes" answers the DMV question too — it must not be eaten here."""
        seen, stop = self._say("yes", pending=None)
        self.assertEqual({}, seen)
        self.assertFalse(stop)

    def test_it_has_a_group_to_itself(self):
        """Two handlers in one group is one handler — this muted the review net once."""
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        block = src.split("handle_driver_word_answer,", 1)[1].split(")", 2)[1]
        self.assertIn("group=-4", block)
        # The invariant is the ORDER, not a magic number: PTB runs groups
        # lowest-first, so the transcriber's group must be numerically below the
        # driver handler's or a spoken "accept" arrives with no text in it.
        import re as _re
        voice = _re.search(r"_global_voice_to_text\),\s*group=(-?\d+)", src)
        self.assertIsNotNone(voice, "voice transcriber is not registered")
        self.assertLess(int(voice.group(1)), -4, "voice must still run BEFORE it")
        self.assertIn("group=-2,", src, "the review-edit net keeps its own group")


class TheOfferSaysSoTest(unittest.TestCase):

    def test_the_driver_is_told_the_word_works(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        self.assertIn("or just reply *accept*", src)


if __name__ == "__main__":
    unittest.main()
