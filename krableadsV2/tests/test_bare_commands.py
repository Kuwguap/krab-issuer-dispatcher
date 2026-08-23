"""Commands work without the slash: "settings" does what "/settings" does.

The bare word is rewritten into a real command (text + bot_command entity) and allowed
to flow on, so PTB routes it through the SAME handler as the typed slash — there is no
second implementation to drift.

Run:  venv\\Scripts\\python.exe -m pytest tests/test_bare_commands.py -q
"""
import asyncio
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

sys.argv = ["pytest"]
import test_real_routing_e2e as e2e  # noqa: E402  (shared real-Application harness)
import bot  # noqa: E402
import telegram  # noqa: E402
from telegram import Update  # noqa: E402

CHAT = e2e.CHAT_ID
USER = e2e.USER_ID

# bare phrase -> the handler the slash command would reach
ROUTES = [
    ("help", "cmd_help"),
    ("settings", "cmd_settings"),
    ("settings, please.", "cmd_settings"),
    ("open settings", "cmd_settings"),
    ("SETTINGS", "cmd_settings"),
    ("whoami", "cmd_whoami"),
    ("who am i", "cmd_whoami"),
    ("me", "cmd_whoami"),
    ("receipts", "handle_driver_receipts_menu_command"),
    ("followup", "cmd_followup_start"),
    ("all followups", "cmd_all_followups"),
    ("announce", "cmd_announce"),
    ("driverblock", "cmd_driverblock"),
    ("test", "cmd_test"),
]

# Whole-message match only: these must stay ordinary text.
NOT_COMMANDS = [
    "name Test Client",
    "driver note please help",
    "the color is settings",
    "help me move the car",
    "Me and my brother",
    "appeal to the court for more time",
    "test drive",
    "price 150",
]


def _text_update(app, i, text, as_command=False):
    msg = {"message_id": i, "date": int(time.time()),
           "chat": {"id": CHAT, "type": "private"},
           "from": {"id": USER, "is_bot": False, "first_name": "S"},
           "text": text}
    if as_command:
        msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text)}]
    return Update.de_json({"update_id": i, "message": msg}, app.bot)


def _reaches(bare, target_name):
    """True when the bare phrase reaches the handler the slash command would."""
    async def run():
        hit = {"n": 0}

        async def spy(update, context, *a, **k):
            hit["n"] += 1
            return bot.ConversationHandler.END

        sup = mock.patch.object(bot, "_user_is_global_supervisor",
                                mock.MagicMock(return_value=True))
        tgt = mock.patch.object(bot, target_name, spy)
        sup.start(); tgt.start()
        app = e2e._build_application()          # built INSIDE the patches
        try:
            with mock.patch.object(telegram.Bot, "_do_post", e2e.TRANSPORT.do_post):
                await app.initialize()
                try:
                    e2e.FAKE_DB.states.clear()
                    await app.process_update(_text_update(app, 1, bare))
                    return hit["n"] > 0
                finally:
                    await app.shutdown()
        finally:
            tgt.stop(); sup.stop()
    return asyncio.run(run())


class BareCommandMappingTest(unittest.TestCase):
    """The pure mapping, without the handler graph."""

    def test_known_phrases_map(self):
        cases = {
            "settings": "settings", "Settings": "settings", "SETTINGS": "settings",
            "settings, please.": "settings", "open settings": "settings",
            "help": "help", "commands": "help",
            "whoami": "whoami", "who am i": "whoami", "my id": "whoami", "me": "whoami",
            "receipt": "receipt", "receipts": "receipt", "recipts": "receipt",
            "followup": "followup", "prospect": "followup", "my clients": "followups",
            "all followups": "allfollowups", "announce": "announce",
            "broadcast": "announce", "driverblock": "driverblock",
            "driver block": "driverblock", "appeal": "appeal", "test": "test",
        }
        wrong = {t: bot._bare_command_for(t) for t, want in cases.items()
                 if bot._bare_command_for(t) != want}
        self.assertEqual({}, wrong)

    def test_a_word_inside_a_value_is_not_a_command(self):
        wrong = [t for t in NOT_COMMANDS if bot._bare_command_for(t) is not None]
        self.assertEqual([], wrong)

    def test_every_mapped_command_is_actually_registered(self):
        """A typo in the map would silently route to a command that does not exist."""
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        appeal_src = (ROOT / "appeal_flow.py").read_text(encoding="utf-8")
        for cmd in set(bot._BARE_COMMANDS.values()):
            with self.subTest(cmd=cmd):
                self.assertTrue(f'"{cmd}"' in src or f'"{cmd}"' in appeal_src,
                                f"/{cmd} is not registered anywhere")


class BareCommandRoutingTest(unittest.TestCase):
    """Through the real handler graph."""

    def test_bare_phrases_reach_the_same_handler_as_the_slash(self):
        failed = [f"{bare} -> {target}" for bare, target in ROUTES
                  if not _reaches(bare, target)]
        self.assertEqual([], failed)


class DoesNotHijackTypedValuesTest(unittest.TestCase):
    """A prompt waiting for a value keeps it, even if it reads like a command."""

    def _rewrote(self, text, user_data):
        msg = SimpleNamespace(text=text)
        update = SimpleNamespace(effective_message=msg)
        ctx = SimpleNamespace(user_data=user_data)
        asyncio.run(bot._bare_command_to_slash(update, ctx))
        return msg.text != text

    def test_settings_input_prompt_keeps_the_value(self):
        # a client source really could be called "Test"
        self.assertFalse(self._rewrote("test", {"tset_await": {"kind": "add_source"}}))

    def test_field_edit_prompt_keeps_the_value(self):
        self.assertFalse(self._rewrote("me", {"phase1_pending_edit_key": "fn"}))

    def test_cancel_still_escapes_a_prompt(self):
        self.assertTrue(self._rewrote("cancel", {"tset_await": {"kind": "add_source"}}))

    def test_an_already_slashed_command_is_untouched(self):
        self.assertFalse(self._rewrote("/settings", {}))


if __name__ == "__main__":
    unittest.main()
