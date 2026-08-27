"""🤖 Instant Tag: the toggle, the Amount, and the pay-to-release dispatch.

The operator's spec, pinned:
  * a review-card toggle "🤖 Instant Tag 🏷️" under the Add-Car rows;
  * AMOUNT is a NEW field — the driver's Stripe charge, price − $50, refreshed
    on every price write, hand-editable, and NEVER parsed from free text;
  * one driver only (All Drivers hidden) unless supervisors flip the
    /settings → ⚡ Instant Tag switch;
  * dispatch = a payment link (no Accept/Decline); paying — or the issuer's
    password — releases the tag, delivered like Skip Dispatch (which now also
    auto-creates TriState insurance when the lead arrived with no insurer);
  * every generated tag file is named after the CLIENT, not the plate.

Run:  venv\\Scripts\\python.exe -m pytest tests/test_instant_tag.py -q
"""
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

SRC = (ROOT / "bot.py").read_text(encoding="utf-8")


class TheAmountTest(unittest.TestCase):

    def test_amount_is_price_minus_fifty(self):
        st = {"pending_price": "$200"}
        bot._sync_driver_amount_from_price(st)
        self.assertEqual(st["driver_amount"], "$150")

    def test_toll_suffix_and_commas_do_not_confuse_it(self):
        st = {"pending_price": "$1,200 + toll"}
        bot._sync_driver_amount_from_price(st)
        self.assertEqual(st["driver_amount"], "$1150")

    def test_it_never_goes_negative(self):
        st = {"pending_price": "$30"}
        bot._sync_driver_amount_from_price(st)
        self.assertEqual(st["driver_amount"], "$0")

    def test_no_price_means_no_amount(self):
        st = {}
        bot._sync_driver_amount_from_price(st)
        self.assertNotIn("driver_amount", st)

    def test_a_price_edit_refreshes_it_and_a_manual_edit_holds_until_then(self):
        st = {}
        bot._apply_single_phase1_edit(st, "price", "200")
        self.assertEqual(st["driver_amount"], "$150")
        bot._apply_single_phase1_edit(st, "amt", "175")   # manual override
        self.assertEqual(st["driver_amount"], "$175")
        bot._apply_single_phase1_edit(st, "price", "300")  # price moved → resync
        self.assertEqual(st["driver_amount"], "$250")

    def test_the_amount_edit_sanitizes_but_never_tolls(self):
        self.assertEqual(bot._clean_inline_value("amt", "make it 150 plus toll"), "$150")
        self.assertEqual(bot._clean_inline_value("amt", "no number here"), "")

    def test_cents_for_stripe(self):
        self.assertEqual(bot._driver_amount_cents({"driver_amount": "$150"}), 15000)
        self.assertIsNone(bot._driver_amount_cents({"driver_amount": ""}))
        self.assertIsNone(bot._driver_amount_cents({}))

    def test_amount_is_never_free_text_parsed(self):
        """No label parser, no AI tool may set the money a card is charged."""
        self.assertNotIn("amt", bot._AI_FIELD_TO_EK.values())
        self.assertNotIn("driver_amount", getattr(bot, "LEAD_FIELDS", ()))
        from utils import nl_router
        self.assertNotIn("driver_amount", nl_router.LEAD_FIELDS)


