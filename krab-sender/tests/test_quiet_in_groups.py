r"""The bot says nothing in a group unless it was actually asked.

Reported: added to a group, it answered every message, with nobody tagging it.

It is a DM tool — you send it a tag, it emails it — but not one handler carried
a chat filter. Any document dropped in a group opened the conversation, and from
then on every text in that chat was "client details" and got a reply. Nobody had
to tag it, and nobody could make it stop.

/groupattach is the one thing that belongs in a group, and it checks the chat
type itself.

Run:  .venv\Scripts\python.exe -m pytest tests/test_quiet_in_groups.py -q
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "bot" / "main.py").read_text(encoding="utf-8")

# Just the handler wiring, so a filters.TEXT mentioned in a docstring elsewhere
# cannot make this pass or fail by accident.
WIRING = SRC.split("def build_application", 1)[-1]


def _lines(pattern):
    return [ln.strip() for ln in WIRING.splitlines() if re.search(pattern, ln)]


def test_every_text_handler_is_private_only():
    """A text handler without a chat filter answers strangers in groups."""
    ungated = [ln for ln in _lines(r"filters\.TEXT")
               if "_DM" not in ln and "ChatType.PRIVATE" not in ln]
    assert ungated == [], f"these answer group messages: {ungated}"


def test_the_document_entry_point_is_private_only():
    """It is the ENTRY POINT — an ungated one turns any group file drop into a
    conversation that then replies to everybody."""
    docs = _lines(r"filters\.Document")
    assert docs, "the document handler vanished"
    for ln in docs:
        assert "_DM" in ln or "ChatType.PRIVATE" in ln, ln


def test_the_free_text_dispatcher_is_private_only():
    i = WIRING.index("handle_free_text,")
    window = WIRING[max(0, i - 200):i]
    assert "_DM" in window or "ChatType.PRIVATE" in window, window


def test_the_insurance_entry_point_is_private_only():
    """It opens a state whose only answer is a private text handler, so in a
    group it would ask a question nobody could answer."""
    line = [ln for ln in _lines(r'CommandHandler\("insurance"')]
    assert line, "the /insurance entry point vanished"
    assert "_DM" in line[0], line[0]


def test_groupattach_is_still_allowed_in_groups():
    """The one command that is SUPPOSED to run in a group."""
    line = [ln for ln in _lines(r'CommandHandler\("groupattach"')]
    assert line, "the /groupattach handler vanished"
    assert "_DM" not in line[0], "groupattach must still work inside a group"


def test_groupattach_still_recognises_a_group():
    body = SRC.split("async def groupattach", 1)[1].split("\nasync def ", 1)[0]
    assert 'chat.type in ("group", "supergroup")' in body


def test_the_private_filter_is_defined_once():
    assert WIRING.count("_DM = filters.ChatType.PRIVATE") == 1
