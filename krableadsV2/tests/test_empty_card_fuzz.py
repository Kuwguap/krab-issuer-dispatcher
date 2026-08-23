"""Fuzz the EMPTY review card with every kind of edit, through the real handler stack.

The production bug was a crash class, not one line: on a card that only has the dispatch
selections saved, every field key is MISSING, and any code that assumes a string there
raises — the handler dies mid-apply and the typed edit silently does nothing. One such
site (_clean_vin_and_car) was fixed; this sweeps the whole review surface for siblings.

Any unhandled exception reaching the Application error handler fails the test, naming the
input that triggered it.

Run:  venv\\Scripts\\python.exe -m pytest tests/test_empty_card_fuzz.py -q
"""
import asyncio
import unittest
from unittest import mock

import telegram

from test_real_routing_e2e import (  # reuse the real-app harness
    CHAT_ID,
    FAKE_DB,
    TRANSPORT,
    USER_ID,
    _build_application,
    _text_update,
    bot,
)

# Exactly what production holds for a freshly opened card: selections only.
EMPTY_CARD = {
    "selected_group_name": "All Dispatchers", "selected_group_id": "all",
    "selected_driver_names": "All Drivers", "selected_driver_ids": ["d1"],
    "selected_source_label": "Facebook",
}

# (typed line, state_key that must end up filled or None when nothing should be set)
LABELED_EDITS = [
    ("name John Damian", "name"),
    ("first name John", "name"),
    ("last name Damian", "name"),
    ("price 150", "pending_price"),
    ("phone 555-123-4567", "pending_phone_number"),
    ("color blue", "color"),
    ("car 2019 Honda Accord", "car"),
    ("address 123 Main St, Newark NJ 07102", "address"),
    ("email a@b.com", "email"),
    ("dln D1234567", "driver_license_id"),
    ("vin 1HGCM82633A004352", "vin"),
    ("insurance Geico", "insurance_company"),
    ("policy number ABC123", "insurance_policy_number"),
    ("driver note gate code 4455", "special_request_drivers"),
    ("issuer note rush", "special_request_issuers"),
    ("delivery address 88 Ocean Ave", "delivery_address"),
    ("date/time tomorrow 5pm", "extra_info"),
    ("price 200 color white", "pending_price"),
]

# Inputs that must never crash; where they land is not asserted here.
NON_CRASH_INPUTS = [
    "John Damian",
    "blue",
    "$500",
    "732 555 1212",
    "1HGCM82633A004352",
    "choose driver Kita",
    "choose dispatcher HighKage",
    "add insurance",
    "no insurance",
    "run vin",
    "keep the same",
    "submit",
    "all",
    "asdfghjkl",
    "hello there how are you",
    "Name John Damian Address 123 Main St VIN 1HGCM82633A004352 Color Red Price 150",
]

PARKED_STATES = [
    ("review", lambda: bot.STATE_AI_REVIEW),
    ("edit_menu", lambda: bot.STATE_AI_EDIT_MENU),
    ("select_driver", lambda: bot.STATE_SELECT_DRIVER),
    ("conversation_lost", lambda: None),
]


class EmptyCardFuzzTest(unittest.TestCase):
    """No typed input may raise on an empty card, in any parked state."""

    def _drive(self, park_state, text):
        """Send one line to a real app holding an EMPTY card; return (errors, data)."""
        async def run():
            errors = []
            app = _build_application()

            async def record_error(update, context):
                errors.append(repr(context.error))

            app.add_error_handler(record_error)
            with mock.patch.object(telegram.Bot, "_do_post", TRANSPORT.do_post):
                await app.initialize()
                try:
                    conv = bot._MAIN_CONV_HANDLER
                    key = (CHAT_ID, USER_ID)
                    FAKE_DB.states.clear()
                    FAKE_DB.set_user_state(USER_ID, "phase1", dict(EMPTY_CARD))
                    app.user_data[USER_ID].clear()
                    app.user_data[USER_ID]["review_message_id"] = 900
                    app.user_data[USER_ID]["review_chat_id"] = CHAT_ID
                    if park_state is None:
                        conv._conversations.pop(key, None)
                    else:
                        conv._conversations[key] = park_state
                    TRANSPORT.reset()
                    await app.process_update(_text_update(app, text, 7201))
                    row = FAKE_DB.get_user_state(USER_ID)
                    return errors, (row or {}).get("data") or {}
                finally:
                    await app.shutdown()
        return asyncio.run(run())

    def test_labeled_edits_apply_on_empty_card(self):
        failures = []
        for text, state_key in LABELED_EDITS:
            errors, data = self._drive(bot.STATE_AI_REVIEW, text)
            if errors:
                failures.append(f"{text!r} raised {errors}")
            elif not str(data.get(state_key) or "").strip(" -"):
                failures.append(f"{text!r} did not fill {state_key!r} (got {data.get(state_key)!r})")
        self.assertEqual([], failures, "\n".join(failures))

    def test_no_input_crashes_in_any_parked_state(self):
        failures = []
        for label, state_fn in PARKED_STATES:
            for text in NON_CRASH_INPUTS + [t for t, _ in LABELED_EDITS]:
                errors, _ = self._drive(state_fn(), text)
                if errors:
                    failures.append(f"[{label}] {text!r} raised {errors}")
        self.assertEqual([], failures, "\n".join(failures))


if __name__ == "__main__":
    unittest.main()
