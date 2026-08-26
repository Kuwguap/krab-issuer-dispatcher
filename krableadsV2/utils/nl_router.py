"""Route a sentence to a CRM action, using OpenAI function calling.

This REPLACES the extraction half of `classify_supervisor_command`
(ai_vision.py:589) and keeps its contract exactly: `{"intent": str, "args": dict}`
or None. Everything downstream in `_route_supervisor_message` (bot.py:7609) —
the hint gate, the confirm staging for destructive actions, the follow-up TTL,
the `_ROUTER_STRONG_RE` fallback, and the rule that `intent == "lead"` hands the
message back to the lead flow — is already right and is left alone.

WHY FUNCTION CALLING RATHER THAN THE PROMPT

The old router asked for JSON in prose and salvaged whatever came back with a
regex (`_parse_json_from_model`, ai_vision.py:1859). That works until an argument
matters. Tools give the model a typed contract, so:

  * `strict` + `additionalProperties: False` means it cannot invent a field;
  * a missing required argument is a VALIDATION result, not a guess — which is
    what makes the "create lead" → "what is the name?" follow-up possible without
    a second API call;
  * an enum is enforced, so `set_lead_field` cannot write to a field that does
    not exist.

WHAT THIS MODULE MUST NEVER DO

  * decide alone. The bot understands most phrasings with no API at all
    (`_classify_review_command`, measured against 954 real utterances) and this
    account has run out of credits before. The model's answer wins when it
    arrives; the deterministic one answers when it does not.
  * act on an argument it did not verify — see `missing_args`.
  * fire while a prompt is waiting for a literal value. Typing "Temp Tag" at a
    field prompt used to wipe the whole card; a model reading it as a command
    would do the same thing with better grammar.

Inert without OPENAI_API_KEY, so a developer machine and the test suite route
deterministically and cost nothing.
"""
import json
import logging
import time
from typing import Optional

from config import Config
from utils.ai_vision import AIVisionQuotaError

logger = logging.getLogger(__name__)

# A tool call that is one answer short, parked for the next message. The TTL
# matches _ROUTER_FOLLOWUP_TTL_SEC in bot.py deliberately: two different
# expiries for the same idea is how one of them gets forgotten.
NL_PENDING_KEY = "nl_pending"
NL_PENDING_TTL_SEC = 180

# A Telegram user re-sends at about five seconds. Being right after eight is
# worse than being approximately right after one — and the deterministic answer
# is already waiting.
ROUTE_TIMEOUT_SEC = 8.0


def _fn(name: str, description: str, properties: dict) -> dict:
    """A tool declaration, strict.

    Under `strict`, EVERY property must be listed in `required` — so an optional
    argument is expressed as a nullable type, never by omission. Getting that
    backwards is the most common way a strict schema is rejected outright.
    """
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": list(properties),
                "additionalProperties": False,
            },
        },
    }


def _req(desc: str = "") -> dict:
    d = {"type": "string"}
    if desc:
        d["description"] = desc
    return d


def _opt(desc: str = "") -> dict:
    """Optional == nullable, for the reason in `_fn`."""
    d = {"type": ["string", "null"]}
    if desc:
        d["description"] = desc
    return d


def _flag(desc: str = "") -> dict:
    d = {"type": "boolean"}
    if desc:
        d["description"] = desc
    return d


# The fields of a lead, exactly as the review card names them, so the schema and
# the adapter cannot drift.
LEAD_FIELDS = (
    "first_name", "last_name", "address", "city_state_zip",
    "delivery_address", "delivery_city_state_zip", "vin", "car", "color",
    "insurance_company", "insurance_policy_number", "phone", "price",
    "issuer_note", "driver_note", "email", "driver_license",
)

