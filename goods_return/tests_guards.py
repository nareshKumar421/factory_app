"""Tests for the goods-return guards.

These matter more than most: the app cannot undo an A/R Return it posts. SAP
restricts cancelling one to a named list of users and ours is not on it — a live
`Cancel` came back `-1116`. So anything these guards let through is permanent
until a person fixes it in SAP.

Pure logic, no database and no SAP.
"""

import datetime
from decimal import Decimal

from django.test import SimpleTestCase

from .guards import (
    BRANCH_CUSTOMER_GROUP,
    GoodsReturnGuardError,
    align_tax_code,
    batch_number_for,
    check_customer,
    check_lines,
    check_posting_date,
    check_reference,
    check_warehouse,
    is_interstate,
)

VARIETY = {"FG0000011": "MUSTARD"}
TAX = {"FG0000011": "CG+SG@5"}
COST = {"FG0000011": Decimal("500")}


def line(item="FG0000011", qty="1"):
    return {"item_code": item, "quantity": Decimal(qty)}


class PostingDateTests(SimpleTestCase):
    def test_today_is_accepted(self):
        check_posting_date(datetime.date.today())

    def test_last_month_is_refused_with_the_sap_error_number(self):
        today = datetime.date.today()
        last_month = (today.replace(day=1) - datetime.timedelta(days=1))
        with self.assertRaises(GoodsReturnGuardError) as ctx:
            check_posting_date(last_month)
        self.assertIn("current month", str(ctx.exception))
        self.assertIn("160027", str(ctx.exception))

    def test_next_month_is_refused_too(self):
        today = datetime.date.today()
        next_month = (today.replace(day=28) + datetime.timedelta(days=7)).replace(day=1)
        with self.assertRaises(GoodsReturnGuardError):
            check_posting_date(next_month)

    def test_no_date_is_left_alone(self):
        check_posting_date(None)


class CustomerTests(SimpleTestCase):
    def test_an_ordinary_customer_passes(self):
        check_customer("CUSTA000025", 101)

    def test_a_branch_customer_is_refused(self):
        with self.assertRaises(GoodsReturnGuardError) as ctx:
            check_customer("CUSTA000606", BRANCH_CUSTOMER_GROUP)
        self.assertIn("160012", str(ctx.exception))
        self.assertIn("branch transfer", str(ctx.exception))

    def test_a_missing_customer_is_refused(self):
        with self.assertRaises(GoodsReturnGuardError):
            check_customer("", 101)

    def test_an_unknown_group_is_allowed_through(self):
        # Better to let SAP decide than to block on a group we could not read.
        check_customer("CUSTA000025", None)


class ReferenceTests(SimpleTestCase):
    def test_reference_is_upper_cased_rather_than_refused(self):
        # SAP requires upper case (160007); the case carries no meaning, so
        # correcting it beats refusing the operator's return.
        self.assertEqual(check_reference("gr-20260827-0001 letter_pad"),
                         "GR-20260827-0001 LETTER_PAD")

    def test_reference_is_trimmed_to_sap_length(self):
        self.assertEqual(len(check_reference("X" * 200)), 100)

    def test_blank_reference_survives(self):
        self.assertEqual(check_reference(None), "")


class WarehouseTests(SimpleTestCase):
    def test_a_warehouse_with_a_branch_passes(self):
        check_warehouse("BH-GR", 2)

    def test_no_warehouse_is_refused(self):
        with self.assertRaises(GoodsReturnGuardError):
            check_warehouse("", 2)

    def test_a_warehouse_without_a_branch_is_refused(self):
        # Omitting the branch fails in SAP with "Specify an active branch", which
        # tells the operator nothing about which warehouse is at fault.
        with self.assertRaises(GoodsReturnGuardError) as ctx:
            check_warehouse("BH-GR", None)
        self.assertIn("BH-GR", str(ctx.exception))
        self.assertIn("BPLid", str(ctx.exception))


class LineTests(SimpleTestCase):
    def test_a_complete_line_passes(self):
        check_lines([line()], variety_codes=VARIETY, tax_codes=TAX, return_costs=COST)

    def test_no_lines_is_refused(self):
        with self.assertRaises(GoodsReturnGuardError):
            check_lines([], variety_codes=VARIETY, tax_codes=TAX, return_costs=COST)

    def test_zero_quantity_is_refused(self):
        with self.assertRaises(GoodsReturnGuardError):
            check_lines([line(qty="0")], variety_codes=VARIETY, tax_codes=TAX,
                        return_costs=COST)

    def test_duplicate_items_are_refused_before_sap_asks(self):
        with self.assertRaises(GoodsReturnGuardError) as ctx:
            check_lines([line(), line()], variety_codes=VARIETY, tax_codes=TAX,
                        return_costs=COST)
        self.assertIn("160020", str(ctx.exception))

    def test_a_missing_variety_names_the_item_and_the_fix(self):
        with self.assertRaises(GoodsReturnGuardError) as ctx:
            check_lines([line()], variety_codes={}, tax_codes=TAX, return_costs=COST)
        self.assertIn("FG0000011", str(ctx.exception))
        self.assertIn("Sub Group", str(ctx.exception))
        self.assertIn("160013", str(ctx.exception))

    def test_a_missing_tax_code_is_refused(self):
        with self.assertRaises(GoodsReturnGuardError) as ctx:
            check_lines([line()], variety_codes=VARIETY, tax_codes={}, return_costs=COST)
        self.assertIn("160009", str(ctx.exception))

    def test_a_missing_cost_is_refused(self):
        # A zero cost would bring the stock back at no value, understating
        # inventory. SAP posts ReturnCost x Quantity as the inventory value.
        with self.assertRaises(GoodsReturnGuardError) as ctx:
            check_lines([line()], variety_codes=VARIETY, tax_codes=TAX, return_costs={})
        self.assertIn("valued", str(ctx.exception))
        self.assertIn("160021", str(ctx.exception))

    def test_a_zero_cost_is_refused_as_well_as_a_missing_one(self):
        with self.assertRaises(GoodsReturnGuardError):
            check_lines([line()], variety_codes=VARIETY, tax_codes=TAX,
                        return_costs={"FG0000011": Decimal("0")})


