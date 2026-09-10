r"""One character must never stop a lead reaching drivers.

Lead 1K3AX0XS (a client form): "A client form reached no drivers / send_failed".
The driver offer is legacy Markdown on purpose -- the reference in code, a closing
italic line -- and it interpolated the customer's delivery details raw. Those
held the email Josue_jeanette@..., one underscore, so Telegram answered "can't
parse entities" for every one of the 76 drivers. The dispatcher groups got
their offer only because it carries no customer text.

Held still here:
  * every customer value in a Markdown driver / dispatcher-group message is
    escaped, and the template's own formatting still renders;
  * if something still slips through, the send is retried ONCE as plain text,
    and every other BadRequest still raises;
  * the dispatcher-group offer and the web-order supervisory notice say, in one
    line, whether the client is insured.

Mocks only -- no network, no database, no Telegram.

Run:  venv\Scripts\python.exe -m pytest tests/test_offer_markdown_safety.py -q
"""
import asyncio
import os
import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from telegram.error import BadRequest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy-key")

import bot  # noqa: E402

LEAD_ID = "8c0e3a2e-6a55-4c41-9a2b-2f6a1f0f9c11"
GROUP_ID = "5d1f0c7e-2b8a-4f3e-9c6d-7a8b9c0d1e2f"
GROUP = {"id": GROUP_ID, "group_name": "HighKage", "is_active": True,
         "group_telegram_id": "-100123"}
EMAIL = "Josue_jeanette@icloud.com"
PARSE_ERR = "Can't parse entities: can't find end of the entity starting at byte offset 188"

# name / addr / csz / dl-addr / dl-csz / vin / car / colour / insurer / policy / notes
VD_UNINSURED = ("Josue Pavon\n2815 Dewey Avenue\nBronx, NY 10465\n-\n-\n"
                "JTLKT324364094480\n2006 Scion xB\nGrey\n-\n-\n-")
VD_OWN = VD_UNINSURED.replace("\nGrey\n-\n-\n", "\nGrey\nGeico_Direct\nPOL_9*\n")


# ------------------------------------------------------------- the validator

def parse_legacy_markdown(text):
    """Telegram's legacy "Markdown" parser, as far as validity goes (tdlib's
    parse_markdown). Returns (plain_text, [(kind, inner), ...]); raises
    ValueError exactly where Telegram answers "can't parse entities".

    A backslash escapes only _ * ` and [ ; anything else it precedes is literal.
    Each of those four, unescaped, opens an entity that must be closed by the
    same character (] for [, optionally followed by (url)). Entities do not
    nest and nothing inside one is escaped -- so a lone _ in a customer's text
    either has no partner, or pairs with the template's own _ and leaves the
    template's partner alone at the end. Either way: rejected.
    """
    out, entities = [], []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "\\" and i + 1 < n and text[i + 1] in "_*`[":
            out.append(text[i + 1])
            i += 2
            continue
        if c not in "_*`[":
            out.append(c)
            i += 1
            continue
        start = i
        if text.startswith("```", i):
            end = text.find("```", i + 3)
            if end < 0:
                raise ValueError(f"can't find end of pre entity at {start}")
            entities.append(("pre", text[i + 3:end]))
            out.append(text[i + 3:end])
            i = end + 3
            continue
        close = "]" if c == "[" else c
        end = text.find(close, i + 1)
        if end < 0:
            raise ValueError(f"can't find end of the entity starting at {start}")
        inner = text[i + 1:end]
        i = end + 1
        if c == "[" and i < n and text[i] == "(":
            paren = text.find(")", i + 1)
            if paren < 0:
                raise ValueError(f"can't find end of the link url at {i}")
            i = paren + 1
        entities.append(({"_": "italic", "*": "bold", "`": "code", "[": "link"}[c], inner))
        out.append(inner)
    return "".join(out), entities


