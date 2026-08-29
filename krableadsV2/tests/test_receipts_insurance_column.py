"""The receipts board shows insurance for the leads that bought it.

A lead is tag-only or tag + insurance. The board only ever showed the tag side,
so a policy that was issued but never emailed was invisible here.
"""
import re
import unittest

import admin_dashboard as ad
import receipts_page


HTML = receipts_page.BOARD_HTML


class InsuranceFieldsTest(unittest.TestCase):
    def test_a_tag_only_lead_is_not_marked_insured(self):
        got = ad._insurance_fields({})
        self.assertFalse(got["has_insurance"])
        self.assertEqual(got["insurance_state"], "none")

    def test_bought_but_nothing_issued_is_pending(self):
        got = ad._insurance_fields({"wants_insurance": True})
        self.assertTrue(got["has_insurance"])
        self.assertEqual(got["insurance_state"], "pending")

    def test_a_policy_with_no_send_is_issued(self):
        got = ad._insurance_fields(
            {"wants_insurance": True, "insurance_card_policy_number": "ABP6312345678"}
        )
        self.assertEqual(got["insurance_state"], "issued")
        self.assertEqual(got["insurance_policy"], "ABP6312345678")

    def test_a_delivered_card_is_sent(self):
        got = ad._insurance_fields({
            "wants_insurance": True,
            "insurance_card_policy_number": "ABP6312345678",
            "insurance_card_sent_at": "2026-08-29T10:00:00Z",
            "insurance_card_sent_to_email": "a@b.com",
        })
        self.assertEqual(got["insurance_state"], "sent")
        self.assertEqual(got["insurance_sent_to"], "a@b.com")

    def test_an_error_outranks_everything(self):
        got = ad._insurance_fields({
            "wants_insurance": True,
            "insurance_card_policy_number": "ABP6312345678",
            "insurance_card_sent_at": "2026-08-29T10:00:00Z",
            "insurance_email_error": "Resend rejected the address",
        })
        self.assertEqual(got["insurance_state"], "failed")
        self.assertIn("Resend", got["insurance_error"])

    def test_an_older_insured_lead_without_the_flag_still_counts(self):
        # wants_insurance predates the columns that record what was issued; a
        # lead holding a policy number is insured whatever the flag says.
        got = ad._insurance_fields({"insurance_card_policy_number": "ABP6312345678"})
        self.assertTrue(got["has_insurance"])
        self.assertEqual(got["insurance_state"], "issued")

    def test_the_portal_login_is_carried_through(self):
        got = ad._insurance_fields(
            {"wants_insurance": True, "portal_email": "a@b.com", "portal_password": "Temp#A9"}
        )
        self.assertEqual(got["portal_email"], "a@b.com")
        self.assertEqual(got["portal_password"], "Temp#A9")


class BoardMarkupTest(unittest.TestCase):
    def test_the_table_has_an_insurance_column(self):
        self.assertIn("<th>Insurance</th>", HTML)

    def test_insurance_sits_between_status_and_updated(self):
        head = HTML[HTML.index("<thead>"): HTML.index("</thead>")]
        cols = re.findall(r"<th[^>]*>([^<]*)</th>", head)
        self.assertIn("Insurance", cols)
        self.assertEqual(cols[cols.index("Insurance") - 1], "Status")
        self.assertEqual(cols[cols.index("Insurance") + 1], "Updated")

    def test_an_issued_card_is_a_link_not_just_a_label(self):
        self.assertIn("/receipts/insurance/", HTML)
        self.assertIn('const viewable = st === "issued" || st === "sent"', HTML)

    def test_the_chip_says_card_rather_than_card_sent(self):
        # "card sent" reported an event elsewhere and gave nowhere to click.
        self.assertNotIn('sent: "card sent"', HTML)
        self.assertIn('sent: "card"', HTML)
        self.assertIn('issued: "card"', HTML)

    def test_every_row_renders_the_chip(self):
        self.assertIn("${insuranceChip(r)}", HTML)

    def test_the_detail_panel_renders_the_block(self):
        self.assertIn("${insuranceBlock(r)}", HTML)

    def test_both_renderers_are_defined(self):
        self.assertIn("function insuranceChip(r)", HTML)
        self.assertIn("function insuranceBlock(r)", HTML)

    def test_the_header_and_the_body_agree_on_column_count(self):
        head = HTML[HTML.index("<thead>"): HTML.index("</thead>")]
        # `<th[\s>]` so the opening `<thead>` is not counted as a column.
        n_cols = len(re.findall(r"<th[\s>]", head))
        for span in re.findall(r'colspan="(\d+)"', HTML):
            self.assertEqual(
                int(span), n_cols,
                f"a colspan of {span} does not match the {n_cols} header columns — "
                "the loading row and month dividers would not span the table",
            )

    def test_no_stray_thirteen_column_spans_survive(self):
        self.assertNotIn('colspan="13"', HTML)


if __name__ == "__main__":
    unittest.main()