class BatchNumberTests(SimpleTestCase):
    """SAP will not receive a return into an existing batch, so we mint one."""

    def test_it_is_derived_from_the_entry_and_line(self):
        self.assertEqual(batch_number_for("GR-20260827-0001", 0), "GR-20260827-0001-0")

    def test_lines_on_one_return_get_different_batches(self):
        self.assertNotEqual(
            batch_number_for("GR-20260827-0001", 0),
            batch_number_for("GR-20260827-0001", 1),
        )

    def test_it_is_upper_cased_and_fits_sap(self):
        result = batch_number_for("gr-" + "x" * 60, 3)
        self.assertEqual(result, result.upper())
        self.assertLessEqual(len(result), 36)


# The sales tax codes JIVO actually holds, as `ar_tax_codes()` returns them.
AR_TAX_CODES = {
    code.upper(): {"code": code, "name": name, "rate": Decimal(rate)}
    for code, name, rate in [
        ("CG+SG@5", "CGST+SGST@5%", "5"),
        ("CG+SG@12", "CGST+SGST@12%", "12"),
        ("CG+SG@18", "CGST+SGST@18%", "18"),
        ("IGST@5", "IGST@5%", "5"),
        ("IGST@12", "IGST@12%", "12"),
        ("IGST@18", "IGST@18%", "18"),
        ("RCGSG@5", "RCM CGST+SGST@5", "5"),
        ("RIGST@5", "RCM IGST @5%", "5"),
        ("GST05R", "SGST @ 2.5 % + CGST @ 2.5 % RCM", "5"),
        ("IG28+C12", "IGST28%+Cess12%", "40"),
        ("CS28+C12", "CGST14+SGST14+CESS12", "40"),
    ]
}


class InterstateTests(SimpleTestCase):
    def test_same_state_is_intra_state(self):
        self.assertIs(is_interstate("HR", "hr"), False)

    def test_different_states_are_inter_state(self):
        self.assertIs(is_interstate("HR", "WB"), True)

    def test_an_unknown_state_gives_no_answer(self):
        # Better to let SAP decide than to rewrite a tax code on a guess.
        self.assertIsNone(is_interstate("", "WB"))
        self.assertIsNone(is_interstate("HR", ""))


class TaxCodeAlignmentTests(SimpleTestCase):
    """SAP refuses the whole return if the GST flavour contradicts the states
    (254000293), so the code the sale used is switched, not passed through."""

    def align(self, code, interstate, **kwargs):
        return align_tax_code(
            code, interstate=interstate, available=AR_TAX_CODES, **kwargs
        )

    def test_an_intra_state_code_is_left_alone_intra_state(self):
        self.assertEqual(self.align("CG+SG@5", False), "CG+SG@5")

    def test_an_igst_code_is_left_alone_inter_state(self):
        self.assertEqual(self.align("IGST@5", True), "IGST@5")

    def test_a_delhi_sale_returned_to_haryana_becomes_igst(self):
        # The real trap: the invoice was intra-state, but the goods come back
        # into a warehouse in another state, which makes the return inter-state.
        self.assertEqual(self.align("CG+SG@5", True), "IGST@5")

    def test_an_inter_state_sale_returned_in_state_becomes_cgst_sgst(self):
        self.assertEqual(self.align("IGST@18", False), "CG+SG@18")

    def test_the_rate_is_preserved(self):
        self.assertEqual(self.align("CG+SG@12", True), "IGST@12")

    def test_unknown_states_leave_the_code_untouched(self):
        self.assertEqual(self.align("CG+SG@5", None), "CG+SG@5")

    def test_rcm_stays_rcm(self):
        self.assertEqual(self.align("RCGSG@5", True), "RIGST@5")
        self.assertEqual(self.align("RIGST@5", False), "GST05R")

    def test_the_cess_pair_keeps_its_cess(self):
        # Both are 40%, so mapping by rate alone would land on CG+SG@40 and
        # silently drop the cess split.
        self.assertEqual(self.align("IG28+C12", False), "CS28+C12")
        self.assertEqual(self.align("CS28+C12", True), "IG28+C12")

    def test_a_missing_counterpart_names_the_item_and_the_error(self):
        with self.assertRaises(GoodsReturnGuardError) as ctx:
            align_tax_code(
                "CG+SG@28",
                interstate=True,
                available=AR_TAX_CODES,
                item_code="FG0000229",
            )
        self.assertIn("FG0000229", str(ctx.exception))
        self.assertIn("254000293", str(ctx.exception))
        self.assertIn("IGST", str(ctx.exception))

    def test_a_blank_code_is_left_to_the_line_check(self):
        self.assertEqual(self.align("", True), "")
