r"""The pieces actually hold together.

Every failure this file guards against has happened for real in this codebase:

  * the receipt portal shipped calling three methods that did not exist on the
    dashboard's database class — twenty-six tests passed over it because they all
    mocked `db`, and a MagicMock answers any method you ask for;
  * buttons have shipped whose callback_data no registered handler matched;
  * code has shipped reading columns whose migration did not exist.

So these tests deliberately use the REAL classes and the REAL route table. Nothing
here is mocked — that is the whole point.

Run:  venv\Scripts\python.exe -m pytest tests/test_wiring.py -q
"""
import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy-key")

import utils.database as udb  # noqa: E402

if "bot" not in sys.modules:
    udb.Database = mock.MagicMock()
import bot  # noqa: E402
import admin_dashboard as ad  # noqa: E402

# The real Database, not the mock bot's import left behind.
_spec = importlib.util.spec_from_file_location("_real_db_wiring", ROOT / "utils" / "database.py")
_real = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_real)
RealDatabase = _real.Database

LEAD = "11111111-2222-3333-4444-555555555555"


class TheWebLayerCanCallWhatItCallsTest(unittest.TestCase):
    """AdminDatabase is a DIFFERENT class from the bot's Database. A method added to
    one is not on the other, and mocking `db` hides it completely."""

    NEEDED = ("save_receipt_file", "get_receipt_file", "has_receipt_file",
              "get_transmissions", "set_lead_status")

    def test_every_method_exists(self):
        missing = [m for m in self.NEEDED
                   if not callable(getattr(ad.AdminDatabase, m, None))]
        self.assertEqual([], missing, f"the web layer would AttributeError on: {missing}")


class TheBotCanCallWhatItCallsTest(unittest.TestCase):

    NEEDED = ("save_receipt_file", "get_receipt_file",
              "get_paid_instant_pdfs_undelivered", "mark_instant_pdf_delivered",
              "get_lead_counts_by_sender", "get_driver_pending_assignment",
              "get_setting", "set_setting", "get_user_state", "set_user_state",
              "claim_insurance_email", "release_insurance_email_claim")

    def test_every_method_exists(self):
        missing = [m for m in self.NEEDED
                   if not callable(getattr(RealDatabase, m, None))]
        self.assertEqual([], missing, f"the bot would AttributeError on: {missing}")


class BothSidesBuildTheSameLinksTest(unittest.TestCase):
    """A link one side signs and the other verifies — a mismatch 404s every upload."""

    def test_the_receipt_link_matches(self):
        self.assertEqual(bot.receipt_portal_url(LEAD), ad.receipt_portal_url(LEAD))

    def test_the_dashboard_verifies_what_the_bot_signs(self):
        token = bot.receipt_portal_url(LEAD).rsplit("/r/", 1)[1]
        self.assertEqual(LEAD, ad.receipt_lead_from_token(token))


class EveryRouteIsMountedTest(unittest.TestCase):

    NEEDED = ("/receipts", "/receipt/<lead_id>", "/r/<token>",
              "/api/transmissions", "/api/transmissions/<lead_id>/status",
              "/api/instant/checkout", "/api/stripe/webhook",
              "/instant/success", "/instant/cancelled",
              "/api/receipts/image/<lead_id>", "/api/receipts/link/<lead_id>")

    def test_all_of_them(self):
        have = {str(r) for r in ad.app.url_map.iter_rules()}
        missing = [r for r in self.NEEDED if r not in have]
        self.assertEqual([], missing, f"not mounted: {missing}")


