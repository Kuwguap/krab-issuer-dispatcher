"""Offline routing smoke test for single free-text/voice commands on the review card.

Drives the REAL bot._smart_place_single_value over a table of utterances with the AI
classifier monkeypatched to a deterministic stub, asserting each lands in the CORRECT
field (this is the fix for "first name John" -> registration address). Also includes an
OPTIONAL live-accuracy pass that runs the real classifier when OPENAI_API_KEY is present.

Run:  venv\\Scripts\\python.exe -m unittest tests.test_smart_place_single_value
"""
import os
import re
import sys
import asyncio
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# bot.py runs `db = Database()` at import; give create_client dummy-but-valid strings and
# mock the Database class so no network I/O happens. Leave OPENAI unset (we patch the seam).
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy-key")

import utils.database as udb  # noqa: E402
udb.Database = mock.MagicMock()
import bot  # noqa: E402


# ── deterministic AI stub: what a good classify_field_value would return ──────────────
STUB = {
    "first name John": {"field": "fn", "value": "John"},
    "first  name john": {"field": "fn", "value": "john"},
    "first-name john": {"field": "fn", "value": "john"},
    "customer first name john": {"field": "fn", "value": "john"},
    "last name Smith": {"field": "ln", "value": "Smith"},
    "John Doe": {"field": "name", "value": "John Doe"},
    "Acme Towing LLC": {"field": "name", "value": "Acme Towing LLC"},
    "blue": {"field": "col", "value": "blue"},
    "midnight blue": {"field": "col", "value": "midnight blue"},
    "Camry": {"field": "car", "value": "Camry"},
    "2019 Honda Accord": {"field": "car", "value": "2019 Honda Accord"},
    "geico": {"field": "ins", "value": "geico"},
    "state farm": {"field": "ins", "value": "state farm"},
    "policy ABC123": {"field": "pol", "value": "ABC123"},
    "Newark NJ 07102": {"field": "csz", "value": "Newark NJ 07102"},
    "deliver to 88 Ocean Ave Newark": {"field": "daddr", "value": "88 Ocean Ave Newark"},
    "deliver tomorrow 5pm": {"field": "xtra", "value": "tomorrow 5pm"},
    "tell the driver gate code 4455": {"field": "driver", "value": "gate code 4455"},
    "issuer note rush": {"field": "issuer", "value": "rush"},
    "DL D1234567": {"field": "dl", "value": "D1234567"},
    # command / gibberish → unknown (must NOT be filed as a field)
    "submit": {"field": "unknown", "value": ""},
    "choose driver Kita": {"field": "unknown", "value": ""},
    "run vin": {"field": "unknown", "value": ""},
    "asdfghjkl": {"field": "unknown", "value": ""},
}


def _stub_classify(utterance):
    return STUB.get((utterance or "").strip(), {"field": "unknown", "value": ""})


# ── expectation table: (utterance, state_key, core_substring or None, is_command) ─────
# core_substring is checked case-insensitively; None means "just assert the key is set".
ROWS = [
    # mangled/labeled first/last name — THE bug: these used to land in address
    ("first name John",           "name", "john",              False),
    ("first  name john",          "name", "john",              False),
    ("first-name john",           "name", "john",              False),
    ("customer first name john",  "name", "john",              False),
    ("last name Smith",           "name", "smith",             False),
    ("John Doe",                  "name", "john doe",          False),
    ("Acme Towing LLC",           "name", "acme towing",       False),
    # color / car
    ("blue",                      "color", "blue",             False),
    ("midnight blue",             "color", "midnight blue",    False),
    ("Camry",                     "car",   "camry",            False),
    ("2019 Honda Accord",         "car",   "honda accord",     False),   # NOT address
    # insurance / policy
    ("geico",                     "insurance_company", "geico", False),
    ("state farm",                "insurance_company", "state farm", False),
    ("policy ABC123",             "insurance_policy_number", "abc123", False),
    # city/state/zip, delivery, notes
    ("Newark NJ 07102",           "city_state_zip", "07102",   False),
    ("deliver to 88 Ocean Ave Newark", "delivery_address", "88 ocean ave", False),
    ("deliver tomorrow 5pm",      "extra_info", "tomorrow 5pm", False),
    ("tell the driver gate code 4455", "special_request_drivers", "gate code 4455", False),
    ("issuer note rush",          "special_request_issuers", "rush", False),
    ("DL D1234567",               "driver_license_id", "d1234567", False),
    # FAST-PATH (deterministic; stub not consulted)
    ("john@example.com",          "email", "john@example.com", False),
    ("732 555 1212",              "pending_phone_number", "7325551212", False),
    ("$500",                      "pending_price", "500",      False),
    ("500",                       "pending_price", "500",      False),
    ("1HGCM82633A004352",         "vin", "1HGCM82633A004352",  False),
    ("12 Main St",                "address", "12 main st",     False),
    ("543 Garden Place, Keyport NJ 07735", "address", "543 garden place", False),
    # commands / gibberish — must NOT mutate any field
    ("submit",                    None, None, True),
    ("choose driver Kita",        None, None, True),
    ("run vin",                   None, None, True),
    ("asdfghjkl",                 None, None, True),
]

