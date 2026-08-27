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
    batch_number_for,
    check_customer,
    check_lines,
    check_posting_date,
    check_reference,
    check_warehouse,
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