class TheValidatorTest(unittest.TestCase):
    """The validator has to be able to fail, or it proves nothing."""

    def test_it_rejects_a_lone_entity_character(self):
        for bad in ("a_b", "a*b", "a`b", "a[b", "x _y_ z_"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    parse_legacy_markdown(bad)

    def test_it_reads_escapes_and_entities(self):
        plain, ents = parse_legacy_markdown("a\\_b `REF1` _it_ *b* [t](http://x)")
        self.assertEqual("a_b REF1 it b t", plain)
        self.assertEqual([("code", "REF1"), ("italic", "it"), ("bold", "b"), ("link", "t")],
                         ents)


def _offer_lead(**over):
    lead = {"id": LEAD_ID, "reference_id": "1K3AX0XS",
            "delivery_details": f"2815 Dewey Avenue\nBronx, NY 10465\n{EMAIL}",
            "extra_info": "ASAP *today* [back door] `gate`",
            "special_request_drivers": "ring twice_ then call",
            "vehicle_details": VD_UNINSURED}
    lead.update(over)
    return lead


def _no_phone_redaction():
    # The OneTimeSecret redaction is not under test; keep it off the network.
    return mock.patch.object(bot, "_sanitize_phones_for_send",
                             lambda t: str(t or "").strip())


# ------------------------------------------------------- 1a the driver offer

class TheDriverOfferParsesTest(unittest.TestCase):

    def test_the_1K3AX0XS_offer_is_valid_markdown_and_keeps_the_email(self):
        with _no_phone_redaction():
            text = bot._driver_offer_message_text(_offer_lead())
        plain, ents = parse_legacy_markdown(text)
        self.assertIn(EMAIL, plain, "the escaped email must read exactly as typed")
        self.assertIn("ASAP *today* [back door] `gate`", plain)
        self.assertIn("ring twice_ then call", plain)
        # The template's own formatting still renders.
        self.assertIn(("code", "1K3AX0XS"), ents)
        self.assertTrue(any(k == "italic" and v.startswith("Tap Accept below")
                            for k, v in ents), ents)

    def test_the_same_offer_unescaped_is_exactly_what_telegram_refused(self):
        """Proof the test would have caught the incident."""
        with _no_phone_redaction(), \
                mock.patch.object(bot, "_telegram_md1_escape", lambda s: str(s or "")):
            text = bot._driver_offer_message_text(_offer_lead(
                extra_info="ASAP", special_request_drivers=""))
        with self.assertRaises(ValueError):
            parse_legacy_markdown(text)

    def test_an_ordinary_lead_reads_as_before(self):
        with _no_phone_redaction():
            text = bot._driver_offer_message_text(_offer_lead(
                delivery_details="Bronx, NY 10465", extra_info="ASAP",
                special_request_drivers=""))
        self.assertIn("📍 Delivery (City, State, Zip): Bronx, NY 10465\n", text)
        self.assertIn(" Delivery Time 🏷️: ASAP\n", text)
        self.assertTrue(text.endswith("_Tap Accept below, or just reply *accept*._"))
        parse_legacy_markdown(text)

    def test_the_resend_builder_parses_too(self):
        # This builder reads the delivery city/state/zip, which comes from the
        # delivery_details column when there is one.
        lead = _offer_lead(delivery_details="2815 Dewey Ave\nBronx_NY *10465*")
        with _no_phone_redaction():
            text = bot._build_driver_resend_request_message(lead)
        plain, ents = parse_legacy_markdown(text)
        self.assertIn("Bronx_NY *10465*", plain)
        self.assertIn("ring twice_ then call", plain)
        self.assertIn(("code", "1K3AX0XS"), ents)


# ------------------------------------------------- 1b the sender's safety net

class ThePlainTextRetryTest(unittest.IsolatedAsyncioTestCase):

    def _ctx(self, *side_effect):
        ctx = mock.MagicMock()
        ctx.bot.send_message = mock.AsyncMock(side_effect=list(side_effect))
        return ctx

    async def test_a_parse_failure_is_sent_once_more_without_parse_mode(self):
        ctx = self._ctx(BadRequest(PARSE_ERR), "ok")
        with self.assertLogs(bot.logger, level="WARNING") as logs:
            out = await bot._send_message_resiliently(
                ctx, 4242, "a_b", parse_mode="Markdown", reply_markup="kb")
        self.assertEqual("ok", out)
        first, second = ctx.bot.send_message.call_args_list
        self.assertEqual("Markdown", first.kwargs["parse_mode"])
        self.assertNotIn("parse_mode", second.kwargs)
        self.assertEqual("a_b", second.kwargs["text"])
        self.assertEqual("kb", second.kwargs["reply_markup"])
        self.assertTrue(any("4242" in line for line in logs.output),
                        "the warning must name the chat")

    async def test_the_retry_happens_once(self):
        ctx = self._ctx(BadRequest(PARSE_ERR), BadRequest("Forbidden: bot was blocked"))
        with self.assertRaises(BadRequest):
            await bot._send_message_resiliently(ctx, 1, "a_b", parse_mode="Markdown")
        self.assertEqual(2, ctx.bot.send_message.await_count)

    async def test_every_other_bad_request_still_raises(self):
        ctx = self._ctx(BadRequest("Chat not found"))
        with self.assertRaises(BadRequest):
            await bot._send_message_resiliently(ctx, 1, "hi", parse_mode="Markdown")
        self.assertEqual(1, ctx.bot.send_message.await_count)

    async def test_no_parse_mode_means_nothing_to_drop(self):
        ctx = self._ctx(BadRequest(PARSE_ERR))
        with self.assertRaises(BadRequest):
            await bot._send_message_resiliently(ctx, 1, "hi")
        self.assertEqual(1, ctx.bot.send_message.await_count)


def _strict_telegram(calls):
    """A send_message that parses Markdown the way Telegram does."""
    async def send_message(chat_id, text, **kw):
        calls.append(dict(kw, chat_id=chat_id, text=text))
        if kw.get("parse_mode") == "Markdown":
            try:
                parse_legacy_markdown(text)
            except ValueError as e:
                raise BadRequest(f"Can't parse entities: {e}")
        return SimpleNamespace(message_id=len(calls))
    return send_message


DRIVERS = [
    {"id": "d1", "driver_name": "Ann", "driver_telegram_id": "111", "is_active": True},
    {"id": "d2", "driver_name": "Bo", "driver_telegram_id": "222", "is_active": True},
]


def _fan_out(lead):
    calls = []
    ctx = mock.MagicMock()
    ctx.bot.send_message = mock.AsyncMock(side_effect=_strict_telegram(calls))
    db = mock.MagicMock()
    db.get_group_driver_rows_for_group.return_value = list(DRIVERS)
    with mock.patch.object(bot, "db", db), _no_phone_redaction(), \
            mock.patch.object(bot, "_only_drivers", lambda rows: list(rows or [])), \
            mock.patch.object(bot, "_get_suspended_driver_ids", lambda: set()):
        result = asyncio.run(bot._send_driver_requests_for_group(ctx, lead, GROUP))
    return result, calls


class TheLeadReachesTheDriversTest(unittest.TestCase):

    def test_the_1K3AX0XS_lead_reaches_every_driver(self):
        (count, _names, reason, _scope), calls = _fan_out(_offer_lead())
        self.assertIsNone(reason)
        self.assertEqual(2, count)
        self.assertEqual(["Markdown", "Markdown"], [c.get("parse_mode") for c in calls])

    def test_even_unescaped_the_safety_net_delivers(self):
        """Take the escaping away and the plain-text retry still gets it there.
        Exactly the incident: ONE lone underscore, in the email. (Two would pair
        up across the template's italic line and parse by accident.)"""
        with mock.patch.object(bot, "_telegram_md1_escape", lambda s: str(s or "")):
            (count, _n, reason, _s), calls = _fan_out(_offer_lead(
                extra_info="ASAP", special_request_drivers=""))
        self.assertIsNone(reason)
        self.assertEqual(2, count)
        self.assertEqual(4, len(calls), "one refused Markdown send + one plain per driver")
        plain = [c for c in calls if "parse_mode" not in c]
        self.assertEqual({111, 222}, {c["chat_id"] for c in plain})
        self.assertTrue(all(EMAIL in c["text"] for c in plain))


# -------------------------------------------------- the dispatcher-group offer

def _group_offer(lead, fn="_post_lead_to_all_groups_for_approval", side_effect=None):
    calls = []
    ctx = mock.MagicMock()
    ctx.bot.send_message = mock.AsyncMock(
        side_effect=side_effect if side_effect is not None else _strict_telegram(calls))
    db = mock.MagicMock()
    with mock.patch.object(bot, "db", db):
        if fn == "_post_lead_to_all_groups_for_approval":
            asyncio.run(bot._post_lead_to_all_groups_for_approval(ctx, lead, [GROUP]))
        else:
            asyncio.run(bot._post_single_group_approval(ctx, lead, GROUP))
    return ctx, db, calls


def _web(**over):
    lead = {"id": LEAD_ID, "reference_id": "1K3AX0XS", "vehicle_details": VD_UNINSURED,
            # A tristatetags.com checkout order is labelled "External API" on the
            # live database (all of the last 22, 1K3AX0XS included). "Client
            # Form" is the /form page, which the bot covers itself at the tag.
            "external_order_id": "f85e6052", "contact_info_source": "External API"}
    lead.update(over)
    return lead


ISSUER_OFFER = (
    "🏷 NEW CLIENT\n"
    "📋 Ref ID: `ISSU1234`\n\n"
    "✅ Double-check the tag for mistakes\n"
    "📲 Send tag with Krab Dispatch (@KrabIssuerBot)\n"
    "📋 Copy/paste client phone, address, and delivery time"
)


class TheGroupOfferTest(unittest.TestCase):

    def test_a_web_lead_offer_says_whether_it_is_insured_and_parses(self):
        for lead, line in (
                (_web(wants_insurance=True), bot.INSURANCE_LINE_YES),
                (_web(vehicle_details=VD_OWN),
                 "🛡 Insurance: customer's own — Geico_Direct, policy POL_9*"),
                (_web(), bot.INSURANCE_LINE_NONE)):
            with self.subTest(line=line):
                _ctx, _db, calls = _group_offer(lead)
                self.assertEqual(1, len(calls))
                self.assertEqual("Markdown", calls[0]["parse_mode"])
                plain, ents = parse_legacy_markdown(calls[0]["text"])
                self.assertIn(line + "\n", plain)
                self.assertIn(("code", "1K3AX0XS"), ents)

    def test_an_issuer_lead_offer_is_exactly_as_it_was(self):
        lead = {"id": LEAD_ID, "reference_id": "ISSU1234", "vehicle_details": VD_UNINSURED}
        for fn in ("_post_lead_to_all_groups_for_approval", "_post_single_group_approval"):
            with self.subTest(fn=fn):
                _ctx, _db, calls = _group_offer(lead, fn)
                want = ISSUER_OFFER if fn.startswith("_post_lead") else ISSUER_OFFER.replace(
                    "NEW CLIENT\n", "NEW CLIENT — Team approval\n").replace(
                    "delivery time", "delivery time\n\nTap **Accept** when ready — the lead "
                    "creator can notify drivers **after** your team accepts.")
                self.assertEqual(want, calls[0]["text"])

    def test_a_parse_failure_on_a_group_offer_is_retried_plain(self):
        for fn in ("_post_lead_to_all_groups_for_approval", "_post_single_group_approval"):
            with self.subTest(fn=fn):
                ctx, db, _ = _group_offer(
                    _web(), fn, side_effect=[BadRequest(PARSE_ERR),
                                             SimpleNamespace(message_id=77)])
                first, second = ctx.bot.send_message.call_args_list
                self.assertEqual("Markdown", first.kwargs["parse_mode"])
                self.assertNotIn("parse_mode", second.kwargs)
                self.assertIn("reply_markup", second.kwargs)
                db.update_group_lead_offer_message.assert_called_once_with(
                    LEAD_ID, GROUP_ID, "-100123", 77)

    def test_any_other_group_offer_failure_is_not_retried(self):
        ctx, db, _ = _group_offer(_web(), side_effect=[BadRequest("Chat not found")])
        self.assertEqual(1, ctx.bot.send_message.await_count)
        db.update_group_lead_offer_message.assert_not_called()

    def test_every_direct_markdown_group_offer_send_has_the_net(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        self.assertNotIn("await context.bot.send_message(\n"
                         "                chat_id=chat_id,\n"
                         "                text=group_offer_message,", src)
        sends = [m.start() for m in re.finditer("text=group_offer_message,", src)]
        self.assertEqual(7, len(sends))
        for at in sends:
            # The call this argument belongs to is the last "await " before it.
            self.assertTrue(src[src.rfind("await ", 0, at):].startswith(
                "await _send_md_or_plain("), src[at - 200:at])

    def test_the_accept_notice_and_the_reassign_offers_have_it_too(self):
        """The group's "Lead Accepted" notice carries the delivery time and the
        issuer's note; the two reassign sends carry the whole driver offer.
        Each escapes the customer's text and has the plain-text net."""
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        at = src.index('"✅ **Lead Accepted**\\n\\n"')
        block = src[at - 400:at + 1000]
        self.assertIn('extra_safe = _telegram_md1_escape(', block)
        self.assertIn("_telegram_md1_escape(_sanitize_phones_for_send(spec_grp))", block)
        send_at = block.index("text=acceptance_message")
        self.assertTrue(block[block.rfind("await ", 0, send_at):].startswith(
            "await _send_md_or_plain("))
        for needle in ("text=_driver_offer_message_text(lead),", "text=offer_text,"):
            hits = [m.start() for m in re.finditer(re.escape(needle), src)]
            self.assertTrue(hits, needle)
            for hit in hits:
                self.assertTrue(src[src.rfind("await ", 0, hit):].startswith(
                    "await _send_md_or_plain("), needle)


# ------------------------------------------------------ 2 the insurance lines

class TheInsuranceLineTest(unittest.TestCase):

    def test_our_cover(self):
        self.assertEqual("🛡 Insurance: ✅ YES — TriState coverage ($100 add-on)",
                         bot._insurance_status_line(_web(wants_insurance=True)))

    def test_their_own_insurer_and_policy(self):
        lead = _web(vehicle_details=VD_OWN)
        self.assertEqual("🛡 Insurance: customer's own — Geico_Direct, policy POL_9*",
                         bot._insurance_status_line(lead))
        self.assertEqual("🛡 Insurance: customer's own — Geico\\_Direct, policy POL\\_9\\*",
                         bot._insurance_status_line(lead, markdown=True))

    def test_nothing_on_file(self):
        self.assertEqual("⚠️ Insurance: NONE on file", bot._insurance_status_line(_web()))

    def test_half_an_insurer_is_nothing_on_file(self):
        lead = _web(vehicle_details=VD_UNINSURED.replace(
            "\nGrey\n-\n-\n", "\nGrey\nGeico\n-\n"))
        self.assertEqual(bot.INSURANCE_LINE_NONE, bot._insurance_status_line(lead))

    def test_a_form_or_dispatch_web_lead_is_covered_at_the_tag(self):
        """Neither takes a payment, so _tag_fields_from_lead arms our cover when
        the tag is built. The line must say so, not NONE."""
        for source in (bot.CLIENT_FORM_SOURCE, "Dispatch Web"):
            with self.subTest(source=source):
                lead = _web(contact_info_source=source)
                self.assertFalse(bot._website_lead_not_paid_for_cover(lead))
                self.assertEqual(bot.INSURANCE_LINE_AT_TAG, bot._insurance_status_line(lead))
                _ctx, _db, calls = _group_offer(lead)
                plain, _ents = parse_legacy_markdown(calls[0]["text"])
                self.assertIn(bot.INSURANCE_LINE_AT_TAG, plain.splitlines())

    def test_an_issuer_lead_with_no_insurer_is_covered_at_the_tag(self):
        lead = {"id": LEAD_ID, "reference_id": "ISSU1234", "vehicle_details": VD_UNINSURED}
        self.assertEqual(bot.INSURANCE_LINE_AT_TAG, bot._insurance_status_line(lead))

    def test_the_line_asks_the_same_question_the_tag_does(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        at = src.index("def _insurance_status_line(")
        body = src[at:src.index("def _group_offer_insurance_line(", at)]
        self.assertIn("_website_lead_not_paid_for_cover(lead)", body)
        tag = src[src.index("async def _tag_fields_from_lead("):]
        self.assertIn("not _website_lead_not_paid_for_cover(lead)", tag[:12000])

    def test_the_supervisory_notice_carries_it(self):
        for lead, line in ((_web(wants_insurance=True), bot.INSURANCE_LINE_YES),
                           (_web(vehicle_details=VD_OWN),
                            "🛡 Insurance: customer's own — Geico_Direct, policy POL_9*"),
                           (_web(), bot.INSURANCE_LINE_NONE)):
            with self.subTest(line=line):
                text = bot._build_web_order_supervisory_text(lead)
                self.assertIn("\n" + line + "\n", text)

    def test_the_supervisory_notice_is_still_plain_text(self):
        ctx = mock.MagicMock()
        ctx.bot.send_message = mock.AsyncMock()
        asyncio.run(bot._send_web_order_supervisory_notice(
            ctx, _web(vehicle_details=VD_OWN), [GROUP]))
        kw = ctx.bot.send_message.await_args.kwargs
        self.assertNotIn("parse_mode", kw)
        self.assertIn("Geico_Direct, policy POL_9*", kw["text"])


if __name__ == "__main__":
    unittest.main()