TOOLS = [
    # ── the lead flow ───────────────────────────────────────────────────────
    _fn(
        "create_lead",
        "The message is CLIENT OR VEHICLE INFORMATION, or a request to add, "
        "create or start a lead, tag, client or sale. WHEN UNSURE, CHOOSE THIS "
        "— a real client's details must never be mistaken for a command. Fill "
        "only what was actually said; leave everything else null and never "
        "invent a value.",
        {
            "first_name": _opt(),
            "last_name": _opt(),
            "phone": _opt("exactly as spoken; do not reformat"),
            "price": _opt("e.g. '150' or '150 plus toll'"),
            "vin": _opt("17 characters, only if one was actually given"),
            "car": _opt("year make model, e.g. '2017 Nissan Altima'"),
            "color": _opt(),
            "address": _opt("registration street address"),
            "city_state_zip": _opt(),
            "insurance_company": _opt(),
            "insurance_policy_number": _opt(),
        },
    ),
    _fn(
        "update_lead",
        "Change ONE field on the lead currently on the review card: 'change the "
        "colour to black', 'the price is 150', 'their phone number is ...'.",
        {
            "field": {"type": "string", "enum": list(LEAD_FIELDS)},
            "value": _req("the new value, as the operator said it"),
            "vehicle": {"type": ["integer", "null"],
                        "description": "null or 1 for the lead's own car; 2+ for an extra car"},
        },
    ),
    _fn(
        "select_driver",
        "Choose which driver gets this lead. Pass 'all' to broadcast.",
        {"driver": _req("a driver's name, or the word 'all'")},
    ),
    _fn(
        "select_dispatcher",
        "Choose the dispatcher TEAM for this lead. Teams are called "
        "'dispatchers' in this bot; the people who deliver are 'drivers'.",
        {"dispatcher": _req("a team name, or the word 'all'")},
    ),
    _fn(
        "add_vehicle",
        "Add another car to the lead on screen. One client, one price, one "
        "receipt — only the tags multiply.",
        {},
    ),
    _fn(
        "submit_lead",
        "Send the lead on the review card to the dispatch group. IRREVERSIBLE. "
        "Only for an unambiguous instruction to send it now — never when the "
        "operator is commenting on the lead, and never as part of a note.",
        {},
    ),
    # ── lookups and reports (the existing router's intents) ──────────────────
    _fn("get_lead_status",
        "Ask about ONE lead by its reference id.",
        {"reference_id": _req("8 characters, e.g. ABC12345")}),
    _fn("list_groups", "Which groups or teams exist or are active.", {}),
    _fn("list_drivers", "Which drivers exist or are active.", {}),
    _fn("list_suspended", "Who is suspended, or blocked for owing receipts.", {}),
    _fn("pending_receipts", "Who owes receipts; outstanding receipt debt.", {}),
    _fn("usage", "Who has been sending leads; recent lead activity.", {}),
    _fn("help", "Asking what the bot can do.", {}),
    # ── admin ───────────────────────────────────────────────────────────────
    _fn("driverblock",
        "Turn the redaction of client phone numbers from drivers on or off.",
        {"enable": _flag()}),
    _fn("group_status",
        "Enable or disable a dispatcher team by name.",
        {"name": _req("the team's name"), "enable": _flag()}),
    # "active", not "enable" — the dispatch branch at bot.py:7936 reads
    # args["active"], while its neighbour group_status reads args["enable"].
    # Declaring the wrong one here does not fail, it INVERTS: a missing key reads
    # as False, so "activate Susan" would deactivate her.
    _fn("driver_status",
        "Activate or deactivate a driver by name.",
        {"name": _req("the driver's name"), "active": _flag()}),
    _fn("broadcast",
        "Send a message to every group, driver and lead sender. Leave the text "
        "null if the operator has not said what to send yet — they will be asked.",
        {"message": _opt("the text to broadcast")}),
    _fn("set_plate",
        "Set a temp-plate counter to a specific number.",
        {"which": {"type": "string", "enum": ["resident", "non_resident"]},
         "number": _req("the digits to set it to")}),
]

# NOT a tool: opening a screen. `_bare_command_to_slash` (bot.py:7018, handler
# group -3) already maps "settings", "show me the drivers", "leaderboard" and 35
# other exact phrases onto the real slash commands, and it runs long before this
# router is reached. A second, probabilistic path to the same screens could only
# ever disagree with the first — and the first cannot hallucinate.

TOOL_NAMES = tuple(t["function"]["name"] for t in TOOLS)

