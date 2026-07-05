"""Regression: field pick must not crash before prompting for a new value."""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import bot  # noqa: E402


def _callback_update(callback_data: str) -> SimpleNamespace:
    message = SimpleNamespace(chat=SimpleNamespace(id=12345))
    query = SimpleNamespace(
        data=callback_data,
        answer=AsyncMock(),
        message=message,
    )
    return SimpleNamespace(callback_query=query, effective_chat=message.chat)


class TestEditFieldPick(unittest.TestCase):
    def test_pick_name_field_returns_edit_value_state(self) -> None:
        context = MagicMock()
        context.user_data = {
            "lead": {
                "vehicle_details": "JANE DOE\n123 MAIN ST\nBRONX NY 10459\n\n\nVIN123\n2021 NISSAN\nBLK\n\n\n",
                "email": "jane@example.com",
            },
            "card_state": "NY",
            "plan_months": 1,
        }
        context.chat_data = {}

        with patch.object(bot, "_send_clean", new_callable=AsyncMock) as send_clean:
            state = asyncio.run(
                bot.handle_edit_pick(_callback_update("ef_name"), context)
            )

        self.assertEqual(state, bot.STATE_EDIT_FIELD_VALUE)
        self.assertEqual(context.user_data["edit_field"], "name")
        send_clean.assert_awaited_once()
        prompt = send_clean.await_args.args[2]
        self.assertIn("Name", prompt)

    def test_pick_driver_license_id_ny_uses_barcode_prompt(self) -> None:
        context = MagicMock()
        context.user_data = {
            "lead": {"vehicle_details": "JANE DOE\nBRONX NY 10459\n", "email": "j@x.com"},
            "card_state": "NY",
        }
        context.chat_data = {}

        with patch.object(bot, "_send_clean", new_callable=AsyncMock) as send_clean:
            state = asyncio.run(
                bot.handle_edit_pick(_callback_update("ef_driver_license_id"), context)
            )

        self.assertEqual(state, bot.STATE_EDIT_FIELD_VALUE)
        prompt = send_clean.await_args.args[2]
        self.assertIn("barcode", prompt.lower())


if __name__ == "__main__":
    unittest.main()
