r"""No handler may talk to Supabase on the event loop.

This is the rule that made the bot feel slow, written down so it cannot come
back. A `db.foo()` inside an `async def` is a synchronous HTTPS round trip --
measured at ~200ms against the live project -- executed on the asyncio event
loop. For its whole duration the bot cannot progress ANY other coroutine: not
another person's button tap, not an answerCallbackQuery, nothing. It was 373 such
calls across 121 handlers, so handle_accept_lead alone froze everything for about
2.8 seconds, and the bot got slower the more people were using it at once.

Two ways to satisfy the rule:

  * `await asyncio.to_thread(db.foo, arg)` -- the call still happens, just not on
    the loop, so everyone else keeps moving;
  * or the answer comes from memory, for the handful of reads below where that is
    provably safe.

_MEMORY_SERVED is deliberately short and each entry has to earn its place. The
conversation state qualifies because this process is its only writer (see
tests/test_state_cache.py, which fails if a second one appears). Settings and the
group list qualify on a few-second TTL because they are read constantly and
change rarely. Lead rows do NOT qualify and must never be added: eight callers
write them, and the accept path races on purpose -- a cached lead is how two
drivers both get told a job is theirs.

Run:  venv\Scripts\python.exe -m pytest tests/test_no_blocking_db_in_handlers.py -q
"""
import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Answered from memory in utils/database.py, so the call costs no round trip.
_MEMORY_SERVED = {
    "get_user_state", "set_user_state", "clear_user_state",   # _StateStore
    "get_setting",                                           # _SETTING_TTL_SEC
    "get_all_groups", "get_group_by_id",                     # _GROUPS_TTL_SEC
}


def _blocking_db_calls(path):
    """(function name, line, method) for every db.* call made straight from an
    async def -- excluding ones already inside asyncio.to_thread, and excluding
    nested functions, whose body is not the async one's."""
    tree = ast.parse(path.read_text(encoding="utf-8"))

    threaded = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and (getattr(n.func, "attr", None)
                                        or getattr(n.func, "id", None)) == "to_thread":
            for s in ast.walk(n):
                threaded.add(id(s))

    out = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        nested = {id(x)
                  for inner in ast.walk(fn)
                  if isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and inner is not fn
                  for x in ast.walk(inner)}
        for n in ast.walk(fn):
            if not isinstance(n, ast.Call) or id(n) in threaded or id(n) in nested:
                continue
            f = n.func
            if (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
                    and f.value.id == "db" and f.attr not in _MEMORY_SERVED):
                out.append((fn.name, n.lineno, f.attr))
    return out


class TheEventLoopIsNeverHeldTest(unittest.TestCase):

    def test_no_handler_calls_supabase_on_the_event_loop(self):
        found = _blocking_db_calls(ROOT / "bot.py")
        if found:
            worst = {}
            for name, line, method in found:
                worst.setdefault(name, []).append("%s:%d" % (method, line))
            detail = "\n".join(
                "    %s -- %s" % (n, ", ".join(v))
                for n, v in sorted(worst.items(), key=lambda kv: -len(kv[1]))[:10])
            self.fail(
                "%d blocking Supabase call(s) in %d async handler(s); each one "
                "freezes the whole bot for ~200ms:\n%s\n\n"
                "Wrap it: await asyncio.to_thread(db.method, args)"
                % (len(found), len(worst), detail))


class TheMemoryServedListStaysHonestTest(unittest.TestCase):

    def test_every_exemption_is_actually_served_from_memory(self):
        """An entry here that is NOT cached is worse than no entry: it exempts a
        real round trip from the rule above."""
        src = (ROOT / "utils" / "database.py").read_text(encoding="utf-8")
        for method in sorted(_MEMORY_SERVED):
            i = src.find("def %s(" % method)
            self.assertNotEqual(-1, i, "%s is gone from database.py" % method)
            body = src[i:i + 2600]
            self.assertTrue(
                "_STATES" in body or "_ttl_get" in body,
                "%s is exempt from the no-blocking rule but reads nothing from "
                "memory" % method)

    def test_lead_rows_are_not_exempt(self):
        """Leads have eight writers and the accept path races deliberately. A
        cached lead is how two drivers both get told the job is theirs."""
        for never in ("get_lead_by_id", "get_lead_assignment_status",
                      "get_renewal_by_id", "update_lead"):
            self.assertNotIn(never, _MEMORY_SERVED)


if __name__ == "__main__":
    unittest.main()
