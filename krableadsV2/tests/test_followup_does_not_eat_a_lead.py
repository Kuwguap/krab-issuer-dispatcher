r"""A lead pasted into an open follow-up is not client-follow-up info.

Reported from a live session: a two-car lead was pasted and came back as

    🤖 Parsed your message → filled: name, phone, notes
    📇 New client follow-up …

The lead never became a lead. Both of those messages come from the follow-up
flow, not the lead flow — "filled: name, phone, notes" is the follow-up's own
field set (bot.py, handle_fu_menu_text).

The cause is that /followup opens a conversation which stays open until it is
finished or cancelled, and with no field tapped that handler treats ANY text as a
client-info paste. So a whole job — two owners, two addresses, two VINs, two
insurers — was compressed into a 500-character notes field and the real work
disappeared.

A message carrying a 17-character VIN is unmistakable. Refuse it, say why, and
leave the follow-up untouched.

Run:  venv\Scripts\python.exe -m pytest tests/test_followup_does_not_eat_a_lead.py -q
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
os.environ.pop("OPENAI_API_KEY", None)

import utils.database as udb  # noqa: E402

if "bot" not in sys.modules:
    udb.Database = mock.MagicMock()
import bot  # noqa: E402

TWO_CAR_LEAD = """Client Charles
Phone 845-423-9476

CHARLES JONES
9 hibiscus Lane Monticello New York 13701
2017 Nissan Altima
VIN: 1N4AL3AP0HC166043
Geico
0407306000

CHARLES G JONES
11530 Mango terrace drive apt.102
 Seffner Florida 33584
2010 Toyota Camry
VIN: 4T1BF3EK6AU051219
Progressive
982658176"""

ONE_CAR_LEAD = """JOHN DOE
5 Oak Street Newark New Jersey 07102
2019 Honda Accord
VIN: 1HGCM82633A004352"""

REAL_FOLLOWUP = """Maria Alvarez
551-555-0134
maria@example.com
wants a tag next week, still needs her insurance card"""


class _Msg:
    def __init__(self, text):
        self.text = text
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)
        return self


class _Update:
    def __init__(self, msg):
        self.message = msg
        self.effective_message = msg


class _Ctx:
    def __init__(self, fu=None):
        self.user_data = {"fu": dict(fu or {})}


def send(text, fu=None):
    """Returns (state, replies, the follow-up record afterwards)."""
    msg = _Msg(text)
    ctx = _Ctx(fu)
    with mock.patch.object(bot, "_fu_render_menu", new=mock.AsyncMock()):
        state = asyncio.run(bot.handle_fu_menu_text(_Update(msg), ctx))
    return state, msg.replies, ctx.user_data["fu"]


class ALeadIsRefusedNotSwallowedTest(unittest.TestCase):

    def test_a_two_car_lead_is_refused(self):
        _state, replies, fu = send(TWO_CAR_LEAD)
        self.assertTrue(replies)
        self.assertIn("LEAD", replies[0])
        self.assertIn("2 VIN", replies[0])

    def test_it_touches_nothing(self):
        """The failure that was reported: the paste became name/phone/notes."""
        _state, _replies, fu = send(TWO_CAR_LEAD)
        for key in ("client_name", "phone_number", "email", "notes"):
            with self.subTest(field=key):
                self.assertIsNone(fu.get(key), f"{key} was filled from a lead")

    def test_a_one_car_lead_too(self):
        _state, replies, fu = send(ONE_CAR_LEAD)
        self.assertIn("LEAD", replies[0])
        self.assertIn("1 VIN", replies[0])
        self.assertFalse(fu.get("client_name"))

    def test_it_says_how_to_get_out(self):
        """Refusing without a way forward is its own dead end."""
        _state, replies, _fu = send(TWO_CAR_LEAD)
        self.assertIn("/cancel", replies[0])

    def test_it_stays_in_the_follow_up_rather_than_ending_it(self):
        state, _replies, _fu = send(TWO_CAR_LEAD)
        self.assertEqual(state, bot.STATE_FU_MENU)

    def test_a_pending_field_is_not_consumed(self):
        """A lead pasted while a field prompt is open must leave that prompt
        open — otherwise the operator loses their place as well as the lead."""
        _state, _replies, fu = send(TWO_CAR_LEAD, {"pending": "email"})
        self.assertEqual(fu.get("pending"), "email")


class RealFollowUpInfoStillWorksTest(unittest.TestCase):
    """A guard that refuses everything is just a broken feature."""

    def test_an_ordinary_paste_is_still_parsed(self):
        _state, replies, fu = send(REAL_FOLLOWUP)
        self.assertIn("Parsed your message", replies[0])
        self.assertTrue(fu.get("phone_number") or fu.get("email")
                        or fu.get("client_name"))

    def test_a_note_that_merely_mentions_a_car(self):
        """No VIN, so nothing to refuse — this is genuine follow-up info."""
        _state, replies, fu = send("Maria Alvarez 551-555-0134 has a Toyota Camry")
        self.assertIn("Parsed your message", replies[0])

    def test_a_seventeen_digit_number_is_not_a_vin(self):
        """_all_vins_17 rejects an all-digit run, so an account number in a note
        does not look like a lead."""
        _state, replies, _fu = send("Maria Alvarez policy 12345678901234567")
        self.assertIn("Parsed your message", replies[0])


if __name__ == "__main__":
    unittest.main()