# The tools the old prompt-based router already had names for. Kept so the
# existing dispatch table in bot.py keeps matching without edits.
LEGACY_INTENTS = frozenset({
    "lead", "list_groups", "list_drivers", "list_suspended", "pending_receipts",
    "usage", "lead_lookup", "driverblock", "group_status", "driver_status",
    "broadcast", "set_plate", "set_plate_from_image", "help", "none",
})

# Tool name → the intent string `_route_supervisor_message` already dispatches
# on. Anything absent keeps its own name and is handled by the new branches.
_TOOL_TO_INTENT = {
    "create_lead": "lead",          # returns False there → the lead flow takes it
    "get_lead_status": "lead_lookup",
}

_SYSTEM = """You are the intent router for a New Jersey temporary-tag dispatch \
bot on Telegram. The operator typed or dictated the message below. Choose exactly \
one tool, or none at all.

Rules, in order of importance:

1. If the message is client or vehicle INFORMATION — a name, an address, a VIN, a \
price, a car — choose create_lead. When you are unsure between create_lead and \
anything else, choose create_lead: a real client's details turned into a command \
is the expensive mistake, and the reverse is merely a re-type.
2. Fill an argument ONLY from what was actually said. Never guess a name, a VIN, \
a price, a driver or a team. Leave it null and it will be asked for.
3. A NOTE about the job is not a command. "driver note call ahead", "no answer at \
the door", "the client says he is running late" are all notes — choose no tool.
4. submit_lead sends a legal document and cannot be undone.
5. If the message is small talk, a question you cannot act on, or genuinely \
ambiguous between two tools, choose no tool.
"""


def is_configured() -> bool:
    return bool((Config.OPENAI_API_KEY or "").strip())


def _spec(tool: str) -> Optional[dict]:
    return next((t for t in TOOLS if t["function"]["name"] == tool), None)


def missing_args(tool: str, args: dict) -> list:
    """Required arguments the model left empty, in declaration order.

    Checked locally rather than by asking the model again: instant, free, and it
    cannot itself hallucinate. A nullable property is optional in everything but
    name, so it never appears here.
    """
    spec = _spec(tool)
    if spec is None:
        return []
    props = spec["function"]["parameters"]["properties"]
    out = []
    for name, prop in props.items():
        types = prop.get("type")
        if isinstance(types, list) and "null" in types:
            continue
        if types == "boolean":
            if args.get(name) is None:
                out.append(name)
            continue
        v = args.get(name)
        if v is None or (isinstance(v, str) and not v.strip()):
            out.append(name)
    return out


def clean_args(tool: str, args: dict) -> dict:
    """Drop nulls, blanks and anything outside the schema.

    `strict` should make the last part unnecessary; it is here because a schema
    change and a model rollout do not land at the same moment.
    """
    spec = _spec(tool)
    if spec is None:
        return {}
    allowed = spec["function"]["parameters"]["properties"]
    out = {}
    for k, v in (args or {}).items():
        if k not in allowed or v is None:
            continue
        if isinstance(v, str):
            v = v.strip()
            if not v:
                continue
        out[k] = v
    return out


def card_summary(card: dict) -> str:
    """What is already on the review card, so a follow-up resolves in one call.

    Field NAMES and whether they are filled — never the values. The model does
    not need the client described to it in order to know that "change it to
    black" means the colour field, and this text goes to a third party.
    """
    if not card:
        return "No lead is currently on screen."
    filled = [f for f in LEAD_FIELDS if str(card.get(f) or "").strip() not in ("", "-")]
    parts = [
        "A lead is on screen.",
        "Filled: " + (", ".join(filled) or "nothing yet") + ".",
        "Empty: " + (", ".join(f for f in LEAD_FIELDS if f not in filled) or "nothing") + ".",
    ]
    for label, key in (("Driver", "selected_driver_names"),
                       ("Dispatcher", "selected_group_name")):
        v = str(card.get(key) or "").strip()
        if v:
            parts.append(f"{label}: {v}.")
    extra = card.get("extra_vehicles")
    if isinstance(extra, list) and extra:
        parts.append(f"It carries {len(extra) + 1} cars.")
    return " ".join(parts)