class TheToggleAndPickerTest(unittest.TestCase):

    def _kb_labels(self, st):
        return [b.text for row in
                bot._build_review_keyboard_with_selections(st).inline_keyboard
                for b in row]

    def test_the_toggle_sits_on_the_card_and_shows_the_amount(self):
        self.assertIn("🤖 Instant Tag 🏷️", self._kb_labels({"vin": "-"}))
        labels = self._kb_labels({"vin": "-", "instant_tag": True,
                                  "driver_amount": "$150"})
        self.assertTrue(any("Instant Tag 🏷️: ON ($150)" in l for l in labels), labels)

    def test_the_callback_is_in_the_review_pattern(self):
        self.assertIn("ph1_itag_toggle", bot.PH1_REVIEW_CB_PATTERN)

    def test_all_drivers_disappears_for_instant_leads(self):
        import asyncio
        from types import SimpleNamespace
        captured = {}

        async def grab(context, chat_id, mid, kb):
            captured["kb"] = kb
        ctx = SimpleNamespace(user_data={"review_chat_id": 1, "review_message_id": 2})
        drivers = [{"id": "d1", "driver_name": "Kita", "is_active": True}]
        with mock.patch.object(bot, "_get_all_drivers_cached", lambda: drivers), \
                mock.patch.object(bot, "_get_suspended_driver_ids", lambda: set()), \
                mock.patch.object(bot, "_edit_message_keyboard", grab), \
                mock.patch.object(bot, "_instant_all_drivers_enabled", lambda: False):
            asyncio.run(bot._open_driver_picker(ctx, {"instant_tag": True}))
            no_all = [b.callback_data for row in captured["kb"].inline_keyboard for b in row]
            asyncio.run(bot._open_driver_picker(ctx, {"instant_tag": False}))
            with_all = [b.callback_data for row in captured["kb"].inline_keyboard for b in row]
        self.assertNotIn("seldrv_all", no_all)
        self.assertIn("seldrv_all", with_all)

    def test_the_supervisor_switch_restores_it(self):
        import asyncio
        from types import SimpleNamespace
        captured = {}

        async def grab(context, chat_id, mid, kb):
            captured["kb"] = kb
        ctx = SimpleNamespace(user_data={"review_chat_id": 1, "review_message_id": 2})
        drivers = [{"id": "d1", "driver_name": "Kita", "is_active": True}]
        with mock.patch.object(bot, "_get_all_drivers_cached", lambda: drivers), \
                mock.patch.object(bot, "_get_suspended_driver_ids", lambda: set()), \
                mock.patch.object(bot, "_edit_message_keyboard", grab), \
                mock.patch.object(bot, "_instant_all_drivers_enabled", lambda: True):
            asyncio.run(bot._open_driver_picker(ctx, {"instant_tag": True}))
        self.assertIn("seldrv_all", [b.callback_data
                                     for row in captured["kb"].inline_keyboard for b in row])

    def test_the_setting_reads_truthily(self):
        fake = mock.MagicMock()
        for raw, want in (("1", True), ("true", True), ("on", True),
                          ("0", False), ("", False), (None, False)):
            fake.get_setting.return_value = raw
            with mock.patch.object(bot, "db", fake):
                self.assertIs(bot._instant_all_drivers_enabled(), want, raw)


class TheWiringHoldsTest(unittest.TestCase):

    def test_instant_dispatch_replaces_the_offer_round(self):
        body = SRC.split("async def _background_dispatch_lead_after_driver_pick", 1)[1]
        head = body[:1600]
        self.assertIn("_dispatch_instant_tag_lead", head)

    def test_paid_delivery_is_the_full_skip_dispatch_send(self):
        body = SRC.split("async def deliver_paid_instant_pdfs", 1)[1]
        body = body.split("\nasync def ", 1)[0]
        self.assertIn("_deliver_skip_dispatch", body)
        self.assertIn('lead.get("instant_tag")', body)

    def test_skip_dispatch_arms_insurance_when_none_detected(self):
        body = SRC.split("async def _deliver_skip_dispatch", 1)[1]
        body = body.split("\nasync def ", 1)[0]
        self.assertIn("_lead_already_insured", body)
        self.assertIn('{"wants_insurance": True}', body)

    def test_the_tag_file_is_named_after_the_client(self):
        body = SRC.split("async def _build_and_send_tag_pdf", 1)[1]
        body = body.split("\nasync def ", 1)[0]
        self.assertIn("_client_file", body)
        self.assertNotIn('filename = f"tag_{re.sub', body.split("_client_file", 1)[0])

    def test_the_checkout_carries_the_amount(self):
        self.assertIn("amount_cents", SRC.split("async def request_instant_pdf_link", 1)[1][:1200])
        ip = (ROOT / "instant_pdf.py").read_text(encoding="utf-8")
        self.assertIn('body.get("amount_cents")', ip)
        self.assertIn('"already paid"', ip)
        self.assertIn('paid_update["instant_pdf_driver_id"]', ip)

    def test_the_migration_exists(self):
        sql = (ROOT / "database" / "migration_instant_tag.sql").read_text(encoding="utf-8")
        self.assertIn("instant_tag", sql)
        self.assertIn("driver_amount", sql)
        import importlib
        real = importlib.import_module("utils.database")
        for k in ("instant_tag", "driver_amount", "wants_insurance"):
            self.assertIn(k, real._OPTIONAL_LEADS_WRITE_KEYS, k)

    def test_the_settings_switch_is_wired(self):
        self.assertIn('"tset_instant"', SRC)
        self.assertIn('if data == "tset_itag_all":', SRC)
        self.assertIn('"instant_all_drivers"', SRC)


if __name__ == "__main__":
    unittest.main()
