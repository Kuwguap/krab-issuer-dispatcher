"""Settings flow: admin gating, plate-counter edits, add group."""
import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["TELEGRAM_BOT_TOKEN"] = "1:AAA"
os.environ["ADMIN_TELEGRAM_IDS"] = "111,222"
os.environ.pop("SUPABASE_URL", None)
os.environ.pop("SUPABASE_KEY", None)

import bot  # noqa: E402
from config import Config  # noqa: E402


def _update(user_id):
    u = MagicMock()
    u.effective_user = MagicMock(id=user_id, username="staff", full_name="Staff Person")
    u.message = MagicMock()
    u.message.reply_text = AsyncMock()
    return u


def test_admin_gate():
    assert Config.is_admin(111) and Config.is_admin("222")
    assert not Config.is_admin(999)


def test_non_admin_refused():
    up = _update(999)
    ctx = MagicMock(); ctx.user_data = {}
    asyncio.run(bot.cmd_settings(up, ctx))
    up.message.reply_text.assert_awaited()
    assert "admin" in up.message.reply_text.await_args.args[0].lower()


def test_plate_input_updates_counter():
    up = _update(111)
    ctx = MagicMock(); ctx.user_data = {"settings_await": {"kind": "plate", "field": "nj_plate_next_number"}}
    captured = {}
    bot.db.update_plate_settings = lambda upd: captured.update(upd) or True
    asyncio.run(bot._apply_settings_input(up, ctx, "200500"))
    assert captured == {"nj_plate_next_number": 200500}, captured
    assert "settings_await" not in ctx.user_data
    print("plate update ->", captured)


def test_plate_input_rejects_non_digits():
    up = _update(111)
    ctx = MagicMock(); ctx.user_data = {"settings_await": {"kind": "plate", "field": "nj_car_next_number"}}
    called = {"n": 0}
    bot.db.update_plate_settings = lambda upd: called.update(n=called["n"] + 1) or True
    asyncio.run(bot._apply_settings_input(up, ctx, "abc"))
    assert called["n"] == 0  # no update on garbage


def test_add_group():
    up = _update(222)
    ctx = MagicMock(); ctx.user_data = {"settings_await": {"kind": "add_group"}}
    got = {}
    bot.db.add_group = lambda name, tgid, *a: got.update(name=name, tgid=tgid) or True
    asyncio.run(bot._apply_settings_input(up, ctx, "Tatiana's Team | -1003741637507"))
    assert got == {"name": "Tatiana's Team", "tgid": "-1003741637507"}, got
    print("add group ->", got)


def test_non_admin_settings_input_ignored():
    up = _update(999)
    ctx = MagicMock(); ctx.user_data = {"settings_await": {"kind": "plate", "field": "nj_plate_next_number"}}
    hit = {"n": 0}
    bot.db.update_plate_settings = lambda upd: hit.update(n=1) or True
    asyncio.run(bot._apply_settings_input(up, ctx, "999999"))
    assert hit["n"] == 0 and "settings_await" not in ctx.user_data


if __name__ == "__main__":
    test_admin_gate()
    test_non_admin_refused()
    test_plate_input_updates_counter()
    test_plate_input_rejects_non_digits()
    test_add_group()
    test_non_admin_settings_input_ignored()
    print("ALL SETTINGS TESTS PASSED")