def classify(user_message: str, *, card: Optional[dict] = None) -> Optional[dict]:
    """`{"intent": str, "args": dict}` — a drop-in for classify_supervisor_command.

    Returns None when the API is unconfigured or the call fails for a reason
    that is not a quota problem; the caller then falls back exactly as it does
    today. Raises AIVisionQuotaError on 429/insufficient quota, reusing the
    class the rest of this codebase already understands.

    Synchronous, like the function it replaces, so the call site does not change.
    Wrap it in asyncio.to_thread at the call site — the existing one does not,
    which blocks the event loop, and matters much more once concurrent updates
    are on.
    """
    txt = (user_message or "").strip()
    if not txt or not is_configured():
        return None
    try:
        from openai import OpenAI
    except ImportError:
        logger.warning("nl_router: openai SDK unavailable")
        return None

    try:
        client = OpenAI(api_key=str(Config.OPENAI_API_KEY).strip(),
                        max_retries=0, timeout=ROUTE_TIMEOUT_SEC)
        resp = client.chat.completions.create(
            model=getattr(Config, "OPENAI_VISION_MODEL", None) or "gpt-4o",
            messages=[
                {"role": "system",
                 "content": _SYSTEM + "\n\n" + card_summary(card or {})},
                {"role": "user", "content": txt[:2000]},
            ],
            tools=TOOLS,
            tool_choice="auto",
            parallel_tool_calls=False,
            temperature=0,
        )
    except Exception as e:
        msg = str(e).lower()
        if any(s in msg for s in ("429", "insufficient_quota", "quota",
                                  "rate limit", "credit balance")):
            raise AIVisionQuotaError("API quota exceeded") from e
        logger.warning("nl_router: classify failed: %s", e)
        return None

    try:
        calls = resp.choices[0].message.tool_calls or []
    except (AttributeError, IndexError):
        return None
    if not calls:
        return {"intent": "none", "args": {}}

    name = calls[0].function.name
    if name not in TOOL_NAMES:
        logger.warning("nl_router: unknown tool %r", name)
        return {"intent": "none", "args": {}}
    try:
        raw = json.loads(calls[0].function.arguments or "{}")
    except json.JSONDecodeError:
        logger.warning("nl_router: unparseable arguments for %s", name)
        return {"intent": "none", "args": {}}

    args = clean_args(name, raw if isinstance(raw, dict) else {})
    intent = _TOOL_TO_INTENT.get(name, name)
    # The old router named this argument "reference"; keep both so the existing
    # dispatch branch works untouched.
    if intent == "lead_lookup" and "reference_id" in args:
        args.setdefault("reference", args["reference_id"])
    return {"intent": intent, "args": args, "tool": name}


# ── a call that is one answer short ─────────────────────────────────────────

def park(user_data: dict, tool: str, args: dict, needs: str) -> None:
    """Remember a tool call waiting on one argument."""
    user_data[NL_PENDING_KEY] = {
        "tool": tool, "args": dict(args), "needs": needs, "ts": time.time(),
    }


def take_parked(user_data: dict) -> Optional[dict]:
    """The parked call, or None if there is none or it has expired.

    Always POPS, whatever the outcome. A parked call is consumed by the next
    message either way, and a stale one left behind is how the next unrelated
    sentence gets filed as somebody's name — the same failure the router's own
    follow-ups guard against with the same TTL.
    """
    pending = (user_data or {}).pop(NL_PENDING_KEY, None)
    if not pending:
        return None
    if time.time() - float(pending.get("ts") or 0) > NL_PENDING_TTL_SEC:
        logger.info("nl_router: a parked %s expired unanswered", pending.get("tool"))
        return None
    return pending


def ask_for(tool: str, needs: str) -> str:
    """The question to ask for one missing argument, in the operator's terms."""
    return {
        ("create_lead", "first_name"): "What is the client's name?",
        ("update_lead", "value"): "What should I change it to?",
        ("select_driver", "driver"): "Which driver should take it?",
        ("select_dispatcher", "dispatcher"): "Which dispatcher team?",
        ("get_lead_status", "reference_id"): "What is the reference id?",
        ("broadcast", "message"): "What should I send to everyone?",
        ("group_status", "name"): "Which team?",
        ("driver_status", "name"): "Which driver?",
        ("set_plate", "number"): "What number should it be set to?",
    }.get((tool, needs), f"What is the {needs.replace('_', ' ')}?")
