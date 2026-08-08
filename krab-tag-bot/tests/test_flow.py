"""End-to-end conversation flow: input -> review -> generate -> group -> driver."""
import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["TELEGRAM_BOT_TOKEN"] = "1:AAA"
os.environ.pop("SUPABASE_URL", None)
os.environ.pop("OPENAI_API_KEY", None)

import bot  # noqa: E402


def _ctx():
    c = MagicMock()
    c.user_data = {}
    c.bot = MagicMock()
    c.bot.send_message = AsyncMock()
    c.bot.send_document = AsyncMock()
    return c


def _text_update(text):
    up = MagicMock()
    up.effective_user = MagicMock(id=111, username="staff", full_name="Staff Person")
    up.message = MagicMock(text=text, caption=None, photo=None, document=None)
    up.message.reply_text = AsyncMock()
    status = MagicMock()
    status.edit_text = AsyncMock()
    up.effective_message = MagicMock()
    up.effective_message.reply_text = AsyncMock(return_value=status)
    return up, status


def _cbq_update(data):
    up = MagicMock()
    up.effective_user = MagicMock(id=111, username="staff", full_name="Staff Person")
    q = MagicMock()
    q.data = data
    q.answer = AsyncMock()
    q.edit_message_text = AsyncMock()
    q.message = MagicMock()
    q.message.reply_text = AsyncMock()
    q.message.reply_document = AsyncMock()
    up.callback_query = q
    return up, q


def test_full_flow():
    ctx = _ctx()

    # stub backend
    bot.db.list_active_groups = lambda: [{"id": "g1", "group_name": "Team A", "group_telegram_id": "-1001"}]
    bot.db.list_drivers = lambda: [{"id": "d1", "driver_name": "Troy", "driver_telegram_id": "555"}]
    bot.tagcore.generate_full = lambda payload, db: (b"%PDF-xxx", {
        "plate": "549005V", "control_number": "9896095819", "first": "Josue", "last": "Pavon",
        "vin": "5N1AL0MM8DC337962", "make": "Infiniti", "model": "JX35", "year": "2013",
        "color": "White", "body": "SUV 4DR", "city": "Bronx", "state": "NY", "zip": "10465",
        "insurance_company": "Progressive", "policy": "999",
    })
    logged = {}
    bot.ledger.log_tag = lambda **kw: logged.update(kw) or True

    # 1) text input -> review
    up, status = _text_update("Josue Pavon\n5N1AL0MM8DC337962\nWhite Infiniti JX35\nBronx, NY 10465")
    asyncio.run(bot.handle_input(up, ctx))
    assert ctx.user_data.get("parsed"), "parsed not stored"
    assert status.edit_text.await_args and "Review" in status.edit_text.await_args.args[0]
    print("review shown:", ctx.user_data["parsed"].get("name"))

    # 2) generate
    upg, qg = _cbq_update("p1_gen")
    asyncio.run(bot.on_generate(upg, ctx))
    assert ctx.user_data.get("tag", {}).get("plate") == "549005V"
    assert logged.get("reference_id") and logged.get("issuer_handle") == "staff"
    qg.message.reply_document.assert_awaited()  # PDF to issuer
    qg.message.reply_text.assert_awaited()      # group buttons
    print("generated + logged ref:", logged["reference_id"])

    # 3) group select -> supervisory + tag sent, driver buttons
    ups, qs = _cbq_update("send_g1")
    asyncio.run(bot.on_group_select(ups, ctx))
    assert ctx.bot.send_message.await_count == 1  # supervisory
    assert ctx.bot.send_document.await_count == 1  # tag to group
    sup = ctx.bot.send_message.await_args.kwargs["text"]
    assert "SUPERVISORY" in sup and logged["reference_id"] in sup
    assert ctx.user_data["sel_group"] == "Team A"
    print("sent to group; supervisory carries reference")

    # 4) driver select -> tag DM'd to driver
    upd, qd = _cbq_update("drv_d1")
    asyncio.run(bot.on_driver_select(upd, ctx))
    assert ctx.bot.send_document.await_count == 2  # +driver DM
    assert str(ctx.bot.send_document.await_args.kwargs["chat_id"]) == "555"
    print("sent to driver Troy")


def test_no_name_or_vin_reprompts():
    ctx = _ctx()
    up, status = _text_update("999 888 777")  # no name, no VIN
    asyncio.run(bot.handle_input(up, ctx))
    assert not ctx.user_data.get("parsed")
    assert "couldn't find" in status.edit_text.await_args.args[0].lower()


if __name__ == "__main__":
    test_full_flow()
    test_no_name_or_vin_reprompts()
    print("ALL FLOW TESTS PASSED")