class EveryNewColumnHasAMigrationTest(unittest.TestCase):
    """Code that reads a column no migration creates fails only in production."""

    def test_the_files_exist(self):
        for name in ("migration_receipt_files.sql",
                     "migration_lead_delivery_status.sql",
                     "migration_instant_pdf.sql",
                     "migration_driver_manual_suspend.sql",
                     "migration_insurance_email_gate.sql"):
            with self.subTest(migration=name):
                self.assertTrue((ROOT / "database" / name).exists(), name)

    def test_the_insurance_email_gate_is_fully_declared(self):
        """Both columns, AND the backfill — without it the release button would
        offer to re-email every historical client whose card already went out."""
        sql = (ROOT / "database" / "migration_insurance_email_gate.sql").read_text(encoding="utf-8")
        for col in ("insurance_emailed_at", "insurance_email_error"):
            with self.subTest(column=col):
                self.assertIn(col, sql, col)
        self.assertIn("UPDATE leads", sql, "backfill UPDATE missing")
        self.assertIn("insurance_card_sent_at IS NOT NULL", sql)

    def test_the_instant_pdf_columns_are_all_declared(self):
        sql = (ROOT / "database" / "migration_instant_pdf.sql").read_text(encoding="utf-8")
        for col in ("instant_pdf_requested_at", "instant_pdf_session_id",
                    "instant_pdf_paid_at", "instant_pdf_delivered_at",
                    "instant_pdf_driver_id", "instant_pdf_amount_cents"):
            with self.subTest(column=col):
                self.assertIn(col, sql, col)

    def test_the_status_column_allows_exactly_the_board_values(self):
        sql = (ROOT / "database" / "migration_lead_delivery_status.sql").read_text(encoding="utf-8")
        for value in ad.AdminDatabase.DELIVERY_STATUSES:
            with self.subTest(value=value):
                self.assertIn(f"'{value}'", sql, value)


class EveryNewButtonReachesAHandlerTest(unittest.TestCase):
    """The recurring bug in this codebase: a keyboard emits callback_data that no
    registered pattern matches, and the tap silently does nothing."""

    def test_the_instant_pdf_button(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        data = [b.callback_data for row in bot._after_send_keyboard(LEAD).inline_keyboard
                for b in row]
        instant = [d for d in data if d.startswith(bot.INSTANT_PDF_CB)]
        self.assertTrue(instant, "the button is not on the keyboard")
        self.assertIn('pattern=f"^{INSTANT_PDF_CB}"', src)

    def test_it_is_both_an_entry_point_and_a_fallback(self):
        """Entry points are ignored mid-conversation; fallbacks are not consulted
        when idle. A button on a long-lived card needs both."""
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        self.assertEqual(2, src.count(
            'CallbackQueryHandler(handle_instant_pdf_request, pattern=f"^{INSTANT_PDF_CB}")'))

    def test_the_receipt_upload_button_carries_a_real_link(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        self.assertIn("url=receipt_portal_url(lead_id)", src)

    def test_the_insurance_email_button_reaches_its_handler(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        data = [b.callback_data for row in bot._insurance_email_keyboard(LEAD).inline_keyboard
                for b in row]
        self.assertEqual([f"ins_email_{LEAD}"], data)
        self.assertIn('pattern=r"^ins_email_"', src)

    def test_setclientemail_is_registered(self):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        self.assertIn('CommandHandler("setclientemail", cmd_set_client_email)', src)


class TheDocumentationMatchesTheCodeTest(unittest.TestCase):
    """A variable the code reads but nothing documents is a variable nobody sets."""

    def test_every_new_env_var_is_in_the_example(self):
        example = (ROOT / ".env.example").read_text(encoding="utf-8")
        for var in ("RECEIPT_PORTAL_BASE", "RECEIPT_LINK_SECRET",
                    "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET",
                    "INSTANT_PDF_CENTS", "INTEGRATIONS_API_KEY",
                    "SUPERVISORY_TELEGRAM_ID"):
            with self.subTest(var=var):
                self.assertIn(var, example, var)

    def test_the_wiring_note_lists_every_migration(self):
        doc = (ROOT / "WIRING.md").read_text(encoding="utf-8")
        for name in ("migration_receipt_files.sql",
                     "migration_lead_delivery_status.sql",
                     "migration_instant_pdf.sql",
                     "migration_insurance_email_gate.sql"):
            with self.subTest(migration=name):
                self.assertIn(name, doc, name)

    def test_it_says_the_webhook_secret_is_not_optional(self):
        doc = (ROOT / "WIRING.md").read_text(encoding="utf-8")
        self.assertIn("STRIPE_WEBHOOK_SECRET", doc)
        # Phrases in prose wrap, so match on words that stay together.
        self.assertIn("every webhook is refused", doc)
        self.assertIn("not optional", doc)


if __name__ == "__main__":
    unittest.main()
