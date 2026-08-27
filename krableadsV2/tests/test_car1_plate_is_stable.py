"""Car 1's plate must be assigned ONCE and reused on every later send.

The bot persists car 1's minted plate to the lead row's `plate` /
`tag_control_number` columns, but used to read it back from the phase1 blob —
which never carried a plate — so every re-send minted a FRESH plate: a
different legal plate on the same car each time, a burned plate number per
send, and (once the web mirror existed) a downloaded tag that disagreed with
the one Telegram delivered. This pins the fix: read the assigned values off
the lead row for car 1.

Run:  venv\\Scripts\\python.exe -m pytest tests/test_car1_plate_is_stable.py -q
"""
import asyncio
import itertools
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy-key")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11")
os.environ.pop("OPENAI_API_KEY", None)

import utils.database as udb  # noqa: E402

if "bot" not in sys.modules:
    udb.Database = mock.MagicMock()
import bot  # noqa: E402

VEHICLE = "\n".join([
    "CHARLES JONES", "9 hibiscus Lane", "Monticello New York 13701",
    "9 hibiscus Lane", "Monticello New York 13701",
    "1N4AL3AP0HC166043", "2017 Nissan Altima", "Grey",
    "Geico", "0407306000", "now 1 hour",
])


def _fake_alloc():
    seq = itertools.count(1)

    def alloc(is_nj):
        n = next(seq)
        return {"plate": f"{n:06d}V", "control_number": f"999{n:07d}"}
    return alloc


class Car1PlateIsStableTest(unittest.TestCase):

    def _build_twice(self):
        """Resolve car 1's tag fields twice, with a FakeDB that persists the
        plate exactly like production (update_lead writes back onto the row)."""
        lead = {"id": "L1", "reference_id": "ABC12345",
                "vehicle_details": VEHICLE, "price": "$150"}
        db = mock.MagicMock()
        db.allocate_temp_plate.side_effect = _fake_alloc()

        def _update(lead_id, payload):
            lead.update(payload)          # the real row gains plate/control
            return True
        db.update_lead.side_effect = _update
        with mock.patch.object(bot, "db", db), \
                mock.patch.object(bot.tag_pdf, "decode_vin_for_tag", lambda v: None) \
                if hasattr(bot, "tag_pdf") else mock.patch.object(bot, "db", db):
            # tag_pdf is imported lazily inside _tag_fields_from_lead; patch the
            # module it pulls so no network VIN decode runs.
            from utils import tag_pdf
            with mock.patch.object(tag_pdf, "decode_vin_for_tag", lambda v: None):
                f1 = asyncio.run(bot._tag_fields_from_lead(lead, vehicle=1))
                f2 = asyncio.run(bot._tag_fields_from_lead(lead, vehicle=1))
        return f1, f2, db

    def test_the_second_send_reuses_the_first_plate(self):
        f1, f2, db = self._build_twice()
        self.assertTrue(f1["plate"], "car 1 got no plate")
        self.assertEqual(f1["plate"], f2["plate"],
                         "car 1 minted a DIFFERENT plate on the re-send")
        self.assertEqual(f1["control_number"], f2["control_number"])

    def test_the_allocator_is_called_once_not_per_send(self):
        _, _, db = self._build_twice()
        self.assertEqual(db.allocate_temp_plate.call_count, 1,
                         "a plate number was burned on the re-send")

    def test_a_preassigned_row_plate_is_honoured(self):
        lead = {"id": "L2", "reference_id": "ZZZ99999",
                "vehicle_details": VEHICLE, "plate": "654321V",
                "tag_control_number": "9990000001"}
        db = mock.MagicMock()
        db.allocate_temp_plate.side_effect = _fake_alloc()
        with mock.patch.object(bot, "db", db):
            from utils import tag_pdf
            with mock.patch.object(tag_pdf, "decode_vin_for_tag", lambda v: None):
                f = asyncio.run(bot._tag_fields_from_lead(lead, vehicle=1))
        self.assertEqual(f["plate"], "654321V")
        db.allocate_temp_plate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