# All phase-1 fields a fresh review starts with (blank).
_STATE_KEYS = [
    "name", "first_name", "last_name", "address", "city_state_zip",
    "delivery_address", "delivery_city_state_zip", "vin", "car", "color",
    "insurance_company", "insurance_policy_number", "extra_info",
    "pending_phone_number", "pending_price", "special_request_issuers",
    "special_request_drivers", "email", "driver_license_id",
]


def _fresh_state():
    return {k: "-" for k in _STATE_KEYS}


def _digits(s):
    return re.sub(r"\D", "", str(s or ""))


class SmartPlaceRoutingTest(unittest.TestCase):
    def setUp(self):
        self._p1 = mock.patch.object(bot.ai_vision, "classify_field_value", side_effect=_stub_classify)
        self._p2 = mock.patch.object(bot.Config, "is_ai_vision_configured", return_value=True)
        self._p1.start()
        self._p2.start()

    def tearDown(self):
        self._p1.stop()
        self._p2.stop()

    def test_each_utterance_routes_to_the_right_field(self):
        failures = []
        for utt, sk, core, is_cmd in ROWS:
            sd = _fresh_state()
            labels = asyncio.run(bot._smart_place_single_value(sd, utt))
            if is_cmd:
                # command/gibberish: nothing filed, no field changed
                if labels:
                    failures.append(f"{utt!r}: expected no placement, got labels={labels}")
                changed = [k for k in _STATE_KEYS if str(sd.get(k) or "-").strip() not in ("-", "")]
                if changed:
                    failures.append(f"{utt!r}: expected no mutation, but changed {changed}")
                continue
            got = str(sd.get(sk) or "").strip()
            if not got or got == "-":
                failures.append(f"{utt!r}: expected {sk} to be set, got {got!r} (labels={labels})")
                continue
            if core is not None:
                hay = _digits(got) if sk in ("pending_phone_number",) else got.lower()
                needle = _digits(core) if sk in ("pending_phone_number",) else core.lower()
                if needle not in hay:
                    failures.append(f"{utt!r}: {sk}={got!r} does not contain {core!r}")
            # THE bug guard: a name/first/last utterance must NOT touch address.
            if sk == "name":
                addr = str(sd.get("address") or "-").strip()
                if addr not in ("-", ""):
                    failures.append(f"{utt!r}: leaked into address={addr!r}")
        self.assertEqual(failures, [], "\n".join(failures))

    def test_degraded_no_ai_alpha_heuristic(self):
        # When AI is unavailable, the deterministic fallback must not misroute a real
        # street address whose house number looks like a year, nor a real vehicle.
        self.assertEqual(bot._alpha_value_ek_heuristic("2015 Broadway"), "addr")     # address, not car
        self.assertEqual(bot._alpha_value_ek_heuristic("1990 Pennsylvania"), "addr")  # address, not car
        self.assertEqual(bot._alpha_value_ek_heuristic("2019 Honda Accord"), "car")   # make word → car
        self.assertEqual(bot._alpha_value_ek_heuristic("blue"), "col")
        self.assertEqual(bot._alpha_value_ek_heuristic("John Doe"), "name")

    def test_structured_fast_path_addr_vs_car(self):
        # Fast-path: explicit street token wins; a year+make defers (None → AI → car);
        # a plain leading-number street still resolves to addr.
        self.assertEqual(bot._structured_value_ek("12 Main St"), "addr")
        self.assertEqual(bot._structured_value_ek("543 Garden Place, Keyport NJ 07735"), "addr")
        self.assertEqual(bot._structured_value_ek("12 Elm"), "addr")            # bare number+word
        self.assertIsNone(bot._structured_value_ek("2019 Honda Accord"))        # vehicle → defer to AI

    def test_multifield_dictation_routes_to_extractor(self):
        # A whole lead spoken in one voice note (single line, no valid 17-char VIN) must be
        # recognized as a multi-field block so it's AI-extracted, not dropped.
        dictation = ("Name John Damien Address 123 Main Street VIN 347GH7ZFF291 Car 2020 "
                     "100 Civic Colour Red Insurance Company Allstate Insurance Insurance "
                     "Policy 267112 Phone 134248123 Price $150")
        self.assertTrue(bot._looks_like_multifield_block(dictation))
        self.assertGreaterEqual(bot._count_field_anchors(dictation), 3)
        # single-field commands must NOT be treated as multi-field blocks
        for single in ("first name John", "blue", "geico", "policy ABC123", "$500", "Camry"):
            self.assertFalse(bot._looks_like_multifield_block(single), single)
        # a clean 2-field one-liner is handled by the labeled path first, not this detector
        self.assertFalse(bot._looks_like_multifield_block("price 200 color white"))
        # multi-line paste and a real 17-char VIN still qualify
        self.assertTrue(bot._looks_like_multifield_block("name John\naddress 12 Main St"))
        self.assertTrue(bot._looks_like_multifield_block("1HGCM82633A004352"))

    def test_dictation_merge_is_non_destructive_of_filled_fields(self):
        # only_empty=True must fill blanks but NEVER overwrite an already-filled field.
        sd = {k: "-" for k in _STATE_KEYS}
        sd["name"] = "Existing Name"          # already set — must be preserved
        sd["pending_phone_number"] = "+15551110000"
        block = "\n".join([
            "John Damien", "123 Main Street", "-", "-", "-", "-", "2020 Civic", "Red",
            "Allstate", "267112", "-", "Phone: 9738675309", "Price: $150",
            "Issuer note: -", "Driver note: -", "Email: -", "DriverLicenseID: -",
        ])
        changed = bot._merge_phase1_adjust(sd, block, only_empty=True)
        self.assertEqual(sd["name"], "Existing Name")            # NOT overwritten
        self.assertEqual(sd["pending_phone_number"], "+15551110000")  # NOT overwritten
        self.assertEqual(sd["address"], "123 Main Street")       # empty → filled
        self.assertIn(sd["color"], ("Red", "RED"))               # empty → filled
        self.assertNotIn("name", changed)
        self.assertNotIn("phone", changed)
        # destructive mode (default) DOES overwrite
        sd2 = {k: "-" for k in _STATE_KEYS}
        sd2["name"] = "Old Name"
        bot._merge_phase1_adjust(sd2, block, only_empty=False)
        self.assertEqual(sd2["name"], "John Damien")

    def test_normalizer_strips_markdown_code_fence(self):
        # Models sometimes wrap the 17-line block in ```...``` — the fence must be
        # stripped, not leaked into the first field (name).
        block = "```\nJohn Damien\n123 Main St\n```"
        out = bot._normalize_ai_phase1_text(block)
        self.assertNotIn("```", out)
        self.assertTrue(out.splitlines()[0].startswith("John"))
        # ```json fence variant too
        self.assertNotIn("```", bot._normalize_ai_phase1_text("```json\nJane\n```"))

    def test_anchor_set_excludes_value_words(self):
        # A single address VALUE must not accumulate spurious anchors → stays single-field.
        self.assertLess(bot._count_field_anchors("123 Price Street, Kansas City"), 3)
        self.assertLess(bot._count_field_anchors("Main Street Jersey City"), 3)
        # a genuine dictation still trips the threshold
        self.assertGreaterEqual(bot._count_field_anchors(
            "Name John Address 123 VIN abc Car Civic Colour Red Phone 555 Price 150"), 3)

    def test_full_handler_dictation_uses_extractor_not_labeled_parser(self):
        # A single-line voice dictation must be routed to the AI extractor (clean) BEFORE
        # the labeled parser (which greedily mis-splits messy dictation). We stub the
        # extractor to a clean 17-line block and assert the fields match it exactly —
        # proving the extractor path was taken, not the mangling labeled path.
        import types
        clean_block = "\n".join([
            "John Damien", "123 Main Street", "-", "-", "-", "-", "2020 Civic", "Red",
            "Allstate", "267112", "-", "Phone: -", "Price: $150",
            "Issuer note: -", "Driver note: -", "Email: -", "DriverLicenseID: -",
        ])
        store = {"data": {k: "-" for k in _STATE_KEYS}}
        with mock.patch.object(bot.ai_vision, "extract_structured_from_text", return_value=clean_block), \
             mock.patch.object(bot.db, "get_user_state", lambda uid: {"data": dict(store["data"])}), \
             mock.patch.object(bot.db, "set_user_state", lambda uid, key, data: store.update(data={**data})):
            class B:
                async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None):
                    return types.SimpleNamespace(message_id=1, chat_id=chat_id)
                async def edit_message_text(self, **k): pass
                async def edit_message_reply_markup(self, **k): pass
                async def delete_message(self, **k): pass
            class M:
                def __init__(self, t):
                    self.text = t; self.caption = None; self.photo = None; self.document = None
                    self.voice = types.SimpleNamespace(file_id="f"); self.chat_id = 42; self.message_id = 9
                async def reply_text(self, t, **k):
                    m = M(t); m.voice = None; return m
                async def delete(self): pass
            ctx = types.SimpleNamespace(bot=B(), user_data={"review_chat_id": 42, "review_message_id": 900}, args=[])
            m = M("Name John Damien Address 123 Main Street Car 2020 Civic Colour Red "
                  "Insurance Allstate Policy 267112 Price $150")
            upd = types.SimpleNamespace(message=m, effective_message=m,
                effective_user=types.SimpleNamespace(id=7, username="jb"),
                effective_chat=types.SimpleNamespace(id=42, type="private"))
            asyncio.run(bot.handle_phase1_review_message(upd, ctx))
        self.assertEqual(store["data"]["name"], "John Damien")
        self.assertEqual(store["data"]["car"], "2020 Civic")        # color NOT absorbed
        self.assertIn(store["data"]["color"], ("Red", "RED"))        # color is its own field
        self.assertEqual(store["data"]["insurance_policy_number"], "267112")  # phone NOT absorbed

    def test_apply_ek_value_edge_cases(self):
        # unknown edit-key → no-op
        self.assertEqual(bot._apply_ek_value(_fresh_state(), "bogus", "x"), [])
        # empty value → no-op
        self.assertEqual(bot._apply_ek_value(_fresh_state(), "col", ""), [])
        # value that doesn't fit the field (phone with <10 digits) → dropped, no false label
        self.assertEqual(bot._apply_ek_value(_fresh_state(), "phone", "hello"), [])
        # value == current → no false "Updated"
        sd = _fresh_state(); sd["color"] = "blue"
        self.assertEqual(bot._apply_ek_value(sd, "col", "blue"), [])
        # a real change reports its label
        self.assertEqual(bot._apply_ek_value(_fresh_state(), "col", "blue"), ["color"])


# ── OPTIONAL: live accuracy of the real classifier (needs a real OPENAI_API_KEY) ──────
_LIVE_ROWS = [(u, STUB[u]["field"]) for u in STUB if STUB[u]["field"] != "unknown"]
_LIVE_ROWS += [("submit", "unknown"), ("choose driver Kita", "unknown"), ("run vin", "unknown")]


class LiveAccuracyTest(unittest.TestCase):
    @unittest.skipUnless(bot.Config.is_ai_vision_configured(), "no OPENAI_API_KEY — live test skipped")
    def test_live_accuracy(self):
        hits, misses = 0, []
        for utt, exp in _LIVE_ROWS:
            res = bot.ai_vision.classify_field_value(utt)
            got = (res or {}).get("field")
            if got == exp:
                hits += 1
            else:
                misses.append(f"{utt!r}: expected {exp}, got {got}")
        rate = hits / len(_LIVE_ROWS)
        self.assertGreaterEqual(rate, 0.9, f"live accuracy {rate:.0%}\n" + "\n".join(misses))


if __name__ == "__main__":
    unittest.main(verbosity=2)
