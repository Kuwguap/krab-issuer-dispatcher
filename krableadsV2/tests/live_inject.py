"""LIVE end-to-end probe: inject a typed message into the REAL bot for ONE chat.

Builds the production handler graph (bot.main() with polling stubbed) against the REAL
Telegram token and REAL Supabase, then feeds it an Update that looks exactly like the
tester typing a line in their DM. The bot's replies land in that real chat, so the review
card can be watched updating live.

It never calls getUpdates and never deletes the webhook, so the deployed instance keeps
polling undisturbed.

Usage:  venv\\Scripts\\python.exe tests/live_inject.py <user_id> "name John Damian" ...
"""
import asyncio
import logging
import sys
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)

import bot  # noqa: E402
import telegram  # noqa: E402
from telegram import Update  # noqa: E402


def build_app():
    captured = {}

    def _fake_run_polling(self, *a, **k):
        captured["app"] = self

    patches = [
        mock.patch.object(bot.Application, "run_polling", _fake_run_polling),
        # never touch the deployed instance's polling / pending updates
        mock.patch.object(bot, "_wait_for_exclusive_polling", lambda *a, **k: True),
        mock.patch("requests.post", mock.MagicMock()),
    ]
    for p in patches:
        p.start()
    try:
        bot.main()
    finally:
        for p in patches:
            p.stop()
    return captured["app"]


def text_update(app, chat_id, user_id, text, mid):
    return Update.de_json({
        "update_id": mid,
        "message": {
            "message_id": mid,
            "date": int(time.time()),
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": "Tester"},
            "text": text,
        },
    }, app.bot)


def show_state(user_id, label):
    st = bot.db.get_user_state(user_id)
    if not st:
        print(f"  [{label}] NO ROW")
        return
    d = st.get("data") or {}
    fields = {k: d.get(k) for k in ("name", "pending_price", "color", "car", "address")}
    print(f"  [{label}] state={st.get('state')!r} keys={len(d)} {fields}")


async def main_async(user_id, lines):
    app = build_app()
    await app.initialize()
    try:
        show_state(user_id, "BEFORE")
        for i, line in enumerate(lines):
            print(f"\n=== INJECTING TYPED MESSAGE: {line!r} ===")
            await app.process_update(text_update(app, user_id, user_id, line, 990000 + i))
            await asyncio.sleep(1.0)
            show_state(user_id, f"AFTER {line!r}")
    finally:
        await app.shutdown()


if __name__ == "__main__":
    uid = int(sys.argv[1])
    msgs = sys.argv[2:] or ["name John Damian"]
    asyncio.run(main_async(uid, msgs))
